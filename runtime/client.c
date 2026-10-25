// runtime/client.c — the http:// client (GET, POST, SSE streams) and run(). Part of runtime.c (one translation unit: see runtime.c).

// ============================================================================ HTTP client and run()
// Plain http:// only, by design: TLS belongs to a proxy (design/tls.md). Every wait goes through wait_fds(), so a task
// talking to another server or a program does not hold its thread. Name lookup (getaddrinfo) still blocks.

#include <netdb.h>
#include <spawn.h>
#include <sys/wait.h>
extern char **environ;

typedef struct HConn {
    int fd;
    char *buf; size_t len, cap;         // received, not used yet
    int chunked; int64_t chunk_left;    // body framing
    int64_t body_left;                  // Content-Length still to read (-1: until close)
    int eof;
    void *res;
} HConn;

static void h_free(HConn *h) {
    if (!h) return;
    if (h->res) pn_res_remove(h->res);
    if (h->fd >= 0) close(h->fd);
    free(h->buf); free(h);
}

static void h_close_res(void *h) { ((HConn *)h)->res = NULL; h_free(h); }

// Read more bytes. 1 = got some, 0 = end of stream, -1 = error, -2 = timeout, -3 = cancelled.
static int h_more(HConn *h, int64_t deadline) {
    for (;;) {
        if (h->len == h->cap) {
            if (h->cap >= ((size_t)64 << 20)) return -1;
            h->cap = h->cap ? h->cap * 2 : 16384;
            char *p = realloc(h->buf, h->cap);
            if (!p) oom();
            h->buf = p;
        }
        ssize_t n = read(h->fd, h->buf + h->len, h->cap - h->len);
        if (n > 0) { h->len += (size_t)n; return 1; }
        if (n == 0) { h->eof = 1; return 0; }
        if (errno == EINTR) continue;
        if (errno != EAGAIN && errno != EWOULDBLOCK) return -1;
        int w = wait_fds(&h->fd, 1, 0, deadline);
        if (w == 0) return -2;
        if (w < 0) return -3;
    }
}

static void h_take(HConn *h, size_t n) { memmove(h->buf, h->buf + n, h->len - n); h->len -= n; }

static PinError *h_error(int r, const char *what) {
    char msg[200];
    if (r == -2) { snprintf(msg, sizeof msg, "%s: no answer in time", what); return make_error(504, msg); }
    if (r == -3) { snprintf(msg, sizeof msg, "%s: cancelled", what); return make_error(499, msg); }
    snprintf(msg, sizeof msg, "%s: the connection failed or closed early", what);
    return make_error(502, msg);
}

// http://host[:port]/path -> connected socket, request sent, response headers read. NULL + *err on failure.
static HConn *h_open(const char *method, Str *url, Map *headers, Str *body, const char *accept, int64_t deadline,
                     int *status, PinError **err) {
    char host[256], port[8] = "80", what[300];
    snprintf(what, sizeof what, "%s %.*s", method, (int)(url->len > 200 ? 200 : url->len), url->data);
    const char *u = url->data, *ue = url->data + url->len;
    if (url->len < 7 || strncmp(u, "http://", 7) != 0) {
        *err = make_error(400, strncmp(u, "https://", url->len < 8 ? (size_t)url->len : 8) == 0
                          ? "Pin does not speak https: call an egress proxy on 127.0.0.1 over http:// and let it open the https connection (pin-language/docs design/tls.md)"
                          : "the URL must start with http://");
        return NULL;
    }
    u += 7;
    const char *slash = memchr(u, '/', (size_t)(ue - u));
    const char *hostend = slash ? slash : ue;
    const char *colon = memchr(u, ':', (size_t)(hostend - u));
    size_t hl = (size_t)((colon ? colon : hostend) - u);
    if (hl == 0 || hl >= sizeof host) { *err = make_error(400, "the URL has no host"); return NULL; }
    memcpy(host, u, hl); host[hl] = 0;
    if (colon) {
        size_t pl = (size_t)(hostend - colon - 1);
        if (pl == 0 || pl >= sizeof port) { *err = make_error(400, "the URL's port is not valid"); return NULL; }
        memcpy(port, colon + 1, pl); port[pl] = 0;
    }
    const char *path = slash ? slash : "/";
    size_t plen = slash ? (size_t)(ue - slash) : 1;
    for (const char *c = u; c < ue; c++)
        if ((unsigned char)*c <= 0x20 || *c == 0x7f) { *err = make_error(400, "the URL contains a space or control character"); return NULL; }

    struct addrinfo hints = {0}, *ai = NULL;
    hints.ai_family = AF_UNSPEC; hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, port, &hints, &ai) != 0 || !ai) {
        char m[300]; snprintf(m, sizeof m, "%s: cannot find host %s", what, host);
        *err = make_error(502, m); return NULL;
    }
    int fd = socket(ai->ai_family, SOCK_STREAM, 0);
    if (fd < 0) { freeaddrinfo(ai); *err = make_error(502, "cannot open a socket"); return NULL; }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
