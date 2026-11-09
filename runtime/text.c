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

// ---- sprintf (the compiler splits the literal format and calls this once per conversion)
// conv: 's' 'd' 'u' 'f' 'F' 'e' 'x' 'X' 'o' 'b' 'c'; flags: 1 '-' (left), 2 '0' (zeros), 4 '+', 8 ' ', 16 has a
// precision. bits: the Str * for 's', the double's bits for 'f' 'F' 'e', the int otherwise. Width and precision
// count bytes as in PHP, but %.Ns never cuts a character in half.
Str *pn_fmt(Str *d, int64_t conv, int64_t flags, int64_t width, int64_t prec, uint64_t bits) {
    char buf[512], spec[32];
    const char *p = buf;
    int64_t n;
    if (width > 4096) width = 4096;
    if (prec > 100) prec = 100;
    if (conv == 's') {
        Str *s = (Str *)bits;
        n = s->len;
        if ((flags & 16) && prec < n) {
            n = prec;
            while (n > 0 && n < s->len && ((unsigned char)s->data[n] & 0xC0) == 0x80) n--;   // a whole character
        }
        p = s->data;
    } else if (conv == 'b') {
        uint64_t v = bits;
        char t[65];
        int k = 0;
        do { t[k++] = (char)('0' + (v & 1)); v >>= 1; } while (v);
        for (int i = 0; i < k; i++) buf[i] = t[k - 1 - i];
        n = k;
    } else if (conv == 'c') {
        Str *c = add_utf8(NULL, (uint32_t)((int64_t)bits < 0 || bits > 0x10FFFF || (bits >= 0xD800 && bits <= 0xDFFF) ? 0xFFFD : bits));
        d = str_add_bytes(d, c->data, c->len);
        pn_str_free(c);
        return d;
    } else {
        int k = 0;
        spec[k++] = '%';
        if (flags & 4) spec[k++] = '+';
        if (flags & 8) spec[k++] = ' ';
        if (flags & 16) k += snprintf(spec + k, sizeof spec - (size_t)k, ".%d", (int)prec);
        if (conv == 'f' || conv == 'F' || conv == 'e') {
            double v;
            memcpy(&v, &bits, 8);
            if (!(flags & 16) && conv != 'e') k += snprintf(spec + k, sizeof spec - (size_t)k, ".6");
            spec[k++] = conv == 'F' ? 'f' : (char)conv;
            spec[k] = 0;
            n = snprintf(buf, sizeof buf, spec, v);
            char *x = conv == 'e' ? strchr(buf, 'e') : NULL;   // PHP writes 1.5e+3, C 1.5e+03
            if (x && (x[1] == '+' || x[1] == '-')) {
                char *z = x + 2;
                while (z[0] == '0' && z[1]) z++;
                memmove(x + 2, z, strlen(z) + 1);
                n = (int64_t)strlen(buf);
            }
        } else {
            if (conv == 'd') { spec[k++] = 'l'; spec[k++] = 'l'; spec[k++] = 'd'; }
            else { spec[k++] = 'l'; spec[k++] = 'l'; spec[k++] = conv == 'u' ? 'u' : (char)conv; }
            spec[k] = 0;
            n = snprintf(buf, sizeof buf, spec, (long long)bits);
        }
        if (n < 0) n = 0;
        if (n >= (int64_t)sizeof buf) n = sizeof buf - 1;
    }
    int64_t pad = width > n ? width - n : 0;
    if (!(flags & 1) && pad) {
        int zero = (flags & 2) && conv != 's';
        if (zero && n && (p[0] == '-' || p[0] == '+' || p[0] == ' ')) { d = str_add_bytes(d, p, 1); p++; n--; }   // -0005
        for (int64_t i = 0; i < pad; i++) d = str_add_bytes(d, zero ? "0" : " ", 1);
    }
    d = str_add_bytes(d, p, n);
    if ((flags & 1) && pad) for (int64_t i = 0; i < pad; i++) d = str_add_bytes(d, " ", 1);
    return d;
}

