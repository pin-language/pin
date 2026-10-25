// runtime/server.c — the HTTP/1.1 server: requests as tasks, headers, limits, Server-Sent Events, epoll / kqueue. Part of runtime.c (one translation unit: see runtime.c).

// ============================================================================ http server

typedef Str *(*Handler)(Str *method, Str *path, Str *body);
static Handler pn_user_handler;   // set by pn_serve(); the compiler passes the program's handler

void pn_http_status(int64_t s) { if (cur_req) cur_req->status = s; }

typedef struct Conn {
    int kind;               // 0 (an FdWait has 1 here: events tell them apart)
    int fd, close_after, want_out, dead;
    char *in; size_t in_len, in_cap;
    char *out; size_t out_len, out_off, out_cap;
    int64_t req_start;      // ms when the request being received began (0 = none pending)
    int64_t out_start;      // ms since a response has been waiting for the client to read it (0 = none)
    int64_t last_active;    // ms of the last completed request (for the keep-alive idle limit)
    struct Conn *prev, *next;   // the worker's open connections, scanned for timeouts
    struct Task *busy;      // the request being handled (its task may be waiting); one at a time per connection
    int orphan;             // closed while busy: freed when the request ends
    int parsing;            // inside handle_input (a request that ends right away is answered there)
    struct Conn *next_done; // requests that ended later, waiting to be answered by the worker loop
    int streaming;          // sse_start() was called: the handler writes the body itself, then the connection closes
    int ep;                 // the worker's poll set (sse_send needs it when the client reads slowly)
} Conn;

static __thread Conn *done_conns;

// ---------------------------------------------------------------- limits (slow clients cannot hold the server)
// A request must arrive completely within PN_READ_TIMEOUT_MS of its first byte (a new connection counts as a
// request that has started: sending nothing is not a way to hold a connection), an idle keep-alive connection
// is closed after PN_IDLE_TIMEOUT_MS, a response the client does not read after PN_WRITE_TIMEOUT_MS, and at
// most PN_MAX_CONNS connections are open at once. A handler that is still working (waiting) is not timed out.
static int64_t read_timeout_ms = 15000, idle_timeout_ms = 60000, write_timeout_ms = 30000, max_conns = 10000;
static int64_t open_conns;          // all workers (atomic)

static int64_t env_ms(const char *name, int64_t dflt) {
    const char *v = getenv(name);
    if (!v || !*v) return dflt;
    char *end;
    long long n = strtoll(v, &end, 10);
    if (*end || n <= 0) { fprintf(stderr, "serve: %s must be a positive number\n", name); exit(1); }
    return (int64_t)n;
}

// ---------------------------------------------------------------- request and response headers
// The handler reads its request's headers and adds response headers through its Req (cur_req follows the
// running task); nothing is shared between requests or threads.

// The value of request header `name` (case-insensitive), trimmed; NULL if absent.
static Str *find_header(Str *name) {
    if (!cur_req || !cur_req->hdr) return NULL;
    const char *p = cur_req->hdr, *end = cur_req->hdr + cur_req->hdr_len;
    while (p < end) {
        const char *e = memmem(p, (size_t)(end - p), "\r\n", 2);
        if (!e) e = end;
        const char *colon = memchr(p, ':', (size_t)(e - p));
        if (colon && colon - p == name->len && strncasecmp(p, name->data, (size_t)name->len) == 0) {
            const char *v = colon + 1, *ve = e;
            while (v < ve && (*v == ' ' || *v == '\t')) v++;
            while (ve > v && (ve[-1] == ' ' || ve[-1] == '\t')) ve--;
            return str_from(v, ve - v);
        }
        p = e + 2;
    }
    return NULL;
}

Str *pn_request_header(Str *name) {
    Str *v = find_header(name);
    return v ? v : str_alloc(0);
}

int64_t pn_request_has_header(Str *name) {
    Str *v = find_header(name);
    if (!v) return 0;
    pn_str_free(v);
    return 1;
}

static int is_token_char(unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || strchr("!#$%&'*+-.^_`|~", c);
}