#ifdef __APPLE__
    { int one = 1; setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one); }
#endif
    int rc = connect(fd, ai->ai_addr, ai->ai_addrlen);
    freeaddrinfo(ai);
    HConn *h = calloc(1, sizeof *h);
    if (!h) oom();
    h->fd = fd; h->body_left = -1;
    h->res = pn_res_add(h_close_res, h);        // closed if the request panics
    if (rc < 0 && errno != EINPROGRESS) { h_free(h); *err = h_error(-1, what); return NULL; }
    if (rc < 0) {
        int w = wait_fds(&fd, 1, 1, deadline), soerr = 0;
        socklen_t sl = sizeof soerr;
        if (w <= 0) { h_free(h); *err = h_error(w == 0 ? -2 : -3, what); return NULL; }
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
        if (soerr) { h_free(h); char m[300]; snprintf(m, sizeof m, "%s: cannot connect", what); *err = make_error(502, m); return NULL; }
    }
    // the request
    size_t cap = 512 + plen + (body ? (size_t)body->len : 0), n = 0;
    if (headers) for (int64_t i = 0; i < headers->used; i++) if (headers->e[i].alive)
        cap += (size_t)((Str *)headers->e[i].key)->len + (size_t)((Str *)headers->e[i].val)->len + 4;
    char *req = malloc(cap);
    if (!req) oom();
    n += (size_t)snprintf(req + n, cap - n, "%s %.*s HTTP/1.1\r\nHost: %s%s%s\r\nAccept: %s\r\nConnection: close\r\n",
                          method, (int)plen, path, host, colon ? ":" : "", colon ? port : "", accept);
    if (headers) for (int64_t i = 0; i < headers->used; i++) {
        Entry *e = &headers->e[i];
        if (!e->alive) continue;
        Str *k = (Str *)e->key, *v = (Str *)e->val;
        for (int64_t j = 0; j < k->len; j++) if (!is_token_char((unsigned char)k->data[j])) { free(req); h_free(h); panic_msg("http header name: invalid character"); }
        for (int64_t j = 0; j < v->len; j++) { unsigned char ch = (unsigned char)v->data[j]; if ((ch < 0x20 && ch != '\t') || ch == 0x7f) { free(req); h_free(h); panic_msg("http header value contains a control character (CR/LF would inject headers)"); } }
        memcpy(req + n, k->data, (size_t)k->len); n += (size_t)k->len;
        memcpy(req + n, ": ", 2); n += 2;
        memcpy(req + n, v->data, (size_t)v->len); n += (size_t)v->len;
        memcpy(req + n, "\r\n", 2); n += 2;
    }
    if (body) {
        n += (size_t)snprintf(req + n, cap - n, "Content-Type: application/json\r\nContent-Length: %lld\r\n", (long long)body->len);
    }
    memcpy(req + n, "\r\n", 2); n += 2;
    if (body) { memcpy(req + n, body->data, (size_t)body->len); n += (size_t)body->len; }
    for (size_t off = 0; off < n;) {
        ssize_t w = write(fd, req + off, n - off);
        if (w > 0) { off += (size_t)w; continue; }
        if (w < 0 && errno == EINTR) continue;
        int r = w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) ? wait_fds(&fd, 1, 1, deadline) : -9;
        if (r <= 0) { free(req); h_free(h); *err = h_error(r == 0 ? -2 : r == -1 ? -3 : -1, what); return NULL; }
    }
    free(req);
    // the response head
    char *hend;
    while (!(hend = h->len >= 4 ? memmem(h->buf, h->len, "\r\n\r\n", 4) : NULL)) {
        int r = h_more(h, deadline);
        if (r <= 0 || h->len > 65536) { h_free(h); *err = h_error(r == 1 ? -1 : r == 0 ? -1 : r, what); return NULL; }
    }
    if (h->len < 12 || strncmp(h->buf, "HTTP/1.", 7) != 0) { h_free(h); *err = make_error(502, "not an HTTP response"); return NULL; }
    *status = atoi(h->buf + 9);
    for (char *p = memmem(h->buf, (size_t)(hend - h->buf) + 2, "\r\n", 2) + 2; p < hend;) {
        char *e = memmem(p, (size_t)(hend + 2 - p), "\r\n", 2);
        if (ieq_prefix(p, e, "content-length:")) h->body_left = strtoll(p + 15, NULL, 10);
        else if (ieq_prefix(p, e, "transfer-encoding:")) {
            for (char *q = p + 18; q + 7 <= e; q++) if (strncasecmp(q, "chunked", 7) == 0) h->chunked = 1;
        }
        p = e + 2;
    }
    if (h->chunked) h->body_left = -1;
    h_take(h, (size_t)(hend - h->buf) + 4);
    return h;
}