// ---- date($format, $timestamp): PHP's format letters, always in UTC (Pin has no time zones yet)
Str *pn_date(Str *fmt, int64_t ts) {
    static const char *days[] = {"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"};
    static const char *months[] = {"January", "February", "March", "April", "May", "June", "July", "August",
                                   "September", "October", "November", "December"};
    time_t t = (time_t)ts;
    struct tm tm;
    gmtime_r(&t, &tm);
    Str *d = str_alloc(32);
    char b[64];
    for (int64_t i = 0; i < fmt->len; i++) {
        char c = fmt->data[i];
        int n = -1;
        switch (c) {
        case 'd': n = snprintf(b, sizeof b, "%02d", tm.tm_mday); break;
        case 'j': n = snprintf(b, sizeof b, "%d", tm.tm_mday); break;
        case 'S': { int m = tm.tm_mday;
                    n = snprintf(b, sizeof b, "%s", m % 10 == 1 && m != 11 ? "st" : m % 10 == 2 && m != 12 ? "nd" :
                                                    m % 10 == 3 && m != 13 ? "rd" : "th"); break; }
        case 'D': n = snprintf(b, sizeof b, "%.3s", days[tm.tm_wday]); break;
        case 'l': n = snprintf(b, sizeof b, "%s", days[tm.tm_wday]); break;
        case 'N': n = snprintf(b, sizeof b, "%d", tm.tm_wday ? tm.tm_wday : 7); break;
        case 'w': n = snprintf(b, sizeof b, "%d", tm.tm_wday); break;
        case 'z': n = snprintf(b, sizeof b, "%d", tm.tm_yday); break;
        case 'F': n = snprintf(b, sizeof b, "%s", months[tm.tm_mon]); break;
        case 'M': n = snprintf(b, sizeof b, "%.3s", months[tm.tm_mon]); break;
        case 'm': n = snprintf(b, sizeof b, "%02d", tm.tm_mon + 1); break;
        case 'n': n = snprintf(b, sizeof b, "%d", tm.tm_mon + 1); break;
        case 't': { static const int md[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
                    int y = tm.tm_year + 1900, leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
                    n = snprintf(b, sizeof b, "%d", md[tm.tm_mon] + (tm.tm_mon == 1 && leap)); break; }
        case 'L': { int y = tm.tm_year + 1900; n = snprintf(b, sizeof b, "%d", (y % 4 == 0 && y % 100 != 0) || y % 400 == 0); break; }
        case 'Y': n = snprintf(b, sizeof b, "%d", tm.tm_year + 1900); break;
        case 'y': n = snprintf(b, sizeof b, "%02d", (tm.tm_year + 1900) % 100); break;
        case 'a': n = snprintf(b, sizeof b, "%s", tm.tm_hour < 12 ? "am" : "pm"); break;
        case 'A': n = snprintf(b, sizeof b, "%s", tm.tm_hour < 12 ? "AM" : "PM"); break;
        case 'g': n = snprintf(b, sizeof b, "%d", tm.tm_hour % 12 ? tm.tm_hour % 12 : 12); break;
        case 'h': n = snprintf(b, sizeof b, "%02d", tm.tm_hour % 12 ? tm.tm_hour % 12 : 12); break;
        case 'G': n = snprintf(b, sizeof b, "%d", tm.tm_hour); break;
        case 'H': n = snprintf(b, sizeof b, "%02d", tm.tm_hour); break;
        case 'i': n = snprintf(b, sizeof b, "%02d", tm.tm_min); break;
        case 's': n = snprintf(b, sizeof b, "%02d", tm.tm_sec); break;
        case 'v': n = snprintf(b, sizeof b, "000"); break;
        case 'u': n = snprintf(b, sizeof b, "000000"); break;
        case 'e': case 'T': n = snprintf(b, sizeof b, "UTC"); break;
        case 'P': n = snprintf(b, sizeof b, "+00:00"); break;
        case 'p': n = snprintf(b, sizeof b, "Z"); break;
        case 'O': n = snprintf(b, sizeof b, "+0000"); break;
        case 'Z': n = snprintf(b, sizeof b, "0"); break;
        case 'U': n = snprintf(b, sizeof b, "%lld", (long long)ts); break;
        case 'c': n = (int)strftime(b, sizeof b, "%Y-%m-%dT%H:%M:%S+00:00", &tm); break;
        case 'r': n = (int)strftime(b, sizeof b, "%a, %d %b %Y %H:%M:%S +0000", &tm); break;
        case '\\': if (i + 1 < fmt->len) { i++; d = str_add_bytes(d, fmt->data + i, 1); } continue;
        }
        if (n < 0) d = str_add_bytes(d, &c, 1);
        else d = str_add_bytes(d, b, n);
    }
    return d;
}

// ---- list helpers that copy items: copy is the generated per-item copier for strings, lists, maps and objects,
// NULL for numbers and bools (copied as bytes)
static void list_put(List *dst, int64_t esize, const char *item, CopyFn copy) {
    if (copy) { void *v = copy(*(void **)item); pn_list_push(dst, 8, (uint64_t)v); return; }
    memcpy(pn_list_push_slot(dst, esize), item, (size_t)esize);
}

// array_slice($xs, $offset, $length): substr()'s rules for negative values, in items
List *pn_list_slice(List *l, int64_t esize, int64_t off, int64_t len, int64_t has_len, CopyFn copy) {
    int64_t n = l->len;
    if (off < 0) { off += n; if (off < 0) off = 0; }
    if (off > n) off = n;
    int64_t end = n;
    if (has_len) {
        if (len < 0) end = n + len < off ? off : n + len;
        else if (len < n - off) end = off + len;
    }
    List *r = pn_list_new(esize);
    for (int64_t i = off; i < end; i++) list_put(r, esize, (char *)l->data + i * esize, copy);
    return r;
}

List *pn_list_merge(List *a, List *b, int64_t esize, CopyFn copy) {
    List *r = pn_list_new(esize);
    for (int64_t i = 0; i < a->len; i++) list_put(r, esize, (char *)a->data + i * esize, copy);
    for (int64_t i = 0; i < b->len; i++) list_put(r, esize, (char *)b->data + i * esize, copy);
    return r;
}

List *pn_list_reverse(List *l, int64_t esize, CopyFn copy) {
    List *r = pn_list_new(esize);
    for (int64_t i = l->len - 1; i >= 0; i--) list_put(r, esize, (char *)l->data + i * esize, copy);
    return r;
}

// array_search($v, $xs): the first index, or -1 (kinds as in_array)
int64_t pn_list_index(List *l, uint64_t bits, int64_t kind) {
    for (int64_t i = 0; i < l->len; i++) {
        if (kind == 2) { if (((uint8_t *)l->data)[i] == (uint8_t)bits) return i; continue; }
        uint64_t x = ((uint64_t *)l->data)[i];
        if (kind == 3) { if (pn_str_eq((Str *)x, (Str *)bits)) return i; }
        else if (kind == 1) { double a, b; memcpy(&a, &x, 8); memcpy(&b, &bits, 8); if (a == b) return i; }
        else if (x == bits) return i;
    }
    return -1;
}

// array_unique($xs): first occurrences, in order (int and string lists through a map, so it stays O(n))
List *pn_list_unique(List *l, int64_t kind) {
    int64_t es = kind == 2 ? 1 : 8;
    List *r = pn_list_new(es);
    Map *seen = kind == 0 || kind == 3 ? pn_map_new(kind, 0) : NULL;
    for (int64_t i = 0; i < l->len; i++) {
        uint64_t x = 0;
        memcpy(&x, (char *)l->data + i * es, (size_t)es);
        if (seen) {
            if (pn_map_has(seen, x)) continue;
            pn_map_slot(seen, x, 1);
        } else if (pn_list_index(r, x, kind) >= 0) {
            continue;
        }
        pn_list_push(r, es, kind == 3 ? (uint64_t)pn_str_dup((Str *)x) : x);
    }
    if (seen) pn_map_free(seen);
    return r;
}

// is_numeric($s) as PHP 8: optional whitespace, a sign, digits with one optional point, an optional exponent,
// optional trailing whitespace
int64_t pn_is_numeric(Str *s) {
    const char *p = s->data, *e = s->data + s->len;
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == '\v' || *p == '\f')) p++;
    if (p < e && (*p == '+' || *p == '-')) p++;
    int digits = 0;
    while (p < e && *p >= '0' && *p <= '9') { p++; digits++; }
    if (p < e && *p == '.') { p++; while (p < e && *p >= '0' && *p <= '9') { p++; digits++; } }
    if (!digits) return 0;
    if (p < e && (*p == 'e' || *p == 'E')) {
        const char *q = p + 1;
        if (q < e && (*q == '+' || *q == '-')) q++;
        if (q < e && *q >= '0' && *q <= '9') { while (q < e && *q >= '0' && *q <= '9') q++; p = q; }
        else return 0;
    }
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == '\v' || *p == '\f')) p++;
    return p == e;
}

