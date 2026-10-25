// runtime/request.c — reading requests (query, form, JSON top level) and printing. Part of runtime.c (one translation unit: see runtime.c).

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
// The value under top-level `key` of a JSON object. The key "" means the text itself is the value (an array
// item from json_at(), or any value from json_raw()).
static int jfind(Str *json, Str *key, const char **vs, const char **ve) {
    const char *p = json->data, *e = json->data + json->len;
    p = jws(p, e);
    if (key->len == 0) {
        const char *end = p < e ? jskip(p, e, 0) : NULL;
        if (!end || jws(end, e) != e) return 0;
        *vs = p; *ve = end;
        return 1;
    }
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

// Nested JSON: json_raw() hands back the JSON text of one value, which the json_* functions read again.
Str *pn_jget_raw(Str *json, Str *key) {
    const char *s, *e;
    if (!jfind(json, key, &s, &e)) return str_alloc(0);
    return str_from(s, e - s);
}

// Walk the items of a top-level array: calls back with each item's span; returns the count, or -1.
static int64_t jarray(Str *json, int64_t want, const char **is, const char **ie) {
    const char *p = jws(json->data, json->data + json->len), *e = json->data + json->len;
    if (p >= e || *p != '[') return -1;
    p = jws(p + 1, e);
    if (p < e && *p == ']') return jws(p + 1, e) == e ? 0 : -1;
    for (int64_t n = 0;; n++) {
        const char *v = jws(p, e), *vend = jskip(v, e, 0);
        if (!vend) return -1;
        if (n == want) { *is = v; *ie = vend; }
        p = jws(vend, e);
        if (p < e && *p == ']') return jws(p + 1, e) == e ? n + 1 : -1;
        if (p >= e || *p != ',') return -1;
        p++;
    }
}

int64_t pn_jget_len(Str *json) {
    const char *s, *e;
    int64_t n = jarray(json, -1, &s, &e);
    if (n >= 0) return n;
    const char *p = jws(json->data, json->data + json->len), *end = json->data + json->len;
    if (p >= end || *p != '{') return -1;               // an object: count its keys
    p = jws(p + 1, end);
    if (p < end && *p == '}') return 0;
    for (int64_t k = 1;; k++) {
        const char *kq = jstr_end(jws(p, end), end);
        if (!kq) return -1;
        p = jws(kq, end);
        if (p >= end || *p != ':') return -1;
        const char *vend = jskip(jws(p + 1, end), end, 0);
        if (!vend) return -1;
        p = jws(vend, end);
        if (p < end && *p == '}') return k;
        if (p >= end || *p != ',') return -1;
        p++;
    }
}

Str *pn_jget_at(Str *json, int64_t i) {
    const char *s = NULL, *e = NULL;
    int64_t n = jarray(json, i, &s, &e);
    if (n < 0 || i < 0 || i >= n || !s) return str_alloc(0);
    return str_from(s, e - s);
}

// ============================================================================ printing

void pn_print_int(int64_t v) { printf("%lld", (long long)v); }
void pn_print_str(Str *s) { fwrite(s->data, 1, (size_t)s->len, stdout); }
void pn_print_float(double v) {
    char buf[48];
    fmt_float(buf, sizeof buf, v);
    fputs(buf, stdout);
}