// Up to `max` body bytes into `out` (chunked framing removed). >0 bytes, 0 = end of body, <0 = error code.
static int64_t h_body(HConn *h, char *out, int64_t max, int64_t deadline) {
    for (;;) {
        if (h->chunked) {
            if (h->chunk_left == 0) {               // next chunk size line
                char *nl;
                while (!(nl = memmem(h->buf, h->len, "\r\n", 2))) { int r = h_more(h, deadline); if (r <= 0) return r == 0 ? -1 : r; }
                int64_t sz = strtoll(h->buf, NULL, 16);
                h_take(h, (size_t)(nl - h->buf) + 2);
                if (sz <= 0) return 0;
                h->chunk_left = sz;
            }
            if (h->len == 0) { int r = h_more(h, deadline); if (r <= 0) return r == 0 ? -1 : r; }
            int64_t k = (int64_t)h->len < h->chunk_left ? (int64_t)h->len : h->chunk_left;
            if (k > max) k = max;
            memcpy(out, h->buf, (size_t)k); h_take(h, (size_t)k);
            h->chunk_left -= k;
            if (h->chunk_left == 0) {               // the CRLF after the chunk
                while (h->len < 2) { int r = h_more(h, deadline); if (r <= 0) return r == 0 ? -1 : r; }
                h_take(h, 2);
                h->chunk_left = 0;
            }
            return k;
        }
        if (h->body_left == 0) return 0;
        if (h->len == 0) {
            int r = h_more(h, deadline);
            if (r == 0) return h->body_left < 0 ? 0 : -1;
            if (r < 0) return r;
        }
        int64_t k = (int64_t)h->len;
        if (h->body_left >= 0 && k > h->body_left) k = h->body_left;
        if (k > max) k = max;
        memcpy(out, h->buf, (size_t)k); h_take(h, (size_t)k);
        if (h->body_left > 0) h->body_left -= k;
        return k;
    }
}

