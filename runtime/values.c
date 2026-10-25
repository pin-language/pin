// runtime/values.c — lists, strings, maps, JSON output, objects and nested containers, explode / implode / sort. Part of runtime.c (one translation unit: see runtime.c).

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
