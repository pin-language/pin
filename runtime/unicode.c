// runtime/unicode.c — counting characters (design/strings.md ST4): mb_* count code points, grapheme_* count what
// people see (UAX #29 extended grapheme clusters). Strings are valid UTF-8 (values.c, ST1), so decoding needs no
// checks. Part of runtime.c (one translation unit: see runtime.c).

#include "unicode_data.h"

enum { GCB_OTHER, GCB_CR, GCB_LF, GCB_CONTROL, GCB_EXTEND, GCB_ZWJ, GCB_RI, GCB_PREPEND, GCB_SPACINGMARK,
       GCB_L, GCB_V, GCB_T, GCB_LV, GCB_LVT };
enum { INCB_NONE, INCB_CONSONANT, INCB_EXTEND, INCB_LINKER };

// The code point at s[i] of a valid UTF-8 string, and its length in *k.
static uint32_t utf8_at(const char *s, int64_t i, int *k) {
    const unsigned char *p = (const unsigned char *)s + i;
    if (p[0] < 0x80) { *k = 1; return p[0]; }
    if (p[0] < 0xE0) { *k = 2; return (uint32_t)(p[0] & 0x1F) << 6 | (p[1] & 0x3F); }
    if (p[0] < 0xF0) { *k = 3; return (uint32_t)(p[0] & 0x0F) << 12 | (uint32_t)(p[1] & 0x3F) << 6 | (p[2] & 0x3F); }
    *k = 4;
    return (uint32_t)(p[0] & 0x07) << 18 | (uint32_t)(p[1] & 0x3F) << 12 | (uint32_t)(p[2] & 0x3F) << 6 | (p[3] & 0x3F);
}

// The index of the last entry of a sorted start[] that is <= cp (start[0] is 0).
static int range_find(const uint32_t *start, int n, uint32_t cp) {
    int lo = 0, hi = n - 1;
    while (lo < hi) {
        int mid = (lo + hi + 1) / 2;
        if (start[mid] <= cp) lo = mid; else hi = mid - 1;
    }
    return lo;
}

static uint16_t uni_lookup(uint32_t cp) {
    return uni_props[range_find(uni_start, (int)(sizeof uni_start / sizeof *uni_start), cp)];
}

// Byte offset where the grapheme cluster that starts at byte i ends (UAX #29 rules GB3-GB13, GB999).
static int64_t grapheme_end(const char *s, int64_t n, int64_t i) {
    int k;
    uint32_t cp = utf8_at(s, i, &k);
    if (cp < 0x80 && i + 1 < n && (unsigned char)s[i + 1] < 0x80 && !(cp == '\r' && s[i + 1] == '\n'))
        return i + 1;                             // ASCII followed by ASCII: always a boundary (not CR LF)
    uint16_t p = uni_lookup(cp);
    int prev = p & 15;
    int ri = prev == GCB_RI;                      // regional indicators in a row (GB12/13: pairs)
    int pict = (p >> 4) & 1;                      // GB11: Extended_Pictographic Extend* ZWJ x Extended_Pictographic
    int pict_zwj = 0;
    int incb = ((p >> 5) & 3) == INCB_CONSONANT ? 1 : 0;   // GB9c: 1 after a consonant, 2 once a linker followed
    for (i += k; i < n; i += k) {
        cp = utf8_at(s, i, &k);
        uint16_t q = uni_lookup(cp);
        int cur = q & 15, cb = (q >> 5) & 3, cpict = (q >> 4) & 1;
        int join;
        if (prev == GCB_CR && cur == GCB_LF) join = 1;                                       // GB3
        else if (prev == GCB_CR || prev == GCB_LF || prev == GCB_CONTROL) join = 0;          // GB4
        else if (cur == GCB_CR || cur == GCB_LF || cur == GCB_CONTROL) join = 0;             // GB5
        else if (prev == GCB_L && (cur == GCB_L || cur == GCB_V || cur == GCB_LV || cur == GCB_LVT)) join = 1;   // GB6
        else if ((prev == GCB_LV || prev == GCB_V) && (cur == GCB_V || cur == GCB_T)) join = 1;  // GB7
        else if ((prev == GCB_LVT || prev == GCB_T) && cur == GCB_T) join = 1;               // GB8
        else if (cur == GCB_EXTEND || cur == GCB_ZWJ || cur == GCB_SPACINGMARK) join = 1;    // GB9, GB9a
        else if (prev == GCB_PREPEND) join = 1;                                              // GB9b
        else if (incb == 2 && cb == INCB_CONSONANT) join = 1;                                // GB9c
        else if (pict_zwj && cpict) join = 1;                                                // GB11
        else if (prev == GCB_RI && cur == GCB_RI) join = ri % 2 == 1;                        // GB12, GB13
        else join = 0;                                                                       // GB999
        if (!join) break;
        // the state the next code point is judged against
        if (cb == INCB_CONSONANT) incb = 1;
        else if (incb && cb == INCB_LINKER) incb = 2;
        else if (incb && cb == INCB_EXTEND) {}
        else incb = 0;
        pict_zwj = pict && cur == GCB_ZWJ;
        if (cpict) pict = 1;
        else if (cur != GCB_EXTEND) pict = 0;
        ri = cur == GCB_RI ? ri + 1 : 0;
        prev = cur;
    }
    return i;
}

