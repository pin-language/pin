// runtime/jdecode.c — the parts of json_decode() written in C (design P2). Part of runtime.c (one translation unit).
//
// json_decode($text) into a class is compiled into Pin functions (one per class, list and map type: see
// pinac.py gen_decoders) that call the __jd_* built-ins below. They work on the JSON text and byte positions,
// so nothing is copied but the values themselves. pn_jd_doc() checks the whole text first (strict RFC 8259,
// nesting at most 64 deep), so the others only check types; they still check every position against the end.
// Every failure is error(400, "<path>: <what>"), the path like $.items[2].qty.

typedef struct { int64_t value; PinError *err; } IntOrError;

#define JD_DEPTH 64

static const char *jd_ws(const char *p, const char *e) {
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    return p;
}

static int jd_hex4(const char *p, const char *e, unsigned *out) {
    if (e - p < 4) return 0;
    unsigned v = 0;
    for (int i = 0; i < 4; i++) {
        char c = p[i];
        int d = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
        if (d < 0) return 0;
        v = v * 16 + (unsigned)d;
    }
    *out = v;
    return 1;
}

// a string starting at its opening quote: the position after the closing quote, or NULL if malformed
static const char *jd_scan_string(const char *p, const char *e) {
    p++;
    while (p < e) {
        unsigned char c = (unsigned char)*p;
        if (c == '"') return p + 1;
        if (c < 0x20) return NULL;
        if (c == '\\') {
            if (p + 1 >= e) return NULL;
            char x = p[1];
            if (x == 'u') { unsigned u; if (!jd_hex4(p + 2, e, &u)) return NULL; p += 6; continue; }
            if (!strchr("\"\\/bfnrt", x) || x == 0) return NULL;
            p += 2;
            continue;
        }
        p++;
    }
    return NULL;
}

static const char *jd_scan_number(const char *p, const char *e) {
    if (p < e && *p == '-') p++;
    if (p >= e) return NULL;
    if (*p == '0') p++;
    else if (*p >= '1' && *p <= '9') { while (p < e && *p >= '0' && *p <= '9') p++; }
    else return NULL;
    if (p < e && *p == '.') {
        p++;
        if (p >= e || *p < '0' || *p > '9') return NULL;
        while (p < e && *p >= '0' && *p <= '9') p++;
    }
    if (p < e && (*p == 'e' || *p == 'E')) {
        p++;
        if (p < e && (*p == '+' || *p == '-')) p++;
        if (p >= e || *p < '0' || *p > '9') return NULL;
        while (p < e && *p >= '0' && *p <= '9') p++;
    }
    return p;
}

// Skip one well-formed value (any kind) starting at p; the position after it. Assumes pn_jd_doc checked the text,
// but never reads past e even if it did not.
static const char *jd_skip(const char *p, const char *e) {
    if (p >= e) return e;
    if (*p == '"') { const char *q = jd_scan_string(p, e); return q ? q : e; }
    if (*p == '{' || *p == '[') {
        int depth = 0;
        while (p < e) {
            char c = *p;
            if (c == '"') { const char *q = jd_scan_string(p, e); if (!q) return e; p = q; continue; }
            if (c == '{' || c == '[') depth++;
            else if (c == '}' || c == ']') { depth--; if (depth == 0) return p + 1; }
            p++;
        }
        return e;
    }
    if (*p == '-' || (*p >= '0' && *p <= '9')) { const char *q = jd_scan_number(p, e); return q ? q : e; }
    if (e - p >= 4 && (!memcmp(p, "true", 4) || !memcmp(p, "null", 4))) return p + 4;
    if (e - p >= 5 && !memcmp(p, "false", 5)) return p + 5;
    return e;
}

static const char *jd_kind(const char *p, const char *e) {
    if (p >= e) return "nothing";
    switch (*p) {
    case '"': return "a string";
    case '{': return "an object";
    case '[': return "an array";
    case 't': case 'f': return "true/false";
    case 'n': return "null";
    default: return "a number";
    }
}

// ---- error messages with the path of a position: walk the document from the start until we reach it
typedef struct { const char *target; char *out; size_t cap; int found; } JdPath;