// str_pad($s, $length, $pad, $type): STR_PAD_RIGHT 1, LEFT 0, BOTH 2; lengths in bytes as PHP, but only whole
// characters of $pad are added
Str *pn_str_pad(Str *s, int64_t len, Str *pad, int64_t type) {
    if (len > ((int64_t)1 << 30)) panic_msg("str_pad(): length too large");
    int64_t need = len - s->len;
    if (need <= 0 || pad->len == 0) return str_from(s->data, s->len);
    int64_t left = type == 0 ? need : type == 2 ? need / 2 : 0, right = need - left;
    Str *d = str_alloc(len);
    for (int side = 0; side < 2; side++) {
        int64_t want = side ? right : left, got = 0, i = 0;
        while (got < want) {
            int k;
            utf8_at(pad->data, i, &k);
            if (got + k > want) break;
            d = str_add_bytes(d, pad->data + i, k);
            got += k;
            i = i + k >= pad->len ? 0 : i + k;
        }
        if (!side) d = str_add_bytes(d, s->data, s->len);
    }
    return d;
}

// ucwords: the first letter of every word (after a space, tab or newline) in upper case, ASCII a-z
Str *pn_ucwords(Str *s) {
    Str *r = str_from(s->data, s->len);
    for (int64_t i = 0; i < r->len; i++) {
        char c = r->data[i];
        if ((i == 0 || strchr(" \t\r\n\f\v", r->data[i - 1])) && c >= 'a' && c <= 'z') r->data[i] = (char)(c - 32);
    }
    return r;
}

