// Runtime for the prototype language.
//   lists   : {len, cap, data}            elements are 1 or 8 bytes
//   strings : {len, cap, data[]}          one allocation; cap < 0 means a compile-time literal (never freed)
//   maps    : ordered hash map (insertion order, like PHP arrays), 8-byte value slots
//   json    : json_encode for lists and maps
//   http    : multi-threaded HTTP/1.1 server with keep-alive (one thread per CPU core),
//             epoll on Linux, kqueue on macOS
// The compiler guarantees every heap value has exactly one owner, so nothing here needs
// reference counting or a garbage collector.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <setjmp.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/event.h>
#else
#include <sched.h>
#include <sys/epoll.h>
#endif

static void oom(void) { fprintf(stderr, "fatal: out of memory\n"); exit(2); }

// ---------------------------------------------------------------- allocation, requests and panics
// Every heap value of the language (lists, strings, maps, objects and their buffers) is allocated here with a
// 16-byte header. While an HTTP request is being handled, its blocks are linked into that request's list
// (struct Req). If the handler panics, its task longjmps back, frees every block of that request and the
// answer is 500. Handlers share nothing (no globals), so nothing outside the request can point at those
// blocks. Outside requests blocks are not linked and the header only costs 16 bytes.
//
// One OS thread runs many tasks (green threads: go / await, and one task per request in the server), so the
// request state is per task: `cur_req` is switched with the running task.

typedef struct Blk { struct Blk *prev, *next; } Blk;
typedef struct Res { struct Res *prev, *next; void (*close)(void *); void *obj; } Res;
struct Task;

typedef struct Req {
    Blk blocks;                         // circular list of the request's blocks
    Res res;                            // open resources (database handles), closed on panic
    int64_t status;                     // http_status()
    char *hdr; size_t hdr_len;          // a copy of the request's header lines
    char *resp_hdr; size_t resp_hdr_len, resp_hdr_cap;   // extra response header lines (plain malloc)
    int resp_has_ctype;
    int panicked;                       // a task of this request panicked: the whole request answers 500
    struct Task *root;                  // the handler's task
    struct Task *tasks;                 // every live task of the request (linked through req_next)
    int cancelled;                      // the client is gone (a stream's connection closed)
    void *conn;                         // its connection (Conn *), for sse_*
} Req;

static __thread Req *cur_req;           // the request of the running task (NULL outside requests)
static __thread jmp_buf *cur_jmp;       // where a panic in the running task goes (NULL: stop the program)

static void blk_link(Blk *b) {
    Blk *h = &cur_req->blocks;
    b->prev = h->prev; b->next = h;
    h->prev->next = b; h->prev = b;
}

static void blk_unlink(Blk *b) { b->prev->next = b->next; b->next->prev = b->prev; }

static void *xmalloc(size_t n) {
    Blk *b = malloc(sizeof(Blk) + n);
    if (!b) oom();
    if (cur_req) blk_link(b); else b->prev = b->next = NULL;
    return b + 1;
}

static void *xcalloc(size_t n) { return memset(xmalloc(n), 0, n); }

static void *xrealloc(void *p, size_t n) {
    if (!p) return xmalloc(n);
    Blk *b = (Blk *)p - 1;
    int linked = b->prev != NULL;
    if (linked) {                       // keep it in the list it was in (possibly another request's)
        Blk *prev = b->prev, *next = b->next;
        blk_unlink(b);
        b = realloc(b, sizeof(Blk) + n);
        if (!b) oom();
        b->prev = prev; b->next = next; prev->next = b; next->prev = b;
    } else {
        b = realloc(b, sizeof(Blk) + n);
        if (!b) oom();
    }
    return b + 1;
}

static void xfree(void *p) {
    if (!p) return;
    Blk *b = (Blk *)p - 1;
    if (b->prev) blk_unlink(b);
    free(b);
}

static Req *req_new(void) {
    Req *r = calloc(1, sizeof(Req));
    if (!r) oom();
    r->blocks.prev = r->blocks.next = &r->blocks;
    r->res.prev = r->res.next = &r->res;
    r->status = 200;
    return r;
}

// ---------------------------------------------------------------- resources
// Things that hold more than memory (database connections, statements) register a close function
// while a request runs. Normally their owner closes them and unregisters; if the handler panics, the
// request cleanup closes whatever is still open, newest first, before freeing the request's memory.

void *pn_res_add(void (*close)(void *), void *obj) {
    if (!cur_req) return NULL;
    Res *h = &cur_req->res;
    Res *r = malloc(sizeof(Res));
    if (!r) oom();
    r->close = close; r->obj = obj;
    r->prev = h->prev; r->next = h;
    h->prev->next = r; h->prev = r;
    return r;
}

void pn_res_remove(void *token) {
    Res *r = token;
    if (!r) return;
    r->prev->next = r->next; r->next->prev = r->prev;
    free(r);
}

// Free everything the request still owns: open resources first (newest first: statements before their
// connection), then every block. After a normal request this finds little or nothing.
static void req_free_all(Req *rq) {
    Req *saved = cur_req;
    cur_req = rq;
    while (rq->res.prev != &rq->res) {
        Res *r = rq->res.prev;
        r->prev->next = r->next; r->next->prev = r->prev;
        r->close(r->obj);
        free(r);
    }
    Blk *b = rq->blocks.next;
    while (b != &rq->blocks) { Blk *n = b->next; free(b); b = n; }
    rq->blocks.prev = rq->blocks.next = &rq->blocks;
    cur_req = saved;
}

// After a panic message: inside a request, abandon it; otherwise stop the program.
__attribute__((noreturn)) static void die(void) {
    if (cur_jmp) longjmp(*cur_jmp, 1);
    exit(1);
}

__attribute__((noreturn)) static void panic_msg(const char *msg) {
    fflush(stdout);
    fprintf(stderr, "panic: %s\n", msg);
    die();
}

// Byte size of n items of esize bytes plus `extra`, or panic. Every allocation whose size comes
// from a program value goes through here, so a huge or wrapped-around int can never produce a
// small buffer that later code believes is big.
static size_t checked_size(int64_t n, int64_t esize, int64_t extra) {
    int64_t bytes;
    if (n < 0 || __builtin_mul_overflow(n, esize, &bytes) || __builtin_add_overflow(bytes, extra, &bytes)
        || bytes > ((int64_t)1 << 47))
        panic_msg("value is too large to allocate");
    return (size_t)bytes;
}

static int64_t checked_add(int64_t a, int64_t b) {
    int64_t r;
    if (__builtin_add_overflow(a, b, &r)) panic_msg("value is too large to allocate");
    return r;
}

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

// ============================================================================ lists

List *pn_list_new(int64_t esize) {
    List *l = xmalloc(sizeof(List));
    l->len = 0; l->cap = 8;
    l->data = xmalloc((size_t)(8 * esize));
    return l;
}

List *pn_list_fill(int64_t n, int64_t esize, uint64_t bits) {
    if (n < 0) panic_msg("fill() with negative size");
    List *l = xmalloc(sizeof(List));
    l->len = n; l->cap = n > 0 ? n : 1;
    l->data = xmalloc(checked_size(l->cap, esize, 0));
    if (bits == 0) memset(l->data, 0, (size_t)n * (size_t)esize);
    else if (esize == 1) memset(l->data, (int)(bits & 0xff), (size_t)n);
    else { uint64_t *d = l->data; for (int64_t i = 0; i < n; i++) d[i] = bits; }
    return l;
}

void pn_list_grow(List *l, int64_t esize) {
    size_t bytes = checked_size(l->cap, esize * 2, 0);
    l->cap *= 2;
    l->data = xrealloc(l->data, bytes);
}

void pn_list_push(List *l, int64_t esize, uint64_t bits) {
    if (l->len == l->cap) pn_list_grow(l, esize);
    memcpy((char *)l->data + l->len * esize, &bits, (size_t)esize);
    l->len++;
}

List *pn_list_copy(List *src, int64_t esize) {
    List *l = xmalloc(sizeof(List));
    l->len = src->len; l->cap = src->len > 0 ? src->len : 1;
    l->data = xmalloc(checked_size(l->cap, esize, 0));
    memcpy(l->data, src->data, (size_t)(src->len * esize));
    return l;
}

void pn_list_free(List *l) {
    if (!l) return;
    xfree(l->data);
    xfree(l);
}

__attribute__((noreturn)) void pn_oob(int64_t i, int64_t len) {
    fflush(stdout);
    fprintf(stderr, "panic: index %lld out of range (list has %lld items)\n", (long long)i, (long long)len);
    die();
}

__attribute__((noreturn)) void pn_divzero(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: division by zero on line %d\n", line);
    die();
}

__attribute__((noreturn)) void pn_overflow(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: integer overflow on line %d (intdiv(PHP_INT_MIN, -1) has no int answer)\n", line);
    die();
}

__attribute__((noreturn)) void pn_float_to_int(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: float is NAN, INF or too large to turn into an int on line %d\n", line);
    die();
}

__attribute__((noreturn)) void pn_noreturn(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: function ended without return (line %d)\n", line);
    die();
}

__attribute__((noreturn)) void pn_map_missing(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: key not found on line %d (check with isset() or use the ?? operator)\n", line);
    die();
}

// ============================================================================ strings

typedef struct { int64_t len, cap; char data[]; } Str;

static Str *str_alloc(int64_t cap) {
    if (cap < 15) cap = 15;
    Str *s = xmalloc(checked_size(cap, 1, (int64_t)sizeof(Str) + 1));
    s->len = 0; s->cap = cap; s->data[0] = 0;
    return s;
}

static Str *str_from(const char *p, int64_t n) {
    Str *s = str_alloc(n);
    memcpy(s->data, p, (size_t)n);
    s->len = n; s->data[n] = 0;
    return s;
}

Str *pn_str_new(int64_t cap) { return str_alloc(cap); }

void pn_str_free(Str *s) { if (s && s->cap >= 0) xfree(s); }

// Copy a string. Literals are immutable and live forever, so their "copy" is the literal itself.
Str *pn_str_dup(Str *s) {
    if (!s) return str_alloc(0);
    if (s->cap < 0) return s;
    return str_from(s->data, s->len);
}

// Make room for `extra` more bytes. Takes ownership of `s`; returns the (possibly new) string.
static Str *str_reserve(Str *s, int64_t extra) {
    if (!s) return str_alloc(extra);
    int64_t need = checked_add(s->len, extra);
    if (s->cap < 0) {                      // literal: never written to, copy first
        Str *n = str_alloc(need > 2 * s->len ? need : 2 * s->len);
        memcpy(n->data, s->data, (size_t)s->len + 1);
        n->len = s->len;
        return n;
    }
    if (need > s->cap) {
        int64_t nc = s->cap * 2;           // cap passed checked_size, so doubling it cannot wrap
        if (nc < need) nc = need;
        s = xrealloc(s, checked_size(nc, 1, (int64_t)sizeof(Str) + 1));
        s->cap = nc;
    }
    return s;
}

static Str *str_add_bytes(Str *d, const char *p, int64_t n) {
    d = str_reserve(d, n);
    memcpy(d->data + d->len, p, (size_t)n);
    d->len += n;
    d->data[d->len] = 0;
    return d;
}

Str *pn_str_add_str(Str *d, Str *src) {
    if (!src) return d ? d : str_alloc(0);
    int same = (d == src);
    int64_t n = src->len;
    Str *r = str_reserve(d, n);
    const char *sp = same ? r->data : src->data;  // appending a string to itself
    memmove(r->data + r->len, sp, (size_t)n);
    r->len += n;
    r->data[r->len] = 0;
    return r;
}

Str *pn_str_add_int(Str *d, int64_t v) {
    char buf[24];
    int n = snprintf(buf, sizeof buf, "%lld", (long long)v);
    return str_add_bytes(d, buf, n);
}

// PHP prints floats with 14 significant digits and drops ".0"
static int fmt_float(char *buf, size_t sz, double v) {
    if (isnan(v)) return snprintf(buf, sz, "NAN");
    if (isinf(v)) return snprintf(buf, sz, v > 0 ? "INF" : "-INF");
    return snprintf(buf, sz, "%.14G", v);
}

Str *pn_str_add_float(Str *d, double v) {
    char buf[48];
    int n = fmt_float(buf, sizeof buf, v);
    return str_add_bytes(d, buf, n);
}

Str *pn_str_from_int(int64_t v) { return pn_str_add_int(NULL, v); }
Str *pn_str_from_float(double v) { return pn_str_add_float(NULL, v); }

int64_t pn_str_eq(Str *a, Str *b) {
    return a->len == b->len && memcmp(a->data, b->data, (size_t)a->len) == 0;
}

int64_t pn_str_cmp(Str *a, Str *b) {
    int64_t n = a->len < b->len ? a->len : b->len;
    int c = memcmp(a->data, b->data, (size_t)n);
    if (c) return c < 0 ? -1 : 1;
    return a->len < b->len ? -1 : a->len > b->len;
}

// Digits from p to e as an int, saturating at PHP_INT_MAX / PHP_INT_MIN like PHP. Requests carry
// numbers from clients; saturating keeps them out of the caller's valid range (a 400) instead of a 500.
static int64_t digits_saturating(const char *p, const char *e, int neg) {
    uint64_t v = 0, limit = neg ? (uint64_t)INT64_MAX + 1 : (uint64_t)INT64_MAX;
    for (; p < e && *p >= '0' && *p <= '9'; p++) {
        uint64_t d = (uint64_t)(*p - '0');
        if (v > (limit - d) / 10) { v = limit; break; }
        v = v * 10 + d;
    }
    return neg ? (int64_t)(0 - v) : (int64_t)v;
}

// intval(): optional whitespace, optional sign, digits; anything else stops the number.
int64_t pn_str_to_int(Str *s) {
    const char *p = s->data, *e = s->data + s->len;
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    int neg = 0;
    if (p < e && (*p == '+' || *p == '-')) neg = (*p++ == '-');
    return digits_saturating(p, e, neg);
}

// floatval() / (float)"...": the leading number like PHP ("2.5kg" -> 2.5, "abc" -> 0)
double pn_str_to_float(Str *s) {
    char buf[64];
    const char *p = s->data, *e = s->data + s->len;
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    size_t n = 0;
    while (p < e && n < sizeof buf - 1 && (*p == '+' || *p == '-' || *p == '.' || *p == 'e' || *p == 'E' ||
                                           (*p >= '0' && *p <= '9')))
        buf[n++] = *p++;
    buf[n] = 0;
    return strtod(buf, NULL);              // stops at the first character that does not fit
}