static void jd_path_add(char *path, size_t *len, size_t cap, const char *s, size_t n) {
    if (*len + n + 1 >= cap) n = *len + 1 < cap ? cap - *len - 1 : 0;
    memcpy(path + *len, s, n);
    *len += n;
    path[*len] = 0;
}

static const char *jd_walk(const char *p, const char *e, JdPath *w, char *path, size_t len, int depth) {
    p = jd_ws(p, e);
    if (w->found) return e;
    if (p == w->target) { snprintf(w->out, w->cap, "%s", path); w->found = 1; return e; }
    if (p >= e || depth > JD_DEPTH) return e;
    if (*p == '[') {
        p = jd_ws(p + 1, e);
        for (int64_t i = 0; p < e && *p != ']'; i++) {
            char buf[32];
            size_t l2 = len;
            int n = snprintf(buf, sizeof buf, "[%lld]", (long long)i);
            jd_path_add(path, &l2, 256, buf, (size_t)n);
            p = jd_walk(p, e, w, path, l2, depth + 1);
            path[len] = 0;
            if (w->found) return e;
            p = jd_ws(p, e);
            if (p < e && *p == ',') p = jd_ws(p + 1, e);
        }
        return p < e ? p + 1 : e;
    }
    if (*p == '{') {
        p = jd_ws(p + 1, e);
        while (p < e && *p == '"') {
            const char *ke = jd_scan_string(p, e);
            if (!ke) return e;
            size_t l2 = len;
            jd_path_add(path, &l2, 256, ".", 1);
            jd_path_add(path, &l2, 256, p + 1, (size_t)(ke - p - 2));   // the key as written (escapes kept)
            p = jd_ws(ke, e);
            if (p < e && *p == ':') p++;
            p = jd_walk(p, e, w, path, l2, depth + 1);
            path[len] = 0;
            if (w->found) return e;
            p = jd_ws(p, e);
            if (p < e && *p == ',') p = jd_ws(p + 1, e);
        }
        return p < e ? p + 1 : e;
    }
    return jd_skip(p, e);
}

static PinError *jd_fail(Str *j, int64_t pos, const char *what) {
    char path[256] = "$", where[256] = "$", msg[600];
    JdPath w = {j->data + pos, where, sizeof where, 0};
    jd_walk(j->data, j->data + j->len, &w, path, 1, 0);
    snprintf(msg, sizeof msg, "%s: %s", w.found ? where : "$", what);
    return make_error(400, msg);
}

static int jd_bad_pos(Str *j, int64_t p) { return p < 0 || p >= j->len; }