int64_t pn_mb_strlen(Str *s) {
    int64_t n = 0;
    for (int64_t i = 0; i < s->len; i++) n += ((unsigned char)s->data[i] & 0xC0) != 0x80;
    return n;
}

int64_t pn_grapheme_strlen(Str *s) {
    int64_t n = 0;
    for (int64_t i = 0; i < s->len; i = grapheme_end(s->data, s->len, i)) n++;
    return n;
}

// Byte offsets of the units (code points or clusters) [start, start+len), with substr()'s rules for negative
// start / length counted in units.
static Str *unit_substr(Str *s, int64_t start, int64_t len, int64_t has_len, int graphemes) {
    int64_t units = graphemes ? pn_grapheme_strlen(s) : pn_mb_strlen(s);
    if (start < 0) { start += units; if (start < 0) start = 0; }
    if (start > units) return str_alloc(0);
    int64_t end = units;
    if (has_len) {
        if (len < 0) { end = units + len; if (end < start) return str_alloc(0); }
        else if (len < units - start) end = start + len;
    }
    int64_t u = 0, i = 0, a = s->len, b = s->len;
    for (;;) {
        if (u == start) a = i;
        if (u == end) { b = i; break; }
        if (i >= s->len) break;
        if (graphemes) i = grapheme_end(s->data, s->len, i);
        else { int k; utf8_at(s->data, i, &k); i += k; }
        u++;
    }
    return str_from(s->data + a, b - a);
}

Str *pn_mb_substr(Str *s, int64_t start, int64_t len, int64_t has_len) { return unit_substr(s, start, len, has_len, 0); }
Str *pn_grapheme_substr(Str *s, int64_t start, int64_t len, int64_t has_len) {
    return unit_substr(s, start, len, has_len, 1);
}

// mb_strpos(): the position of the first needle counted in code points, or -1.
int64_t pn_mb_strpos(Str *h, Str *needle) {
    int64_t at = pn_strpos(h, needle);
    if (at <= 0) return at;
    int64_t n = 0;
    for (int64_t i = 0; i < at; i++) n += ((unsigned char)h->data[i] & 0xC0) != 0x80;
    return n;
}

Str *pn_mb_chr(int64_t cp) {
    if (cp < 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
        fflush(stdout);
        fprintf(stderr, "panic: mb_chr(%lld): not a Unicode character (0 to 0x10FFFF, without the surrogates "
                "0xD800-0xDFFF)\n", (long long)cp);
        die();
    }
    return add_utf8(NULL, (uint32_t)cp);
}

int64_t pn_mb_ord(Str *s) {
    if (!s->len) panic_msg("mb_ord(\"\"): the string is empty");
    int k;
    return utf8_at(s->data, 0, &k);
}

