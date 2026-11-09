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

static uint8_t uni_lookup(uint32_t cp) {
    int lo = 0, hi = (int)(sizeof uni_start / sizeof *uni_start) - 1;
    while (lo < hi) {                             // the last entry whose start is <= cp
        int mid = (lo + hi + 1) / 2;
        if (uni_start[mid] <= cp) lo = mid; else hi = mid - 1;
    }
    return uni_props[lo];
}

// Byte offset where the grapheme cluster that starts at byte i ends (UAX #29 rules GB3-GB13, GB999).
static int64_t grapheme_end(const char *s, int64_t n, int64_t i) {
    int k;
    uint32_t cp = utf8_at(s, i, &k);
    if (cp < 0x80 && i + 1 < n && (unsigned char)s[i + 1] < 0x80 && !(cp == '\r' && s[i + 1] == '\n'))
        return i + 1;                             // ASCII followed by ASCII: always a boundary (not CR LF)
    uint8_t p = uni_lookup(cp);
    int prev = p & 15;
    int ri = prev == GCB_RI;                      // regional indicators in a row (GB12/13: pairs)
    int pict = (p >> 4) & 1;                      // GB11: Extended_Pictographic Extend* ZWJ x Extended_Pictographic
    int pict_zwj = 0;
    int incb = ((p >> 5) & 3) == INCB_CONSONANT ? 1 : 0;   // GB9c: 1 after a consonant, 2 once a linker followed
    for (i += k; i < n; i += k) {
        cp = utf8_at(s, i, &k);
        uint8_t q = uni_lookup(cp);
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