// Add a response header. A name or value that could break the header block (CR, LF and other control bytes:
// "header injection") panics, as do the headers the server writes itself.
void pn_response_header(Str *name, Str *value) {
    Req *r = cur_req;
    if (!r || !r->root) panic_msg("response_header() only works inside a serve() handler");
    if (name->len == 0) panic_msg("response_header(): empty header name");
    for (int64_t i = 0; i < name->len; i++)
        if (!is_token_char((unsigned char)name->data[i])) panic_msg("response_header(): invalid character in the header name");
    for (int64_t i = 0; i < value->len; i++) {
        unsigned char c = (unsigned char)value->data[i];
        if ((c < 0x20 && c != '\t') || c == 0x7f)
            panic_msg("response_header(): the value contains a control character (CR/LF would let it inject headers)");
    }
    static const char *const own[] = {"content-length", "connection", "transfer-encoding", "date", NULL};
    for (int i = 0; own[i]; i++)
        if ((size_t)name->len == strlen(own[i]) && strncasecmp(name->data, own[i], (size_t)name->len) == 0)
            panic_msg("response_header(): Content-Length, Connection, Transfer-Encoding and Date are set by the server");
    if (name->len == 12 && strncasecmp(name->data, "content-type", 12) == 0) r->resp_has_ctype = 1;
    size_t need = r->resp_hdr_len + (size_t)name->len + (size_t)value->len + 4;
    if (need > (64u << 10)) panic_msg("response_header(): more than 64 KB of response headers");
    if (need > r->resp_hdr_cap) {
        r->resp_hdr_cap = need * 2;
        char *p = realloc(r->resp_hdr, r->resp_hdr_cap);
        if (!p) oom();
        r->resp_hdr = p;
    }
    char *d = r->resp_hdr + r->resp_hdr_len;
    memcpy(d, name->data, (size_t)name->len); d += name->len;
    memcpy(d, ": ", 2); d += 2;
    memcpy(d, value->data, (size_t)value->len); d += value->len;
    memcpy(d, "\r\n", 2);
    r->resp_hdr_len = need;
}

static const char *reason(int64_t s) {
    switch (s) {
    case 200: return "OK"; case 201: return "Created"; case 202: return "Accepted"; case 204: return "No Content";
    case 301: return "Moved Permanently"; case 302: return "Found"; case 303: return "See Other";
    case 304: return "Not Modified"; case 307: return "Temporary Redirect"; case 308: return "Permanent Redirect";
    case 400: return "Bad Request"; case 401: return "Unauthorized"; case 402: return "Payment Required";
    case 403: return "Forbidden"; case 404: return "Not Found"; case 405: return "Method Not Allowed";
    case 406: return "Not Acceptable"; case 408: return "Request Timeout"; case 409: return "Conflict";
    case 410: return "Gone"; case 411: return "Length Required"; case 412: return "Precondition Failed";
    case 413: return "Content Too Large"; case 414: return "URI Too Long"; case 415: return "Unsupported Media Type";
    case 418: return "I'm a teapot"; case 422: return "Unprocessable Content"; case 423: return "Locked";
    case 425: return "Too Early"; case 426: return "Upgrade Required"; case 428: return "Precondition Required";
    case 429: return "Too Many Requests"; case 431: return "Request Header Fields Too Large";
    case 451: return "Unavailable For Legal Reasons";
    case 500: return "Internal Server Error"; case 501: return "Not Implemented"; case 502: return "Bad Gateway";
    case 503: return "Service Unavailable"; case 504: return "Gateway Timeout";
    default: return "Status";              // unusual codes: the number is what clients read
    }
}

// ---------------------------------------------------------------- errors (throws / try)

// An error that leaves the top level: print it and stop (error-handling.md E-R6).
__attribute__((noreturn)) void pn_uncaught(PinError *e) {
    fflush(stdout);
    fprintf(stderr, "error: %.*s (code %lld)\n", (int)e->message->len, e->message->data, (long long)e->code);
    exit(1);
}

// An error that leaves a handler: status from its code (400-599, else 500), the standard status text as the
// body, and the message only in the log — internal details never reach the client (E3).
Str *pn_handler_error(PinError *e) {
    int64_t s = e->code >= 400 && e->code <= 599 ? e->code : 500;
    cur_req->status = s;
    cur_req->resp_hdr_len = 0; cur_req->resp_has_ctype = 0;   // like a panic: none of the handler's headers
    fprintf(stderr, "error %lld: %.*s\n", (long long)e->code, (int)e->message->len, e->message->data);
    char buf[96];
    int n = snprintf(buf, sizeof buf, "{\"error\":\"%s\"}", reason(s));
    pn_str_free(e->message);
    xfree(e);
    return str_from(buf, n);
}

