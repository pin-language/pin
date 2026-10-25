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

static void (*pool_scan_hook)(int64_t now);   // set by the database pool (sqlite.c) when it is compiled in
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
