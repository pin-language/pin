// runtime/crypto.c — hashing, secure random numbers, passwords (design P5). Part of runtime.c (one translation unit).
//
// SHA-256 (FIPS 180-4), HMAC-SHA256 (RFC 2104) and PBKDF2-HMAC-SHA256 (RFC 8018), written here so programs need
// no extra library; checked against NIST / RFC 4231 / RFC 7914 vectors and Python's hashlib (tests/crypto.pn).
// Random bytes come from the operating system (getentropy: macOS, Linux glibc 2.25+).

#include <sys/random.h>
#if defined(__x86_64__)
#include <cpuid.h>
#include <immintrin.h>
#endif
#if defined(__aarch64__)
#include <arm_neon.h>
#if defined(__linux__)
#include <sys/auxv.h>
#ifndef HWCAP_SHA2
#define HWCAP_SHA2 (1 << 6)
#endif
#endif
#endif

typedef struct { uint32_t h[8]; uint64_t n; uint8_t buf[64]; size_t used; } Sha256;

static const uint32_t SHA_K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha_block_c(Sha256 *s, const uint8_t *p) {
    uint32_t w[64], a, b, c, d, e, f, g, h;
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3]; e = s->h[4]; f = s->h[5]; g = s->h[6]; h = s->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + SHA_K[i] + w[i];
        uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

#if defined(__aarch64__)
// The same block with the CPU's SHA-256 instructions (ARMv8 FEAT_SHA2): ~13x faster on an Apple M2, which
// makes password_hash ~0.04 s instead of ~0.5 s. Checked against sha_block_c on 100,000 random blocks.
__attribute__((target("sha2")))
static void sha_block_arm(Sha256 *s, const uint8_t *p) {
    uint32x4_t s0 = vld1q_u32(s->h), s1 = vld1q_u32(s->h + 4), a0 = s0, a1 = s1;
    uint32x4_t m0 = vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(p)));
    uint32x4_t m1 = vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(p + 16)));
    uint32x4_t m2 = vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(p + 32)));
    uint32x4_t m3 = vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(p + 48)));
    for (int i = 0; i < 16; i++) {
        uint32x4_t w = vaddq_u32(m0, vld1q_u32(SHA_K + 4 * i)), t = s0;
        s0 = vsha256hq_u32(s0, s1, w);
        s1 = vsha256h2q_u32(s1, t, w);
        uint32x4_t n = i < 12 ? vsha256su1q_u32(vsha256su0q_u32(m0, m1), m2, m3) : m0;
        m0 = m1; m1 = m2; m2 = m3; m3 = n;
    }
    vst1q_u32(s->h, vaddq_u32(s0, a0));
    vst1q_u32(s->h + 4, vaddq_u32(s1, a1));
}
#endif

#if defined(__x86_64__)
// The same block with the x86 SHA extensions (Intel since Goldmont / Ice Lake, AMD since Zen): the state is kept
// as ABEF / CDGH, as the instructions want it. Checked by tests/crypto.pn's vectors on CI's x86 runners.
__attribute__((target("sha,sse4.1,ssse3")))
static void sha_block_x86(Sha256 *s, const uint8_t *p) {
    const __m128i swap = _mm_set_epi64x(0x0c0d0e0f08090a0bULL, 0x0405060700010203ULL);   // big-endian words
    __m128i t = _mm_shuffle_epi32(_mm_loadu_si128((const __m128i *)s->h), 0xB1);          // CDAB
    __m128i s1 = _mm_shuffle_epi32(_mm_loadu_si128((const __m128i *)(s->h + 4)), 0x1B);   // EFGH
    __m128i s0 = _mm_alignr_epi8(t, s1, 8);                                               // ABEF
    s1 = _mm_blend_epi16(s1, t, 0xF0);                                                    // CDGH
    __m128i a0 = s0, a1 = s1;
    __m128i m0 = _mm_shuffle_epi8(_mm_loadu_si128((const __m128i *)p), swap);
    __m128i m1 = _mm_shuffle_epi8(_mm_loadu_si128((const __m128i *)(p + 16)), swap);
    __m128i m2 = _mm_shuffle_epi8(_mm_loadu_si128((const __m128i *)(p + 32)), swap);
    __m128i m3 = _mm_shuffle_epi8(_mm_loadu_si128((const __m128i *)(p + 48)), swap);
    for (int i = 0; i < 16; i++) {
        __m128i w = _mm_add_epi32(m0, _mm_loadu_si128((const __m128i *)(SHA_K + 4 * i)));
        s1 = _mm_sha256rnds2_epu32(s1, s0, w);
        s0 = _mm_sha256rnds2_epu32(s0, s1, _mm_shuffle_epi32(w, 0x0E));
        __m128i n = m0;
        if (i < 12)
            n = _mm_sha256msg2_epu32(_mm_add_epi32(_mm_sha256msg1_epu32(m0, m1), _mm_alignr_epi8(m3, m2, 4)), m3);
        m0 = m1; m1 = m2; m2 = m3; m3 = n;
    }
    s0 = _mm_add_epi32(s0, a0);
    s1 = _mm_add_epi32(s1, a1);
    t = _mm_shuffle_epi32(s0, 0x1B);                                                      // FEBA
    s1 = _mm_shuffle_epi32(s1, 0xB1);                                                     // DCHG
    _mm_storeu_si128((__m128i *)s->h, _mm_blend_epi16(t, s1, 0xF0));                      // DCBA
    _mm_storeu_si128((__m128i *)(s->h + 4), _mm_alignr_epi8(s1, t, 8));                   // HGFE
}