// strrev: the characters as a reader sees them, in reverse (PHP reverses bytes, which breaks UTF-8 and Thai marks)
Str *pn_strrev(Str *s) {
    Str *d = str_alloc(s->len);
    d->len = s->len;
    d->data[s->len] = 0;
    for (int64_t i = 0; i < s->len;) {
        int64_t j = grapheme_end(s->data, s->len, i);
        memcpy(d->data + (s->len - j), s->data + i, (size_t)(j - i));
        i = j;
    }
    return d;
}

// json_encode($v, JSON_PRETTY_PRINT): PHP's layout (4 spaces, ": ", empty [] and {} kept on one line) from the
// compact text, which is valid JSON already
Str *pn_json_pretty(Str *j) {
    Str *d = str_alloc(j->len * 2 + 16);
    int depth = 0;
    for (int64_t i = 0; i < j->len; i++) {
        char c = j->data[i];
        if (c == '"') {                                // copy a string as it is, escapes included
            int64_t k = i + 1;
            while (k < j->len && j->data[k] != '"') k += j->data[k] == '\\' ? 2 : 1;
            d = str_add_bytes(d, j->data + i, k - i + 1);
            i = k;
            continue;
        }
        if ((c == '[' || c == '{') && i + 1 < j->len && (j->data[i + 1] == ']' || j->data[i + 1] == '}')) {
            d = str_add_bytes(d, j->data + i, 2);
            i++;
            continue;
        }
        if (c == '[' || c == '{' || c == ',') {
            d = str_add_bytes(d, &c, 1);
            if (c != ',') depth++;
            d = str_add_bytes(d, "\n", 1);
            for (int k = 0; k < depth; k++) d = str_add_bytes(d, "    ", 4);
        } else if (c == ']' || c == '}') {
            depth--;
            d = str_add_bytes(d, "\n", 1);
            for (int k = 0; k < depth; k++) d = str_add_bytes(d, "    ", 4);
            d = str_add_bytes(d, &c, 1);
        } else if (c == ':') {
            d = str_add_bytes(d, ": ", 2);
        } else {
            d = str_add_bytes(d, &c, 1);
        }
    }
    return d;
}

