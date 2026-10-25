// runtime/tasks.c — green threads: context switch, scheduler, timers, waiting on fds, go / await, main as a task. Part of runtime.c (one translation unit: see runtime.c).

// ============================================================================ tasks (go / await)
// Green threads: each task has its own stack (mmap'd, with an unmapped guard page below it, so running off
// the end faults instead of overwriting memory). A task runs only on the OS thread that created it — no
// work stealing — so two tasks never run at the same time on the same data. `main` itself runs as a task,
// and in the server every request is one. Switching is a few loads and stores (pn_ctx_switch below).

#include <sys/mman.h>
#include <poll.h>
#if defined(__has_feature)
#  if __has_feature(address_sanitizer)
#    define PN_ASAN 1
#  endif
#endif
#if defined(__SANITIZE_ADDRESS__) && !defined(PN_ASAN)
#  define PN_ASAN 1
#endif
#ifdef PN_ASAN
// declared here: some clang packages (Debian) ship the sanitizer runtime but not its headers
void __sanitizer_start_switch_fiber(void **fake_stack_save, const void *bottom, size_t size);
void __sanitizer_finish_switch_fiber(void *fake_stack_save, const void **bottom_old, size_t *size_old);
void __asan_unpoison_memory_region(void const volatile *addr, size_t size);
#endif

typedef struct { int64_t len, cap; void *data; } List;
enum { T_READY, T_RUNNING, T_WAITING, T_DONE };

typedef struct Task {
    void *sp;                           // saved stack pointer while it is not running
    char *map; size_t map_size;         // the mapping: guard page + stack
    void (*fn)(void *); void *arg;
    int state, panicked, auto_release, queued, zombie, cancelled;
    struct Sub *waiting_sub;            // waiting in hub_next() for this subscription
    struct FdWait *waiting_fd[2];       // waiting for a socket or pipe (wait_fds)
    Req *req; jmp_buf *jmp;             // cur_req / cur_jmp while it runs
    struct Task *next_ready;
    struct Task *waiter;                // a task waiting in pn_await for this one
    struct Task *awaiting;              // the task this one waits for
    int64_t wake_at; int heap_idx;      // sleeping: its place in the timer heap (-1 = none)
    struct Task *req_next, *req_prev;   // the request's tasks
    void (*on_done)(struct Task *);     // the server: answer the request
    void *ctx;
    void *fake;                         // AddressSanitizer fiber bookkeeping
} Task;

#define TASK_STACK (1u << 20)           // 1 MB per task (virtual memory: pages are used only when touched)
#define MAIN_STACK (8u << 20)           // main gets what a normal program's stack would have

static __thread Task *cur_task;         // NULL = the scheduler (the thread's own stack)
static __thread void *sched_sp;
static __thread __attribute__((unused)) const void *sched_bottom;
static __thread __attribute__((unused)) size_t sched_size;
static __thread Task *ready_head, *ready_tail;
static __thread Task **timers;          // binary min-heap on wake_at
static __thread int ntimers, timers_cap;
static __thread Task *pool;             // finished tasks kept for reuse (their stacks stay mapped)
static __thread int npool;

static int64_t now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

#ifdef __APPLE__
#define SYM(x) "_" #x
#else
#define SYM(x) #x
#endif

void pn_ctx_switch(void **save, void *to);
__attribute__((visibility("hidden"), used)) void pn_task_main(Task *t);

#if defined(__aarch64__)
// saves x19-x30 and d8-d15 (the registers a call must preserve) on the current stack, switches stacks, restores
__asm__(".text\n.p2align 2\n.globl " SYM(pn_ctx_switch) "\n" SYM(pn_ctx_switch) ":\n"
        "  sub sp, sp, #160\n"
        "  stp x19, x20, [sp, #0]\n  stp x21, x22, [sp, #16]\n  stp x23, x24, [sp, #32]\n"
        "  stp x25, x26, [sp, #48]\n  stp x27, x28, [sp, #64]\n  stp x29, x30, [sp, #80]\n"
        "  stp d8, d9, [sp, #96]\n  stp d10, d11, [sp, #112]\n  stp d12, d13, [sp, #128]\n  stp d14, d15, [sp, #144]\n"
        "  mov x9, sp\n  str x9, [x0]\n  mov sp, x1\n"
        "  ldp x19, x20, [sp, #0]\n  ldp x21, x22, [sp, #16]\n  ldp x23, x24, [sp, #32]\n"
        "  ldp x25, x26, [sp, #48]\n  ldp x27, x28, [sp, #64]\n  ldp x29, x30, [sp, #80]\n"
        "  ldp d8, d9, [sp, #96]\n  ldp d10, d11, [sp, #112]\n  ldp d12, d13, [sp, #128]\n  ldp d14, d15, [sp, #144]\n"
        "  add sp, sp, #160\n  ret\n"
        ".globl " SYM(pn_task_entry) "\n" SYM(pn_task_entry) ":\n"
        "  mov x0, x19\n  bl " SYM(pn_task_main) "\n  brk #0\n");
