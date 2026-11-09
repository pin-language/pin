// runtime/text.c — the PHP built-ins that are small helpers over strings, lists and maps: str_replace, trims,
// ucfirst, htmlspecialchars, base64, URL decoding, number_format, round, in_array, array_keys / values / sum,
// max / min of a list. Part of runtime.c (one translation unit: see runtime.c). Every result of a string
// function here is valid UTF-8 when its inputs are (strings.md ST1).

// str_replace($search, $replace, $subject): every match, left to right; an empty $search changes nothing.
Str *pn_str_replace(Str *search, Str *rep, Str *subj) {
    if (search->len == 0) return str_from(subj->data, subj->len);
    Str *d = str_alloc(subj->len);
    const char *p = subj->data, *end = subj->data + subj->len;
    for (;;) {
        const char *m = memmem(p, (size_t)(end - p), search->data, (size_t)search->len);
        if (!m) break;
        d = str_add_bytes(d, p, m - p);
        d = str_add_bytes(d, rep->data, rep->len);
        p = m + search->len;
    }
    return str_add_bytes(d, p, end - p);
}

static int is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == 0; }

// ltrim (side 1) / rtrim (side 2): the same whitespace as trim()
Str *pn_str_trim_side(Str *s, int64_t side) {
    int64_t a = 0, b = s->len;
    if (side == 1) while (a < b && is_space(s->data[a])) a++;
    if (side == 2) while (b > a && is_space(s->data[b - 1])) b--;
    return str_from(s->data + a, b - a);
}

// ucfirst / lcfirst: the first byte if it is an ASCII letter (as PHP 8.2+)
Str *pn_ucfirst(Str *s, int64_t upper) {
    Str *r = str_from(s->data, s->len);
    if (r->len) {
        char c = r->data[0];
        if (upper && c >= 'a' && c <= 'z') r->data[0] = (char)(c - 32);
        if (!upper && c >= 'A' && c <= 'Z') r->data[0] = (char)(c + 32);
    }
    return r;
}

// htmlspecialchars: & < > " ' as entities (PHP 8.1's default ENT_QUOTES | ENT_SUBSTITUTE | ENT_HTML401)
Str *pn_htmlspecialchars(Str *s) {
    Str *d = str_alloc(s->len + 16);
    int64_t run = 0;
    for (int64_t i = 0; i < s->len; i++) {
        const char *e = NULL;
        switch (s->data[i]) {
        case '&': e = "&amp;"; break; case '<': e = "&lt;"; break; case '>': e = "&gt;"; break;
        case '"': e = "&quot;"; break; case '\'': e = "&#039;"; break;
        }
        if (!e) { run++; continue; }
        d = str_add_bytes(d, s->data + i - run, run);
        run = 0;
        d = str_add_bytes(d, e, (int64_t)strlen(e));
    }
    return str_add_bytes(d, s->data + s->len - run, run);
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

Str *pn_base64_encode(Str *s) {
    Str *d = str_alloc(checked_size((s->len + 2) / 3, 4, 0));
    const unsigned char *p = (const unsigned char *)s->data;
    for (int64_t i = 0; i < s->len; i += 3) {
        uint32_t v = (uint32_t)p[i] << 16 | (i + 1 < s->len ? (uint32_t)p[i + 1] << 8 : 0) | (i + 2 < s->len ? p[i + 2] : 0);
        char q[4] = {B64[v >> 18 & 63], B64[v >> 12 & 63], i + 1 < s->len ? B64[v >> 6 & 63] : '=', i + 2 < s->len ? B64[v & 63] : '='};
        d = str_add_bytes(d, q, 4);
    }
    return d;
}

// base64_decode: standard or URL-safe alphabet, padding optional, whitespace skipped. Fails with 400 for any
// other character, and for bytes that are not UTF-8 text (Pin has no binary strings yet: strings.md ST2).
StrOrError pn_base64_decode(Str *s) {
    StrOrError r = {NULL, NULL};
    Str *d = str_alloc(s->len);
    uint32_t acc = 0;
    int bits = 0, pad = 0;
    for (int64_t i = 0; i < s->len; i++) {
        char c = s->data[i];
        int v;
        if (c >= 'A' && c <= 'Z') v = c - 'A';
        else if (c >= 'a' && c <= 'z') v = c - 'a' + 26;
        else if (c >= '0' && c <= '9') v = c - '0' + 52;
        else if (c == '+' || c == '-') v = 62;
        else if (c == '/' || c == '_') v = 63;
        else if (c == '=') { pad++; continue; }
        else if (c == ' ' || c == '\n' || c == '\r' || c == '\t') continue;
        else { pn_str_free(d); r.err = make_error(400, "base64_decode(): not base64 text"); return r; }
        if (pad) { pn_str_free(d); r.err = make_error(400, "base64_decode(): text after the = padding"); return r; }
        acc = acc << 6 | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            char b = (char)(acc >> bits & 0xFF);
            d = str_add_bytes(d, &b, 1);
        }
    }
    if (!utf8_valid(d->data, d->len)) {
        pn_str_free(d);
        r.err = make_error(400, "base64_decode(): the decoded bytes are not UTF-8 text");
        return r;
    }
    r.value = d;
    return r;
}