// array_shift: drop the first item (the compiler has read it already); the rest moves down
void pn_list_drop_first(List *l, int64_t esize) {
    memmove(l->data, (char *)l->data + esize, (size_t)((l->len - 1) * esize));
    l->len--;
}

// usort($xs, "cmp"): a stable merge sort of 8-byte items with the program's comparison function (through a
// generated wrapper that takes and gives 64-bit words); a panic inside it leaves through the request as usual
typedef int64_t (*CmpFn)(uint64_t, uint64_t);

void pn_list_usort(List *l, CmpFn cmp) {
    int64_t n = l->len;
    if (n < 2) return;
    uint64_t *a = l->data, *tmp = xmalloc(checked_size(n, 8, 0));
    for (int64_t w = 1; w < n; w *= 2) {
        for (int64_t lo = 0; lo < n; lo += 2 * w) {
            int64_t mid = lo + w < n ? lo + w : n, hi = lo + 2 * w < n ? lo + 2 * w : n, i = lo, j = mid, k = lo;
            while (i < mid && j < hi) tmp[k++] = cmp(a[j], a[i]) < 0 ? a[j++] : a[i++];   // ties keep order
            while (i < mid) tmp[k++] = a[i++];
            while (j < hi) tmp[k++] = a[j++];
        }
        memcpy(a, tmp, (size_t)n * 8);
    }
    xfree(tmp);
}

// ---- ksort / krsort / asort / arsort: reorder a map's entries (stable, as PHP 8) and rebuild its index.
// by_value 0: by key (kind m->kk), 1: by value (kind m->vk: 0 int, 1 float, 2 bool, 3 string)
static int entry_cmp(const Entry *a, const Entry *b, int kind, int by_value) {
    uint64_t x = by_value ? a->val : a->key, y = by_value ? b->val : b->key;
    if (kind == 3) return (int)pn_str_cmp((Str *)x, (Str *)y);
    if (kind == 1) { double p, q; memcpy(&p, &x, 8); memcpy(&q, &y, 8); return p < q ? -1 : p > q ? 1 : 0; }
    if (kind == 2) return (x != 0) - (y != 0);
    return (int64_t)x < (int64_t)y ? -1 : (int64_t)x > (int64_t)y;
}

void pn_map_sort(Map *m, int64_t by_value, int64_t reverse) {
    int64_t w = 0;
    for (int64_t i = 0; i < m->used; i++) if (m->e[i].alive) m->e[w++] = m->e[i];   // drop deleted entries
    m->used = w;
    int kind = by_value ? m->vk : m->kk;
    Entry *a = m->e, *tmp = xmalloc(checked_size(w, (int64_t)sizeof(Entry), 0));
    for (int64_t step = 1; step < w; step *= 2) {
        for (int64_t lo = 0; lo < w; lo += 2 * step) {
            int64_t mid = lo + step < w ? lo + step : w, hi = lo + 2 * step < w ? lo + 2 * step : w;
            int64_t i = lo, j = mid, k = lo;
            while (i < mid && j < hi) {
                int c = entry_cmp(&a[j], &a[i], kind, (int)by_value);
                tmp[k++] = (reverse ? c > 0 : c < 0) ? a[j++] : a[i++];
            }
            while (i < mid) tmp[k++] = a[i++];
            while (j < hi) tmp[k++] = a[j++];
        }
        memcpy(a, tmp, (size_t)w * sizeof(Entry));
    }
    xfree(tmp);
    map_reindex(m);
}