static void out_add(Conn *c, const char *p, size_t n) {
    if (c->out_len + n > c->out_cap) {
        c->out_cap = (c->out_len + n) * 2;
        char *p = realloc(c->out, c->out_cap);
        if (!p) oom();
        c->out = p;
    }
    memcpy(c->out + c->out_len, p, n);
    c->out_len += n;
}

static int ieq_prefix(const char *p, const char *end, const char *name) {
    size_t n = strlen(name);
    if ((size_t)(end - p) < n) return 0;
    return strncasecmp(p, name, n) == 0;
}

static const char internal_error_body[] = "{\"error\":\"Internal Server Error\"}";

typedef struct {
    Conn *c;
    Str *method, *path, *body, *resp;
    int keep;
    char what[120];         // "GET /path" for the log
} RunArgs;

static void conn_free(Conn *c);

// Runs as the request's task. A panic in it (or in a task it started) frees everything the request
// allocated and becomes a 500; the panic message (already on stderr) is the only trace, so the client never
// sees internal details.
static void handler_task(void *arg) {
    RunArgs *volatile ra = arg;
    Req *volatile r = cur_req;
    jmp_buf jb;
    if (setjmp(jb)) {
        cur_jmp = NULL;
        req_kill_tasks(r);
        r->resp_hdr_len = 0; r->resp_has_ctype = 0;        // a 500 carries none of the handler's headers
        req_free_all(r);
        fprintf(stderr, "  in request %s -> 500\n", ra->what);
        r->status = 500;
        ra->resp = NULL;
        return;
    }
    cur_jmp = &jb;
    ra->resp = pn_user_handler(ra->method, ra->path, ra->body);
    cur_jmp = NULL;
    if (r->panicked) die();                     // a task it started panicked after it finished waiting
}

// The request's task ended: write the response (unless the client is gone) and free the request.
static void request_done(Task *t) {
    RunArgs *ra = t->ctx;
    Req *r = t->req;
    Conn *c = ra->c;
    if (c->streaming) {
        c->close_after = 1;                     // the handler wrote the body; the end of the stream is a close
    } else if (!c->orphan) {
        int64_t st = r->status;
        const char *body = ra->resp ? ra->resp->data : internal_error_body;
        size_t blen = ra->resp ? (size_t)ra->resp->len : sizeof internal_error_body - 1;
        char head[160];
        int hn = snprintf(head, sizeof head, "HTTP/1.1 %lld %s\r\n%s", (long long)st, reason(st),
                          r->resp_has_ctype ? "" : "Content-Type: application/json\r\n");
        out_add(c, head, (size_t)hn);
        if (r->resp_hdr_len) out_add(c, r->resp_hdr, r->resp_hdr_len);
        hn = snprintf(head, sizeof head, "Content-Length: %lld\r\n%s\r\n", (long long)blen,
                      ra->keep ? "" : "Connection: close\r\n");
        out_add(c, head, (size_t)hn);
        out_add(c, body, blen);
        if (!ra->keep) c->close_after = 1;
    }
    c->busy = NULL;
    task_unlink_req(t);
    req_free_all(r);                            // the response body, method, path, body and anything left over
    free(r->hdr); free(r->resp_hdr); free(r);
    t->req = NULL;
    if (cur_req == r) cur_req = NULL;           // we run inside t: leave_task() copies cur_req back into t->req
    free(ra);
    if (c->orphan) conn_free(c);
    else if (!c->parsing) { c->next_done = done_conns; done_conns = c; }   // answered by the worker loop
}