// ---- checking the whole document (iterative: a hostile body cannot overflow the C stack)
IntOrError pn_jd_doc(Str *j) {
    IntOrError r = {0, NULL};
    const char *s = j->data, *e = s + j->len, *p = jd_ws(s, e);
    char stack[JD_DEPTH + 1];
    int depth = 0;
    const char *start = p;
    char msg[160];
    enum { VALUE, AFTER } want = VALUE;
    for (;;) {
        p = jd_ws(p, e);
        if (want == VALUE) {
            if (p >= e) { snprintf(msg, sizeof msg, "invalid JSON: it ends where a value should be (byte %lld)", (long long)(p - s)); goto bad; }
            char c = *p;
            if (c == '{' || c == '[') {
                if (depth == JD_DEPTH) { snprintf(msg, sizeof msg, "invalid JSON: nested deeper than %d levels", JD_DEPTH); goto bad; }
                stack[depth++] = c;
                p = jd_ws(p + 1, e);
                if (p < e && *p == (c == '{' ? '}' : ']')) { depth--; p++; want = AFTER; continue; }
                if (c == '{') {
                    if (p >= e || *p != '"') { snprintf(msg, sizeof msg, "invalid JSON: a key (in double quotes) expected at byte %lld", (long long)(p - s)); goto bad; }
                    const char *q = jd_scan_string(p, e);
                    if (!q) { snprintf(msg, sizeof msg, "invalid JSON: bad string at byte %lld", (long long)(p - s)); goto bad; }
                    p = jd_ws(q, e);
                    if (p >= e || *p != ':') { snprintf(msg, sizeof msg, "invalid JSON: ':' expected at byte %lld", (long long)(p - s)); goto bad; }
                    p++;
                }
                continue;                                    // want a VALUE
            }
            const char *q = NULL;
            if (c == '"') q = jd_scan_string(p, e);
            else if (c == '-' || (c >= '0' && c <= '9')) q = jd_scan_number(p, e);
            else if (e - p >= 4 && (!memcmp(p, "true", 4) || !memcmp(p, "null", 4))) q = p + 4;
            else if (e - p >= 5 && !memcmp(p, "false", 5)) q = p + 5;
            if (!q && c == '"') { snprintf(msg, sizeof msg, "invalid JSON: bad string at byte %lld (an unknown \\ escape, a raw control character or no closing quote)", (long long)(p - s)); goto bad; }
            if (!q) { snprintf(msg, sizeof msg, "invalid JSON: unexpected '%c' at byte %lld", c >= 32 && c < 127 ? c : '?', (long long)(p - s)); goto bad; }
            p = q;
            want = AFTER;
            continue;
        }
        // AFTER a value
        if (depth == 0) {
            if (p != e) { snprintf(msg, sizeof msg, "invalid JSON: more text after the value at byte %lld", (long long)(p - s)); goto bad; }
            r.value = start - s;
            return r;
        }
        char top = stack[depth - 1];
        if (p < e && *p == ',') {
            p = jd_ws(p + 1, e);
            if (top == '{') {
                if (p >= e || *p != '"') { snprintf(msg, sizeof msg, "invalid JSON: a key (in double quotes) expected at byte %lld", (long long)(p - s)); goto bad; }
                const char *q = jd_scan_string(p, e);
                if (!q) { snprintf(msg, sizeof msg, "invalid JSON: bad string at byte %lld", (long long)(p - s)); goto bad; }
                p = jd_ws(q, e);
                if (p >= e || *p != ':') { snprintf(msg, sizeof msg, "invalid JSON: ':' expected at byte %lld", (long long)(p - s)); goto bad; }
                p++;
            }
            want = VALUE;
            continue;
        }
        if (p < e && *p == (top == '{' ? '}' : ']')) { depth--; p++; continue; }
        snprintf(msg, sizeof msg, "invalid JSON: ',' or '%c' expected at byte %lld", top == '{' ? '}' : ']', (long long)(p - s));
        goto bad;
    }
bad:
    r.err = make_error(400, msg);
    return r;
}

// ---- strings
// Decode the JSON string at p into buf (malloc'd, NUL-terminated); its length, or -1 for a lone surrogate.
static int64_t jd_unescape(const char *p, const char *e, char **buf) {
    const char *q = jd_scan_string(p, e);
    if (!q) return -1;
    char *out = malloc((size_t)(q - p) + 1);          // never longer than the escaped form
    if (!out) oom();
    int64_t n = 0;
    for (p++; p < q - 1;) {
        if (*p != '\\') { out[n++] = *p++; continue; }
        char x = p[1];
        p += 2;
        switch (x) {
        case 'b': out[n++] = '\b'; break;
        case 'f': out[n++] = '\f'; break;
        case 'n': out[n++] = '\n'; break;
        case 'r': out[n++] = '\r'; break;
        case 't': out[n++] = '\t'; break;
        case 'u': {
            unsigned u, lo;
            jd_hex4(p, e, &u);
            p += 4;
            if (u >= 0xd800 && u <= 0xdbff) {           // a surrogate pair makes one character past U+FFFF
                if (q - 1 - p < 6 || p[0] != '\\' || p[1] != 'u' || !jd_hex4(p + 2, e, &lo) || lo < 0xdc00 || lo > 0xdfff) {
                    free(out); return -1;
                }
                p += 6;
                u = 0x10000 + ((u - 0xd800) << 10) + (lo - 0xdc00);
            } else if (u >= 0xdc00 && u <= 0xdfff) { free(out); return -1; }
            if (u < 0x80) out[n++] = (char)u;
            else if (u < 0x800) { out[n++] = (char)(0xc0 | u >> 6); out[n++] = (char)(0x80 | (u & 63)); }
            else if (u < 0x10000) { out[n++] = (char)(0xe0 | u >> 12); out[n++] = (char)(0x80 | (u >> 6 & 63)); out[n++] = (char)(0x80 | (u & 63)); }
            else { out[n++] = (char)(0xf0 | u >> 18); out[n++] = (char)(0x80 | (u >> 12 & 63)); out[n++] = (char)(0x80 | (u >> 6 & 63)); out[n++] = (char)(0x80 | (u & 63)); }
            break;
        }
        default: out[n++] = x;                           // \" \\ \/
        }
    }
    out[n] = 0;
    *buf = out;
    return n;
}