static StrOrError http_call(const char *method, Str *url, Map *headers, Str *body, int64_t ms) {
    StrOrError r = {NULL, NULL};
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    int status = 0;
    HConn *h = h_open(method, url, headers, body, "*/*", deadline, &status, &r.err);
    if (!h) return r;
    Str *out = str_alloc(1024);
    char chunk[16384];
    for (;;) {
        int64_t k = h_body(h, chunk, sizeof chunk, deadline);
        if (k == 0) break;
        if (k < 0) { pn_str_free(out); r.err = h_error((int)k, method); h_free(h); return r; }
        if (out->len + k > ((int64_t)64 << 20)) { pn_str_free(out); r.err = make_error(502, "response larger than 64 MB"); h_free(h); return r; }
        out = str_add_bytes(out, chunk, k);
    }
    h_free(h);
    if (status >= 400) {                        // the body is the message (cut), the status is the code
        char m[400];
        snprintf(m, sizeof m, "%s %.*s answered %d: %.*s", method, (int)(url->len > 150 ? 150 : url->len), url->data,
                 status, (int)(out->len > 200 ? 200 : out->len), out->data);
        pn_str_free(out);
        r.err = make_error(status, m);
        return r;
    }
    r.value = out;
    return r;
}

StrOrError pn_http_get(Str *url, Map *headers, int64_t ms) { return http_call("GET", url, headers, NULL, ms); }
StrOrError pn_http_post(Str *url, Map *headers, Str *body, int64_t ms) { return http_call("POST", url, headers, body, ms); }

// --- reading a Server-Sent Events stream
typedef struct HStream { HConn *h; Str *pending; int64_t last_id; } HStream;
typedef struct { HStream *value; PinError *err; } StreamOrError;

StreamOrError pn_http_stream(Str *url, Map *headers, int64_t last_id, int64_t ms) {
    StreamOrError r = {NULL, NULL};
    int status = 0;
    Map *hd = headers;
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    char idbuf[24];
    HConn *h;
    if (last_id > 0) {                          // ask for what came after it
        Map *m = pn_map_copy(headers);
        Str *k = str_from("Last-Event-ID", 13);
        snprintf(idbuf, sizeof idbuf, "%lld", (long long)last_id);
        Str *v = str_from(idbuf, (int64_t)strlen(idbuf));
        uint64_t *slot = pn_map_slot(m, (uint64_t)k, 1);
        pn_str_free((Str *)*slot);
        *slot = (uint64_t)v;
        pn_str_free(k);
        hd = m;
        h = h_open("GET", url, hd, NULL, "text/event-stream", deadline, &status, &r.err);
        pn_map_free(m);
    } else h = h_open("GET", url, hd, NULL, "text/event-stream", deadline, &status, &r.err);
    if (!h) return r;
    if (status != 200) {
        char m[200]; snprintf(m, sizeof m, "the stream answered %d", status);
        h_free(h); r.err = make_error(status >= 400 ? status : 502, m); return r;
    }
    HStream *st = xcalloc(sizeof *st);
    st->h = h; st->last_id = last_id; st->pending = str_alloc(256);
    r.value = st;
    return r;
}

void pn_stream_close(HStream *st) {
    if (!st) return;
    h_free(st->h);
    pn_str_free(st->pending);
    xfree(st);
}

int64_t pn_stream_last_id(HStream *st) { return st->last_id; }