// Start the handler for one complete request; it may finish now or wait (then the connection waits too).
static void start_request(Conn *c, const char *line, const char *sp1, const char *sp2, const char *hdr,
                          const char *hdr_end, const char *body, int64_t blen, int keep) {
    Req *r = req_new();
    r->hdr_len = hdr_end > hdr ? (size_t)(hdr_end - hdr) : 0;     // a request may have no header lines at all
    r->hdr = malloc(r->hdr_len + 1);
    if (!r->hdr) oom();
    memcpy(r->hdr, hdr, r->hdr_len);            // the connection's buffer moves on while the handler waits
    RunArgs *ra = calloc(1, sizeof *ra);
    if (!ra) oom();
    ra->c = c; ra->keep = keep;
    snprintf(ra->what, sizeof ra->what, "%.*s", (int)(sp2 - line), line);
    cur_req = r;
    ra->method = str_from(line, sp1 - line);
    ra->path = str_from(sp1 + 1, sp2 - sp1 - 1);
    ra->body = str_from(body, blen);
    Task *t = task_spawn(handler_task, ra, TASK_STACK);
    cur_req = NULL;
    r->root = t;
    r->conn = c;
    t->ctx = ra;
    t->on_done = request_done;
    t->auto_release = 1;
    c->busy = t;
    run_ready();                                // run it now: most handlers finish without waiting
}

