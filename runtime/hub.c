// runtime/hub.c — the Hub (messages between requests) and await_timeout. Part of runtime.c (one translation unit: see runtime.c).

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