// ---- letter case (ST5): Unicode's full mappings (ß -> SS), and Σ -> ς at the end of a word in lower case

// The index of cp in a sorted code point array, or -1.
static int cp_find(const uint32_t *cps, int n, uint32_t cp) {
    int lo = 0, hi = n - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (cps[mid] == cp) return mid;
        if (cps[mid] < cp) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
}

#define MAP_TEXT(name, i) (name##_text + name##_at[i]), (int64_t)(name##_at[(i) + 1] - name##_at[i])

// Σ is final when a cased letter comes before it and none after it, skipping case-ignorable ones (Unicode 3.13).
static int sigma_final(const char *s, int64_t n, int64_t at, int64_t end) {
    int64_t j = at;
    int found = 0;
    while (j > 0) {
        int64_t k = j - 1;
        while (k > 0 && ((unsigned char)s[k] & 0xC0) == 0x80) k--;
        int len;
        uint16_t p = uni_lookup(utf8_at(s, k, &len));
        if (p & 128) { j = k; continue; }
        found = (p & 256) != 0;
        break;
    }
    if (!found) return 0;
    for (j = end; j < n;) {
        int len;
        uint16_t p = uni_lookup(utf8_at(s, j, &len));
        if (p & 128) { j += len; continue; }
        return !(p & 256);
    }
    return 1;
}

Str *pn_mb_case(Str *s, int64_t upper) {
    Str *d = str_alloc(s->len + 8);
    int64_t run = 0;                              // bytes copied unchanged, added in one go
    for (int64_t i = 0; i < s->len;) {
        unsigned char c = (unsigned char)s->data[i];
        if (c < 0x80) {                           // ASCII: A-Z / a-z only
            char x = (char)c;
            if (upper && c >= 'a' && c <= 'z') x = (char)(c - 32);
            else if (!upper && c >= 'A' && c <= 'Z') x = (char)(c + 32);
            else { run++; i++; continue; }
            d = str_add_bytes(d, s->data + i - run, run); run = 0;
            d = str_add_bytes(d, &x, 1);
            i++;
            continue;
        }
        int k;
        uint32_t cp = utf8_at(s->data, i, &k);
        int idx = upper ? cp_find(upper_cp, (int)(sizeof upper_cp / sizeof *upper_cp), cp)
                        : cp_find(lower_cp, (int)(sizeof lower_cp / sizeof *lower_cp), cp);
        if (idx < 0 && !(cp == 0x3A3 && !upper)) { run += k; i += k; continue; }
        d = str_add_bytes(d, s->data + i - run, run); run = 0;
        if (idx < 0) d = str_add_bytes(d, sigma_final(s->data, s->len, i, i + k) ? "\xCF\x82" : "\xCF\x83", 2);
        else if (upper) d = str_add_bytes(d, MAP_TEXT(upper, idx));
        else d = str_add_bytes(d, MAP_TEXT(lower, idx));
        i += k;
    }
    return str_add_bytes(d, s->data + s->len - run, run);
}

// ---- normalisation (ST6, UAX #15): NFD / NFKD decompose, then NFC / NFKC compose again

enum { HS = 0xAC00, HL = 0x1100, HV = 0x1161, HT = 0x11A7, HLN = 19, HVN = 21, HTN = 28, HN = HVN * HTN, HCOUNT = HLN * HN };

static int ccc_of(uint32_t cp) {
    if (cp < 0x300) return 0;
    return ccc_value[range_find(ccc_start, (int)(sizeof ccc_start / sizeof *ccc_start), cp)];
}

typedef struct { uint32_t *v; int64_t n, cap; } CpBuf;

static void cp_push(CpBuf *b, uint32_t cp) {
    if (b->n == b->cap) {
        b->cap = b->cap ? b->cap * 2 : 64;
        b->v = xrealloc(b->v, checked_size(b->cap, 4, 0));
    }
    b->v[b->n++] = cp;
}