#elif defined(__x86_64__)
__asm__(".text\n.globl " SYM(pn_ctx_switch) "\n" SYM(pn_ctx_switch) ":\n"
        "  pushq %rbp\n  pushq %rbx\n  pushq %r12\n  pushq %r13\n  pushq %r14\n  pushq %r15\n"
        "  movq %rsp, (%rdi)\n  movq %rsi, %rsp\n"
        "  popq %r15\n  popq %r14\n  popq %r13\n  popq %r12\n  popq %rbx\n  popq %rbp\n  ret\n"
        ".globl " SYM(pn_task_entry) "\n" SYM(pn_task_entry) ":\n"
        "  movq %rbx, %rdi\n  call " SYM(pn_task_main) "\n  ud2\n");
#else
#error "tasks need a context switch for this CPU (aarch64 and x86_64 are supported)"
#endif
void pn_task_entry(void);

static size_t page_size(void) { static size_t ps; if (!ps) ps = (size_t)sysconf(_SC_PAGESIZE); return ps; }

static void frame_init(Task *t) {
    uintptr_t top = ((uintptr_t)t->map + t->map_size) & ~(uintptr_t)15;
#if defined(__aarch64__)
    uint64_t *f = (uint64_t *)(top - 160);
    memset(f, 0, 160);
    f[0] = (uint64_t)t;                         // x19 -> pn_task_entry passes it to pn_task_main
    f[11] = (uint64_t)pn_task_entry;            // x30: where `ret` goes
#else
    uint64_t *f = (uint64_t *)(top - 72);
    memset(f, 0, 72);
    f[4] = (uint64_t)t;                         // rbx
    f[6] = (uint64_t)pn_task_entry;             // return address
#endif
    t->sp = f;
}