// The data of the next event (comments skipped); 504 when none comes in time, 502 when the stream ends.
StrOrError pn_stream_next(HStream *st, int64_t ms) {
    StrOrError r = {NULL, NULL};
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    for (;;) {
        Str *p = st->pending;
        char *end = NULL; size_t sep = 0;
        for (int64_t i = 0; i + 1 < p->len; i++) {           // an event ends at a blank line
            if (p->data[i] == '\n' && p->data[i + 1] == '\n') { end = p->data + i; sep = 2; break; }
            if (i + 3 < p->len && memcmp(p->data + i, "\r\n\r\n", 4) == 0) { end = p->data + i; sep = 4; break; }
        }
        if (end) {
            Str *data = NULL;
            for (char *q = p->data; q < end;) {
                char *nl = memchr(q, '\n', (size_t)(end - q));
                char *le = nl ? nl : end;
                char *lend = le > q && le[-1] == '\r' ? le - 1 : le;
                if (lend - q >= 5 && memcmp(q, "data:", 5) == 0) {
                    char *v = q + 5; if (v < lend && *v == ' ') v++;
                    if (data) data = str_add_bytes(data, "\n", 1); else data = str_alloc(64);
                    data = str_add_bytes(data, v, lend - v);
                } else if (lend - q >= 3 && memcmp(q, "id:", 3) == 0) {
                    char *v = q + 3; if (v < lend && *v == ' ') v++;
                    st->last_id = strtoll(v, NULL, 10);
                }
                q = nl ? nl + 1 : end;
            }
            size_t used = (size_t)(end - p->data) + sep;
            memmove(p->data, p->data + used, (size_t)p->len - used);
            p->len -= (int64_t)used;
            if (data) { r.value = data; return r; }   // only comments: look for the next one
            continue;
        }
        char chunk[8192];
        int64_t k = h_body(st->h, chunk, sizeof chunk, deadline);
        if (k == 0) { r.err = make_error(502, "stream_next(): the stream ended"); return r; }
        if (k < 0) { r.err = h_error((int)k, "stream_next()"); return r; }
        if (p->len + k > (1 << 20)) { r.err = make_error(502, "stream_next(): an event larger than 1 MB"); return r; }
        st->pending = str_add_bytes(st->pending, chunk, k);
    }
}

// --- read_line(): one line of standard input (for programs driven through a pipe, like MCP over stdio)
// Output is flushed first, so a program that answers a line per line is never stuck with its answer in a
// buffer. Waiting does not hold the thread (other tasks run). End of input fails with code 1.
static pthread_mutex_t stdin_mu = PTHREAD_MUTEX_INITIALIZER;
static char *stdin_buf;
static size_t stdin_len, stdin_cap;
static int stdin_eof;

StrOrError pn_read_line(void) {
    StrOrError r = {NULL, NULL};
    fflush(stdout);
    pthread_mutex_lock(&stdin_mu);
    for (;;) {
        char *nl = stdin_len ? memchr(stdin_buf, '\n', stdin_len) : NULL;
        if (nl || (stdin_eof && stdin_len)) {
            size_t n = nl ? (size_t)(nl - stdin_buf) : stdin_len;
            size_t take = nl ? n + 1 : n;
            if (n && stdin_buf[n - 1] == '\r') n--;
            r.value = str_from(stdin_buf, (int64_t)n);
            memmove(stdin_buf, stdin_buf + take, stdin_len - take);
            stdin_len -= take;
            pthread_mutex_unlock(&stdin_mu);
            return r;
        }
        if (stdin_eof) { pthread_mutex_unlock(&stdin_mu); r.err = make_error(1, "read_line(): end of input"); return r; }
        if (stdin_len == stdin_cap) {
            if (stdin_cap >= ((size_t)64 << 20)) { pthread_mutex_unlock(&stdin_mu); r.err = make_error(413, "read_line(): a line longer than 64 MB"); return r; }
            stdin_cap = stdin_cap ? stdin_cap * 2 : 65536;
            char *p = realloc(stdin_buf, stdin_cap);
            if (!p) oom();
            stdin_buf = p;
        }
        int fd = 0;
        pthread_mutex_unlock(&stdin_mu);
        int w = wait_fds(&fd, 1, 0, INT64_MAX / 4);      // until there is input (stdin stays blocking)
        pthread_mutex_lock(&stdin_mu);
        if (w < 0) { pthread_mutex_unlock(&stdin_mu); r.err = make_error(499, "read_line(): cancelled"); return r; }
        ssize_t n = read(0, stdin_buf + stdin_len, stdin_cap - stdin_len);
        if (n > 0) stdin_len += (size_t)n;
        else if (n == 0 || (errno != EINTR && errno != EAGAIN)) stdin_eof = 1;
    }
}