static int sha_x86_ok(void) {                   // SHA (CPUID 7: EBX bit 29), SSE4.1 and SSSE3 (CPUID 1: ECX 19, 9)
    unsigned a, b, c, d;
    if (!__get_cpuid(1, &a, &b, &c, &d) || !(c & (1u << 19)) || !(c & (1u << 9))) return 0;
    if (!__get_cpuid_count(7, 0, &a, &b, &c, &d)) return 0;
    return (b >> 29) & 1;
}
#endif

#if defined(__aarch64__) || defined(__x86_64__)
static int sha_hw = -1;                         // 1: the CPU has SHA-256 instructions (found once)
#endif

static void sha_block(Sha256 *s, const uint8_t *p) {
#if defined(__aarch64__)
    int hw = __atomic_load_n(&sha_hw, __ATOMIC_RELAXED);
    if (hw < 0) {
#if defined(__APPLE__)
        hw = 1;                                 // every Apple arm64 CPU has them
#elif defined(__linux__)
        hw = (getauxval(AT_HWCAP) & HWCAP_SHA2) != 0;
#else
        hw = 0;
#endif
        __atomic_store_n(&sha_hw, hw, __ATOMIC_RELAXED);
    }
    if (hw) { sha_block_arm(s, p); return; }
#elif defined(__x86_64__)
    int hw = __atomic_load_n(&sha_hw, __ATOMIC_RELAXED);
    if (hw < 0) {
        hw = getenv("PIN_SHA_SOFTWARE") ? 0 : sha_x86_ok();   // PIN_SHA_SOFTWARE=1: the portable code (for tests)
        __atomic_store_n(&sha_hw, hw, __ATOMIC_RELAXED);
    }
    if (hw) { sha_block_x86(s, p); return; }
#endif
    sha_block_c(s, p);
}

static void sha_init(Sha256 *s) {
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(s->h, iv, sizeof iv);
    s->n = 0; s->used = 0;
}

static void sha_update(Sha256 *s, const uint8_t *p, size_t len) {
    s->n += len;
    while (len) {
        size_t k = 64 - s->used < len ? 64 - s->used : len;
        memcpy(s->buf + s->used, p, k);
        s->used += k; p += k; len -= k;
        if (s->used == 64) { sha_block(s, s->buf); s->used = 0; }
    }
}

static void sha_final(Sha256 *s, uint8_t out[32]) {
    uint64_t bits = s->n * 8;
    s->buf[s->used++] = 0x80;
    if (s->used > 56) {                                  // no room for the length: one more block
        memset(s->buf + s->used, 0, 64 - s->used);
        sha_block(s, s->buf);
        s->used = 0;
    }
    memset(s->buf + s->used, 0, 56 - s->used);
    for (int i = 0; i < 8; i++) s->buf[56 + i] = (uint8_t)(bits >> (56 - 8 * i));
    sha_block(s, s->buf);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (uint8_t)(s->h[i] >> 24); out[4 * i + 1] = (uint8_t)(s->h[i] >> 16);
        out[4 * i + 2] = (uint8_t)(s->h[i] >> 8); out[4 * i + 3] = (uint8_t)s->h[i];
    }
}