StrOrError pn_jd_string(Str *j, int64_t p) {
    StrOrError r = {NULL, NULL};
    if (jd_bad_pos(j, p)) { r.err = make_error(400, "$: a string expected"); return r; }
    const char *s = j->data + p, *e = j->data + j->len;
    if (*s != '"') {
        char what[64];
        snprintf(what, sizeof what, "want a string, got %s", jd_kind(s, e));
        r.err = jd_fail(j, p, what);
        return r;
    }
    char *buf;
    int64_t n = jd_unescape(s, e, &buf);
    if (n < 0) { r.err = jd_fail(j, p, "invalid \\u escape (a lone surrogate)"); return r; }
    r.value = str_from(buf, n);
    free(buf);
    return r;
}

// ---- numbers and booleans
IntOrError pn_jd_int(Str *j, int64_t p) {
    IntOrError r = {0, NULL};
    if (jd_bad_pos(j, p)) { r.err = make_error(400, "$: a number expected"); return r; }
    const char *s = j->data + p, *e = j->data + j->len;
    const char *q = (*s == '-' || (*s >= '0' && *s <= '9')) ? jd_scan_number(s, e) : NULL;
    if (!q) {
        char what[64];
        snprintf(what, sizeof what, "want an integer, got %s", jd_kind(s, e));
        r.err = jd_fail(j, p, what);
        return r;
    }
    int neg = *s == '-';
    uint64_t v = 0;
    const char *d = s + neg;
    for (; d < q && *d >= '0' && *d <= '9'; d++) {
        if (v > (UINT64_MAX - 9) / 10) { r.err = jd_fail(j, p, "the number is too big for an int"); return r; }
        v = v * 10 + (uint64_t)(*d - '0');
    }
    if (d < q) { r.err = jd_fail(j, p, "want an integer, got a number with a fraction or exponent"); return r; }
    if ((!neg && v > (uint64_t)INT64_MAX) || (neg && v > (uint64_t)INT64_MAX + 1)) {
        r.err = jd_fail(j, p, "the number is too big for an int");
        return r;
    }
    r.value = neg ? (int64_t)(0 - v) : (int64_t)v;
    return r;
}

// The double comes back as its bits in an int64: C returns {double, pointer} in other registers than LLVM's
// {double, ptr} on arm64, so a struct with a double in it must not cross into Pin code.
IntOrError pn_jd_float(Str *j, int64_t p) {
    IntOrError r = {0, NULL};
    if (jd_bad_pos(j, p)) { r.err = make_error(400, "$: a number expected"); return r; }
    const char *s = j->data + p, *e = j->data + j->len;
    const char *q = (*s == '-' || (*s >= '0' && *s <= '9')) ? jd_scan_number(s, e) : NULL;
    if (!q) {
        char what[64];
        snprintf(what, sizeof what, "want a number, got %s", jd_kind(s, e));
        r.err = jd_fail(j, p, what);
        return r;
    }
    char small[64], *num = small;
    size_t n = (size_t)(q - s);
    if (n >= sizeof small) { num = malloc(n + 1); if (!num) oom(); }
    memcpy(num, s, n);
    num[n] = 0;
    double v = strtod(num, NULL);
    if (num != small) free(num);
    if (isinf(v)) { r.err = jd_fail(j, p, "the number is too big for a float"); return r; }
    memcpy(&r.value, &v, sizeof v);
    return r;
}

IntOrError pn_jd_bool(Str *j, int64_t p) {
    IntOrError r = {0, NULL};
    if (jd_bad_pos(j, p)) { r.err = make_error(400, "$: true or false expected"); return r; }
    const char *s = j->data + p, *e = j->data + j->len;
    if (e - s >= 4 && !memcmp(s, "true", 4)) { r.value = 1; return r; }
    if (e - s >= 5 && !memcmp(s, "false", 5)) return r;
    char what[64];
    snprintf(what, sizeof what, "want true or false, got %s", jd_kind(s, e));
    r.err = jd_fail(j, p, what);
    return r;
}