// PHP substr semantics, including negative start / length
Str *pn_substr(Str *s, int64_t start, int64_t len, int64_t has_len) {
    int64_t n = s->len;
    if (start < 0) { start += n; if (start < 0) start = 0; }
    if (start > n) return str_alloc(0);
    int64_t end = n;
    if (has_len) {
        if (len < 0) { end = n + len; if (end < start) return str_alloc(0); }
        else if (len < n - start) end = start + len;
    }
    return str_from(s->data + start, end - start);
}

int64_t pn_strpos(Str *h, Str *needle) {
    if (needle->len == 0) return 0;
    void *p = memmem(h->data, (size_t)h->len, needle->data, (size_t)needle->len);
    return p ? (int64_t)((char *)p - h->data) : -1;
}

int64_t pn_str_contains(Str *h, Str *n) { return pn_strpos(h, n) >= 0; }
int64_t pn_str_starts(Str *h, Str *n) { return h->len >= n->len && memcmp(h->data, n->data, (size_t)n->len) == 0; }
int64_t pn_str_ends(Str *h, Str *n) {
    return h->len >= n->len && memcmp(h->data + h->len - n->len, n->data, (size_t)n->len) == 0;
}

Str *pn_str_repeat(Str *s, int64_t times) {
    if (times < 0) panic_msg("str_repeat() with a negative count");
    if (times > 0 && s->len > INT64_MAX / times) panic_msg("str_repeat() result is too large");
    Str *r = str_alloc(s->len * times);
    for (int64_t i = 0; i < times; i++) memcpy(r->data + i * s->len, s->data, (size_t)s->len);
    r->len = s->len * times;
    r->data[r->len] = 0;
    return r;
}

Str *pn_str_case(Str *s, int64_t upper) {
    Str *r = str_from(s->data, s->len);
    for (int64_t i = 0; i < r->len; i++) {
        char c = r->data[i];
        if (upper && c >= 'a' && c <= 'z') r->data[i] = c - 32;
        if (!upper && c >= 'A' && c <= 'Z') r->data[i] = c + 32;
    }
    return r;
}

Str *pn_str_trim(Str *s) {
    int64_t a = 0, b = s->len;
    while (a < b && strchr(" \t\n\r\v", s->data[a]) && s->data[a]) a++;
    while (b > a && strchr(" \t\n\r\v", s->data[b - 1]) && s->data[b - 1]) b--;
    return str_from(s->data + a, b - a);
}

// ============================================================================ maps

// kinds: 0 int, 1 float, 2 bool, 3 string, 4 owned pointer (list, map or object; freed by compiled code)
typedef struct { uint64_t hash, key, val; int64_t alive; } Entry;
typedef struct {
    int64_t count;          // first field so count() is one load, same as lists
    int64_t used, cap;      // entries used (including deleted) / allocated
    Entry *e;
    int64_t *idx;           // open addressing: entry index + 1, 0 = empty
    int64_t isize;
    int32_t kk, vk;
} Map;

static uint64_t hash_int(uint64_t x) {
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
    return x;
}

static uint64_t hash_bytes(const char *p, int64_t n) {
    uint64_t h = 0xcbf29ce484222325ULL;
    for (int64_t i = 0; i < n; i++) { h ^= (unsigned char)p[i]; h *= 0x100000001b3ULL; }
    return hash_int(h);
}

static uint64_t map_hash(Map *m, uint64_t key) {
    if (m->kk == 3) { Str *s = (Str *)key; return hash_bytes(s->data, s->len); }
    return hash_int(key);
}

static int key_eq(Map *m, uint64_t a, uint64_t b) {
    if (m->kk == 3) return pn_str_eq((Str *)a, (Str *)b);
    return a == b;
}

Map *pn_map_new(int64_t kk, int64_t vk) {
    Map *m = xmalloc(sizeof(Map));
    m->count = 0; m->used = 0; m->cap = 8;
    m->e = xmalloc(sizeof(Entry) * 8);
    m->isize = 16;
    m->idx = xcalloc(16 * sizeof(int64_t));
    m->kk = (int32_t)kk; m->vk = (int32_t)vk;
    return m;
}

static void map_reindex(Map *m) {
    xfree(m->idx);
    m->idx = xcalloc(checked_size(m->isize, (int64_t)sizeof(int64_t), 0));
    uint64_t mask = (uint64_t)m->isize - 1;
    for (int64_t i = 0; i < m->used; i++) {
        uint64_t j = m->e[i].hash & mask;
        while (m->idx[j]) j = (j + 1) & mask;
        m->idx[j] = i + 1;
    }
}

// Make room for one more entry: drop deleted entries, grow if still full.
static void map_make_room(Map *m) {
    if (m->used < m->cap) return;
    if (m->count < m->used) {              // compact deleted entries
        int64_t w = 0;
        for (int64_t i = 0; i < m->used; i++) if (m->e[i].alive) m->e[w++] = m->e[i];
        m->used = w;
    }
    if (m->used >= m->cap * 3 / 4) {
        m->cap *= 2;
        m->e = xrealloc(m->e, sizeof(Entry) * (size_t)m->cap);
        m->isize = m->cap * 2;
    }
    map_reindex(m);
}

// Pointer to the 8-byte value slot for `key`. With create, a missing key is added with a zero value.
uint64_t *pn_map_slot(Map *m, uint64_t key, int64_t create) {
    uint64_t h = map_hash(m, key);
    uint64_t mask = (uint64_t)m->isize - 1, j = h & mask;
    for (;;) {
        int64_t k = m->idx[j];
        if (!k) break;
        Entry *e = &m->e[k - 1];
        if (e->alive && e->hash == h && key_eq(m, e->key, key)) return &e->val;
        j = (j + 1) & mask;
    }
    if (!create) return NULL;
    if (m->used == m->cap) {
        map_make_room(m);
        mask = (uint64_t)m->isize - 1; j = h & mask;
        while (m->idx[j]) j = (j + 1) & mask;
    }
    Entry *e = &m->e[m->used];
    e->hash = h;
    e->key = m->kk == 3 ? (uint64_t)pn_str_dup((Str *)key) : key;
    e->val = 0;
    e->alive = 1;
    m->idx[j] = ++m->used;
    m->count++;
    return &e->val;
}

int64_t pn_map_has(Map *m, uint64_t key) { return pn_map_slot(m, key, 0) != NULL; }

void pn_map_del(Map *m, uint64_t key) {
    uint64_t *slot = pn_map_slot(m, key, 0);
    if (!slot) return;
    Entry *e = (Entry *)((char *)slot - offsetof(Entry, val));
    if (m->kk == 3) pn_str_free((Str *)e->key);
    if (m->vk == 3) pn_str_free((Str *)e->val);
    e->alive = 0;
    m->count--;
}

int64_t pn_map_next(Map *m, int64_t pos) {
    while (pos < m->used && !m->e[pos].alive) pos++;
    return pos < m->used ? pos : -1;
}

Entry *pn_map_entry(Map *m, int64_t i) { return &m->e[i]; }

void pn_map_free(Map *m) {
    if (!m) return;
    for (int64_t i = 0; i < m->used; i++) {
        if (!m->e[i].alive) continue;
        if (m->kk == 3) pn_str_free((Str *)m->e[i].key);
        if (m->vk == 3) pn_str_free((Str *)m->e[i].val);
    }
    xfree(m->e); xfree(m->idx); xfree(m);
}

Map *pn_map_copy(Map *src) {
    if (src->vk == 4) panic_msg("internal: map of objects copied without its item copier");
    Map *m = pn_map_new(src->kk, src->vk);
    for (int64_t i = 0; i < src->used; i++) {
        Entry *e = &src->e[i];
        if (!e->alive) continue;
        uint64_t *slot = pn_map_slot(m, e->key, 1);
        *slot = src->vk == 3 ? (uint64_t)pn_str_dup((Str *)e->val) : e->val;
    }
    return m;
}

// ============================================================================ json

static Str *json_string(Str *d, const char *p, int64_t n) {
    d = str_add_bytes(d, "\"", 1);
    int64_t run = 0;
    for (int64_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)p[i];
        const char *esc = NULL;
        char buf[8];
        if (c == '"') esc = "\\\"";
        else if (c == '\\') esc = "\\\\";
        else if (c == '\n') esc = "\\n";
        else if (c == '\r') esc = "\\r";
        else if (c == '\t') esc = "\\t";
        else if (c < 0x20) { snprintf(buf, sizeof buf, "\\u%04x", c); esc = buf; }
        if (esc) {
            d = str_add_bytes(d, p + i - run, run);
            d = str_add_bytes(d, esc, (int64_t)strlen(esc));
            run = 0;
        } else run++;
    }
    d = str_add_bytes(d, p + n - run, run);
    return str_add_bytes(d, "\"", 1);
}

static Str *json_float(Str *d, double v) {
    char buf[40];
    if (!isfinite(v)) return str_add_bytes(d, "0", 1);
    for (int prec = 1; prec <= 17; prec++) {       // shortest form that reads back exactly
        snprintf(buf, sizeof buf, "%.*g", prec, v);
        if (strtod(buf, NULL) == v) break;
    }
    // shortest %g of a whole number can come out as 3.8e+02; write whole numbers below 1e15 like PHP: 380.0
    if (strpbrk(buf, "eE") && v == floor(v) && fabs(v) < 1e15) snprintf(buf, sizeof buf, "%.1f", v);
    if (!strpbrk(buf, ".eE")) strcat(buf, ".0");
    return str_add_bytes(d, buf, (int64_t)strlen(buf));
}

static Str *json_value(Str *d, uint64_t bits, int kind) {
    switch (kind) {
    case 0: return pn_str_add_int(d, (int64_t)bits);
    case 1: { double v; memcpy(&v, &bits, 8); return json_float(d, v); }
    case 2: return (bits & 0xff) ? str_add_bytes(d, "true", 4) : str_add_bytes(d, "false", 5);
    default: { Str *s = (Str *)bits; return json_string(d, s->data, s->len); }
    }
}

Str *pn_json_add_list(Str *d, List *l, int64_t kind) {
    d = str_add_bytes(d, "[", 1);
    for (int64_t i = 0; i < l->len; i++) {
        if (i) d = str_add_bytes(d, ",", 1);
        uint64_t bits = 0;
        memcpy(&bits, (char *)l->data + i * (kind == 2 ? 1 : 8), kind == 2 ? 1 : 8);
        d = json_value(d, bits, (int)kind);
    }
    return str_add_bytes(d, "]", 1);
}

Str *pn_json_list(List *l, int64_t kind) { return pn_json_add_list(str_alloc(16 + l->len * 4), l, kind); }

static Str *json_key(Str *d, Map *m, Entry *e) {
    if (m->kk == 3) { Str *k = (Str *)e->key; d = json_string(d, k->data, k->len); }
    else { char buf[24]; int n = snprintf(buf, sizeof buf, "\"%lld\"", (long long)e->key); d = str_add_bytes(d, buf, n); }
    return str_add_bytes(d, ":", 1);
}

Str *pn_json_add_map(Str *d, Map *m) {
    d = str_add_bytes(d, "{", 1);
    int first = 1;
    for (int64_t i = 0; i < m->used; i++) {
        Entry *e = &m->e[i];
        if (!e->alive) continue;
        if (!first) d = str_add_bytes(d, ",", 1);
        first = 0;
        d = json_value(json_key(d, m, e), e->val, m->vk);
    }
    return str_add_bytes(d, "}", 1);
}

Str *pn_json_map(Map *m) { return pn_json_add_map(str_alloc(16 + m->count * 16), m); }

Str *pn_json_str(Str *s) { return json_string(NULL, s->data, s->len); }
Str *pn_json_float(double v) { return json_float(NULL, v); }
Str *pn_json_add_str(Str *d, Str *s) { return json_string(d, s->data, s->len); }
Str *pn_json_add_float(Str *d, double v) { return json_float(d, v); }

// ============================================================================ objects and nested containers
// An object is a block of 8-byte fields. Lists whose items are lists, maps or objects, and maps whose values
// are (value kind 4), hold owned pointers; the compiler passes the function that frees, copies or writes the
// JSON of one item (generated for each type), so the runtime needs no type information.

typedef void (*FreeFn)(void *);
typedef void *(*CopyFn)(void *);
typedef Str *(*JsonFn)(Str *, void *);

void *pn_obj_new(int64_t size) { return xcalloc((size_t)size); }
void pn_obj_free(void *o) { xfree(o); }

void pn_list_free_with(List *l, FreeFn f) {
    if (!l) return;
    void **d = l->data;
    for (int64_t i = 0; i < l->len; i++) f(d[i]);
    pn_list_free(l);
}

List *pn_list_copy_with(List *src, CopyFn f) {
    List *l = pn_list_copy(src, 8);
    void **d = l->data;
    for (int64_t i = 0; i < l->len; i++) d[i] = f(d[i]);
    return l;
}

Str *pn_json_add_list_with(Str *d, List *l, JsonFn f) {
    d = str_add_bytes(d, "[", 1);
    void **items = l->data;
    for (int64_t i = 0; i < l->len; i++) {
        if (i) d = str_add_bytes(d, ",", 1);
        d = f(d, items[i]);
    }
    return str_add_bytes(d, "]", 1);
}

void pn_map_free_with(Map *m, FreeFn f) {
    if (!m) return;
    for (int64_t i = 0; i < m->used; i++) {
        if (!m->e[i].alive) continue;
        if (m->kk == 3) pn_str_free((Str *)m->e[i].key);
        f((void *)m->e[i].val);
    }
    xfree(m->e); xfree(m->idx); xfree(m);
}

Map *pn_map_copy_with(Map *src, CopyFn f) {
    Map *m = pn_map_new(src->kk, src->vk);
    for (int64_t i = 0; i < src->used; i++) {
        Entry *e = &src->e[i];
        if (!e->alive) continue;
        *pn_map_slot(m, e->key, 1) = (uint64_t)f((void *)e->val);
    }
    return m;
}

void pn_map_del_with(Map *m, uint64_t key, FreeFn f) {
    uint64_t *slot = pn_map_slot(m, key, 0);
    if (!slot) return;
    Entry *e = (Entry *)((char *)slot - offsetof(Entry, val));
    if (m->kk == 3) pn_str_free((Str *)e->key);
    f((void *)e->val);
    e->alive = 0;
    m->count--;
}

Str *pn_json_add_map_with(Str *d, Map *m, JsonFn f) {
    d = str_add_bytes(d, "{", 1);
    int first = 1;
    for (int64_t i = 0; i < m->used; i++) {
        Entry *e = &m->e[i];
        if (!e->alive) continue;
        if (!first) d = str_add_bytes(d, ",", 1);
        first = 0;
        d = f(json_key(d, m, e), (void *)e->val);
    }
    return str_add_bytes(d, "}", 1);
}