static Task *task_alloc(size_t stack) {
    Task *t;
    if (stack == TASK_STACK && pool) {
        t = pool; pool = t->next_ready; npool--;
        char *map = t->map; size_t ms = t->map_size;
        memset(t, 0, sizeof *t);
        t->map = map; t->map_size = ms;
    } else {
        t = calloc(1, sizeof *t);
        if (!t) oom();
        t->map_size = stack + page_size();
        t->map = mmap(NULL, t->map_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
        if (t->map == MAP_FAILED) oom();
        mprotect(t->map, page_size(), PROT_NONE);   // the guard page
    }
    t->heap_idx = -1;
#ifdef PN_ASAN
    // a reused stack still carries the previous task's poisoned redzones
    __asan_unpoison_memory_region(t->map + page_size(), t->map_size - page_size());
#endif
    return t;
}

static void task_unlink_req(Task *t) {
    if (!t->req) return;
    if (t->req_prev) t->req_prev->req_next = t->req_next; else t->req->tasks = t->req_next;
    if (t->req_next) t->req_next->req_prev = t->req_prev;
    t->req_prev = t->req_next = NULL;
}

static void task_release(Task *t) {
    task_unlink_req(t);
    if (t->queued) { t->zombie = 1; return; }       // still in the ready queue: freed when popped
    if (t->map_size == TASK_STACK + page_size() && npool < 64) {
        t->next_ready = pool; pool = t; npool++;
    } else {
        munmap(t->map, t->map_size);
        free(t);
    }
}

static void make_ready(Task *t) {
    if (t->state == T_READY || t->state == T_DONE || t->queued) return;
    t->state = T_READY;
    t->queued = 1;
    t->next_ready = NULL;
    if (ready_tail) ready_tail->next_ready = t; else ready_head = t;
    ready_tail = t;
}

static Task *task_spawn(void (*fn)(void *), void *arg, size_t stack) {
    Task *t = task_alloc(stack);
    t->fn = fn; t->arg = arg;
    t->req = cur_req;
    t->cancelled = cur_task ? cur_task->cancelled : 0;   // started by a cancelled task: cancelled too
    if (t->req) {                               // a task belongs to the request it was started in
        t->req_next = t->req->tasks;
        if (t->req_next) t->req_next->req_prev = t;
        t->req->tasks = t;
    }
    frame_init(t);
    t->state = T_WAITING;
    make_ready(t);
    return t;
}

// --- the timer heap (sleep, and later timeouts)
static void heap_swap(int a, int b) {
    Task *x = timers[a]; timers[a] = timers[b]; timers[b] = x;
    timers[a]->heap_idx = a; timers[b]->heap_idx = b;
}
static void heap_up(int i) {
    while (i > 0 && timers[(i - 1) / 2]->wake_at > timers[i]->wake_at) { heap_swap(i, (i - 1) / 2); i = (i - 1) / 2; }
}
static void heap_down(int i) {
    for (;;) {
        int l = 2 * i + 1, r = l + 1, m = i;
        if (l < ntimers && timers[l]->wake_at < timers[m]->wake_at) m = l;
        if (r < ntimers && timers[r]->wake_at < timers[m]->wake_at) m = r;
        if (m == i) return;
        heap_swap(i, m); i = m;
    }
}
static void heap_push(Task *t) {
    if (ntimers == timers_cap) {
        timers_cap = timers_cap ? timers_cap * 2 : 64;
        Task **p = realloc(timers, sizeof(Task *) * (size_t)timers_cap);
        if (!p) oom();
        timers = p;
    }
    t->heap_idx = ntimers;
    timers[ntimers++] = t;
    heap_up(t->heap_idx);
}
static void heap_remove(Task *t) {
    int i = t->heap_idx;
    if (i < 0) return;
    t->heap_idx = -1;
    if (--ntimers == i) return;
    timers[i] = timers[ntimers];
    timers[i]->heap_idx = i;
    heap_up(i);
    heap_down(timers[i]->heap_idx);
}

// Wake every sleeper whose time has come; milliseconds until the next one (-1 = none).
static int64_t timers_fire(int64_t now) {
    while (ntimers && timers[0]->wake_at <= now) {
        Task *t = timers[0];
        heap_remove(t);
        make_ready(t);
    }
    return ntimers ? timers[0]->wake_at - now : -1;
}

// --- switching
static void enter_task(Task *t) {               // from the scheduler into t, until t parks or ends
    cur_task = t;
    t->state = T_RUNNING;
    cur_req = t->req; cur_jmp = t->jmp;
#ifdef PN_ASAN
    void *fake = NULL;
    __sanitizer_start_switch_fiber(&fake, t->map + page_size(), t->map_size - page_size());
#endif
    pn_ctx_switch(&sched_sp, t->sp);
#ifdef PN_ASAN
    __sanitizer_finish_switch_fiber(fake, NULL, NULL);
#endif
    cur_task = NULL; cur_req = NULL; cur_jmp = NULL;
    if (t->state == T_DONE && t->auto_release) task_release(t);
}

static void leave_task(int final) {             // from the running task back to the scheduler
    Task *t = cur_task;
    t->req = cur_req; t->jmp = cur_jmp;
#ifdef PN_ASAN
    __sanitizer_start_switch_fiber(final ? NULL : &t->fake, sched_bottom, sched_size);
#endif
    pn_ctx_switch(&t->sp, sched_sp);
#ifdef PN_ASAN
    __sanitizer_finish_switch_fiber(t->fake, NULL, NULL);
#endif
    (void)final;
}

static void run_ready(void) {
    while (ready_head) {
        Task *t = ready_head;
        ready_head = t->next_ready;
        if (!ready_head) ready_tail = NULL;
        t->queued = 0;
        if (t->zombie) { t->zombie = 0; task_release(t); continue; }
        enter_task(t);
    }
}

// A request whose task panicked: wake its handler, which then panics too and cleans the request up.
static void wake_root(Req *r) {
    Task *root = r->root;
    if (!root || root == cur_task || root->state != T_WAITING) return;
    heap_remove(root);
    if (root->awaiting) { root->awaiting->waiter = NULL; root->awaiting = NULL; }
    make_ready(root);
}

static void park(void) {                        // wait until someone calls make_ready()
    cur_task->state = T_WAITING;
    leave_task(0);
    if (cur_req && cur_req->panicked) die();    // another task of this request panicked
}

void pn_task_main(Task *t_) {
    Task *volatile t = t_;
#ifdef PN_ASAN
    __sanitizer_finish_switch_fiber(NULL, &sched_bottom, &sched_size);
#endif
    jmp_buf jb;
    if (t->req) {
        if (setjmp(jb)) {                       // a panic in this task: the whole request fails
            cur_jmp = NULL;
            t->panicked = 1;
            t->req->panicked = 1;
            wake_root(t->req);
            goto done;
        }
        cur_jmp = &jb;
    }
    t->fn(t->arg);
done:
    cur_jmp = NULL;
    t->state = T_DONE;
    if (t->waiter) { Task *w = t->waiter; t->waiter = NULL; w->awaiting = NULL; make_ready(w); }
    if (t->on_done) t->on_done(t);
    leave_task(1);
    abort();                                    // never resumed
}

// Destroy the other tasks of a failed request (they are parked or queued, never running).
static void fd_disarm_task(Task *t);

static void req_kill_tasks(Req *r) {
    for (Task *t = r->tasks, *nx; t; t = nx) {
        nx = t->req_next;
        if (t == cur_task) continue;
        heap_remove(t);
        fd_disarm_task(t);                      // its stack is going away: no event may point into it
        t->waiter = NULL; t->awaiting = NULL;
        t->state = T_DONE;
        task_release(t);
    }
}

// --- cancellation (cooperative: a cancelled task stops waiting; its next wait returns at once)
static int cancelled_now(void) { return cur_task && (cur_task->cancelled || (cur_req && cur_req->cancelled)); }

static void hub_forget_waiter(Task *t);

static void wake_waiting(Task *t) {             // end whatever wait t is in (it will see it is cancelled)
    if (t->state != T_WAITING || t->awaiting) return;   // awaiting another task: that one is woken instead
    heap_remove(t);
    if (t->waiting_sub) hub_forget_waiter(t);
    make_ready(t);
}

static void task_cancel(Task *t) {
    t->cancelled = 1;
    if (t->awaiting) task_cancel(t->awaiting);  // and what it is waiting for
    else wake_waiting(t);
}

// --- waking a thread from another one (Hub messages): a pipe the thread's loop also polls
typedef struct Sched {
    pthread_mutex_t mu;
    struct Sub *inbox;                  // subscriptions with news for this thread (owner->mu)
    int rd, wr;
} Sched;
static __thread Sched *my_sched;

static Sched *sched_self(void) {
    if (!my_sched) {
        Sched *sc = calloc(1, sizeof *sc);
        int fds[2];
        if (!sc || pipe(fds) < 0) oom();
        fcntl(fds[0], F_SETFL, fcntl(fds[0], F_GETFL) | O_NONBLOCK);
        fcntl(fds[1], F_SETFL, fcntl(fds[1], F_GETFL) | O_NONBLOCK);
        pthread_mutex_init(&sc->mu, NULL);
        sc->rd = fds[0]; sc->wr = fds[1];
        my_sched = sc;
    }
    return my_sched;
}

static void hub_drain(void);                    // after the pipe said there is news

// --- the built-ins
void pn_sleep(int64_t ms) {
    if (!cur_task) { struct timespec ts = {ms / 1000, (long)(ms % 1000) * 1000000}; if (ms > 0) nanosleep(&ts, NULL); return; }
    if (cancelled_now()) return;
    if (ms <= 0) { make_ready(cur_task); cur_task->state = T_READY; leave_task(0); if (cur_req && cur_req->panicked) die(); return; }
    cur_task->wake_at = now_ms() + ms;
    heap_push(cur_task);
    park();
}

// go f(...): the compiler packs the arguments into `frame` and passes a function that unpacks them, calls f
// and stores the result back into the frame.
void *pn_go(void (*fn)(void *), void *frame) { return task_spawn(fn, frame, TASK_STACK); }

// await: wait for the task, then hand back its frame (result and error inside) and free the task.
void *pn_await(Task *t) {
    while (t->state != T_DONE) {
        t->waiter = cur_task;
        cur_task->awaiting = t;
        park();
    }
    int panicked = t->panicked;
    void *frame = t->arg;
    task_release(t);
    if (panicked) die();                        // the request already failed; this task goes with it
    return frame;
}

// await_all($tasks): wait for every task (in order), and return a list of their results; the tasks list is
// consumed. If any failed, every other result is freed, *err_out gets the first error (the others are
// freed) and NULL is returned.
List *pn_list_new(int64_t esize);
void pn_list_free(List *l);
void pn_list_push(List *l, int64_t esize, uint64_t bits);
List *pn_await_all(List *tasks, int64_t esize, void (*free_res)(void *), void (*free_err)(void *), void **err_out) {
    List *out = pn_list_new(esize);
    void *err = NULL;
    for (int64_t i = 0; i < tasks->len; i++) {
        void **f = pn_await(((Task **)tasks->data)[i]);
        void *e = f[1];
        if (e) { if (!err) err = e; else free_err(e); }
        else if (!err) {
            uint64_t bits = 0;
            memcpy(&bits, f, (size_t)esize);
            pn_list_push(out, esize, bits);
        } else if (free_res) free_res(f[0]);
        xfree(f);
    }
    pn_list_free(tasks);
    if (err) {
        if (free_res) for (int64_t i = 0; i < out->len; i++) free_res(((void **)out->data)[i]);
        pn_list_free(out);
        *err_out = err;
        return NULL;
    }
    return out;
}

static int hub_has_subscribers(void);

// --- waiting for a socket or a pipe (HTTP client, run()). In the server the worker's epoll/kqueue set
// watches the fd; in a plain program main_poll() does, with poll(). The FdWait lives on the waiting task's
// stack, so it is removed before the task can go away.
typedef struct FdWait { int kind; int fd; int write; int fired; Task *task; } FdWait;   // kind 1 (Conn: 0)
static __thread int my_ep = -1;
static __thread FdWait *main_waits[64];
static __thread int main_nwaits;

static void ep_arm(FdWait *w);
static void ep_disarm(FdWait *w);

static void fd_unregister(FdWait *w) {
    if (my_ep >= 0) { ep_disarm(w); return; }
    for (int i = 0; i < main_nwaits; i++)
        if (main_waits[i] == w) { main_waits[i] = main_waits[--main_nwaits]; break; }
}

static void fd_disarm_task(Task *t) {
    for (int i = 0; i < 2; i++) if (t->waiting_fd[i]) { fd_unregister(t->waiting_fd[i]); t->waiting_fd[i] = NULL; }
}

static void fd_fired(FdWait *w) {
    w->fired = 1;
    Task *t = w->task;
    if (t->state == T_WAITING) { heap_remove(t); make_ready(t); }
}

static void main_poll(int64_t wait) {
    struct pollfd pf[65];
    int n = 0;
    pf[n++] = (struct pollfd){sched_self()->rd, POLLIN, 0};
    for (int i = 0; i < main_nwaits; i++)
        pf[n++] = (struct pollfd){main_waits[i]->fd, main_waits[i]->write ? POLLOUT : POLLIN, 0};
    poll(pf, (nfds_t)n, wait < 0 ? -1 : (int)(wait > 2000000000 ? 2000000000 : wait));
    if (pf[0].revents) hub_drain();
    FdWait *hit[64]; int nh = 0;
    for (int i = 1; i < n; i++) if (pf[i].revents) hit[nh++] = main_waits[i - 1];
    for (int i = 0; i < nh; i++) fd_fired(hit[i]);
}

// Wait until one of the fds is ready (write: for writing), the deadline passes, or the task is cancelled.
// 1 = ready, 0 = timed out, -1 = cancelled.
static int wait_fds(const int *fds, int n, int write, int64_t deadline) {
    if (cancelled_now()) return -1;
    if (!cur_task) {                            // outside any task: just block
        struct pollfd pf[2];
        for (int i = 0; i < n; i++) pf[i] = (struct pollfd){fds[i], write ? POLLOUT : POLLIN, 0};
        int64_t left = deadline - now_ms();
        return poll(pf, (nfds_t)n, left < 0 ? 0 : (int)left) > 0;
    }
    FdWait w[2];
    for (int i = 0; i < n; i++) {
        w[i] = (FdWait){1, fds[i], write, 0, cur_task};
        if (my_ep >= 0) ep_arm(&w[i]);
        else {
            if (main_nwaits == 64) panic_msg("too many sockets and pipes waited on at once (64)");
            main_waits[main_nwaits++] = &w[i];
        }
        cur_task->waiting_fd[i] = &w[i];
    }
    cur_task->wake_at = deadline;
    heap_push(cur_task);
    cur_task->state = T_WAITING;
    leave_task(0);                              // not park(): unregister before a pending panic unwinds
    heap_remove(cur_task);
    fd_disarm_task(cur_task);
    if (cur_req && cur_req->panicked) die();
    if (cancelled_now()) return -1;
    return (n > 0 && w[0].fired) || (n > 1 && w[1].fired) ? 1 : 0;
}

// The program's main runs as the first task, so it can await and sleep like any other.
static int (*main_body)(void);
static int main_code;
static void main_task(void *arg) { (void)arg; main_code = main_body(); }

int pn_run_main(int (*body)(void)) {
    main_body = body;
    Task *m = task_spawn(main_task, NULL, MAIN_STACK);
    for (;;) {
        run_ready();
        if (m->state == T_DONE) { task_release(m); return main_code; }
        int64_t wait = timers_fire(now_ms());
        if (ready_head) continue;
        if (wait < 0 && !hub_has_subscribers() && !main_nwaits) {
            fflush(stdout);
            fprintf(stderr, "panic: every task is waiting and nothing can wake them (deadlock)\n");
            exit(1);
        }
        main_poll(wait);                        // until the next timer, a Hub message or a socket / pipe
        timers_fire(now_ms());
    }
}