// Answer with an error status and close: for requests the server will not pass to the handler.
static void refuse(Conn *c, int status) {
    char msg[200];
    int n = snprintf(msg, sizeof msg,
                     "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     "{\"error\":\"%s\"}", status, reason(status), (int)strlen(reason(status)) + 12, reason(status));
    out_add(c, msg, (size_t)n);
    c->close_after = 1;
    c->in_len = 0;
}

// Parse and answer the complete requests in the input buffer, one at a time. -1 = close the connection.
static int handle_input(Conn *c) {
    size_t off = 0;
    c->parsing = 1;
    while (!c->close_after && !c->busy) {
        char *buf = c->in + off;
        size_t avail = c->in_len - off;
        char *hend = memmem(buf, avail, "\r\n\r\n", 4);
        if (!hend) {
            if (avail > 65536) { refuse(c, 431); c->parsing = 0; return 0; }
            break;
        }
        if (hend - buf > 65536) { refuse(c, 431); c->parsing = 0; return 0; }   // also when it arrived in one read
        char *line_end = memmem(buf, (size_t)(hend - buf) + 2, "\r\n", 2);
        char *sp1 = memchr(buf, ' ', (size_t)(line_end - buf));
        char *sp2 = sp1 ? memchr(sp1 + 1, ' ', (size_t)(line_end - sp1 - 1)) : NULL;
        if (!sp2) { refuse(c, 400); c->parsing = 0; return 0; }
        int http10 = (line_end - sp2 - 1 == 8) && memcmp(sp2 + 1, "HTTP/1.0", 8) == 0;
        int keep = !http10;
        int64_t clen = 0;
        int seen_len = 0, bad = 0;
        for (char *p = line_end + 2; p < hend && !bad;) {
            char *e = memmem(p, (size_t)(hend + 2 - p), "\r\n", 2);
            if (ieq_prefix(p, e, "content-length:")) {
                // only digits, and one value: anything else is how request smuggling starts (RFC 9112 6.3)
                char *v = p + 15, *ve = e;
                while (v < ve && (*v == ' ' || *v == '\t')) v++;
                while (ve > v && (ve[-1] == ' ' || ve[-1] == '\t')) ve--;
                int64_t n = 0;
                if (v == ve || ve - v > 12) bad = 400;
                for (char *d = v; d < ve && !bad; d++) {
                    if (*d < '0' || *d > '9') bad = 400;
                    else n = n * 10 + (*d - '0');
                }
                if (!bad && seen_len && n != clen) bad = 400;
                clen = n; seen_len = 1;
            } else if (ieq_prefix(p, e, "transfer-encoding:")) {
                bad = 501;                            // chunked bodies are not supported: refuse, never guess
            } else if (ieq_prefix(p, e, "connection:")) {
                char *v = p + 11; while (*v == ' ') v++;
                if (ieq_prefix(v, e, "close")) keep = 0;
                else if (ieq_prefix(v, e, "keep-alive")) keep = 1;
            }
            p = e + 2;
        }
        if (!bad && clen > (64 << 20)) bad = 413;
        if (bad) { refuse(c, bad); c->parsing = 0; return 0; }
        size_t hlen = (size_t)(hend - buf) + 4;
        if (avail < hlen + (size_t)clen) break;      // body not complete yet
        start_request(c, buf, sp1, sp2, line_end + 2, hend, hend + 4, clen, keep);
        off += hlen + (size_t)clen;
    }
    if (off) { memmove(c->in, c->in + off, c->in_len - off); c->in_len -= off; }
    c->parsing = 0;
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

// ---------------------------------------------------------------- server-sent events (a stream from a handler)
static void poll_want_out(int p, Conn *c, int on);

// The connection of the running handler, or NULL with *err set.
static Conn *sse_conn(PinError **err) {
    Conn *c = cur_req ? cur_req->conn : NULL;
    if (!c) panic_msg("sse_*() only works inside a serve() handler");
    if (c->orphan || cur_req->cancelled) { *err = make_error(499, "the client closed the stream"); return NULL; }
    return c;
}

static PinError *sse_flush(Conn *c) {
    int f = flush_out(c);
    if (f < 0) { cur_req->cancelled = 1; return make_error(499, "the client closed the stream"); }
    if (f == 0) {
        if (!c->want_out) { c->want_out = 1; poll_want_out(c->ep, c, 1); }
        if (!c->out_start) c->out_start = now_ms();
        if (c->out_len > ((size_t)1 << 20)) return make_error(503, "the client reads the stream too slowly");
    } else c->out_start = 0;
    return NULL;
}

void *pn_sse_start(void) {
    PinError *err = NULL;
    Conn *c = sse_conn(&err);
    if (!c) return err;
    if (c->streaming) panic_msg("sse_start(): the stream has already started");
    char head[256];
    int n = snprintf(head, sizeof head, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     "X-Accel-Buffering: no\r\n");
    out_add(c, head, (size_t)n);
    if (cur_req->resp_hdr_len) out_add(c, cur_req->resp_hdr, cur_req->resp_hdr_len);
    out_add(c, "Connection: close\r\n\r\n", 21);
    c->streaming = 1;
    return sse_flush(c);
}

static void *sse_write(int64_t id, Str *data, int comment) {
    PinError *err = NULL;
    Conn *c = sse_conn(&err);
    if (!c) return err;
    if (!c->streaming) panic_msg("call sse_start() before sending events");
    char buf[32];
    if (id >= 0) { int n = snprintf(buf, sizeof buf, "id: %lld\n", (long long)id); out_add(c, buf, (size_t)n); }
    const char *p = data->data, *e = data->data + data->len;
    do {                                        // one "data:" line per line of the text (SSE framing)
        const char *nl = memchr(p, '\n', (size_t)(e - p));
        const char *end = nl ? nl : e;
        if (end > p && end[-1] == '\r') end--;
        out_add(c, comment ? ": " : "data: ", comment ? 2 : 6);
        out_add(c, p, (size_t)(end - p));
        out_add(c, "\n", 1);
        p = nl ? nl + 1 : e;
    } while (p < e);
    out_add(c, "\n", 1);
    return sse_flush(c);
}

void *pn_sse_send(Str *data) { return sse_write(-1, data, 0); }
void *pn_sse_send_id(int64_t id, Str *data) { return sse_write(id < 0 ? 0 : id, data, 0); }
void *pn_sse_comment(Str *text) { return sse_write(-1, text, 1); }

// ---------------------------------------------------------------- event polling
// One small interface over epoll (Linux) and kqueue (macOS). An event with c == NULL is the listener.

typedef struct { Conn *c; int in, out, err; } PollEv;

#ifdef __APPLE__
static int poll_new(void) { return kqueue(); }

static void kq_set(int p, int fd, int16_t filter, uint16_t flags, void *data) {
    struct kevent k;
    EV_SET(&k, fd, filter, flags, 0, 0, data);
    kevent(p, &k, 1, NULL, 0, NULL);
}

static void poll_listen(int p, int fd) { kq_set(p, fd, EVFILT_READ, EV_ADD, NULL); }
static void poll_add(int p, Conn *c) { kq_set(p, c->fd, EVFILT_READ, EV_ADD, c); }
static void poll_want_out(int p, Conn *c, int on) { kq_set(p, c->fd, EVFILT_WRITE, on ? EV_ADD : EV_DELETE, c); }
static void poll_del(int p, Conn *c) { (void)p; (void)c; }   // close() drops kqueue registrations

static int poll_wait(int p, PollEv *out, int max, int timeout_ms) {
    struct kevent ks[256];
    struct timespec ts = {timeout_ms / 1000, (long)(timeout_ms % 1000) * 1000000};
    int n = kevent(p, NULL, 0, ks, max < 256 ? max : 256, &ts);
    for (int i = 0; i < n; i++) {
        out[i].c = ks[i].udata;
        out[i].in = ks[i].filter == EVFILT_READ;
        out[i].out = ks[i].filter == EVFILT_WRITE;
        out[i].err = (ks[i].flags & EV_ERROR) != 0;
    }
    return n;
}
#else
static int poll_new(void) { return epoll_create1(0); }

static void poll_listen(int p, int fd) {
    struct epoll_event ev = {.events = EPOLLIN, .data.ptr = NULL};
    epoll_ctl(p, EPOLL_CTL_ADD, fd, &ev);
}

static void poll_add(int p, Conn *c) {
    struct epoll_event ev = {.events = EPOLLIN | EPOLLRDHUP, .data.ptr = c};
    epoll_ctl(p, EPOLL_CTL_ADD, c->fd, &ev);
}

static void poll_want_out(int p, Conn *c, int on) {
    struct epoll_event ev = {.events = EPOLLIN | EPOLLRDHUP | (on ? EPOLLOUT : 0), .data.ptr = c};
    epoll_ctl(p, EPOLL_CTL_MOD, c->fd, &ev);
}

static void poll_del(int p, Conn *c) { epoll_ctl(p, EPOLL_CTL_DEL, c->fd, NULL); }

static int poll_wait(int p, PollEv *out, int max, int timeout_ms) {
    struct epoll_event evs[256];
    int n = epoll_wait(p, evs, max < 256 ? max : 256, timeout_ms);
    for (int i = 0; i < n; i++) {
        uint32_t e = evs[i].events;
        out[i].c = evs[i].data.ptr;
        out[i].in = (e & (EPOLLIN | EPOLLHUP | EPOLLRDHUP)) != 0;
        out[i].out = (e & EPOLLOUT) != 0;
        out[i].err = (e & EPOLLERR) != 0;
    }
    return n;
}
#endif

#ifdef __APPLE__
static void ep_arm(FdWait *w) { kq_set(my_ep, w->fd, w->write ? EVFILT_WRITE : EVFILT_READ, EV_ADD | EV_ONESHOT, w); }
static void ep_disarm(FdWait *w) { if (!w->fired) kq_set(my_ep, w->fd, w->write ? EVFILT_WRITE : EVFILT_READ, EV_DELETE, w); }
#else
static void ep_arm(FdWait *w) {
    struct epoll_event ev = {.events = (w->write ? EPOLLOUT : EPOLLIN | EPOLLRDHUP) | EPOLLONESHOT, .data.ptr = w};
    epoll_ctl(my_ep, EPOLL_CTL_ADD, w->fd, &ev);
}
static void ep_disarm(FdWait *w) { epoll_ctl(my_ep, EPOLL_CTL_DEL, w->fd, NULL); }
#endif

static int set_nonblock(int fd) { return fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK); }

static int accept_conn(int lfd) {
#ifdef __APPLE__
    int fd = accept(lfd, NULL, NULL);
    if (fd >= 0) {
        int one = 1;
        set_nonblock(fd);
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
    }
    return fd;
#else
    return accept4(lfd, NULL, NULL, SOCK_NONBLOCK);
#endif
}

static int listen_port;
static int shared_listener = -1;   // macOS: SO_REUSEPORT does not spread connections, so threads share one

static int open_listener(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    if (fd < 0) { perror("serve: socket"); exit(1); }
    set_nonblock(fd);
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
#ifndef __APPLE__
    setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof one);
#endif
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET; a.sin_port = htons((uint16_t)port); a.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0 || listen(fd, 4096) < 0) {
        perror("serve: cannot listen");
        exit(1);
    }
    return fd;
}