// ============================================================================ list<string>, explode, implode, sort

// list<string> elements are owned Str pointers (8 bytes); literals (cap < 0) are never freed.
void pn_list_free_str(List *l) {
    if (!l) return;
    Str **d = l->data;
    for (int64_t i = 0; i < l->len; i++) pn_str_free(d[i]);
    pn_list_free(l);
}

List *pn_list_copy_str(List *src) {
    List *l = pn_list_copy(src, 8);
    Str **d = l->data;
    for (int64_t i = 0; i < l->len; i++) d[i] = pn_str_dup(d[i]);
    return l;
}

List *pn_list_fill_str(int64_t n, Str *s) {
    List *l = pn_list_fill(n, 8, 0);
    Str **d = l->data;
    for (int64_t i = 0; i < n; i++) d[i] = pn_str_dup(s);
    return l;
}

// explode($sep, $s): PHP order; an empty separator panics like PHP's ValueError
List *pn_explode(Str *sep, Str *s) {
    if (sep->len == 0) panic_msg("explode(): the separator cannot be empty");
    List *l = pn_list_new(8);
    const char *p = s->data, *e = s->data + s->len;
    for (;;) {
        const char *hit = memmem(p, (size_t)(e - p), sep->data, (size_t)sep->len);
        const char *end = hit ? hit : e;
        Str *part = str_from(p, end - p);
        if (l->len == l->cap) pn_list_grow(l, 8);
        ((Str **)l->data)[l->len++] = part;
        if (!hit) break;
        p = hit + sep->len;
    }
    return l;
}

// implode($glue, $list): kind 0 int, 1 float, 3 string
Str *pn_implode(Str *glue, List *l, int64_t kind) {
    Str *r = str_alloc(16);
    for (int64_t i = 0; i < l->len; i++) {
        if (i) r = pn_str_add_str(r, glue);
        if (kind == 3) r = pn_str_add_str(r, ((Str **)l->data)[i]);
        else if (kind == 1) r = pn_str_add_float(r, ((double *)l->data)[i]);
        else r = pn_str_add_int(r, ((int64_t *)l->data)[i]);
    }
    return r;
}

static int cmp_int(const void *a, const void *b) {
    int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
    return (x > y) - (x < y);
}
static int cmp_float(const void *a, const void *b) {     // NAN sorts last
    double x = *(const double *)a, y = *(const double *)b;
    if (isnan(x) || isnan(y)) return isnan(x) - isnan(y);
    return (x > y) - (x < y);
}
static int cmp_str(const void *a, const void *b) { return (int)pn_str_cmp(*(Str *const *)a, *(Str *const *)b); }
static int cmp_bool(const void *a, const void *b) { return *(const unsigned char *)a - *(const unsigned char *)b; }

// sort / rsort in place: kind 0 int, 1 float, 2 bool, 3 string (byte order)
void pn_list_sort(List *l, int64_t kind, int64_t reverse) {
    if (l->len < 2) return;
    size_t es = kind == 2 ? 1 : 8;
    int (*cmp)(const void *, const void *) = kind == 0 ? cmp_int : kind == 1 ? cmp_float : kind == 2 ? cmp_bool : cmp_str;
    qsort(l->data, (size_t)l->len, es, cmp);
    if (reverse) {
        char tmp[8], *d = l->data;
        for (int64_t i = 0, j = l->len - 1; i < j; i++, j--) {
            memcpy(tmp, d + i * es, es); memcpy(d + i * es, d + j * es, es); memcpy(d + j * es, tmp, es);
        }
    }
}

// ============================================================================ request helpers
// Everything here reads untrusted client input: every read is bounded by an explicit end pointer.