// rawurldecode (plus = 0) / urldecode (plus = 1, + is a space): %XX decoded, bytes that are not UTF-8 -> U+FFFD
Str *pn_urldecode(Str *s, int64_t plus) {
    Str *d = str_alloc(s->len);
    for (int64_t i = 0; i < s->len; i++) {
        char c = s->data[i];
        if (c == '+' && plus) c = ' ';
        else if (c == '%' && i + 2 < s->len && hexval(s->data[i + 1]) >= 0 && hexval(s->data[i + 2]) >= 0) {
            c = (char)(hexval(s->data[i + 1]) * 16 + hexval(s->data[i + 2]));
            i += 2;
        }
        d = str_add_bytes(d, &c, 1);
    }
    return str_make_valid(d);
}

// round($x, $digits) as PHP: half away from zero, after a check that the decimal value really is a half
// (round(1.955, 2) is 1.96 because 1.955 prints as 1.955, though the double is 1.95499...)
double pn_round(double x, int64_t digits) {
    if (!isfinite(x) || digits > 15 || digits < -15) return x;
    double f = pow(10.0, (double)(digits < 0 ? -digits : digits));
    double y = digits >= 0 ? x * f : x / f;
    char buf[40];
    snprintf(buf, sizeof buf, "%.15g", y);             // the value as it reads in decimal
    double r = round(strtod(buf, NULL));
    return digits >= 0 ? r / f : r * f;
}

// number_format($x, $decimals, $point, $thousands): rounded like round(), digits grouped by three
Str *pn_number_format(double x, int64_t decimals, Str *point, Str *sep) {
    if (decimals < 0) decimals = 0;
    if (decimals > 15) decimals = 15;
    char buf[400];
    double v = pn_round(x, decimals);
    snprintf(buf, sizeof buf, "%.*f", (int)decimals, fabs(v));
    char *dot = strchr(buf, '.');
    int64_t intlen = dot ? dot - buf : (int64_t)strlen(buf);
    Str *d = str_alloc(64);
    if (v < 0 && strspn(buf, "0.") != strlen(buf)) d = str_add_bytes(d, "-", 1);
    for (int64_t i = 0; i < intlen; i++) {
        if (i && (intlen - i) % 3 == 0) d = str_add_bytes(d, sep->data, sep->len);
        d = str_add_bytes(d, buf + i, 1);
    }
    if (dot && decimals) {
        d = str_add_bytes(d, point->data, point->len);
        d = str_add_bytes(d, dot + 1, (int64_t)strlen(dot + 1));
    }
    return d;
}

// in_array($v, $list) for kinds 0 int, 1 float, 2 bool, 3 string (strict, like in_array(..., true))
int64_t pn_list_contains(List *l, uint64_t bits, int64_t kind) {
    for (int64_t i = 0; i < l->len; i++) {
        if (kind == 2) { if (((uint8_t *)l->data)[i] == (uint8_t)bits) return 1; continue; }
        uint64_t x = ((uint64_t *)l->data)[i];
        if (kind == 3) { if (pn_str_eq((Str *)x, (Str *)bits)) return 1; }
        else if (kind == 1) { double a, b; memcpy(&a, &x, 8); memcpy(&b, &bits, 8); if (a == b) return 1; }
        else if (x == bits) return 1;
    }
    return 0;
}

// array_keys($map): int or string keys in insertion order (strings copied)
List *pn_map_keys(Map *m) {
    List *l = pn_list_new(8);
    for (int64_t i = 0; i < m->used; i++) {
        Entry *e = &m->e[i];
        if (!e->alive) continue;
        pn_list_push(l, 8, m->kk == 3 ? (uint64_t)pn_str_dup((Str *)e->key) : e->key);
    }
    return l;
}

// array_values($map) for values of kind 0-3 (bools from 8-byte slots to 1-byte items) or, with copy, owned
// pointers (each value copied by the generated function)
List *pn_map_values(Map *m, CopyFn copy) {
    int64_t es = m->vk == 2 ? 1 : 8;
    List *l = pn_list_new(es);
    for (int64_t i = 0; i < m->used; i++) {
        Entry *e = &m->e[i];
        if (!e->alive) continue;
        uint64_t v = e->val;
        if (m->vk == 3) v = (uint64_t)pn_str_dup((Str *)v);
        else if (copy) v = (uint64_t)copy((void *)v);
        pn_list_push(l, es, m->vk == 2 ? (v != 0) : v);
    }
    return l;
}

// array_sum / max / min over a list<int> (kind 0) or list<float> (kind 1); max and min of [] panic as PHP throws
int64_t pn_list_sum_int(List *l) {
    uint64_t s = 0;                               // wraps like + (int-overflow.md)
    for (int64_t i = 0; i < l->len; i++) s += ((uint64_t *)l->data)[i];
    return (int64_t)s;
}

double pn_list_sum_float(List *l) {
    double s = 0;
    for (int64_t i = 0; i < l->len; i++) s += ((double *)l->data)[i];
    return s;
}

uint64_t pn_list_extreme(List *l, int64_t kind, int64_t want_max) {
    if (!l->len) panic_msg(want_max ? "max() of an empty list" : "min() of an empty list");
    uint64_t best = ((uint64_t *)l->data)[0];
    for (int64_t i = 1; i < l->len; i++) {
        uint64_t x = ((uint64_t *)l->data)[i];
        if (kind == 1) {
            double a, b;
            memcpy(&a, &x, 8); memcpy(&b, &best, 8);
            if (want_max ? a > b : a < b) best = x;
        } else if (want_max ? (int64_t)x > (int64_t)best : (int64_t)x < (int64_t)best) {
            best = x;
        }
    }
    return best;
}