// ---- objects decoded into a class: the keys must all be known, each at most once
static int jd_key_eq(const char *k, int64_t kn, Str *want) {
    return kn == want->len && memcmp(k, want->data, (size_t)kn) == 0;
}

IntOrError pn_jd_object(Str *j, int64_t p, List *keys) {
    IntOrError r = {p, NULL};
    if (jd_bad_pos(j, p) || j->data[p] != '{') {
        char what[64];
        snprintf(what, sizeof what, "want an object, got %s", jd_bad_pos(j, p) ? "nothing" : jd_kind(j->data + p, j->data + j->len));
        r.err = jd_bad_pos(j, p) ? make_error(400, "$: an object expected") : jd_fail(j, p, what);
        return r;
    }
    const char *s = j->data + p + 1, *e = j->data + j->len;
    char seen[256] = {0};
    int64_t nkeys = keys->len < 256 ? keys->len : 256;
    s = jd_ws(s, e);
    while (s < e && *s == '"') {
        char *k;
        int64_t kn = jd_unescape(s, e, &k);
        if (kn < 0) { r.err = jd_fail(j, s - j->data, "invalid \\u escape in a key"); return r; }
        int64_t i = 0;
        while (i < nkeys && !jd_key_eq(k, kn, ((Str **)keys->data)[i])) i++;
        char what[200];
        if (i == nkeys || seen[i]) {
            snprintf(what, sizeof what, i == nkeys ? "unknown field \"%.80s\"" : "field \"%.80s\" appears twice", k);
            free(k);
            r.err = jd_fail(j, p, what);
            return r;
        }
        free(k);
        seen[i] = 1;
        s = jd_ws(jd_scan_string(s, e), e);
        if (s < e && *s == ':') s++;
        s = jd_ws(jd_skip(jd_ws(s, e), e), e);
        if (s < e && *s == ',') s = jd_ws(s + 1, e);
    }
    return r;
}

// position of the value of `key` in the object at p (checked by pn_jd_object), or -1
static int64_t jd_find(Str *j, int64_t p, Str *key) {
    if (jd_bad_pos(j, p) || j->data[p] != '{') return -1;
    const char *s = jd_ws(j->data + p + 1, j->data + j->len), *e = j->data + j->len;
    while (s < e && *s == '"') {
        char *k;
        int64_t kn = jd_unescape(s, e, &k);
        if (kn < 0) return -1;
        int eq = jd_key_eq(k, kn, key);
        free(k);
        s = jd_ws(jd_scan_string(s, e), e);
        if (s < e && *s == ':') s++;
        s = jd_ws(s, e);
        if (eq) return s - j->data;
        s = jd_ws(jd_skip(s, e), e);
        if (s < e && *s == ',') s = jd_ws(s + 1, e);
    }
    return -1;
}

IntOrError pn_jd_field(Str *j, int64_t p, Str *key) {
    IntOrError r = {jd_find(j, p, key), NULL};
    if (r.value < 0) {
        char what[160];
        snprintf(what, sizeof what, "missing field \"%.80s\"", key->data);
        r.err = jd_bad_pos(j, p) ? make_error(400, what) : jd_fail(j, p, what);
    }
    return r;
}

int64_t pn_jd_has(Str *j, int64_t p, Str *key) { return jd_find(j, p, key) >= 0; }

// ---- arrays: position of the first item (-1 if empty), then of each next one (-1 at the end)
IntOrError pn_jd_first(Str *j, int64_t p) {
    IntOrError r = {-1, NULL};
    if (jd_bad_pos(j, p) || j->data[p] != '[') {
        char what[64];
        snprintf(what, sizeof what, "want an array, got %s", jd_bad_pos(j, p) ? "nothing" : jd_kind(j->data + p, j->data + j->len));
        r.err = jd_bad_pos(j, p) ? make_error(400, "$: an array expected") : jd_fail(j, p, what);
        return r;
    }
    const char *s = jd_ws(j->data + p + 1, j->data + j->len);
    if (s < j->data + j->len && *s != ']') r.value = s - j->data;
    return r;
}