// HMAC with the two padded-key states kept, so PBKDF2's many rounds hash only the message part again.
typedef struct { Sha256 inner, outer; } Hmac;

static void hmac_init(Hmac *m, const uint8_t *key, size_t klen) {
    uint8_t k[64] = {0}, pad[64];
    if (klen > 64) { Sha256 t; sha_init(&t); sha_update(&t, key, klen); sha_final(&t, k); }
    else memcpy(k, key, klen);
    for (int i = 0; i < 64; i++) pad[i] = k[i] ^ 0x36;
    sha_init(&m->inner); sha_update(&m->inner, pad, 64);
    for (int i = 0; i < 64; i++) pad[i] = k[i] ^ 0x5c;
    sha_init(&m->outer); sha_update(&m->outer, pad, 64);
}

static void hmac_once(const Hmac *m, const uint8_t *p, size_t len, uint8_t out[32]) {
    Sha256 s = m->inner;
    uint8_t d[32];
    sha_update(&s, p, len);
    sha_final(&s, d);
    s = m->outer;
    sha_update(&s, d, 32);
    sha_final(&s, out);
}

static void pbkdf2_sha256(const uint8_t *pw, size_t pwlen, const uint8_t *salt, size_t slen, uint32_t iters,
                          uint8_t *out, size_t outlen) {
    Hmac m;
    hmac_init(&m, pw, pwlen);
    uint8_t *msg = malloc(slen + 4);
    if (!msg) oom();
    memcpy(msg, salt, slen);
    for (uint32_t block = 1; outlen; block++) {
        uint8_t u[32], t[32];
        msg[slen] = (uint8_t)(block >> 24); msg[slen + 1] = (uint8_t)(block >> 16);
        msg[slen + 2] = (uint8_t)(block >> 8); msg[slen + 3] = (uint8_t)block;
        hmac_once(&m, msg, slen + 4, u);
        memcpy(t, u, 32);
        for (uint32_t i = 1; i < iters; i++) {
            hmac_once(&m, u, 32, u);
            for (int j = 0; j < 32; j++) t[j] ^= u[j];
        }
        size_t k = outlen < 32 ? outlen : 32;
        memcpy(out, t, k);
        out += k; outlen -= k;
    }
    free(msg);
}

static Str *hex_of(const uint8_t *p, size_t n) {
    static const char digits[] = "0123456789abcdef";
    Str *s = str_alloc((int64_t)n * 2);
    for (size_t i = 0; i < n; i++) { s->data[2 * i] = digits[p[i] >> 4]; s->data[2 * i + 1] = digits[p[i] & 15]; }
    s->len = (int64_t)n * 2; s->data[s->len] = 0;
    return s;
}

static void os_random(void *p, size_t n) {
    uint8_t *b = p;
    while (n) {
        size_t k = n < 256 ? n : 256;                   // getentropy gives at most 256 bytes per call
        if (getentropy(b, k) != 0) { fprintf(stderr, "panic: the system gave no random bytes\n"); die(); }
        b += k; n -= k;
    }
}

// --- the built-ins

Str *pn_hash_sha256(Str *data) {
    Sha256 s; uint8_t d[32];
    sha_init(&s); sha_update(&s, (const uint8_t *)data->data, (size_t)data->len); sha_final(&s, d);
    return hex_of(d, 32);
}

Str *pn_hash_hmac_sha256(Str *data, Str *key) {
    Hmac m; uint8_t d[32];
    hmac_init(&m, (const uint8_t *)key->data, (size_t)key->len);
    hmac_once(&m, (const uint8_t *)data->data, (size_t)data->len, d);
    return hex_of(d, 32);
}

// Compares in time that depends only on the lengths, so a client cannot guess a token byte by byte.
int64_t pn_hash_equals(Str *known, Str *user) {
    if (known->len != user->len) return 0;
    volatile uint8_t diff = 0;
    for (int64_t i = 0; i < known->len; i++) diff |= (uint8_t)(known->data[i] ^ user->data[i]);
    return diff == 0;
}

