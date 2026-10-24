// Runtime for the prototype language.
//   lists   : {len, cap, data}            elements are 1 or 8 bytes
//   strings : {len, cap, data[]}          one allocation; cap < 0 means a compile-time literal (never freed)
//   maps    : ordered hash map (insertion order, like PHP arrays), 8-byte value slots
//   json    : json_encode for lists and maps
//   http    : multi-threaded epoll HTTP/1.1 server with keep-alive (one thread per CPU core)
// The compiler guarantees every heap value has exactly one owner, so nothing here needs
// reference counting or a garbage collector.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <unistd.h>

static void oom(void) { fprintf(stderr, "fatal: out of memory\n"); exit(2); }
static void *xmalloc(size_t n) { void *p = malloc(n ? n : 1); if (!p) oom(); return p; }
static void *xrealloc(void *p, size_t n) { p = realloc(p, n ? n : 1); if (!p) oom(); return p; }

__attribute__((noreturn)) static void panic_msg(const char *msg) {
    fflush(stdout);
    fprintf(stderr, "panic: %s\n", msg);
    exit(1);
}

// ============================================================================ lists

typedef struct { int64_t len, cap; void *data; } List;

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
    l->data = xmalloc((size_t)(l->cap * esize));
    if (bits == 0) memset(l->data, 0, (size_t)(n * esize));
    else if (esize == 1) memset(l->data, (int)(bits & 0xff), (size_t)n);
    else { uint64_t *d = l->data; for (int64_t i = 0; i < n; i++) d[i] = bits; }
    return l;
}

void pn_list_grow(List *l, int64_t esize) {
    l->cap *= 2;
    l->data = xrealloc(l->data, (size_t)(l->cap * esize));
}

void pn_list_push(List *l, int64_t esize, uint64_t bits) {
    if (l->len == l->cap) pn_list_grow(l, esize);
    memcpy((char *)l->data + l->len * esize, &bits, (size_t)esize);
    l->len++;
}

List *pn_list_copy(List *src, int64_t esize) {
    List *l = xmalloc(sizeof(List));
    l->len = src->len; l->cap = src->len > 0 ? src->len : 1;
    l->data = xmalloc((size_t)(l->cap * esize));
    memcpy(l->data, src->data, (size_t)(src->len * esize));
    return l;
}

void pn_list_free(List *l) {
    if (!l) return;
    free(l->data);
    free(l);
}

__attribute__((noreturn)) void pn_oob(int64_t i, int64_t len) {
    fflush(stdout);
    fprintf(stderr, "panic: index %lld out of range (list has %lld items)\n", (long long)i, (long long)len);
    exit(1);
}

__attribute__((noreturn)) void pn_divzero(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: division by zero on line %d\n", line);
    exit(1);
}

__attribute__((noreturn)) void pn_noreturn(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: function ended without return (line %d)\n", line);
    exit(1);
}

__attribute__((noreturn)) void pn_map_missing(int32_t line) {
    fflush(stdout);
    fprintf(stderr, "panic: key not found on line %d (check with isset() or use the ?? operator)\n", line);
    exit(1);
}

// ============================================================================ strings

typedef struct { int64_t len, cap; char data[]; } Str;