// url_path("/a/b?x=1") -> "/a/b"
Str *pn_url_path(Str *path) {
    const char *q = memchr(path->data, '?', (size_t)path->len);
    return str_from(path->data, q ? q - path->data : path->len);
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// Find `key` in "a=1&b=2" (application/x-www-form-urlencoded); returns the decoded value or "".
static Str *form_lookup(const char *p, const char *e, Str *key) {
    while (p < e) {
        const char *amp = memchr(p, '&', (size_t)(e - p));
        const char *end = amp ? amp : e;
        const char *eq = memchr(p, '=', (size_t)(end - p));
        const char *kend = eq ? eq : end;
        // compare the decoded name with key
        Str *name = str_alloc(kend - p);
        for (const char *c = p; c < kend; c++) {
            char ch = *c;
            if (ch == '+') ch = ' ';
            else if (ch == '%' && kend - c > 2 && hexval(c[1]) >= 0 && hexval(c[2]) >= 0) {
                ch = (char)(hexval(c[1]) * 16 + hexval(c[2])); c += 2;
            }
            name->data[name->len++] = ch;
        }
        name->data[name->len] = 0;
        int match = pn_str_eq(name, key);
        pn_str_free(name);
        if (match) {
            Str *v = str_alloc(eq ? end - eq - 1 : 0);
            for (const char *c = eq ? eq + 1 : end; c < end; c++) {
                char ch = *c;
                if (ch == '+') ch = ' ';
                else if (ch == '%' && end - c > 2 && hexval(c[1]) >= 0 && hexval(c[2]) >= 0) {
                    ch = (char)(hexval(c[1]) * 16 + hexval(c[2])); c += 2;
                }
                v->data[v->len++] = ch;
            }
            v->data[v->len] = 0;
            return v;
        }
        p = amp ? amp + 1 : e;
    }
    return str_alloc(0);
}

Str *pn_query_param(Str *path, Str *key) {
    const char *q = memchr(path->data, '?', (size_t)path->len);
    if (!q) return str_alloc(0);
    return form_lookup(q + 1, path->data + path->len, key);
}

Str *pn_form_param(Str *body, Str *key) { return form_lookup(body->data, body->data + body->len, key); }

// is `key` present at all (even as "key=" or a bare "key")?
static int form_has(const char *p, const char *e, Str *key) {
    while (p < e) {
        const char *amp = memchr(p, '&', (size_t)(e - p)), *end = amp ? amp : e;
        const char *eq = memchr(p, '=', (size_t)(end - p)), *kend = eq ? eq : end;
        // decode the name into a temporary and compare (names are short; reuse form_lookup's rules)
        Str *name = str_alloc(kend - p);
        for (const char *c = p; c < kend; c++) {
            char ch = *c;
            if (ch == '+') ch = ' ';
            else if (ch == '%' && kend - c > 2 && hexval(c[1]) >= 0 && hexval(c[2]) >= 0) {
                ch = (char)(hexval(c[1]) * 16 + hexval(c[2])); c += 2;
            }
            name->data[name->len++] = ch;
        }
        name->data[name->len] = 0;
        int match = kend > p && pn_str_eq(name, key);
        pn_str_free(name);
        if (match) return 1;
        p = amp ? amp + 1 : e;
    }
    return 0;
}

int64_t pn_query_has(Str *path, Str *key) {
    const char *q = memchr(path->data, '?', (size_t)path->len);
    return q ? form_has(q + 1, path->data + path->len, key) : 0;
}

int64_t pn_form_has(Str *body, Str *key) { return form_has(body->data, body->data + body->len, key); }

// --- a minimal JSON reader for the top level of an object: {"key": value, ...}
static const char *jws(const char *p, const char *e) {
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    return p;
}

// end of the string starting at p (p points at the opening quote), or NULL if unterminated
static const char *jstr_end(const char *p, const char *e) {
    for (p++; p < e; p++) {
        if (*p == '\\') { p++; continue; }
        if (*p == '"') return p + 1;
    }
    return NULL;
}

// skip one value; NULL if malformed. depth guards against stack exhaustion on hostile input.
static const char *jskip(const char *p, const char *e, int depth) {
    p = jws(p, e);
    if (p >= e || depth > 64) return NULL;
    if (*p == '"') return jstr_end(p, e);
    if (*p == '{' || *p == '[') {
        char close = *p == '{' ? '}' : ']';
        p = jws(p + 1, e);
        if (p < e && *p == close) return p + 1;
        for (;;) {
            if (close == '}') {
                p = jws(p, e);
                if (p >= e || *p != '"' || !(p = jstr_end(p, e))) return NULL;
                p = jws(p, e);
                if (p >= e || *p != ':') return NULL;
                p++;
            }
            if (!(p = jskip(p, e, depth + 1))) return NULL;
            p = jws(p, e);
            if (p >= e) return NULL;
            if (*p == ',') { p++; continue; }
            if (*p == close) return p + 1;
            return NULL;
        }
    }
    static const char *lits[] = {"true", "false", "null"};
    for (int i = 0; i < 3; i++) {
        size_t n = strlen(lits[i]);
        if ((size_t)(e - p) >= n && memcmp(p, lits[i], n) == 0) return p + n;
    }
    const char *s = p;           // a number
    while (p < e && (*p == '-' || *p == '+' || *p == '.' || (*p >= '0' && *p <= '9') || *p == 'e' || *p == 'E')) p++;
    return p > s && (*s == '-' || (*s >= '0' && *s <= '9')) ? p : NULL;
}

// append the UTF-8 for code point cp
static Str *add_utf8(Str *d, uint32_t cp) {
    char b[4]; int n;
    if (cp < 0x80) { b[0] = (char)cp; n = 1; }
    else if (cp < 0x800) { b[0] = (char)(0xC0 | cp >> 6); b[1] = (char)(0x80 | (cp & 0x3F)); n = 2; }
    else if (cp < 0x10000) {
        b[0] = (char)(0xE0 | cp >> 12); b[1] = (char)(0x80 | ((cp >> 6) & 0x3F)); b[2] = (char)(0x80 | (cp & 0x3F)); n = 3;
    } else {
        b[0] = (char)(0xF0 | cp >> 18); b[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        b[2] = (char)(0x80 | ((cp >> 6) & 0x3F)); b[3] = (char)(0x80 | (cp & 0x3F)); n = 4;
    }
    return str_add_bytes(d, b, n);
}

static int hex4(const char *p, const char *e, uint32_t *out) {
    if (e - p < 4) return 0;
    uint32_t v = 0;
    for (int i = 0; i < 4; i++) { int h = hexval(p[i]); if (h < 0) return 0; v = v * 16 + (uint32_t)h; }
    *out = v;
    return 1;
}

// decode the JSON string starting at p (opening quote) up to q (just after the closing quote)
static Str *jstr_decode(const char *p, const char *q) {
    Str *d = str_alloc(q - p);
    const char *e = q - 1;                  // closing quote
    for (p++; p < e; p++) {
        if (*p != '\\') { d = str_add_bytes(d, p, 1); continue; }
        if (++p >= e) break;
        char c = *p, out = 0;
        switch (c) {
        case 'n': out = '\n'; break; case 't': out = '\t'; break; case 'r': out = '\r'; break;
        case 'b': out = '\b'; break; case 'f': out = '\f'; break;
        case 'u': {
            uint32_t cp;
            if (!hex4(p + 1, e, &cp)) { out = 'u'; break; }
            p += 4;
            if (cp >= 0xD800 && cp < 0xDC00 && e - p > 6 && p[1] == '\\' && p[2] == 'u') {
                uint32_t lo;
                if (hex4(p + 3, e, &lo) && lo >= 0xDC00 && lo < 0xE000) {
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00); p += 6;
                } else cp = 0xFFFD;
            } else if (cp >= 0xD800 && cp < 0xE000) cp = 0xFFFD;   // lone surrogate
            d = add_utf8(d, cp);
            continue;
        }
        default: out = c;                  // \" \\ \/
        }
        d = str_add_bytes(d, &out, 1);
    }
    return d;
}

// pointer to the value of top-level `key` and its end, or 0
static int jfind(Str *json, Str *key, const char **vs, const char **ve) {
    const char *p = json->data, *e = json->data + json->len;
    p = jws(p, e);
    if (p >= e || *p != '{') return 0;
    p = jws(p + 1, e);
    if (p < e && *p == '}') return 0;
    for (;;) {
        p = jws(p, e);
        if (p >= e || *p != '"') return 0;
        const char *ks = p, *kq = jstr_end(p, e);
        if (!kq) return 0;
        p = jws(kq, e);
        if (p >= e || *p != ':') return 0;
        const char *v = jws(p + 1, e), *vend = jskip(v, e, 0);
        if (!vend) return 0;
        Str *k = jstr_decode(ks, kq);
        int match = pn_str_eq(k, key);
        pn_str_free(k);
        if (match) { *vs = v; *ve = vend; return 1; }
        p = jws(vend, e);
        if (p >= e || *p != ',') return 0;
        p++;
    }
}

int64_t pn_jget_has(Str *json, Str *key) { const char *s, *e; return jfind(json, key, &s, &e); }

Str *pn_jget_string(Str *json, Str *key) {
    const char *s, *e;
    if (!jfind(json, key, &s, &e) || *s != '"') return str_alloc(0);
    return jstr_decode(s, e);
}

int64_t pn_jget_int(Str *json, Str *key) {
    const char *s, *e;
    if (!jfind(json, key, &s, &e)) return 0;
    const char *p = s;
    int neg = 0;
    if (p < e && *p == '-') { neg = 1; p++; }
    if (p >= e || *p < '0' || *p > '9') return 0;
    const char *q = p;
    while (q < e && *q >= '0' && *q <= '9') q++;
    if (q != e) return 0;                  // 1.5, 1e3: not an int
    return digits_saturating(p, e, neg);
}

double pn_jget_float(Str *json, Str *key) {
    const char *s, *e;
    if (!jfind(json, key, &s, &e) || e - s > 63 || !(*s == '-' || (*s >= '0' && *s <= '9'))) return 0.0;
    char buf[64];
    memcpy(buf, s, (size_t)(e - s));
    buf[e - s] = 0;
    char *end;
    double v = strtod(buf, &end);
    return *end || !isfinite(v) ? 0.0 : v;     // 1e999 is not a usable float
}

int64_t pn_jget_bool(Str *json, Str *key) {
    const char *s, *e;
    return jfind(json, key, &s, &e) && e - s == 4 && memcmp(s, "true", 4) == 0;
}

// ============================================================================ printing

void pn_print_int(int64_t v) { printf("%lld", (long long)v); }
void pn_print_str(Str *s) { fwrite(s->data, 1, (size_t)s->len, stdout); }
void pn_print_float(double v) {
    char buf[48];
    fmt_float(buf, sizeof buf, v);
    fputs(buf, stdout);
}

// ============================================================================ hub (messages between requests)
// Requests share nothing, so they talk through the runtime's Hub of named topics (concurrency.md C9). A
// message is copied in once (refcounted, immutable) and handed to every subscriber; no program memory is
// shared. Each topic keeps its last HUB_KEEP messages with increasing ids, so a reader that reconnects can
// catch up (hub_resume). A subscriber whose queue fills up is dropped instead of using memory without limit.

#define HUB_KEEP 100
#define HUB_QUEUE 100

typedef struct Msg { int64_t refs, id, len; char data[]; } Msg;

typedef struct Sub {
    struct Topic *topic;
    struct Sub *prev, *next;            // the topic's subscribers (hub_mu)
    Sched *owner;                       // the thread whose task reads it
    Msg *q[HUB_QUEUE]; int qh, qn;      // delivered, not read yet (hub_mu)
    int overflow;
    Task *waiter;                       // a task in hub_next() (owner thread only)
    int in_inbox; struct Sub *next_wake;   // queued on owner->inbox (owner->mu)
    int64_t last_id;
    void *res;                          // the request resource that unsubscribes it on a panic
} Sub;

typedef struct Topic {
    struct Topic *next;
    Sub *subs;
    Msg *ring[HUB_KEEP]; int rh, rn;    // the last messages, for hub_resume
    int64_t next_id;
    size_t nlen;
    char name[];
} Topic;

static pthread_mutex_t hub_mu = PTHREAD_MUTEX_INITIALIZER;
static Topic *hub_topics[1024];
static int64_t hub_nsubs;               // subscriptions open in the process (hub_mu)

static void msg_unref(Msg *m) { if (__atomic_sub_fetch(&m->refs, 1, __ATOMIC_ACQ_REL) == 0) free(m); }

static Topic *topic_get(Str *name) {    // hub_mu held
    uint64_t h = hash_bytes(name->data, name->len) & 1023;
    for (Topic *t = hub_topics[h]; t; t = t->next)
        if (t->nlen == (size_t)name->len && memcmp(t->name, name->data, t->nlen) == 0) return t;
    Topic *t = calloc(1, sizeof(Topic) + (size_t)name->len);
    if (!t) oom();
    t->nlen = (size_t)name->len;
    memcpy(t->name, name->data, t->nlen);
    t->next = hub_topics[h]; hub_topics[h] = t;
    return t;
}

static void sub_deliver(Sub *s, Msg *m) {       // hub_mu held
    if (s->qn == HUB_QUEUE) { s->overflow = 1; }
    else {
        __atomic_add_fetch(&m->refs, 1, __ATOMIC_RELAXED);
        s->q[(s->qh + s->qn++) % HUB_QUEUE] = m;
    }
    if (s->owner == my_sched) {                 // same thread: wake its reader directly
        if (s->waiter) { Task *w = s->waiter; hub_forget_waiter(w); heap_remove(w); make_ready(w); }
        return;
    }
    pthread_mutex_lock(&s->owner->mu);
    if (!s->in_inbox) { s->in_inbox = 1; s->next_wake = s->owner->inbox; s->owner->inbox = s; }
    pthread_mutex_unlock(&s->owner->mu);
    ssize_t w = write(s->owner->wr, "!", 1);    // a full pipe already means "news": fine to drop
    (void)w;
}

// This thread was told there is news: wake the readers of those subscriptions.
static void hub_drain(void) {
    Sched *sc = sched_self();
    char buf[256];
    while (read(sc->rd, buf, sizeof buf) > 0) {}
    pthread_mutex_lock(&sc->mu);
    Sub *list = sc->inbox;
    sc->inbox = NULL;
    for (Sub *x = list; x; x = x->next_wake) x->in_inbox = 0;
    pthread_mutex_unlock(&sc->mu);
    for (Sub *x = list, *nx; x; x = nx) {
        nx = x->next_wake;
        if (x->waiter) { Task *w = x->waiter; hub_forget_waiter(w); heap_remove(w); make_ready(w); }
    }
}

static int hub_has_subscribers(void) {
    pthread_mutex_lock(&hub_mu);
    int n = hub_nsubs > 0;
    pthread_mutex_unlock(&hub_mu);
    return n;
}

static void hub_forget_waiter(Task *t) {
    if (t->waiting_sub) { t->waiting_sub->waiter = NULL; t->waiting_sub = NULL; }
}

void pn_hub_publish(Str *topic, Str *text) {
    Msg *m = malloc(sizeof(Msg) + (size_t)text->len);
    if (!m) oom();
    m->refs = 1; m->len = text->len;
    memcpy(m->data, text->data, (size_t)text->len);
    pthread_mutex_lock(&hub_mu);
    Topic *t = topic_get(topic);
    m->id = ++t->next_id;
    if (t->rn == HUB_KEEP) { msg_unref(t->ring[t->rh]); t->rh = (t->rh + 1) % HUB_KEEP; t->rn--; }
    t->ring[(t->rh + t->rn++) % HUB_KEEP] = m;  // the topic's reference
    for (Sub *x = t->subs; x; x = x->next) sub_deliver(x, m);
    pthread_mutex_unlock(&hub_mu);
}

void pn_hub_unsubscribe(Sub *s);
static void sub_close(void *s) { ((Sub *)s)->res = NULL; pn_hub_unsubscribe(s); }

static Sub *subscribe(Str *topic, int64_t after) {
    Sub *s = calloc(1, sizeof *s);
    if (!s) oom();
    s->owner = sched_self();
    pthread_mutex_lock(&hub_mu);
    Topic *t = topic_get(topic);
    s->topic = t;
    s->next = t->subs; if (t->subs) t->subs->prev = s; t->subs = s;
    hub_nsubs++;
    s->last_id = after >= 0 ? after : t->next_id;
    if (after >= 0)                             // catch up on what was published after `after`
        for (int i = 0; i < t->rn; i++) {
            Msg *m = t->ring[(t->rh + i) % HUB_KEEP];
            if (m->id > after && s->qn < HUB_QUEUE) {
                __atomic_add_fetch(&m->refs, 1, __ATOMIC_RELAXED);
                s->q[(s->qh + s->qn++) % HUB_QUEUE] = m;
            }
        }
    pthread_mutex_unlock(&hub_mu);
    s->res = pn_res_add(sub_close, s);          // a panicking request unsubscribes too
    return s;
}

Sub *pn_hub_subscribe(Str *topic) { return subscribe(topic, -1); }
Sub *pn_hub_resume(Str *topic, int64_t last_id) { return subscribe(topic, last_id < 0 ? 0 : last_id); }
int64_t pn_hub_last_id(Sub *s) { return s->last_id; }

void pn_hub_unsubscribe(Sub *s) {
    if (!s) return;
    if (s->res) pn_res_remove(s->res);
    if (s->waiter) hub_forget_waiter(s->waiter);
    pthread_mutex_lock(&s->owner->mu);          // off this thread's news list
    for (Sub **pp = &s->owner->inbox; *pp; pp = &(*pp)->next_wake)
        if (*pp == s) { *pp = s->next_wake; break; }
    pthread_mutex_unlock(&s->owner->mu);
    pthread_mutex_lock(&hub_mu);
    if (s->prev) s->prev->next = s->next; else s->topic->subs = s->next;
    if (s->next) s->next->prev = s->prev;
    hub_nsubs--;
    while (s->qn) { msg_unref(s->q[s->qh]); s->qh = (s->qh + 1) % HUB_QUEUE; s->qn--; }
    pthread_mutex_unlock(&hub_mu);
    free(s);
}

// An Error object the compiled code can free: { int64 code; Str *message } made with xmalloc.
typedef struct { int64_t code; Str *message; } PinError;
static PinError *make_error(int64_t code, const char *msg) {
    PinError *e = xmalloc(sizeof *e);
    e->code = code;
    e->message = str_from(msg, (int64_t)strlen(msg));
    return e;
}

typedef struct { Str *value; PinError *err; } StrOrError;

// hub_next($sub, $ms): the next message, waiting at most $ms (504 when none came; 507 when this reader was
// too slow and messages were dropped; 499 when the request was cancelled).
StrOrError pn_hub_next(Sub *s, int64_t ms) {
    StrOrError r = {NULL, NULL};
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    for (;;) {
        pthread_mutex_lock(&hub_mu);
        if (s->qn) {
            Msg *m = s->q[s->qh];
            s->qh = (s->qh + 1) % HUB_QUEUE; s->qn--;
            pthread_mutex_unlock(&hub_mu);
            r.value = str_from(m->data, m->len);
            s->last_id = m->id;
            msg_unref(m);
            return r;
        }
        int over = s->overflow;
        pthread_mutex_unlock(&hub_mu);
        if (over) { r.err = make_error(507, "hub_next(): this reader fell more than 100 messages behind; messages were dropped"); return r; }
        if (cancelled_now()) { r.err = make_error(499, "hub_next(): cancelled"); return r; }
        if (!cur_task || now_ms() >= deadline) { r.err = make_error(504, "hub_next(): no message in time"); return r; }
        s->waiter = cur_task;
        cur_task->waiting_sub = s;
        cur_task->wake_at = deadline;
        heap_push(cur_task);
        park();
        heap_remove(cur_task);
        hub_forget_waiter(cur_task);
    }
}

PinError *pn_timeout_error(void) { return make_error(504, "await_timeout(): the task did not finish in time"); }

// await_timeout($t, $ms): like await, but after $ms the task is cancelled (its waits end) and NULL comes
// back once it has stopped; what it produced is freed with free_res / free_err.
void *pn_await_timeout(Task *t, int64_t ms, void (*free_res)(void *), void (*free_err)(void *)) {
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    while (t->state != T_DONE && now_ms() < deadline && !cancelled_now()) {
        t->waiter = cur_task;
        cur_task->awaiting = t;
        cur_task->wake_at = deadline;
        heap_push(cur_task);
        park();
        heap_remove(cur_task);
        if (t->waiter == cur_task) t->waiter = NULL;
        cur_task->awaiting = NULL;
    }
    if (t->state == T_DONE) return pn_await(t);
    task_cancel(t);                             // its waits end now; wait for it to stop
    void **f = pn_await(t);
    if (f[1]) free_err(f[1]);
    else if (free_res) free_res(f[0]);
    xfree(f);
    return NULL;
}

// ============================================================================ http server

typedef Str *(*Handler)(Str *method, Str *path, Str *body);
static Handler pn_user_handler;   // set by pn_serve(); the compiler passes the program's handler

void pn_http_status(int64_t s) { if (cur_req) cur_req->status = s; }

typedef struct Conn {
    int kind;               // 0 (an FdWait has 1 here: events tell them apart)
    int fd, close_after, want_out, dead;
    char *in; size_t in_len, in_cap;
    char *out; size_t out_len, out_off, out_cap;
    int64_t req_start;      // ms when the request being received began (0 = none pending)
    int64_t out_start;      // ms since a response has been waiting for the client to read it (0 = none)
    int64_t last_active;    // ms of the last completed request (for the keep-alive idle limit)
    struct Conn *prev, *next;   // the worker's open connections, scanned for timeouts
    struct Task *busy;      // the request being handled (its task may be waiting); one at a time per connection
    int orphan;             // closed while busy: freed when the request ends
    int parsing;            // inside handle_input (a request that ends right away is answered there)
    struct Conn *next_done; // requests that ended later, waiting to be answered by the worker loop
    int streaming;          // sse_start() was called: the handler writes the body itself, then the connection closes
    int ep;                 // the worker's poll set (sse_send needs it when the client reads slowly)
} Conn;

static __thread Conn *done_conns;

// ---------------------------------------------------------------- limits (slow clients cannot hold the server)
// A request must arrive completely within PN_READ_TIMEOUT_MS of its first byte (a new connection counts as a
// request that has started: sending nothing is not a way to hold a connection), an idle keep-alive connection
// is closed after PN_IDLE_TIMEOUT_MS, a response the client does not read after PN_WRITE_TIMEOUT_MS, and at
// most PN_MAX_CONNS connections are open at once. A handler that is still working (waiting) is not timed out.
static int64_t read_timeout_ms = 15000, idle_timeout_ms = 60000, write_timeout_ms = 30000, max_conns = 10000;
static int64_t open_conns;          // all workers (atomic)

static int64_t env_ms(const char *name, int64_t dflt) {
    const char *v = getenv(name);
    if (!v || !*v) return dflt;
    char *end;
    long long n = strtoll(v, &end, 10);
    if (*end || n <= 0) { fprintf(stderr, "serve: %s must be a positive number\n", name); exit(1); }
    return (int64_t)n;
}

// ---------------------------------------------------------------- request and response headers
// The handler reads its request's headers and adds response headers through its Req (cur_req follows the
// running task); nothing is shared between requests or threads.

// The value of request header `name` (case-insensitive), trimmed; NULL if absent.
static Str *find_header(Str *name) {
    if (!cur_req || !cur_req->hdr) return NULL;
    const char *p = cur_req->hdr, *end = cur_req->hdr + cur_req->hdr_len;
    while (p < end) {
        const char *e = memmem(p, (size_t)(end - p), "\r\n", 2);
        if (!e) e = end;
        const char *colon = memchr(p, ':', (size_t)(e - p));
        if (colon && colon - p == name->len && strncasecmp(p, name->data, (size_t)name->len) == 0) {
            const char *v = colon + 1, *ve = e;
            while (v < ve && (*v == ' ' || *v == '\t')) v++;
            while (ve > v && (ve[-1] == ' ' || ve[-1] == '\t')) ve--;
            return str_from(v, ve - v);
        }
        p = e + 2;
    }
    return NULL;
}

Str *pn_request_header(Str *name) {
    Str *v = find_header(name);
    return v ? v : str_alloc(0);
}

int64_t pn_request_has_header(Str *name) {
    Str *v = find_header(name);
    if (!v) return 0;
    pn_str_free(v);
    return 1;
}

static int is_token_char(unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || strchr("!#$%&'*+-.^_`|~", c);
}

// Add a response header. A name or value that could break the header block (CR, LF and other control bytes:
// "header injection") panics, as do the headers the server writes itself.
void pn_response_header(Str *name, Str *value) {
    Req *r = cur_req;
    if (!r || !r->root) panic_msg("response_header() only works inside a serve() handler");
    if (name->len == 0) panic_msg("response_header(): empty header name");
    for (int64_t i = 0; i < name->len; i++)
        if (!is_token_char((unsigned char)name->data[i])) panic_msg("response_header(): invalid character in the header name");
    for (int64_t i = 0; i < value->len; i++) {
        unsigned char c = (unsigned char)value->data[i];
        if ((c < 0x20 && c != '\t') || c == 0x7f)
            panic_msg("response_header(): the value contains a control character (CR/LF would let it inject headers)");
    }
    static const char *const own[] = {"content-length", "connection", "transfer-encoding", "date", NULL};
    for (int i = 0; own[i]; i++)
        if ((size_t)name->len == strlen(own[i]) && strncasecmp(name->data, own[i], (size_t)name->len) == 0)
            panic_msg("response_header(): Content-Length, Connection, Transfer-Encoding and Date are set by the server");
    if (name->len == 12 && strncasecmp(name->data, "content-type", 12) == 0) r->resp_has_ctype = 1;
    size_t need = r->resp_hdr_len + (size_t)name->len + (size_t)value->len + 4;
    if (need > (64u << 10)) panic_msg("response_header(): more than 64 KB of response headers");
    if (need > r->resp_hdr_cap) {
        r->resp_hdr_cap = need * 2;
        char *p = realloc(r->resp_hdr, r->resp_hdr_cap);
        if (!p) oom();
        r->resp_hdr = p;
    }
    char *d = r->resp_hdr + r->resp_hdr_len;
    memcpy(d, name->data, (size_t)name->len); d += name->len;
    memcpy(d, ": ", 2); d += 2;
    memcpy(d, value->data, (size_t)value->len); d += value->len;
    memcpy(d, "\r\n", 2);
    r->resp_hdr_len = need;
}

static const char *reason(int64_t s) {
    switch (s) {
    case 200: return "OK"; case 201: return "Created"; case 202: return "Accepted"; case 204: return "No Content";
    case 301: return "Moved Permanently"; case 302: return "Found"; case 303: return "See Other";
    case 304: return "Not Modified"; case 307: return "Temporary Redirect"; case 308: return "Permanent Redirect";
    case 400: return "Bad Request"; case 401: return "Unauthorized"; case 402: return "Payment Required";
    case 403: return "Forbidden"; case 404: return "Not Found"; case 405: return "Method Not Allowed";
    case 406: return "Not Acceptable"; case 408: return "Request Timeout"; case 409: return "Conflict";
    case 410: return "Gone"; case 411: return "Length Required"; case 412: return "Precondition Failed";
    case 413: return "Content Too Large"; case 414: return "URI Too Long"; case 415: return "Unsupported Media Type";
    case 418: return "I'm a teapot"; case 422: return "Unprocessable Content"; case 423: return "Locked";
    case 425: return "Too Early"; case 426: return "Upgrade Required"; case 428: return "Precondition Required";
    case 429: return "Too Many Requests"; case 431: return "Request Header Fields Too Large";
    case 451: return "Unavailable For Legal Reasons";
    case 500: return "Internal Server Error"; case 501: return "Not Implemented"; case 502: return "Bad Gateway";
    case 503: return "Service Unavailable"; case 504: return "Gateway Timeout";
    default: return "Status";              // unusual codes: the number is what clients read
    }
}

// ---------------------------------------------------------------- errors (throws / try)

// An error that leaves the top level: print it and stop (error-handling.md E-R6).
__attribute__((noreturn)) void pn_uncaught(PinError *e) {
    fflush(stdout);
    fprintf(stderr, "error: %.*s (code %lld)\n", (int)e->message->len, e->message->data, (long long)e->code);
    exit(1);
}

// An error that leaves a handler: status from its code (400-599, else 500), the standard status text as the
// body, and the message only in the log — internal details never reach the client (E3).
Str *pn_handler_error(PinError *e) {
    int64_t s = e->code >= 400 && e->code <= 599 ? e->code : 500;
    cur_req->status = s;
    cur_req->resp_hdr_len = 0; cur_req->resp_has_ctype = 0;   // like a panic: none of the handler's headers
    fprintf(stderr, "error %lld: %.*s\n", (long long)e->code, (int)e->message->len, e->message->data);
    char buf[96];
    int n = snprintf(buf, sizeof buf, "{\"error\":\"%s\"}", reason(s));
    pn_str_free(e->message);
    xfree(e);
    return str_from(buf, n);
}

static void out_add(Conn *c, const char *p, size_t n) {
    if (c->out_len + n > c->out_cap) {
        c->out_cap = (c->out_len + n) * 2;
        char *p = realloc(c->out, c->out_cap);
        if (!p) oom();
        c->out = p;
    }
    memcpy(c->out + c->out_len, p, n);
    c->out_len += n;
}

static int ieq_prefix(const char *p, const char *end, const char *name) {
    size_t n = strlen(name);
    if ((size_t)(end - p) < n) return 0;
    return strncasecmp(p, name, n) == 0;
}

static const char internal_error_body[] = "{\"error\":\"Internal Server Error\"}";

typedef struct {
    Conn *c;
    Str *method, *path, *body, *resp;
    int keep;
    char what[120];         // "GET /path" for the log
} RunArgs;

static void conn_free(Conn *c);

// Runs as the request's task. A panic in it (or in a task it started) frees everything the request
// allocated and becomes a 500; the panic message (already on stderr) is the only trace, so the client never
// sees internal details.
static void handler_task(void *arg) {
    RunArgs *volatile ra = arg;
    Req *volatile r = cur_req;
    jmp_buf jb;
    if (setjmp(jb)) {
        cur_jmp = NULL;
        req_kill_tasks(r);
        r->resp_hdr_len = 0; r->resp_has_ctype = 0;        // a 500 carries none of the handler's headers
        req_free_all(r);
        fprintf(stderr, "  in request %s -> 500\n", ra->what);
        r->status = 500;
        ra->resp = NULL;
        return;
    }
    cur_jmp = &jb;
    ra->resp = pn_user_handler(ra->method, ra->path, ra->body);
    cur_jmp = NULL;
    if (r->panicked) die();                     // a task it started panicked after it finished waiting
}

// The request's task ended: write the response (unless the client is gone) and free the request.
static void request_done(Task *t) {
    RunArgs *ra = t->ctx;
    Req *r = t->req;
    Conn *c = ra->c;
    if (c->streaming) {
        c->close_after = 1;                     // the handler wrote the body; the end of the stream is a close
    } else if (!c->orphan) {
        int64_t st = r->status;
        const char *body = ra->resp ? ra->resp->data : internal_error_body;
        size_t blen = ra->resp ? (size_t)ra->resp->len : sizeof internal_error_body - 1;
        char head[160];
        int hn = snprintf(head, sizeof head, "HTTP/1.1 %lld %s\r\n%s", (long long)st, reason(st),
                          r->resp_has_ctype ? "" : "Content-Type: application/json\r\n");
        out_add(c, head, (size_t)hn);
        if (r->resp_hdr_len) out_add(c, r->resp_hdr, r->resp_hdr_len);
        hn = snprintf(head, sizeof head, "Content-Length: %lld\r\n%s\r\n", (long long)blen,
                      ra->keep ? "" : "Connection: close\r\n");
        out_add(c, head, (size_t)hn);
        out_add(c, body, blen);
        if (!ra->keep) c->close_after = 1;
    }
    c->busy = NULL;
    task_unlink_req(t);
    req_free_all(r);                            // the response body, method, path, body and anything left over
    free(r->hdr); free(r->resp_hdr); free(r);
    t->req = NULL;
    free(ra);
    if (c->orphan) conn_free(c);
    else if (!c->parsing) { c->next_done = done_conns; done_conns = c; }   // answered by the worker loop
}

// Start the handler for one complete request; it may finish now or wait (then the connection waits too).
static void start_request(Conn *c, const char *line, const char *sp1, const char *sp2, const char *hdr,
                          const char *hdr_end, const char *body, int64_t blen, int keep) {
    Req *r = req_new();
    r->hdr_len = hdr_end > hdr ? (size_t)(hdr_end - hdr) : 0;     // a request may have no header lines at all
    r->hdr = malloc(r->hdr_len + 1);
    if (!r->hdr) oom();
    memcpy(r->hdr, hdr, r->hdr_len);            // the connection's buffer moves on while the handler waits
    RunArgs *ra = calloc(1, sizeof *ra);
    if (!ra) oom();
    ra->c = c; ra->keep = keep;
    snprintf(ra->what, sizeof ra->what, "%.*s", (int)(sp2 - line), line);
    cur_req = r;
    ra->method = str_from(line, sp1 - line);
    ra->path = str_from(sp1 + 1, sp2 - sp1 - 1);
    ra->body = str_from(body, blen);
    Task *t = task_spawn(handler_task, ra, TASK_STACK);
    cur_req = NULL;
    r->root = t;
    r->conn = c;
    t->ctx = ra;
    t->on_done = request_done;
    t->auto_release = 1;
    c->busy = t;
    run_ready();                                // run it now: most handlers finish without waiting
}

// Answer with an error status and close: for requests the server will not pass to the handler.
static void refuse(Conn *c, int status) {
    char msg[200];
    int n = snprintf(msg, sizeof msg,
                     "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     "{\"error\":\"%s\"}", status, reason(status), (int)strlen(reason(status)) + 12, reason(status));
    out_add(c, msg, (size_t)n);
    c->close_after = 1;
    c->in_len = 0;
}

// Parse and answer the complete requests in the input buffer, one at a time. -1 = close the connection.
static int handle_input(Conn *c) {
    size_t off = 0;
    c->parsing = 1;
    while (!c->close_after && !c->busy) {
        char *buf = c->in + off;
        size_t avail = c->in_len - off;
        char *hend = memmem(buf, avail, "\r\n\r\n", 4);
        if (!hend) {
            if (avail > 65536) { refuse(c, 431); c->parsing = 0; return 0; }
            break;
        }
        if (hend - buf > 65536) { refuse(c, 431); c->parsing = 0; return 0; }   // also when it arrived in one read
        char *line_end = memmem(buf, (size_t)(hend - buf) + 2, "\r\n", 2);
        char *sp1 = memchr(buf, ' ', (size_t)(line_end - buf));
        char *sp2 = sp1 ? memchr(sp1 + 1, ' ', (size_t)(line_end - sp1 - 1)) : NULL;
        if (!sp2) { refuse(c, 400); c->parsing = 0; return 0; }
        int http10 = (line_end - sp2 - 1 == 8) && memcmp(sp2 + 1, "HTTP/1.0", 8) == 0;
        int keep = !http10;
        int64_t clen = 0;
        int seen_len = 0, bad = 0;
        for (char *p = line_end + 2; p < hend && !bad;) {
            char *e = memmem(p, (size_t)(hend + 2 - p), "\r\n", 2);
            if (ieq_prefix(p, e, "content-length:")) {
                // only digits, and one value: anything else is how request smuggling starts (RFC 9112 6.3)
                char *v = p + 15, *ve = e;
                while (v < ve && (*v == ' ' || *v == '\t')) v++;
                while (ve > v && (ve[-1] == ' ' || ve[-1] == '\t')) ve--;
                int64_t n = 0;
                if (v == ve || ve - v > 12) bad = 400;
                for (char *d = v; d < ve && !bad; d++) {
                    if (*d < '0' || *d > '9') bad = 400;
                    else n = n * 10 + (*d - '0');
                }
                if (!bad && seen_len && n != clen) bad = 400;
                clen = n; seen_len = 1;
            } else if (ieq_prefix(p, e, "transfer-encoding:")) {
                bad = 501;                            // chunked bodies are not supported: refuse, never guess
            } else if (ieq_prefix(p, e, "connection:")) {
                char *v = p + 11; while (*v == ' ') v++;
                if (ieq_prefix(v, e, "close")) keep = 0;
                else if (ieq_prefix(v, e, "keep-alive")) keep = 1;
            }
            p = e + 2;
        }
        if (!bad && clen > (64 << 20)) bad = 413;
        if (bad) { refuse(c, bad); c->parsing = 0; return 0; }
        size_t hlen = (size_t)(hend - buf) + 4;
        if (avail < hlen + (size_t)clen) break;      // body not complete yet
        start_request(c, buf, sp1, sp2, line_end + 2, hend, hend + 4, clen, keep);
        off += hlen + (size_t)clen;
    }
    if (off) { memmove(c->in, c->in + off, c->in_len - off); c->in_len -= off; }
    c->parsing = 0;
    return 0;
}

// 1 = everything written, 0 = would block, -1 = error
static int flush_out(Conn *c) {
    while (c->out_off < c->out_len) {
        ssize_t n = write(c->fd, c->out + c->out_off, c->out_len - c->out_off);
        if (n > 0) { c->out_off += (size_t)n; continue; }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
        return -1;
    }
    c->out_off = c->out_len = 0;
    return 1;
}

// ---------------------------------------------------------------- server-sent events (a stream from a handler)
static void poll_want_out(int p, Conn *c, int on);

// The connection of the running handler, or NULL with *err set.
static Conn *sse_conn(PinError **err) {
    Conn *c = cur_req ? cur_req->conn : NULL;
    if (!c) panic_msg("sse_*() only works inside a serve() handler");
    if (c->orphan || cur_req->cancelled) { *err = make_error(499, "the client closed the stream"); return NULL; }
    return c;
}

static PinError *sse_flush(Conn *c) {
    int f = flush_out(c);
    if (f < 0) { cur_req->cancelled = 1; return make_error(499, "the client closed the stream"); }
    if (f == 0) {
        if (!c->want_out) { c->want_out = 1; poll_want_out(c->ep, c, 1); }
        if (!c->out_start) c->out_start = now_ms();
        if (c->out_len > ((size_t)1 << 20)) return make_error(503, "the client reads the stream too slowly");
    } else c->out_start = 0;
    return NULL;
}

void *pn_sse_start(void) {
    PinError *err = NULL;
    Conn *c = sse_conn(&err);
    if (!c) return err;
    if (c->streaming) panic_msg("sse_start(): the stream has already started");
    char head[256];
    int n = snprintf(head, sizeof head, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     "X-Accel-Buffering: no\r\n");
    out_add(c, head, (size_t)n);
    if (cur_req->resp_hdr_len) out_add(c, cur_req->resp_hdr, cur_req->resp_hdr_len);
    out_add(c, "Connection: close\r\n\r\n", 21);
    c->streaming = 1;
    return sse_flush(c);
}

static void *sse_write(int64_t id, Str *data, int comment) {
    PinError *err = NULL;
    Conn *c = sse_conn(&err);
    if (!c) return err;
    if (!c->streaming) panic_msg("call sse_start() before sending events");
    char buf[32];
    if (id >= 0) { int n = snprintf(buf, sizeof buf, "id: %lld\n", (long long)id); out_add(c, buf, (size_t)n); }
    const char *p = data->data, *e = data->data + data->len;
    do {                                        // one "data:" line per line of the text (SSE framing)
        const char *nl = memchr(p, '\n', (size_t)(e - p));
        const char *end = nl ? nl : e;
        if (end > p && end[-1] == '\r') end--;
        out_add(c, comment ? ": " : "data: ", comment ? 2 : 6);
        out_add(c, p, (size_t)(end - p));
        out_add(c, "\n", 1);
        p = nl ? nl + 1 : e;
    } while (p < e);
    out_add(c, "\n", 1);
    return sse_flush(c);
}

void *pn_sse_send(Str *data) { return sse_write(-1, data, 0); }
void *pn_sse_send_id(int64_t id, Str *data) { return sse_write(id < 0 ? 0 : id, data, 0); }
void *pn_sse_comment(Str *text) { return sse_write(-1, text, 1); }

// ---------------------------------------------------------------- event polling
// One small interface over epoll (Linux) and kqueue (macOS). An event with c == NULL is the listener.

typedef struct { Conn *c; int in, out, err; } PollEv;

#ifdef __APPLE__
static int poll_new(void) { return kqueue(); }

static void kq_set(int p, int fd, int16_t filter, uint16_t flags, void *data) {
    struct kevent k;
    EV_SET(&k, fd, filter, flags, 0, 0, data);
    kevent(p, &k, 1, NULL, 0, NULL);
}

static void poll_listen(int p, int fd) { kq_set(p, fd, EVFILT_READ, EV_ADD, NULL); }
static void poll_add(int p, Conn *c) { kq_set(p, c->fd, EVFILT_READ, EV_ADD, c); }
static void poll_want_out(int p, Conn *c, int on) { kq_set(p, c->fd, EVFILT_WRITE, on ? EV_ADD : EV_DELETE, c); }
static void poll_del(int p, Conn *c) { (void)p; (void)c; }   // close() drops kqueue registrations

static int poll_wait(int p, PollEv *out, int max, int timeout_ms) {
    struct kevent ks[256];
    struct timespec ts = {timeout_ms / 1000, (long)(timeout_ms % 1000) * 1000000};
    int n = kevent(p, NULL, 0, ks, max < 256 ? max : 256, &ts);
    for (int i = 0; i < n; i++) {
        out[i].c = ks[i].udata;
        out[i].in = ks[i].filter == EVFILT_READ;
        out[i].out = ks[i].filter == EVFILT_WRITE;
        out[i].err = (ks[i].flags & EV_ERROR) != 0;
    }
    return n;
}
#else
static int poll_new(void) { return epoll_create1(0); }

static void poll_listen(int p, int fd) {
    struct epoll_event ev = {.events = EPOLLIN, .data.ptr = NULL};
    epoll_ctl(p, EPOLL_CTL_ADD, fd, &ev);
}

static void poll_add(int p, Conn *c) {
    struct epoll_event ev = {.events = EPOLLIN | EPOLLRDHUP, .data.ptr = c};
    epoll_ctl(p, EPOLL_CTL_ADD, c->fd, &ev);
}

static void poll_want_out(int p, Conn *c, int on) {
    struct epoll_event ev = {.events = EPOLLIN | EPOLLRDHUP | (on ? EPOLLOUT : 0), .data.ptr = c};
    epoll_ctl(p, EPOLL_CTL_MOD, c->fd, &ev);
}

static void poll_del(int p, Conn *c) { epoll_ctl(p, EPOLL_CTL_DEL, c->fd, NULL); }

static int poll_wait(int p, PollEv *out, int max, int timeout_ms) {
    struct epoll_event evs[256];
    int n = epoll_wait(p, evs, max < 256 ? max : 256, timeout_ms);
    for (int i = 0; i < n; i++) {
        uint32_t e = evs[i].events;
        out[i].c = evs[i].data.ptr;
        out[i].in = (e & (EPOLLIN | EPOLLHUP | EPOLLRDHUP)) != 0;
        out[i].out = (e & EPOLLOUT) != 0;
        out[i].err = (e & EPOLLERR) != 0;
    }
    return n;
}
#endif

#ifdef __APPLE__
static void ep_arm(FdWait *w) { kq_set(my_ep, w->fd, w->write ? EVFILT_WRITE : EVFILT_READ, EV_ADD | EV_ONESHOT, w); }
static void ep_disarm(FdWait *w) { if (!w->fired) kq_set(my_ep, w->fd, w->write ? EVFILT_WRITE : EVFILT_READ, EV_DELETE, w); }
#else
static void ep_arm(FdWait *w) {
    struct epoll_event ev = {.events = (w->write ? EPOLLOUT : EPOLLIN | EPOLLRDHUP) | EPOLLONESHOT, .data.ptr = w};
    epoll_ctl(my_ep, EPOLL_CTL_ADD, w->fd, &ev);
}
static void ep_disarm(FdWait *w) { epoll_ctl(my_ep, EPOLL_CTL_DEL, w->fd, NULL); }
#endif

static int set_nonblock(int fd) { return fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK); }

static int accept_conn(int lfd) {
#ifdef __APPLE__
    int fd = accept(lfd, NULL, NULL);
    if (fd >= 0) {
        int one = 1;
        set_nonblock(fd);
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
    }
    return fd;
#else
    return accept4(lfd, NULL, NULL, SOCK_NONBLOCK);
#endif
}

static int listen_port;
static int shared_listener = -1;   // macOS: SO_REUSEPORT does not spread connections, so threads share one

static int open_listener(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    if (fd < 0) { perror("serve: socket"); exit(1); }
    set_nonblock(fd);
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
#ifndef __APPLE__
    setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof one);
#endif
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET; a.sin_port = htons((uint16_t)port); a.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0 || listen(fd, 4096) < 0) {
        perror("serve: cannot listen");
        exit(1);
    }
    return fd;
}