// Best effort: tell a client why it is being dropped (the socket is non-blocking; a full buffer loses it).
static void say_and_close(int fd, int status) {
    char msg[200];
    int n = snprintf(msg, sizeof msg,
                     "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     "{\"error\":\"%s\"}", status, reason(status), (int)strlen(reason(status)) + 12, reason(status));
    ssize_t w = write(fd, msg, (size_t)n);
    (void)w;
    close(fd);
}

static void conn_free(Conn *c) {
    free(c->in); free(c->out); free(c);
    __atomic_sub_fetch(&open_conns, 1, __ATOMIC_RELAXED);
}

// Close a connection. If its request is still being handled, the Conn stays until the request ends.
static void conn_close(int ep, Conn *c, int status) {
    c->dead = 1;
    poll_del(ep, c);
    c->prev->next = c->next; c->next->prev = c->prev;
    if (status) say_and_close(c->fd, status); else close(c->fd);
    if (c->busy) {
        c->orphan = 1;
        Req *r = c->busy->req;
        if (r) { r->cancelled = 1; for (Task *t = r->tasks; t; t = t->req_next) wake_waiting(t); }
    } else conn_free(c);
}

static void conn_times(Conn *c, int64_t now);

// After input arrived or a request ended: answer what is complete, write what is queued, close if done.
static void conn_progress(int ep, Conn *c, int64_t now) {
    int dead = 0;
    if (!c->busy && c->in_len && handle_input(c) < 0) dead = 1;
    if (!dead && c->out_len) {
        int f = flush_out(c);
        if (f < 0) dead = 1;
        else if (f == 0 && !c->want_out) { c->want_out = 1; poll_want_out(ep, c, 1); }
        else if (f == 1 && c->want_out) { c->want_out = 0; poll_want_out(ep, c, 0); }
    }
    if (!dead && c->close_after && c->out_len == 0 && !c->busy) dead = 1;
    if (dead) conn_close(ep, c, 0);
    else conn_times(c, now);
}

// Keep the timers of one connection current after something happened on it.
static void conn_times(Conn *c, int64_t now) {
    if (c->busy) { c->req_start = 0; c->last_active = now; return; }   // the handler is working: no limit
    if (c->out_len) { if (!c->out_start) c->out_start = now; }
    else c->out_start = 0;
    if (c->in_len) { if (!c->req_start) c->req_start = now; }   // a request has begun; its clock does not restart
    else if (c->req_start) { c->req_start = 0; c->last_active = now; }
}

static void *worker(void *arg) {
    int lfd = shared_listener >= 0 ? shared_listener : open_listener(listen_port);
    int ep = poll_new();
    my_ep = ep;
    poll_listen(ep, lfd);
    Conn *wake = calloc(1, sizeof(Conn));       // events for this thread's Hub wake pipe carry this
    if (!wake) oom();
    wake->fd = sched_self()->rd;
    poll_add(ep, wake);
    PollEv evs[256];
    Conn conns = {0};                                     // circular list of this worker's open connections
    conns.prev = conns.next = &conns;
    int64_t last_scan = now_ms();
    (void)arg;
    for (;;) {
        run_ready();
        int64_t now = now_ms();
        while (done_conns) {                                  // requests that ended after waiting: answer now
            Conn *c = done_conns;
            done_conns = c->next_done;
            if (!c->dead) conn_progress(ep, c, now);
        }
        int64_t wait = timers_fire(now);
        if (ready_head) continue;
        int n = poll_wait(ep, evs, 256, wait < 0 || wait > 1000 ? 1000 : (int)wait);
        now = now_ms();
        timers_fire(now);
        for (int i = 0; i < n; i++) {
            Conn *c = evs[i].c;
            if (c == wake) { hub_drain(); continue; }
            if (c && c->kind == 1) { fd_fired((FdWait *)c); continue; }   // a task's socket or pipe
            if (!c) {                                         // new connections
                for (;;) {
                    int fd = accept_conn(lfd);
                    if (fd < 0) break;
                    if (__atomic_add_fetch(&open_conns, 1, __ATOMIC_RELAXED) > max_conns) {
                        __atomic_sub_fetch(&open_conns, 1, __ATOMIC_RELAXED);
                        say_and_close(fd, 503);               // too many open: refuse instead of running out
                        continue;
                    }
                    int one = 1;
                    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
                    Conn *nc = calloc(1, sizeof(Conn));
                    if (!nc) oom();
                    nc->fd = fd;
                    nc->ep = ep;
                    nc->in_cap = 4096; nc->in = malloc(nc->in_cap);
                    if (!nc->in) oom();
                    nc->req_start = now;                      // the first request must arrive in time too
                    nc->prev = conns.prev; nc->next = &conns; conns.prev->next = nc; conns.prev = nc;
                    poll_add(ep, nc);
                }
                continue;
            }
            if (c->dead) continue;                            // closed earlier in this batch (kqueue: read + write events)
            int dead = evs[i].err;
            if (!dead && evs[i].in) {
                for (;;) {
                    if (c->in_len == c->in_cap) {
                        if (c->in_cap >= ((size_t)80 << 20)) { dead = 1; break; }   // way past every limit
                        c->in_cap *= 2;
                        char *p = realloc(c->in, c->in_cap);
                        if (!p) oom();
                        c->in = p;
                    }
                    ssize_t r = read(c->fd, c->in + c->in_len, c->in_cap - c->in_len);
                    if (r > 0) { c->in_len += (size_t)r; continue; }
                    if (r == 0) {
                        if (c->streaming) dead = 1;   // a stream's reader left: end the stream
                        else c->close_after = 1;      // client finished sending: still answer it
                        break;
                    }
                    if (errno == EINTR) continue;
                    if (errno != EAGAIN && errno != EWOULDBLOCK) dead = 1;
                    break;
                }
            }
            if (dead) { conn_close(ep, c, 0); continue; }
            conn_progress(ep, c, now);
        }
        if (now - last_scan >= 1000) {                        // once a second: drop clients that are too slow
            last_scan = now;
            void (*scan)(int64_t) = __atomic_load_n(&pool_scan_hook, __ATOMIC_ACQUIRE);
            if (scan) scan(now);                              // and close long-idle database connections
            for (Conn *c = conns.next, *nx; c != &conns; c = nx) {
                nx = c->next;
                if (c->busy && !(c->streaming && c->out_start && now - c->out_start > write_timeout_ms)) continue;
                int late_out = c->out_start && now - c->out_start > write_timeout_ms;
                int late_in = !late_out && c->req_start && now - c->req_start > read_timeout_ms;
                int idle = !c->req_start && !c->out_len && now - c->last_active > idle_timeout_ms;
                if (!late_out && !late_in && !idle) continue;
                conn_close(ep, c, late_in && !c->out_len ? 408 : 0);   // 408: the request never arrived in full
            }
        }
    }
    return NULL;
}

void pn_serve(int64_t port, Handler handler) {
    if (!handler) panic_msg("serve(): no handler compiled in");
    if (cur_task) {               // called from main's task: from here on this stack runs the scheduler
        cur_task = NULL;
        cur_req = NULL; cur_jmp = NULL;
    }
    pn_user_handler = handler;
    signal(SIGPIPE, SIG_IGN);
    setvbuf(stdout, NULL, _IOLBF, 0);
    listen_port = (int)port;
    read_timeout_ms = env_ms("PN_READ_TIMEOUT_MS", read_timeout_ms);
    idle_timeout_ms = env_ms("PN_IDLE_TIMEOUT_MS", idle_timeout_ms);
    write_timeout_ms = env_ms("PN_WRITE_TIMEOUT_MS", write_timeout_ms);
    max_conns = env_ms("PN_MAX_CONNS", max_conns);
    int threads = 0;
    const char *env = getenv("PN_THREADS");
    if (env) threads = atoi(env);
    if (threads <= 0) {
#ifdef __APPLE__
        threads = (int)sysconf(_SC_NPROCESSORS_ONLN);
#else
        cpu_set_t set;
        threads = sched_getaffinity(0, sizeof set, &set) == 0 ? CPU_COUNT(&set) : 1;
#endif
        if (threads <= 0) threads = 1;
    }
#ifdef __APPLE__
    shared_listener = open_listener(listen_port);
#endif
    fprintf(stderr, "listening on :%lld with %d thread(s)\n", (long long)port, threads);
    for (int i = 1; i < threads; i++) {
        pthread_t t;
        pthread_create(&t, NULL, worker, NULL);
    }
    worker(NULL);
}