static void decompose(CpBuf *b, uint32_t cp, int compat) {
    if (cp >= HS && cp < HS + HCOUNT) {           // Hangul syllables by arithmetic
        uint32_t i = cp - HS;
        cp_push(b, HL + i / HN);
        cp_push(b, HV + (i % HN) / HTN);
        if (i % HTN) cp_push(b, HT + i % HTN);
        return;
    }
    int idx = compat ? cp_find(nfkd_cp, (int)(sizeof nfkd_cp / sizeof *nfkd_cp), cp)
                     : cp_find(nfd_cp, (int)(sizeof nfd_cp / sizeof *nfd_cp), cp);
    if (idx < 0) { cp_push(b, cp); return; }
    const char *t = compat ? nfkd_text + nfkd_at[idx] : nfd_text + nfd_at[idx];
    int64_t len = compat ? nfkd_at[idx + 1] - nfkd_at[idx] : nfd_at[idx + 1] - nfd_at[idx];
    for (int64_t j = 0; j < len;) {               // already fully decomposed by the generator
        int k;
        cp_push(b, utf8_at(t, j, &k));
        j += k;
    }
}

static uint32_t compose_pair(uint32_t a, uint32_t b) {
    if (a >= HL && a < HL + HLN && b >= HV && b < HV + HVN) return HS + ((a - HL) * HVN + (b - HV)) * HTN;
    if (a >= HS && a < HS + HCOUNT && (a - HS) % HTN == 0 && b > HT && b < HT + HTN) return a + (b - HT);
    uint64_t key = (uint64_t)a << 21 | b;
    int lo = 0, hi = (int)(sizeof comp_key / sizeof *comp_key) - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (comp_key[mid] == key) return comp_value[mid];
        if (comp_key[mid] < key) lo = mid + 1; else hi = mid - 1;
    }
    return 0;
}

// normalizer_normalize($s, Normalizer::FORM_C / FORM_D / FORM_KC / FORM_KD): PHP's numbers 16, 4, 32, 8.
Str *pn_normalize(Str *s, int64_t form) {
    if (form != 4 && form != 8 && form != 16 && form != 32) {
        fflush(stdout);
        fprintf(stderr, "panic: normalizer_normalize(): form %lld is not Normalizer::FORM_C, FORM_D, FORM_KC or "
                "FORM_KD\n", (long long)form);
        die();
    }
    int64_t i = 0;
    while (i < s->len && (unsigned char)s->data[i] < 0x80) i++;
    if (i == s->len) return str_from(s->data, s->len);   // ASCII is the same in every form
    int compat = form == 8 || form == 32, compose = form == 16 || form == 32;
    CpBuf b = {0};
    for (i = 0; i < s->len;) {
        int k;
        decompose(&b, utf8_at(s->data, i, &k), compat);
        i += k;
    }
    for (int64_t j = 1; j < b.n; j++) {           // canonical order: sort each run of marks by class, stably
        int cj = ccc_of(b.v[j]);
        if (!cj) continue;
        for (int64_t m = j; m > 0 && ccc_of(b.v[m - 1]) > cj; m--) {
            uint32_t t = b.v[m]; b.v[m] = b.v[m - 1]; b.v[m - 1] = t;
        }
    }
    if (compose && b.n) {                         // the composition algorithm of UAX #15 (blocked marks stay)
        int64_t starter = 0, w = 1;
        int last = ccc_of(b.v[0]) ? 256 : 0;
        for (int64_t j = 1; j < b.n; j++) {
            uint32_t cp = b.v[j];
            int cc = ccc_of(cp);
            uint32_t c = compose_pair(b.v[starter], cp);
            if (c && (last < cc || last == 0)) { b.v[starter] = c; continue; }
            if (!cc) starter = w;
            last = cc;
            b.v[w++] = cp;
        }
        b.n = w;
    }
    Str *d = str_alloc(s->len + 8);
    for (int64_t j = 0; j < b.n; j++) d = add_utf8(d, b.v[j]);
    xfree(b.v);
    return d;
}