// Best effort: tell a client why it is being dropped (the socket is non-blocking; a full buffer loses it).
static void say_and_close(int fd, int status) {
    char msg[200];
    int n = snprintf(msg, sizeof msg,
                     "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     "{\"error\":\"%s\"}", status, reason(status), (int)strlen(reason(status)) + 12, reason(status));
    ssize_t w = write(fd, msg, (size_t)n);
    (void)w;
    close(fd);
}

static void conn_free(Conn *c) {
    free(c->in); free(c->out); free(c);
    __atomic_sub_fetch(&open_conns, 1, __ATOMIC_RELAXED);
}

// Close a connection. If its request is still being handled, the Conn stays until the request ends.
static void conn_close(int ep, Conn *c, int status) {
    c->dead = 1;
    poll_del(ep, c);
    c->prev->next = c->next; c->next->prev = c->prev;
    if (status) say_and_close(c->fd, status); else close(c->fd);
    if (c->busy) {
        c->orphan = 1;
        Req *r = c->busy->req;
        if (r) { r->cancelled = 1; for (Task *t = r->tasks; t; t = t->req_next) wake_waiting(t); }
    } else conn_free(c);
}

static void conn_times(Conn *c, int64_t now);

// After input arrived or a request ended: answer what is complete, write what is queued, close if done.
static void conn_progress(int ep, Conn *c, int64_t now) {
    int dead = 0;
    if (!c->busy && c->in_len && handle_input(c) < 0) dead = 1;
    if (!dead && c->out_len) {
        int f = flush_out(c);
        if (f < 0) dead = 1;
        else if (f == 0 && !c->want_out) { c->want_out = 1; poll_want_out(ep, c, 1); }
        else if (f == 1 && c->want_out) { c->want_out = 0; poll_want_out(ep, c, 0); }
    }
    if (!dead && c->close_after && c->out_len == 0 && !c->busy) dead = 1;
    if (dead) conn_close(ep, c, 0);
    else conn_times(c, now);
}

// Keep the timers of one connection current after something happened on it.
static void conn_times(Conn *c, int64_t now) {
    if (c->busy) { c->req_start = 0; c->last_active = now; return; }   // the handler is working: no limit
    if (c->out_len) { if (!c->out_start) c->out_start = now; }
    else c->out_start = 0;
    if (c->in_len) { if (!c->req_start) c->req_start = now; }   // a request has begun; its clock does not restart
    else if (c->req_start) { c->req_start = 0; c->last_active = now; }
}

static void *worker(void *arg) {
    int lfd = shared_listener >= 0 ? shared_listener : open_listener(listen_port);
    int ep = poll_new();
    my_ep = ep;
    poll_listen(ep, lfd);
    Conn *wake = calloc(1, sizeof(Conn));       // events for this thread's Hub wake pipe carry this
    if (!wake) oom();
    wake->fd = sched_self()->rd;
    poll_add(ep, wake);
    PollEv evs[256];
    Conn conns = {0};                                     // circular list of this worker's open connections
    conns.prev = conns.next = &conns;
    int64_t last_scan = now_ms();
    (void)arg;
    for (;;) {
        run_ready();
        int64_t now = now_ms();
        while (done_conns) {                                  // requests that ended after waiting: answer now
            Conn *c = done_conns;
            done_conns = c->next_done;
            if (!c->dead) conn_progress(ep, c, now);
        }
        int64_t wait = timers_fire(now);
        if (ready_head) continue;
        int n = poll_wait(ep, evs, 256, wait < 0 || wait > 1000 ? 1000 : (int)wait);
        now = now_ms();
        timers_fire(now);
        for (int i = 0; i < n; i++) {
            Conn *c = evs[i].c;
            if (c == wake) { hub_drain(); continue; }
            if (c && c->kind == 1) { fd_fired((FdWait *)c); continue; }   // a task's socket or pipe
            if (!c) {                                         // new connections
                for (;;) {
                    int fd = accept_conn(lfd);
                    if (fd < 0) break;
                    if (__atomic_add_fetch(&open_conns, 1, __ATOMIC_RELAXED) > max_conns) {
                        __atomic_sub_fetch(&open_conns, 1, __ATOMIC_RELAXED);
                        say_and_close(fd, 503);               // too many open: refuse instead of running out
                        continue;
                    }
                    int one = 1;
                    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
                    Conn *nc = calloc(1, sizeof(Conn));
                    if (!nc) oom();
                    nc->fd = fd;
                    nc->ep = ep;
                    nc->in_cap = 4096; nc->in = malloc(nc->in_cap);
                    if (!nc->in) oom();
                    nc->req_start = now;                      // the first request must arrive in time too
                    nc->prev = conns.prev; nc->next = &conns; conns.prev->next = nc; conns.prev = nc;
                    poll_add(ep, nc);
                }
                continue;
            }
            if (c->dead) continue;                            // closed earlier in this batch (kqueue: read + write events)
            int dead = evs[i].err;
            if (!dead && evs[i].in) {
                for (;;) {
                    if (c->in_len == c->in_cap) {
                        if (c->in_cap >= ((size_t)80 << 20)) { dead = 1; break; }   // way past every limit
                        c->in_cap *= 2;
                        char *p = realloc(c->in, c->in_cap);
                        if (!p) oom();
                        c->in = p;
                    }
                    ssize_t r = read(c->fd, c->in + c->in_len, c->in_cap - c->in_len);
                    if (r > 0) { c->in_len += (size_t)r; continue; }
                    if (r == 0) {
                        if (c->streaming) dead = 1;   // a stream's reader left: end the stream
                        else c->close_after = 1;      // client finished sending: still answer it
                        break;
                    }
                    if (errno == EINTR) continue;
                    if (errno != EAGAIN && errno != EWOULDBLOCK) dead = 1;
                    break;
                }
            }
            if (dead) { conn_close(ep, c, 0); continue; }
            conn_progress(ep, c, now);
        }
        if (now - last_scan >= 1000) {                        // once a second: drop clients that are too slow
            last_scan = now;
            for (Conn *c = conns.next, *nx; c != &conns; c = nx) {
                nx = c->next;
                if (c->busy && !(c->streaming && c->out_start && now - c->out_start > write_timeout_ms)) continue;
                int late_out = c->out_start && now - c->out_start > write_timeout_ms;
                int late_in = !late_out && c->req_start && now - c->req_start > read_timeout_ms;
                int idle = !c->req_start && !c->out_len && now - c->last_active > idle_timeout_ms;
                if (!late_out && !late_in && !idle) continue;
                conn_close(ep, c, late_in && !c->out_len ? 408 : 0);   // 408: the request never arrived in full
            }
        }
    }
    return NULL;
}

void pn_serve(int64_t port, Handler handler) {
    if (!handler) panic_msg("serve(): no handler compiled in");
    if (cur_task) {               // called from main's task: from here on this stack runs the scheduler
        cur_task = NULL;
        cur_req = NULL; cur_jmp = NULL;
    }
    pn_user_handler = handler;
    signal(SIGPIPE, SIG_IGN);
    setvbuf(stdout, NULL, _IOLBF, 0);
    listen_port = (int)port;
    read_timeout_ms = env_ms("PN_READ_TIMEOUT_MS", read_timeout_ms);
    idle_timeout_ms = env_ms("PN_IDLE_TIMEOUT_MS", idle_timeout_ms);
    write_timeout_ms = env_ms("PN_WRITE_TIMEOUT_MS", write_timeout_ms);
    max_conns = env_ms("PN_MAX_CONNS", max_conns);
    int threads = 0;
    const char *env = getenv("PN_THREADS");
    if (env) threads = atoi(env);
    if (threads <= 0) {
#ifdef __APPLE__
        threads = (int)sysconf(_SC_NPROCESSORS_ONLN);
#else
        cpu_set_t set;
        threads = sched_getaffinity(0, sizeof set, &set) == 0 ? CPU_COUNT(&set) : 1;
#endif
        if (threads <= 0) threads = 1;
    }
#ifdef __APPLE__
    shared_listener = open_listener(listen_port);
#endif
    fprintf(stderr, "listening on :%lld with %d thread(s)\n", (long long)port, threads);
    for (int i = 1; i < threads; i++) {
        pthread_t t;
        pthread_create(&t, NULL, worker, NULL);
    }
    worker(NULL);
}

// ============================================================================ HTTP client and run()
// Plain http:// only for now (TLS is a later decision). Every wait goes through wait_fds(), so a task
// talking to another server or a program does not hold its thread. Name lookup (getaddrinfo) still blocks.

#include <netdb.h>
#include <spawn.h>
#include <sys/wait.h>
extern char **environ;

typedef struct HConn {
    int fd;
    char *buf; size_t len, cap;         // received, not used yet
    int chunked; int64_t chunk_left;    // body framing
    int64_t body_left;                  // Content-Length still to read (-1: until close)
    int eof;
    void *res;
} HConn;

static void h_free(HConn *h) {
    if (!h) return;
    if (h->res) pn_res_remove(h->res);
    if (h->fd >= 0) close(h->fd);
    free(h->buf); free(h);
}

static void h_close_res(void *h) { ((HConn *)h)->res = NULL; h_free(h); }

// Read more bytes. 1 = got some, 0 = end of stream, -1 = error, -2 = timeout, -3 = cancelled.
static int h_more(HConn *h, int64_t deadline) {
    for (;;) {
        if (h->len == h->cap) {
            if (h->cap >= ((size_t)64 << 20)) return -1;
            h->cap = h->cap ? h->cap * 2 : 16384;
            char *p = realloc(h->buf, h->cap);
            if (!p) oom();
            h->buf = p;
        }
        ssize_t n = read(h->fd, h->buf + h->len, h->cap - h->len);
        if (n > 0) { h->len += (size_t)n; return 1; }
        if (n == 0) { h->eof = 1; return 0; }
        if (errno == EINTR) continue;
        if (errno != EAGAIN && errno != EWOULDBLOCK) return -1;
        int w = wait_fds(&h->fd, 1, 0, deadline);
        if (w == 0) return -2;
        if (w < 0) return -3;
    }
}

static void h_take(HConn *h, size_t n) { memmove(h->buf, h->buf + n, h->len - n); h->len -= n; }

static PinError *h_error(int r, const char *what) {
    char msg[200];
    if (r == -2) { snprintf(msg, sizeof msg, "%s: no answer in time", what); return make_error(504, msg); }
    if (r == -3) { snprintf(msg, sizeof msg, "%s: cancelled", what); return make_error(499, msg); }
    snprintf(msg, sizeof msg, "%s: the connection failed or closed early", what);
    return make_error(502, msg);
}

// http://host[:port]/path -> connected socket, request sent, response headers read. NULL + *err on failure.
static HConn *h_open(const char *method, Str *url, Map *headers, Str *body, const char *accept, int64_t deadline,
                     int *status, PinError **err) {
    char host[256], port[8] = "80", what[300];
    snprintf(what, sizeof what, "%s %.*s", method, (int)(url->len > 200 ? 200 : url->len), url->data);
    const char *u = url->data, *ue = url->data + url->len;
    if (url->len < 7 || strncmp(u, "http://", 7) != 0) {
        *err = make_error(400, strncmp(u, "https://", url->len < 8 ? (size_t)url->len : 8) == 0
                          ? "https:// is not supported yet: use http:// (for example through a local TLS proxy)"
                          : "the URL must start with http://");
        return NULL;
    }
    u += 7;
    const char *slash = memchr(u, '/', (size_t)(ue - u));
    const char *hostend = slash ? slash : ue;
    const char *colon = memchr(u, ':', (size_t)(hostend - u));
    size_t hl = (size_t)((colon ? colon : hostend) - u);
    if (hl == 0 || hl >= sizeof host) { *err = make_error(400, "the URL has no host"); return NULL; }
    memcpy(host, u, hl); host[hl] = 0;
    if (colon) {
        size_t pl = (size_t)(hostend - colon - 1);
        if (pl == 0 || pl >= sizeof port) { *err = make_error(400, "the URL's port is not valid"); return NULL; }
        memcpy(port, colon + 1, pl); port[pl] = 0;
    }
    const char *path = slash ? slash : "/";
    size_t plen = slash ? (size_t)(ue - slash) : 1;
    for (const char *c = u; c < ue; c++)
        if ((unsigned char)*c <= 0x20 || *c == 0x7f) { *err = make_error(400, "the URL contains a space or control character"); return NULL; }

    struct addrinfo hints = {0}, *ai = NULL;
    hints.ai_family = AF_UNSPEC; hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, port, &hints, &ai) != 0 || !ai) {
        char m[300]; snprintf(m, sizeof m, "%s: cannot find host %s", what, host);
        *err = make_error(502, m); return NULL;
    }
    int fd = socket(ai->ai_family, SOCK_STREAM, 0);
    if (fd < 0) { freeaddrinfo(ai); *err = make_error(502, "cannot open a socket"); return NULL; }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