// range($from, $to, $step): ints, up or down as PHP; a step of 0 panics
List *pn_range(int64_t from, int64_t to, int64_t step) {
    if (step == 0) panic_msg("range(): the step cannot be 0");
    uint64_t s = step < 0 ? (uint64_t)0 - (uint64_t)step : (uint64_t)step;
    uint64_t span = from <= to ? (uint64_t)to - (uint64_t)from : (uint64_t)from - (uint64_t)to;
    uint64_t n = span / s + 1;
    if (n > ((uint64_t)1 << 32)) panic_msg("range(): too many items");
    List *l = pn_list_new(8);
    for (uint64_t i = 0; i < n; i++) {
        uint64_t v = from <= to ? (uint64_t)from + i * s : (uint64_t)from - i * s;
        pn_list_push(l, 8, v);
    }
    return l;
}

// array_count_values($xs): how many times each int (kind 0) or string (kind 3) occurs, in first-seen order
Map *pn_count_values(List *l, int64_t kind) {
    Map *m = pn_map_new(kind, 0);
    for (int64_t i = 0; i < l->len; i++) {
        uint64_t *slot = pn_map_slot(m, ((uint64_t *)l->data)[i], 1);
        (*slot)++;
    }
    return m;
}

// array_flip($m): values become keys (int or string values), later duplicates win as in PHP
Map *pn_map_flip(Map *m) {
    Map *r = pn_map_new(m->vk, m->kk);
    for (int64_t i = 0; i < m->used; i++) {
        Entry *e = &m->e[i];
        if (!e->alive) continue;
        uint64_t *slot = pn_map_slot(r, e->val, 1);
        if (r->vk == 3 && *slot) pn_str_free((Str *)*slot);
        *slot = m->kk == 3 ? (uint64_t)pn_str_dup((Str *)e->key) : e->key;
    }
    return r;
}

// array_combine($keys, $values): kinds kk (0 / 3) and vk (0-3); different lengths panic (PHP throws)
Map *pn_map_combine(List *k, List *v, int64_t kk, int64_t vk) {
    if (k->len != v->len) panic_msg("array_combine(): the two lists have different lengths");
    Map *m = pn_map_new(kk, vk);
    for (int64_t i = 0; i < k->len; i++) {
        uint64_t *slot = pn_map_slot(m, ((uint64_t *)k->data)[i], 1);
        uint64_t x = 0;
        if (vk == 2) x = ((uint8_t *)v->data)[i]; else x = ((uint64_t *)v->data)[i];
        if (vk == 3) { if (*slot) pn_str_free((Str *)*slot); x = (uint64_t)pn_str_dup((Str *)x); }
        *slot = x;
    }
    return m;
}

// mb_str_split($s, $n): pieces of $n characters (code points)
List *pn_mb_str_split(Str *s, int64_t n) {
    if (n < 1) panic_msg("mb_str_split(): the length must be at least 1");
    List *l = pn_list_new(8);
    int64_t i = 0;
    while (i < s->len) {
        int64_t j = i;
        for (int64_t c = 0; c < n && j < s->len; c++) { int k; utf8_at(s->data, j, &k); j += k; }
        pn_list_push(l, 8, (uint64_t)str_from(s->data + i, j - i));
        i = j;
    }
    return l;
}

// pow / ** for ints: exact while it fits (wrapping past 64 bits, like *); a negative exponent panics, since
// the result would be a fraction (write a float base: 2.0 ** -1)
int64_t pn_ipow(int64_t b, int64_t e, int32_t line) {
    if (e < 0) {
        fflush(stdout);
        fprintf(stderr, "panic: int ** negative exponent on line %d gives a fraction: write the base as a float (2.0 ** -1)\n", line);
        die();
    }
    uint64_t r = 1, x = (uint64_t)b;
    while (e) { if (e & 1) r *= x; x *= x; e >>= 1; }
    return (int64_t)r;
}