int64_t pn_jd_next(Str *j, int64_t q) {
    if (jd_bad_pos(j, q)) return -1;
    const char *e = j->data + j->len, *s = jd_ws(jd_skip(j->data + q, e), e);
    if (s < e && *s == ',') return jd_ws(s + 1, e) - j->data;
    return -1;
}

// ---- objects decoded into a map<string, V>: entries in order, keys at most once
static int jd_cmp_keys(const void *a, const void *b) {
    const Str *x = *(Str *const *)a, *y = *(Str *const *)b;
    int64_t n = x->len < y->len ? x->len : y->len;
    int c = memcmp(x->data, y->data, (size_t)n);
    return c ? c : (x->len > y->len) - (x->len < y->len);
}

IntOrError pn_jd_mfirst(Str *j, int64_t p) {
    IntOrError r = {-1, NULL};
    if (jd_bad_pos(j, p) || j->data[p] != '{') {
        char what[64];
        snprintf(what, sizeof what, "want an object, got %s", jd_bad_pos(j, p) ? "nothing" : jd_kind(j->data + p, j->data + j->len));
        r.err = jd_bad_pos(j, p) ? make_error(400, "$: an object expected") : jd_fail(j, p, what);
        return r;
    }
    const char *e = j->data + j->len, *s = jd_ws(j->data + p + 1, e);
    int64_t n = 0, cap = 16;
    Str **ks = malloc(sizeof(Str *) * (size_t)cap);       // plain malloc: freed below on every path
    if (!ks) oom();
    const char *first = s;
    while (s < e && *s == '"') {
        char *k;
        int64_t kn = jd_unescape(s, e, &k);
        if (kn < 0) { for (int64_t i = 0; i < n; i++) free(ks[i]); free(ks); r.err = jd_fail(j, s - j->data, "invalid \\u escape in a key"); return r; }
        Str *ksr = malloc(sizeof(Str) + (size_t)kn + 1);
        if (!ksr) oom();
        ksr->len = kn; ksr->cap = kn;
        memcpy(ksr->data, k, (size_t)kn + 1);
        free(k);
        if (n == cap) { cap *= 2; ks = realloc(ks, sizeof(Str *) * (size_t)cap); if (!ks) oom(); }
        ks[n++] = ksr;
        s = jd_ws(jd_scan_string(s, e), e);
        if (s < e && *s == ':') s++;
        s = jd_ws(jd_skip(jd_ws(s, e), e), e);
        if (s < e && *s == ',') s = jd_ws(s + 1, e);
    }
    qsort(ks, (size_t)n, sizeof(Str *), jd_cmp_keys);
    for (int64_t i = 1; i < n && !r.err; i++) {
        if (jd_cmp_keys(&ks[i - 1], &ks[i]) == 0) {
            char what[200];
            snprintf(what, sizeof what, "key \"%.80s\" appears twice", ks[i]->data);
            r.err = jd_fail(j, p, what);
        }
    }
    for (int64_t i = 0; i < n; i++) free(ks[i]);
    free(ks);
    if (!r.err && first < e && *first == '"') r.value = first - j->data;
    return r;
}

Str *pn_jd_key(Str *j, int64_t q) {
    char *k;
    int64_t kn = jd_bad_pos(j, q) ? -1 : jd_unescape(j->data + q, j->data + j->len, &k);
    if (kn < 0) return str_alloc(0);                     // pn_jd_mfirst already refused bad keys
    Str *s = str_from(k, kn);
    free(k);
    return s;
}

int64_t pn_jd_val(Str *j, int64_t q) {
    if (jd_bad_pos(j, q)) return -1;
    const char *e = j->data + j->len, *s = jd_scan_string(j->data + q, e);
    if (!s) return -1;
    s = jd_ws(s, e);
    if (s < e && *s == ':') s++;
    return jd_ws(s, e) - j->data;
}

int64_t pn_jd_mnext(Str *j, int64_t q) {
    int64_t v = pn_jd_val(j, q);
    if (v < 0) return -1;
    const char *e = j->data + j->len, *s = jd_ws(jd_skip(j->data + v, e), e);
    if (s < e && *s == ',') { s = jd_ws(s + 1, e); if (s < e && *s == '"') return s - j->data; }
    return -1;
}