#ifdef __APPLE__
    { int one = 1; setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one); }
#endif
    int rc = connect(fd, ai->ai_addr, ai->ai_addrlen);
    freeaddrinfo(ai);
    HConn *h = calloc(1, sizeof *h);
    if (!h) oom();
    h->fd = fd; h->body_left = -1;
    h->res = pn_res_add(h_close_res, h);        // closed if the request panics
    if (rc < 0 && errno != EINPROGRESS) { h_free(h); *err = h_error(-1, what); return NULL; }
    if (rc < 0) {
        int w = wait_fds(&fd, 1, 1, deadline), soerr = 0;
        socklen_t sl = sizeof soerr;
        if (w <= 0) { h_free(h); *err = h_error(w == 0 ? -2 : -3, what); return NULL; }
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
        if (soerr) { h_free(h); char m[300]; snprintf(m, sizeof m, "%s: cannot connect", what); *err = make_error(502, m); return NULL; }
    }
    // the request
    size_t cap = 512 + plen + (body ? (size_t)body->len : 0), n = 0;
    if (headers) for (int64_t i = 0; i < headers->used; i++) if (headers->e[i].alive)
        cap += (size_t)((Str *)headers->e[i].key)->len + (size_t)((Str *)headers->e[i].val)->len + 4;
    char *req = malloc(cap);
    if (!req) oom();
    n += (size_t)snprintf(req + n, cap - n, "%s %.*s HTTP/1.1\r\nHost: %s%s%s\r\nAccept: %s\r\nConnection: close\r\n",
                          method, (int)plen, path, host, colon ? ":" : "", colon ? port : "", accept);
    if (headers) for (int64_t i = 0; i < headers->used; i++) {
        Entry *e = &headers->e[i];
        if (!e->alive) continue;
        Str *k = (Str *)e->key, *v = (Str *)e->val;
        for (int64_t j = 0; j < k->len; j++) if (!is_token_char((unsigned char)k->data[j])) { free(req); h_free(h); panic_msg("http header name: invalid character"); }
        for (int64_t j = 0; j < v->len; j++) { unsigned char ch = (unsigned char)v->data[j]; if ((ch < 0x20 && ch != '\t') || ch == 0x7f) { free(req); h_free(h); panic_msg("http header value contains a control character (CR/LF would inject headers)"); } }
        memcpy(req + n, k->data, (size_t)k->len); n += (size_t)k->len;
        memcpy(req + n, ": ", 2); n += 2;
        memcpy(req + n, v->data, (size_t)v->len); n += (size_t)v->len;
        memcpy(req + n, "\r\n", 2); n += 2;
    }
    if (body) {
        n += (size_t)snprintf(req + n, cap - n, "Content-Type: application/json\r\nContent-Length: %lld\r\n", (long long)body->len);
    }
    memcpy(req + n, "\r\n", 2); n += 2;
    if (body) { memcpy(req + n, body->data, (size_t)body->len); n += (size_t)body->len; }
    for (size_t off = 0; off < n;) {
        ssize_t w = write(fd, req + off, n - off);
        if (w > 0) { off += (size_t)w; continue; }
        if (w < 0 && errno == EINTR) continue;
        int r = w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) ? wait_fds(&fd, 1, 1, deadline) : -9;
        if (r <= 0) { free(req); h_free(h); *err = h_error(r == 0 ? -2 : r == -1 ? -3 : -1, what); return NULL; }
    }
    free(req);
    // the response head
    char *hend;
    while (!(hend = h->len >= 4 ? memmem(h->buf, h->len, "\r\n\r\n", 4) : NULL)) {
        int r = h_more(h, deadline);
        if (r <= 0 || h->len > 65536) { h_free(h); *err = h_error(r == 1 ? -1 : r == 0 ? -1 : r, what); return NULL; }
    }
    if (h->len < 12 || strncmp(h->buf, "HTTP/1.", 7) != 0) { h_free(h); *err = make_error(502, "not an HTTP response"); return NULL; }
    *status = atoi(h->buf + 9);
    for (char *p = memmem(h->buf, (size_t)(hend - h->buf) + 2, "\r\n", 2) + 2; p < hend;) {
        char *e = memmem(p, (size_t)(hend + 2 - p), "\r\n", 2);
        if (ieq_prefix(p, e, "content-length:")) h->body_left = strtoll(p + 15, NULL, 10);
        else if (ieq_prefix(p, e, "transfer-encoding:")) {
            for (char *q = p + 18; q + 7 <= e; q++) if (strncasecmp(q, "chunked", 7) == 0) h->chunked = 1;
        }
        p = e + 2;
    }
    if (h->chunked) h->body_left = -1;
    h_take(h, (size_t)(hend - h->buf) + 4);
    return h;
}