// --- run(): start a program (no shell: arguments go to it as they are), collect its output
StrOrError pn_run(Str *cmd, List *args, int64_t ms) {
    StrOrError r = {NULL, NULL};
    int64_t deadline = now_ms() + (ms > 0 ? ms : 0);
    char **argv = calloc((size_t)args->len + 2, sizeof(char *));
    if (!argv) oom();
    argv[0] = strndup(cmd->data, (size_t)cmd->len);
    for (int64_t i = 0; i < args->len; i++) {
        Str *a = ((Str **)args->data)[i];
        if (memchr(a->data, 0, (size_t)a->len)) panic_msg("run(): an argument contains a NUL byte");
        argv[i + 1] = strndup(a->data, (size_t)a->len);
    }
    int outp[2], errp[2];
    if (pipe(outp) < 0 || pipe(errp) < 0) oom();
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_adddup2(&fa, outp[1], 1);
    posix_spawn_file_actions_adddup2(&fa, errp[1], 2);
    posix_spawn_file_actions_addclose(&fa, outp[0]);
    posix_spawn_file_actions_addclose(&fa, errp[0]);
    posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
    pid_t pid;
    int rc = posix_spawnp(&pid, argv[0], &fa, NULL, argv, environ);
    posix_spawn_file_actions_destroy(&fa);
    close(outp[1]); close(errp[1]);
    for (int64_t i = 0; i <= args->len; i++) free(argv[i]);
    free(argv);
    if (rc != 0) {
        close(outp[0]); close(errp[0]);
        char m[300]; snprintf(m, sizeof m, "run(): cannot start %.*s: %s", (int)(cmd->len > 100 ? 100 : cmd->len), cmd->data, strerror(rc));
        r.err = make_error(127, m);
        return r;
    }
    fcntl(outp[0], F_SETFL, fcntl(outp[0], F_GETFL) | O_NONBLOCK);
    fcntl(errp[0], F_SETFL, fcntl(errp[0], F_GETFL) | O_NONBLOCK);
    Str *out = str_alloc(256), *err = str_alloc(64);
    int fds[2] = {outp[0], errp[0]}, open_n = 2, late = 0, cancelled = 0;
    char buf[16384];
    while (open_n) {
        int any = 0;
        for (int i = 0; i < 2; i++) {
            if (fds[i] < 0) continue;
            ssize_t n = read(fds[i], buf, sizeof buf);
            if (n > 0) {
                any = 1;
                if (i == 0 && out->len < ((int64_t)64 << 20)) out = str_add_bytes(out, buf, n);
                if (i == 1 && err->len < 4096) err = str_add_bytes(err, buf, n);
            } else if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                close(fds[i]); fds[i] = -1; open_n--;
            }
        }
        if (any || !open_n) continue;
        int live[2], nl = 0;
        for (int i = 0; i < 2; i++) if (fds[i] >= 0) live[nl++] = fds[i];
        int w = wait_fds(live, nl, 0, deadline);
        if (w == 0) { late = 1; break; }
        if (w < 0) { cancelled = 1; break; }
    }
    for (int i = 0; i < 2; i++) if (fds[i] >= 0) close(fds[i]);
    if (late || cancelled) kill(pid, SIGKILL);
    int st = 0;
    while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {}
    if (late || cancelled) {
        pn_str_free(out); pn_str_free(err);
        r.err = make_error(late ? 504 : 499, late ? "run(): the program did not finish in time and was stopped" : "run(): cancelled");
        return r;
    }
    int code = WIFEXITED(st) ? WEXITSTATUS(st) : 128 + (WIFSIGNALED(st) ? WTERMSIG(st) : 0);
    if (code != 0) {
        char m[600];
        snprintf(m, sizeof m, "run(): %.*s exited with %d: %.*s", (int)(cmd->len > 100 ? 100 : cmd->len), cmd->data, code,
                 (int)(err->len > 400 ? 400 : err->len), err->data);
        pn_str_free(out); pn_str_free(err);
        r.err = make_error(code, m);
        return r;
    }
    pn_str_free(err);
    r.value = out;
    return r;
}