static Str *str_alloc(int64_t cap) {
    if (cap < 15) cap = 15;
    Str *s = xmalloc(sizeof(Str) + (size_t)cap + 1);
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

void pn_str_free(Str *s) { if (s && s->cap >= 0) free(s); }

// Copy a string. Literals are immutable and live forever, so their "copy" is the literal itself.
Str *pn_str_dup(Str *s) {
    if (!s) return str_alloc(0);
    if (s->cap < 0) return s;
    return str_from(s->data, s->len);
}

// Make room for `extra` more bytes. Takes ownership of `s`; returns the (possibly new) string.
static Str *str_reserve(Str *s, int64_t extra) {
    if (!s) return str_alloc(extra);
    if (s->cap < 0) {                      // literal: never written to, copy first
        Str *n = str_alloc(s->len + extra > 2 * s->len ? s->len + extra : 2 * s->len);
        memcpy(n->data, s->data, (size_t)s->len + 1);
        n->len = s->len;
        return n;
    }
    if (s->len + extra > s->cap) {
        int64_t nc = s->cap * 2;
        if (nc < s->len + extra) nc = s->len + extra;
        s = xrealloc(s, sizeof(Str) + (size_t)nc + 1);
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

// intval(): optional whitespace, optional sign, digits; anything else stops the number
int64_t pn_str_to_int(Str *s) {
    const char *p = s->data, *e = s->data + s->len;
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    int neg = 0;
    if (p < e && (*p == '+' || *p == '-')) neg = (*p++ == '-');
    uint64_t v = 0;
    while (p < e && *p >= '0' && *p <= '9') v = v * 10 + (uint64_t)(*p++ - '0');
    return neg ? -(int64_t)v : (int64_t)v;
}

// PHP substr semantics, including negative start / length
Str *pn_substr(Str *s, int64_t start, int64_t len, int64_t has_len) {
    int64_t n = s->len;
    if (start < 0) { start += n; if (start < 0) start = 0; }
    if (start > n) return str_alloc(0);
    int64_t end = n;
    if (has_len) {
        if (len < 0) { end = n + len; if (end < start) return str_alloc(0); }
        else if (start + len < n) end = start + len;
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

// kinds: 0 int, 1 float, 2 bool, 3 string
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
    m->idx = calloc(16, sizeof(int64_t));
    if (!m->idx) oom();
    m->kk = (int32_t)kk; m->vk = (int32_t)vk;
    return m;
}

static void map_reindex(Map *m) {
    free(m->idx);
    m->idx = calloc((size_t)m->isize, sizeof(int64_t));
    if (!m->idx) oom();
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
    free(m->e); free(m->idx); free(m);
}

Map *pn_map_copy(Map *src) {
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

Str *pn_json_list(List *l, int64_t kind) {
    Str *d = str_alloc(16 + l->len * 4);
    d = str_add_bytes(d, "[", 1);
    for (int64_t i = 0; i < l->len; i++) {
        if (i) d = str_add_bytes(d, ",", 1);
        uint64_t bits = 0;
        memcpy(&bits, (char *)l->data + i * (kind == 2 ? 1 : 8), kind == 2 ? 1 : 8);
        d = json_value(d, bits, (int)kind);
    }
    return str_add_bytes(d, "]", 1);
}

Str *pn_json_map(Map *m) {
    Str *d = str_alloc(16 + m->count * 16);
    d = str_add_bytes(d, "{", 1);
    int first = 1;
    for (int64_t i = 0; i < m->used; i++) {
        Entry *e = &m->e[i];
        if (!e->alive) continue;
        if (!first) d = str_add_bytes(d, ",", 1);
        first = 0;
        if (m->kk == 3) { Str *k = (Str *)e->key; d = json_string(d, k->data, k->len); }
        else { char buf[24]; int n = snprintf(buf, sizeof buf, "\"%lld\"", (long long)e->key); d = str_add_bytes(d, buf, n); }
        d = str_add_bytes(d, ":", 1);
        d = json_value(d, e->val, m->vk);
    }
    return str_add_bytes(d, "}", 1);
}

Str *pn_json_str(Str *s) { return json_string(NULL, s->data, s->len); }
Str *pn_json_float(double v) { return json_float(NULL, v); }

// ============================================================================ printing

void pn_print_int(int64_t v) { printf("%lld", (long long)v); }
void pn_print_str(Str *s) { fwrite(s->data, 1, (size_t)s->len, stdout); }
void pn_print_float(double v) {
    char buf[48];
    fmt_float(buf, sizeof buf, v);
    fputs(buf, stdout);
}

// ============================================================================ http server

extern Str *pn_user_handler(Str *method, Str *path, Str *body) __attribute__((weak));

static __thread int64_t http_status = 200;
void pn_http_status(int64_t s) { http_status = s; }

typedef struct {
    int fd, close_after, want_out;
    char *in; size_t in_len, in_cap;
    char *out; size_t out_len, out_off, out_cap;
} Conn;

static const char *reason(int64_t s) {
    switch (s) {
    case 200: return "OK"; case 201: return "Created"; case 204: return "No Content";
    case 400: return "Bad Request"; case 401: return "Unauthorized"; case 403: return "Forbidden";
    case 404: return "Not Found"; case 405: return "Method Not Allowed"; case 500: return "Internal Server Error";
    default: return "Status";
    }
}

static void out_add(Conn *c, const char *p, size_t n) {
    if (c->out_len + n > c->out_cap) {
        c->out_cap = (c->out_len + n) * 2;
        c->out = xrealloc(c->out, c->out_cap);
    }
    memcpy(c->out + c->out_len, p, n);
    c->out_len += n;
}

static int ieq_prefix(const char *p, const char *end, const char *name) {
    size_t n = strlen(name);
    if ((size_t)(end - p) < n) return 0;
    return strncasecmp(p, name, n) == 0;
}

// Parse and answer every complete request in the input buffer. -1 = close the connection.
static int handle_input(Conn *c) {
    size_t off = 0;
    while (!c->close_after) {
        char *buf = c->in + off;
        size_t avail = c->in_len - off;
        char *hend = memmem(buf, avail, "\r\n\r\n", 4);
        if (!hend) { if (avail > 65536) return -1; break; }
        char *line_end = memmem(buf, (size_t)(hend - buf) + 2, "\r\n", 2);
        char *sp1 = memchr(buf, ' ', (size_t)(line_end - buf));
        char *sp2 = sp1 ? memchr(sp1 + 1, ' ', (size_t)(line_end - sp1 - 1)) : NULL;
        if (!sp2) {
            static const char bad[] = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
            out_add(c, bad, sizeof bad - 1);
            c->close_after = 1;
            c->in_len = 0;
            return 0;
        }
        int http10 = (line_end - sp2 - 1 == 8) && memcmp(sp2 + 1, "HTTP/1.0", 8) == 0;
        int keep = !http10;
        int64_t clen = 0;
        for (char *p = line_end + 2; p < hend;) {
            char *e = memmem(p, (size_t)(hend + 2 - p), "\r\n", 2);
            if (ieq_prefix(p, e, "content-length:")) clen = strtoll(p + 15, NULL, 10);
            else if (ieq_prefix(p, e, "connection:")) {
                char *v = p + 11; while (*v == ' ') v++;
                if (ieq_prefix(v, e, "close")) keep = 0;
                else if (ieq_prefix(v, e, "keep-alive")) keep = 1;
            }
            p = e + 2;
        }
        if (clen < 0 || clen > (64 << 20)) return -1;
        size_t hlen = (size_t)(hend - buf) + 4;
        if (avail < hlen + (size_t)clen) break;      // body not complete yet

        Str *method = str_from(buf, sp1 - buf);
        Str *path = str_from(sp1 + 1, sp2 - sp1 - 1);
        Str *body = str_from(hend + 4, clen);
        http_status = 200;
        Str *resp = pn_user_handler(method, path, body);
        char head[256];
        int hn = snprintf(head, sizeof head,
                          "HTTP/1.1 %lld %s\r\nContent-Type: application/json\r\nContent-Length: %lld\r\n%s\r\n",
                          (long long)http_status, reason(http_status), (long long)(resp ? resp->len : 0),
                          keep ? "" : "Connection: close\r\n");
        out_add(c, head, (size_t)hn);
        if (resp) out_add(c, resp->data, (size_t)resp->len);
        pn_str_free(resp); pn_str_free(method); pn_str_free(path); pn_str_free(body);
        off += hlen + (size_t)clen;
        if (!keep) c->close_after = 1;
    }
    if (off) { memmove(c->in, c->in + off, c->in_len - off); c->in_len -= off; }
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

static void conn_close(int ep, Conn *c) {
    epoll_ctl(ep, EPOLL_CTL_DEL, c->fd, NULL);
    close(c->fd);
    free(c->in); free(c->out); free(c);
}

static int listen_port;

static int open_listener(int port) {
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof one);
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET; a.sin_port = htons((uint16_t)port); a.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0 || listen(fd, 4096) < 0) {
        perror("serve: cannot listen");
        exit(1);
    }
    return fd;
}

static void *worker(void *arg) {
    int lfd = open_listener(listen_port);
    int ep = epoll_create1(0);
    struct epoll_event ev = {.events = EPOLLIN, .data.ptr = NULL};
    epoll_ctl(ep, EPOLL_CTL_ADD, lfd, &ev);
    struct epoll_event evs[256];
    (void)arg;
    for (;;) {
        int n = epoll_wait(ep, evs, 256, -1);
        for (int i = 0; i < n; i++) {
            Conn *c = evs[i].data.ptr;
            if (!c) {                                         // new connections
                for (;;) {
                    int fd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK);
                    if (fd < 0) break;
                    int one = 1;
                    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
                    Conn *nc = calloc(1, sizeof(Conn));
                    if (!nc) oom();
                    nc->fd = fd;
                    nc->in_cap = 4096; nc->in = xmalloc(nc->in_cap);
                    struct epoll_event cev = {.events = EPOLLIN | EPOLLRDHUP, .data.ptr = nc};
                    epoll_ctl(ep, EPOLL_CTL_ADD, fd, &cev);
                }
                continue;
            }
            uint32_t e = evs[i].events;
            int dead = 0, peer_done = 0;
            if (e & EPOLLERR) dead = 1;
            if (!dead && (e & (EPOLLIN | EPOLLHUP | EPOLLRDHUP))) {
                for (;;) {
                    if (c->in_len == c->in_cap) { c->in_cap *= 2; c->in = xrealloc(c->in, c->in_cap); }
                    ssize_t r = read(c->fd, c->in + c->in_len, c->in_cap - c->in_len);
                    if (r > 0) { c->in_len += (size_t)r; continue; }
                    if (r == 0) { peer_done = 1; break; }      // client finished sending: still answer it
                    if (errno == EINTR) continue;
                    if (errno != EAGAIN && errno != EWOULDBLOCK) dead = 1;
                    break;
                }
                if (!dead && handle_input(c) < 0) dead = 1;
                if (peer_done) c->close_after = 1;
            }
            if (!dead && c->out_len) {
                int f = flush_out(c);
                if (f < 0) dead = 1;
                else if (f == 0 && !c->want_out) {
                    c->want_out = 1;
                    struct epoll_event cev = {.events = EPOLLIN | EPOLLOUT | EPOLLRDHUP, .data.ptr = c};
                    epoll_ctl(ep, EPOLL_CTL_MOD, c->fd, &cev);
                } else if (f == 1 && c->want_out) {
                    c->want_out = 0;
                    struct epoll_event cev = {.events = EPOLLIN | EPOLLRDHUP, .data.ptr = c};
                    epoll_ctl(ep, EPOLL_CTL_MOD, c->fd, &cev);
                }
            }
            if (!dead && c->close_after && c->out_len == 0) dead = 1;
            if (dead) conn_close(ep, c);
        }
    }
    return NULL;
}

void pn_serve(int64_t port) {
    if (!pn_user_handler) panic_msg("serve(): no handler compiled in");
    signal(SIGPIPE, SIG_IGN);
    setvbuf(stdout, NULL, _IOLBF, 0);
    listen_port = (int)port;
    int threads = 0;
    const char *env = getenv("PN_THREADS");
    if (env) threads = atoi(env);
    if (threads <= 0) {
        cpu_set_t set;
        threads = sched_getaffinity(0, sizeof set, &set) == 0 ? CPU_COUNT(&set) : 1;
    }
    fprintf(stderr, "listening on :%lld with %d thread(s)\n", (long long)port, threads);
    for (int i = 1; i < threads; i++) {
        pthread_t t;
        pthread_create(&t, NULL, worker, NULL);
    }
    worker(NULL);
}