// Up to `max` body bytes into `out` (chunked framing removed). >0 bytes, 0 = end of body, <0 = error code.
static int64_t h_body(HConn *h, char *out, int64_t max, int64_t deadline) {
    for (;;) {
        if (h->chunked) {
            if (h->chunk_left == 0) {               // next chunk size line
                char *nl;
                while (!(nl = memmem(h->buf, h->len, "\r\n", 2))) { int r = h_more(h, deadline); if (r <= 0) return r == 0 ? -1 : r; }
                int64_t sz = strtoll(h->buf, NULL, 16);
                h_take(h, (size_t)(nl - h->buf) + 2);
                if (sz <= 0) return 0;
                h->chunk_left = sz;
            }
            if (h->len == 0) { int r = h_more(h, deadline); if (r <= 0) return r == 0 ? -1 : r; }
            int64_t k = (int64_t)h->len < h->chunk_left ? (int64_t)h->len : h->chunk_left;
            if (k > max) k = max;
            memcpy(out, h->buf, (size_t)k); h_take(h, (size_t)k);
            h->chunk_left -= k;
            if (h->chunk_left == 0) {               // the CRLF after the chunk
                while (h->len < 2) { int r = h_more(h, deadline); if (r <= 0) return r == 0 ? -1 : r; }
                h_take(h, 2);
                h->chunk_left = 0;
            }
            return k;
        }
        if (h->body_left == 0) return 0;
        if (h->len == 0) {
            int r = h_more(h, deadline);
            if (r == 0) return h->body_left < 0 ? 0 : -1;
            if (r < 0) return r;
        }
        int64_t k = (int64_t)h->len;
        if (h->body_left >= 0 && k > h->body_left) k = h->body_left;
        if (k > max) k = max;
        memcpy(out, h->buf, (size_t)k); h_take(h, (size_t)k);
        if (h->body_left > 0) h->body_left -= k;
        return k;
    }
}

static StrOrError http_call(const char *method, Str *url, Map *headers, Str *body, int64_t ms) {
    StrOrError r = {NULL, NULL};
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    int status = 0;
    HConn *h = h_open(method, url, headers, body, "*/*", deadline, &status, &r.err);
    if (!h) return r;
    Str *out = str_alloc(1024);
    char chunk[16384];
    for (;;) {
        int64_t k = h_body(h, chunk, sizeof chunk, deadline);
        if (k == 0) break;
        if (k < 0) { pn_str_free(out); r.err = h_error((int)k, method); h_free(h); return r; }
        if (out->len + k > ((int64_t)64 << 20)) { pn_str_free(out); r.err = make_error(502, "response larger than 64 MB"); h_free(h); return r; }
        out = str_add_bytes(out, chunk, k);
    }
    h_free(h);
    if (status >= 400) {                        // the body is the message (cut), the status is the code
        char m[400];
        snprintf(m, sizeof m, "%s %.*s answered %d: %.*s", method, (int)(url->len > 150 ? 150 : url->len), url->data,
                 status, (int)(out->len > 200 ? 200 : out->len), out->data);
        pn_str_free(out);
        r.err = make_error(status, m);
        return r;
    }
    r.value = out;
    return r;
}

StrOrError pn_http_get(Str *url, Map *headers, int64_t ms) { return http_call("GET", url, headers, NULL, ms); }
StrOrError pn_http_post(Str *url, Map *headers, Str *body, int64_t ms) { return http_call("POST", url, headers, body, ms); }

// --- reading a Server-Sent Events stream
typedef struct HStream { HConn *h; Str *pending; int64_t last_id; } HStream;
typedef struct { HStream *value; PinError *err; } StreamOrError;

StreamOrError pn_http_stream(Str *url, Map *headers, int64_t last_id, int64_t ms) {
    StreamOrError r = {NULL, NULL};
    int status = 0;
    Map *hd = headers;
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    char idbuf[24];
    HConn *h;
    if (last_id > 0) {                          // ask for what came after it
        Map *m = pn_map_copy(headers);
        Str *k = str_from("Last-Event-ID", 13);
        snprintf(idbuf, sizeof idbuf, "%lld", (long long)last_id);
        Str *v = str_from(idbuf, (int64_t)strlen(idbuf));
        uint64_t *slot = pn_map_slot(m, (uint64_t)k, 1);
        pn_str_free((Str *)*slot);
        *slot = (uint64_t)v;
        pn_str_free(k);
        hd = m;
        h = h_open("GET", url, hd, NULL, "text/event-stream", deadline, &status, &r.err);
        pn_map_free(m);
    } else h = h_open("GET", url, hd, NULL, "text/event-stream", deadline, &status, &r.err);
    if (!h) return r;
    if (status != 200) {
        char m[200]; snprintf(m, sizeof m, "the stream answered %d", status);
        h_free(h); r.err = make_error(status >= 400 ? status : 502, m); return r;
    }
    HStream *st = xcalloc(sizeof *st);
    st->h = h; st->last_id = last_id; st->pending = str_alloc(256);
    r.value = st;
    return r;
}

void pn_stream_close(HStream *st) {
    if (!st) return;
    h_free(st->h);
    pn_str_free(st->pending);
    xfree(st);
}

int64_t pn_stream_last_id(HStream *st) { return st->last_id; }