Str *pn_random_token(int64_t bytes) {
    if (bytes < 1 || bytes > 1024) {
        fflush(stdout);
        fprintf(stderr, "panic: random_token(%lld): the size must be 1 to 1024 bytes\n", (long long)bytes);
        die();
    }
    uint8_t buf[1024];
    os_random(buf, (size_t)bytes);
    return hex_of(buf, (size_t)bytes);
}

int64_t pn_random_int(int64_t min, int64_t max) {
    if (min > max) {
        fflush(stdout);
        fprintf(stderr, "panic: random_int(%lld, %lld): min is greater than max\n", (long long)min, (long long)max);
        die();
    }
    uint64_t range = (uint64_t)max - (uint64_t)min;     // 0 .. 2^64-1, no overflow in unsigned
    uint64_t r;
    if (range == UINT64_MAX) { os_random(&r, sizeof r); return (int64_t)r; }
    uint64_t n = range + 1, limit = UINT64_MAX - (UINT64_MAX % n);   // reject the uneven top: no bias
    do os_random(&r, sizeof r); while (r >= limit);
    return (int64_t)((uint64_t)min + r % n);
}

// password_hash: PBKDF2-HMAC-SHA256, 600,000 rounds (OWASP's 2023 figure for this algorithm), 16-byte random salt.
// Stored as  $pbkdf2-sha256$600000$<salt hex>$<hash hex>  so password_verify can read the rounds back.
#define PW_ROUNDS 600000
#define PW_MAX_ROUNDS 10000000                          // a stored hash cannot make verify run for minutes

Str *pn_password_hash(Str *pw) {
    uint8_t salt[16], dk[32];
    os_random(salt, sizeof salt);
    pbkdf2_sha256((const uint8_t *)pw->data, (size_t)pw->len, salt, sizeof salt, PW_ROUNDS, dk, sizeof dk);
    char out[160];
    int n = snprintf(out, sizeof out, "$pbkdf2-sha256$%d$", PW_ROUNDS);
    for (int i = 0; i < 16; i++) n += snprintf(out + n, sizeof out - (size_t)n, "%02x", salt[i]);
    out[n++] = '$';
    for (int i = 0; i < 32; i++) n += snprintf(out + n, sizeof out - (size_t)n, "%02x", dk[i]);
    return str_from(out, n);
}

static int lowhex(char c) {                 // lowercase hex only: hashes are written that way
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

// false for anything that is not a hash made by password_hash (it often comes from a database: never panic)
int64_t pn_password_verify(Str *pw, Str *stored) {
    const char *p = stored->data, *end = p + stored->len;
    static const char tag[] = "$pbkdf2-sha256$";
    if (stored->len < (int64_t)sizeof tag - 1 || memcmp(p, tag, sizeof tag - 1) != 0) return 0;
    p += sizeof tag - 1;
    uint64_t rounds = 0;
    while (p < end && *p >= '0' && *p <= '9' && rounds <= PW_MAX_ROUNDS) rounds = rounds * 10 + (uint64_t)(*p++ - '0');
    if (rounds < 1 || rounds > PW_MAX_ROUNDS || p >= end || *p++ != '$') return 0;
    uint8_t salt[64], want[32], got[32];
    size_t slen = 0;
    while (p + 1 < end && *p != '$') {
        int hi = lowhex(p[0]), lo = lowhex(p[1]);
        if (hi < 0 || lo < 0 || slen == sizeof salt) return 0;
        salt[slen++] = (uint8_t)(hi << 4 | lo);
        p += 2;
    }
    if (slen == 0 || p >= end || *p++ != '$' || end - p != 64) return 0;
    for (int i = 0; i < 32; i++) {
        int hi = lowhex(p[2 * i]), lo = lowhex(p[2 * i + 1]);
        if (hi < 0 || lo < 0) return 0;
        want[i] = (uint8_t)(hi << 4 | lo);
    }
    pbkdf2_sha256((const uint8_t *)pw->data, (size_t)pw->len, salt, slen, (uint32_t)rounds, got, sizeof got);
    volatile uint8_t diff = 0;
    for (int i = 0; i < 32; i++) diff |= got[i] ^ want[i];
    return diff == 0;
}