// The data of the next event (comments skipped); 504 when none comes in time, 502 when the stream ends.
StrOrError pn_stream_next(HStream *st, int64_t ms) {
    StrOrError r = {NULL, NULL};
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    for (;;) {
        Str *p = st->pending;
        char *end = NULL; size_t sep = 0;
        for (int64_t i = 0; i + 1 < p->len; i++) {           // an event ends at a blank line
            if (p->data[i] == '\n' && p->data[i + 1] == '\n') { end = p->data + i; sep = 2; break; }
            if (i + 3 < p->len && memcmp(p->data + i, "\r\n\r\n", 4) == 0) { end = p->data + i; sep = 4; break; }
        }
        if (end) {
            Str *data = NULL;
            for (char *q = p->data; q < end;) {
                char *nl = memchr(q, '\n', (size_t)(end - q));
                char *le = nl ? nl : end;
                char *lend = le > q && le[-1] == '\r' ? le - 1 : le;
                if (lend - q >= 5 && memcmp(q, "data:", 5) == 0) {
                    char *v = q + 5; if (v < lend && *v == ' ') v++;
                    if (data) data = str_add_bytes(data, "\n", 1); else data = str_alloc(64);
                    data = str_add_bytes(data, v, lend - v);
                } else if (lend - q >= 3 && memcmp(q, "id:", 3) == 0) {
                    char *v = q + 3; if (v < lend && *v == ' ') v++;
                    st->last_id = strtoll(v, NULL, 10);
                }
                q = nl ? nl + 1 : end;
            }
            size_t used = (size_t)(end - p->data) + sep;
            memmove(p->data, p->data + used, (size_t)p->len - used);
            p->len -= (int64_t)used;
            if (data) { r.value = data; return r; }   // only comments: look for the next one
            continue;
        }
        char chunk[8192];
        int64_t k = h_body(st->h, chunk, sizeof chunk, deadline);
        if (k == 0) { r.err = make_error(502, "stream_next(): the stream ended"); return r; }
        if (k < 0) { r.err = h_error((int)k, "stream_next()"); return r; }
        if (p->len + k > (1 << 20)) { r.err = make_error(502, "stream_next(): an event larger than 1 MB"); return r; }
        st->pending = str_add_bytes(st->pending, chunk, k);
    }
}

// --- run(): start a program (no shell: arguments go to it as they are), collect its output
StrOrError pn_run(Str *cmd, List *args, int64_t ms) {
    StrOrError r = {NULL, NULL};
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    char **argv = calloc((size_t)args->len + 2, sizeof(char *));
    if (!argv) oom();
    argv[0] = strndup(cmd->data, (size_t)cmd->len);
    for (int64_t i = 0; i < args->len; i++) {
        Str *a = ((Str **)args->data)[i];
        if (memchr(a->data, 0, (size_t)a->len)) panic_msg("run(): an argument contains a NUL byte");
        argv[i + 1] = strndup(a->data, (size_t)a->len);
    }
    int outp[2], errp[2];
    if (pipe(outp) < 0 || pipe(errp) < 0) oom();
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_adddup2(&fa, outp[1], 1);
    posix_spawn_file_actions_adddup2(&fa, errp[1], 2);
    posix_spawn_file_actions_addclose(&fa, outp[0]);
    posix_spawn_file_actions_addclose(&fa, errp[0]);
    posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
    pid_t pid;
    int rc = posix_spawnp(&pid, argv[0], &fa, NULL, argv, environ);
    posix_spawn_file_actions_destroy(&fa);
    close(outp[1]); close(errp[1]);
    for (int64_t i = 0; i <= args->len; i++) free(argv[i]);
    free(argv);
    if (rc != 0) {
        close(outp[0]); close(errp[0]);
        char m[300]; snprintf(m, sizeof m, "run(): cannot start %.*s: %s", (int)(cmd->len > 100 ? 100 : cmd->len), cmd->data, strerror(rc));
        r.err = make_error(127, m);
        return r;
    }
    fcntl(outp[0], F_SETFL, fcntl(outp[0], F_GETFL) | O_NONBLOCK);
    fcntl(errp[0], F_SETFL, fcntl(errp[0], F_GETFL) | O_NONBLOCK);
    Str *out = str_alloc(256), *err = str_alloc(64);
    int fds[2] = {outp[0], errp[0]}, open_n = 2, late = 0, cancelled = 0;
    char buf[16384];
    while (open_n) {
        int any = 0;
        for (int i = 0; i < 2; i++) {
            if (fds[i] < 0) continue;
            ssize_t n = read(fds[i], buf, sizeof buf);
            if (n > 0) {
                any = 1;
                if (i == 0 && out->len < ((int64_t)64 << 20)) out = str_add_bytes(out, buf, n);
                if (i == 1 && err->len < 4096) err = str_add_bytes(err, buf, n);
            } else if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                close(fds[i]); fds[i] = -1; open_n--;
            }
        }
        if (any || !open_n) continue;
        int live[2], nl = 0;
        for (int i = 0; i < 2; i++) if (fds[i] >= 0) live[nl++] = fds[i];
        int w = wait_fds(live, nl, 0, deadline);
        if (w == 0) { late = 1; break; }
        if (w < 0) { cancelled = 1; break; }
    }
    for (int i = 0; i < 2; i++) if (fds[i] >= 0) close(fds[i]);
    if (late || cancelled) kill(pid, SIGKILL);
    int st = 0;
    while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {}
    if (late || cancelled) {
        pn_str_free(out); pn_str_free(err);
        r.err = make_error(late ? 504 : 499, late ? "run(): the program did not finish in time and was stopped" : "run(): cancelled");
        return r;
    }
    int code = WIFEXITED(st) ? WEXITSTATUS(st) : 128 + (WIFSIGNALED(st) ? WTERMSIG(st) : 0);
    if (code != 0) {
        char m[600];
        snprintf(m, sizeof m, "run(): %.*s exited with %d: %.*s", (int)(cmd->len > 100 ? 100 : cmd->len), cmd->data, code,
                 (int)(err->len > 400 ? 400 : err->len), err->data);
        pn_str_free(out); pn_str_free(err);
        r.err = make_error(code, m);
        return r;
    }
    pn_str_free(err);
    r.value = out;
    return r;
}

// ============================================================================ SQLite (db_* built-ins)
// Compiled only for programs that use the database (the compiler adds -DPIN_SQLITE -lsqlite3).
// `db` and `rows` are ordinary owned values: the compiler frees them like lists, which closes the
// connection / finalizes the statement. Inside a request they are also registered as resources, so a
// panic closes them before the request's memory is freed. A connection is closed with sqlite3_close_v2,
// which waits for its statements to be finalized, so freeing a db before its rows is safe.
#ifdef PIN_SQLITE
#include <sqlite3.h>

typedef struct { sqlite3 *h; void *res; char err[256]; } Db;
typedef struct { sqlite3_stmt *st; void *res; int state; } Rows;      // state: 0 before first row, 1 row, 2 done

static void db_res_close(void *o) { Db *d = o; if (d->h) sqlite3_close_v2(d->h); d->h = NULL; }
static void rows_res_close(void *o) { Rows *r = o; if (r->st) sqlite3_finalize(r->st); r->st = NULL; }

__attribute__((noreturn)) static void db_panic(const char *what, const char *detail) {
    fflush(stdout);
    fprintf(stderr, "panic: %s: %s\n", what, detail);
    die();
}

Db *pn_db_open(Str *path) {
    sqlite3 *h = NULL;
    int rc = sqlite3_open_v2(path->data, &h, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_NOMUTEX, NULL);
    if (rc != SQLITE_OK) {
        char msg[300];
        snprintf(msg, sizeof msg, "cannot open %s (%s)", path->data, h ? sqlite3_errmsg(h) : sqlite3_errstr(rc));
        if (h) sqlite3_close_v2(h);
        db_panic("db_open()", msg);
    }
    sqlite3_busy_timeout(h, 5000);                       // wait for other writers instead of failing at once
    sqlite3_exec(h, "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;", NULL, NULL, NULL);
    Db *d = xmalloc(sizeof(Db));
    d->h = h; d->err[0] = 0;
    d->res = pn_res_add(db_res_close, d);
    return d;
}

void pn_db_close(Db *d) {
    if (!d) return;
    pn_res_remove(d->res);
    if (d->h) sqlite3_close_v2(d->h);
    xfree(d);
}

void pn_rows_free(Rows *r) {
    if (!r) return;
    pn_res_remove(r->res);
    if (r->st) sqlite3_finalize(r->st);
    xfree(r);
}

// prepare one statement and bind the list of values to its ? placeholders (kind: 0 int, 1 float, 3 string)
static sqlite3_stmt *db_prepare(Db *d, Str *sql, List *params, int64_t kind, const char *fn) {
    if (!d->h) db_panic(fn, "the database is closed");
    sqlite3_stmt *st = NULL;
    const char *tail = NULL;
    if (sqlite3_prepare_v2(d->h, sql->data, (int)sql->len, &st, &tail) != SQLITE_OK)
        db_panic(fn, sqlite3_errmsg(d->h));
    if (!st) db_panic(fn, "the SQL is empty");
    while (tail && tail < sql->data + sql->len && (*tail == ' ' || *tail == '\n' || *tail == '\t' || *tail == '\r' || *tail == ';'))
        tail++;
    if (tail && tail < sql->data + sql->len) {
        sqlite3_finalize(st);
        db_panic(fn, "one SQL statement per call (call it again for the next statement)");
    }
    int want = sqlite3_bind_parameter_count(st);
    if (want != params->len) {
        char msg[160];
        snprintf(msg, sizeof msg, "the SQL has %d ? placeholder(s) but %lld value(s) were given", want, (long long)params->len);
        sqlite3_finalize(st);
        db_panic(fn, msg);
    }
    for (int64_t i = 0; i < params->len; i++) {
        int rc;
        if (kind == 3) {
            Str *s = ((Str **)params->data)[i];
            rc = sqlite3_bind_text(st, (int)i + 1, s->data, (int)s->len, SQLITE_TRANSIENT);
        } else if (kind == 1) {
            rc = sqlite3_bind_double(st, (int)i + 1, ((double *)params->data)[i]);
        } else {
            rc = sqlite3_bind_int64(st, (int)i + 1, ((int64_t *)params->data)[i]);
        }
        if (rc != SQLITE_OK) { sqlite3_finalize(st); db_panic(fn, sqlite3_errmsg(d->h)); }
    }
    return st;
}

// INSERT / UPDATE / DELETE / CREATE ...: rows changed, or -1 when a constraint (UNIQUE, NOT NULL, CHECK,
// FOREIGN KEY) refused it — db_error() says which. Any other failure is a bug in the SQL: panic.
int64_t pn_db_exec(Db *d, Str *sql, List *params, int64_t kind) {
    sqlite3_stmt *st = db_prepare(d, sql, params, kind, "db_exec()");
    int rc;
    while ((rc = sqlite3_step(st)) == SQLITE_ROW) {}
    if (rc == SQLITE_DONE) {
        sqlite3_finalize(st);
        d->err[0] = 0;
        return sqlite3_changes(d->h);
    }
    snprintf(d->err, sizeof d->err, "%s", sqlite3_errmsg(d->h));
    sqlite3_finalize(st);
    if ((rc & 0xff) == SQLITE_CONSTRAINT) return -1;
    db_panic("db_exec()", d->err);
}

Rows *pn_db_query(Db *d, Str *sql, List *params, int64_t kind) {
    sqlite3_stmt *st = db_prepare(d, sql, params, kind, "db_query()");
    Rows *r = xmalloc(sizeof(Rows));
    r->st = st; r->state = 0;
    r->res = pn_res_add(rows_res_close, r);
    return r;
}

int64_t pn_db_next(Rows *r) {
    if (!r->st || r->state == 2) return 0;
    int rc = sqlite3_step(r->st);
    if (rc == SQLITE_ROW) { r->state = 1; return 1; }
    if (rc == SQLITE_DONE) { r->state = 2; return 0; }
    db_panic("db_next()", sqlite3_errmsg(sqlite3_db_handle(r->st)));
}

static int db_col(Rows *r, Str *name, const char *fn) {
    if (r->state != 1) db_panic(fn, "no current row: read columns only after db_next($rows) returned true");
    int n = sqlite3_column_count(r->st);
    for (int i = 0; i < n; i++) {
        const char *c = sqlite3_column_name(r->st, i);
        if (c && (int64_t)strlen(c) == name->len && memcmp(c, name->data, (size_t)name->len) == 0) return i;
    }
    char msg[512];
    int off = snprintf(msg, sizeof msg, "no column named \"%s\" (the query has:", name->data);
    for (int i = 0; i < n && off < (int)sizeof msg - 40; i++)
        off += snprintf(msg + off, sizeof msg - (size_t)off, " %s", sqlite3_column_name(r->st, i));
    snprintf(msg + off, sizeof msg - (size_t)off, ")");
    db_panic(fn, msg);
}

int64_t pn_db_int(Rows *r, Str *col) { return sqlite3_column_int64(r->st, db_col(r, col, "db_int()")); }
double pn_db_float(Rows *r, Str *col) { return sqlite3_column_double(r->st, db_col(r, col, "db_float()")); }
int64_t pn_db_is_null(Rows *r, Str *col) { return sqlite3_column_type(r->st, db_col(r, col, "db_is_null()")) == SQLITE_NULL; }

Str *pn_db_string(Rows *r, Str *col) {
    int i = db_col(r, col, "db_string()");
    const unsigned char *p = sqlite3_column_text(r->st, i);
    return p ? str_from((const char *)p, sqlite3_column_bytes(r->st, i)) : str_alloc(0);
}

int64_t pn_db_last_id(Db *d) { return d->h ? sqlite3_last_insert_rowid(d->h) : 0; }
Str *pn_db_error(Db *d) { return str_from(d->err, (int64_t)strlen(d->err)); }

// the whole result as a JSON array of objects, with JSON types from SQLite's (NULL -> null)
Str *pn_db_json(Db *d, Str *sql, List *params, int64_t kind) {
    Rows *r = pn_db_query(d, sql, params, kind);         // a heap Rows, so a panic below can finalize it
    sqlite3_stmt *st = r->st;
    Str *out = str_add_bytes(NULL, "[", 1);
    int rc, first = 1, n = sqlite3_column_count(st);
    while ((rc = sqlite3_step(st)) == SQLITE_ROW) {
        out = str_add_bytes(out, first ? "{" : ",{", first ? 1 : 2);
        first = 0;
        for (int i = 0; i < n; i++) {
            if (i) out = str_add_bytes(out, ",", 1);
            const char *name = sqlite3_column_name(st, i);
            out = json_string(out, name, (int64_t)strlen(name));
            out = str_add_bytes(out, ":", 1);
            switch (sqlite3_column_type(st, i)) {
            case SQLITE_INTEGER: out = pn_str_add_int(out, sqlite3_column_int64(st, i)); break;
            case SQLITE_FLOAT: out = json_float(out, sqlite3_column_double(st, i)); break;
            case SQLITE_NULL: out = str_add_bytes(out, "null", 4); break;
            default: {
                const char *p = (const char *)sqlite3_column_text(st, i);
                out = json_string(out, p ? p : "", sqlite3_column_bytes(st, i));
            }
            }
        }
        out = str_add_bytes(out, "}", 1);
    }
    if (rc != SQLITE_DONE) {
        char msg[256];
        snprintf(msg, sizeof msg, "%s", sqlite3_errmsg(d->h));
        db_panic("db_json()", msg);
    }
    pn_rows_free(r);
    return str_add_bytes(out, "]", 1);
}
#endif
