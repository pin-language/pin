// runtime/postgres.c — db_* on a PostgreSQL database (pin-language/docs design/postgres.md). Included by sqlite.c
// (it shares Db, Rows, the pool's configuration and db_panic); compiled in with -DPIN_SQLITE like the rest.
//
// db_open("postgres://...") or ("postgresql://...") picks this file's code; any other path is SQLite.
// - libpq (PostgreSQL's own client library) does the protocol, SCRAM and TLS (G1). It is loaded with dlopen
//   the first time a program opens a PostgreSQL database: the URL comes from env() at run time, so the
//   compiler cannot know whether to link it, and SQLite-only programs must not need it installed.
// - Every wait goes through wait_fds() (G2): while one request waits for the database, the others on its
//   thread run. A cancelled request (client gone, deadline passed) stops waiting; its connection is closed.
// - `?` placeholders become $1, $2, ... here (G3); values always travel as parameters, never in the SQL.
// - Results are read whole (PQgetResult), so a `rows` never holds the connection: closing $db first is safe.
// - Connections are pooled per thread and per URL like SQLite's (G4, pool.md): returned with no transaction
//   open (ROLLBACK first), closed after a panic or a cancelled wait.
#include <ctype.h>
#include <dlfcn.h>

void pn_rows_free(Rows *r);

typedef struct PGconn PGconn;
typedef struct PGresult PGresult;
enum { PG_CONNECTION_OK = 0 };
enum { PG_POLL_FAILED = 0, PG_POLL_READING = 1, PG_POLL_WRITING = 2, PG_POLL_OK = 3 };
enum { PG_EMPTY_QUERY = 0, PG_COMMAND_OK = 1, PG_TUPLES_OK = 2, PG_FATAL_ERROR = 7 };
enum { PG_TRANS_IDLE = 0, PG_TRANS_INTRANS = 2, PG_TRANS_INERROR = 3 };
#define PG_DIAG_SQLSTATE 'C'
#define PG_DIAG_MESSAGE 'M'
#define PG_DIAG_DETAIL 'D'
#define PG_DIAG_SEVERITY 'V'
#define PG_CONNECT_MS 10000

static struct {                                      // the libpq functions used (a stable C API since 9.x)
    PGconn *(*connectStart)(const char *);
    int (*connectPoll)(PGconn *);
    int (*status)(const PGconn *);
    char *(*errorMessage)(const PGconn *);
    int (*socket)(const PGconn *);
    int (*setnonblocking)(PGconn *, int);
    int (*sendQueryParams)(PGconn *, const char *, int, const unsigned *, const char *const *, const int *, const int *, int);
    int (*flush)(PGconn *);
    int (*consumeInput)(PGconn *);
    int (*isBusy)(PGconn *);
    PGresult *(*getResult)(PGconn *);
    int (*resultStatus)(const PGresult *);
    char *(*resultErrorField)(const PGresult *, int);
    char *(*resultErrorMessage)(const PGresult *);
    void (*clear)(PGresult *);
    int (*ntuples)(const PGresult *);
    int (*nfields)(const PGresult *);
    char *(*fname)(const PGresult *, int);
    unsigned (*ftype)(const PGresult *, int);
    char *(*getvalue)(const PGresult *, int, int);
    int (*getlength)(const PGresult *, int, int);
    int (*getisnull)(const PGresult *, int, int);
    char *(*cmdTuples)(PGresult *);
    int (*transactionStatus)(const PGconn *);
    void (*finish)(PGconn *);
    void *(*setNoticeReceiver)(PGconn *, void (*)(void *, const PGresult *), void *);
} pq;
static const char *pq_missing;                       // why libpq could not be loaded (NULL = loaded)
static pthread_once_t pq_once = PTHREAD_ONCE_INIT;

static void pq_load(void) {
    const char *tries[] = {getenv("PIN_LIBPQ"),
#ifdef __APPLE__
        "libpq.5.dylib", "/opt/homebrew/opt/libpq/lib/libpq.5.dylib", "/usr/local/opt/libpq/lib/libpq.5.dylib",
        "/opt/homebrew/lib/postgresql@17/libpq.5.dylib", "/opt/homebrew/opt/postgresql@16/lib/libpq.5.dylib",
        "/Applications/Postgres.app/Contents/Versions/latest/lib/libpq.5.dylib",
#else
        "libpq.so.5", "libpq.so",
#endif
    };
    void *h = NULL;
    for (size_t i = 0; i < sizeof tries / sizeof *tries && !h; i++)
        if (tries[i] && tries[i][0]) h = dlopen(tries[i], RTLD_NOW | RTLD_LOCAL);
    if (!h) {
        pq_missing = "PostgreSQL's client library (libpq) is not installed. Ubuntu/Debian: sudo apt install libpq5 · "
                     "Fedora: sudo dnf install libpq · macOS: brew install libpq (or set PIN_LIBPQ to the file's path)";
        return;
    }
#define PQ(field, sym) if (!(*(void **)&pq.field = dlsym(h, sym))) { pq_missing = "libpq has no " sym " (too old?)"; return; }
    PQ(connectStart, "PQconnectStart") PQ(connectPoll, "PQconnectPoll") PQ(status, "PQstatus")
    PQ(errorMessage, "PQerrorMessage") PQ(socket, "PQsocket") PQ(setnonblocking, "PQsetnonblocking")
    PQ(sendQueryParams, "PQsendQueryParams") PQ(flush, "PQflush") PQ(consumeInput, "PQconsumeInput")
    PQ(isBusy, "PQisBusy") PQ(getResult, "PQgetResult") PQ(resultStatus, "PQresultStatus")
    PQ(resultErrorField, "PQresultErrorField") PQ(resultErrorMessage, "PQresultErrorMessage") PQ(clear, "PQclear")
    PQ(ntuples, "PQntuples") PQ(nfields, "PQnfields") PQ(fname, "PQfname") PQ(ftype, "PQftype")
    PQ(getvalue, "PQgetvalue") PQ(getlength, "PQgetlength") PQ(getisnull, "PQgetisnull") PQ(cmdTuples, "PQcmdTuples")
    PQ(transactionStatus, "PQtransactionStatus") PQ(finish, "PQfinish") PQ(setNoticeReceiver, "PQsetNoticeReceiver")
#undef PQ
}

static int pg_url(const char *path) { return strncmp(path, "postgres://", 11) == 0 || strncmp(path, "postgresql://", 13) == 0; }

// The URL as it may be printed: the password (user:secret@) replaced by ***.
static void pg_shown(const char *url, char *out, size_t n) {
    const char *s = strstr(url, "://"), *at = s ? strchr(s + 3, '@') : NULL;
    const char *colon = s && at ? memchr(s + 3, ':', (size_t)(at - s - 3)) : NULL;
    if (colon) snprintf(out, n, "%.*s:***%s", (int)(colon - url), url, at);
    else snprintf(out, n, "%s", url);
}

// ---------------------------------------------------------------- waiting (G2)
// 1 = ready, 0 = the wait was cut short (deadline for connecting passed, or the request was cancelled).
static int pg_wait(PGconn *c, int write, int64_t deadline) {
    int fd = pq.socket(c);
    if (fd < 0) return 0;
    return wait_fds(&fd, 1, write, deadline) == 1;
}

static void pg_trim(char *m) {                       // libpq's messages: on one line, without the final newline
    size_t n = strlen(m);
    while (n && (m[n - 1] == '\n' || m[n - 1] == ' ')) m[--n] = 0;
    char *w = m;
    for (char *r = m; *r; r++) {
        if (*r == '\n' || *r == '\t') { if (w > m && w[-1] != ' ') *w++ = ' '; continue; }
        *w++ = *r;
    }
    *w = 0;
}

__attribute__((noreturn)) static void pg_conn_panic(const char *fn, PGconn *c, const char *what) {
    char msg[600];
    snprintf(msg, sizeof msg, "%s%s%s", what, c ? ": " : "", c ? pq.errorMessage(c) : "");
    pg_trim(msg);
    db_panic(fn, msg);
}

static const char *pg_why_stopped(void) {
    return cur_req && cur_req->timed_out ? "the request's deadline passed while waiting for the database"
                                         : "the request ended while waiting for the database";
}

// The server's NOTICEs ("table does not exist, skipping") are dropped; WARNINGs go to stderr on one line.
static void pg_notice(void *arg, const PGresult *r) {
    (void)arg;
    const char *sev = pq.resultErrorField(r, PG_DIAG_SEVERITY), *m = pq.resultErrorField(r, PG_DIAG_MESSAGE);
    if (sev && m && strcmp(sev, "WARNING") == 0) fprintf(stderr, "warning: PostgreSQL: %s\n", m);
}

__attribute__((noreturn)) static void pg_connect_fail(PGconn *c, const char *what, const char *fn) {
    char msg[700];
    snprintf(msg, sizeof msg, "%s: %s", what, pq.errorMessage(c));
    pq.finish(c);
    pg_trim(msg);
    db_panic(fn, msg);
}

static PGconn *pg_connect(const char *url, const char *fn) {
    pthread_once(&pq_once, pq_load);
    if (pq_missing) db_panic(fn, pq_missing);
    PGconn *c = pq.connectStart(url);
    if (!c) db_panic(fn, "out of memory");
    char shown[300], what[400];
    pg_shown(url, shown, sizeof shown);
    snprintf(what, sizeof what, "cannot connect to %s", shown);
    if (pq.socket(c) < 0) pg_connect_fail(c, what, fn);   // a bad URL, or the host name did not resolve
    int64_t give_up = now_ms() + PG_CONNECT_MS;
    for (int st = PG_POLL_WRITING; st != PG_POLL_OK;) {
        if (st == PG_POLL_FAILED) pg_connect_fail(c, what, fn);
        if (!pg_wait(c, st == PG_POLL_WRITING, give_up)) {
            pq.finish(c);
            if (cancelled_now()) db_panic(fn, pg_why_stopped());
            snprintf(what + strlen(what), sizeof what - strlen(what), ": no answer within %d s", PG_CONNECT_MS / 1000);
            db_panic(fn, what);
        }
        st = pq.connectPoll(c);
    }
    if (pq.setnonblocking(c, 1) != 0) pg_connect_fail(c, what, fn);
    pq.setNoticeReceiver(c, pg_notice, NULL);
    return c;
}

// ---------------------------------------------------------------- one query
// Rewrite ? to $1, $2, ... outside quotes, quoted names, comments and dollar-quoted strings; *count = how many.
static char *pg_placeholders(Str *sql, int *count) {
    const char *s = sql->data;
    int64_t n = sql->len;
    char *out = xmalloc(checked_size(n, 6, 1));      // "?" -> "$NNNNN" at most
    int64_t o = 0;
    int k = 0;
    for (int64_t i = 0; i < n;) {
        char ch = s[i];
        if (ch == '\'' || ch == '"') {               // 'text' (with '' inside) or "name"
            int64_t j = i + 1;
            while (j < n && !(s[j] == ch && !(j + 1 < n && s[j + 1] == ch))) j += s[j] == ch ? 2 : 1;
            j = j < n ? j + 1 : n;
            memcpy(out + o, s + i, (size_t)(j - i)); o += j - i; i = j;
        } else if (ch == '-' && i + 1 < n && s[i + 1] == '-') {
            int64_t j = i;
            while (j < n && s[j] != '\n') j++;
            memcpy(out + o, s + i, (size_t)(j - i)); o += j - i; i = j;
        } else if (ch == '/' && i + 1 < n && s[i + 1] == '*') {
            int64_t j = i + 2;
            while (j + 1 < n && !(s[j] == '*' && s[j + 1] == '/')) j++;
            j = j + 1 < n ? j + 2 : n;
            memcpy(out + o, s + i, (size_t)(j - i)); o += j - i; i = j;
        } else if (ch == '$' && (i == 0 || !(isalnum((unsigned char)s[i - 1]) || s[i - 1] == '_'))) {
            int64_t j = i + 1;                       // $tag$ ... $tag$ (tag may be empty)
            while (j < n && (isalnum((unsigned char)s[j]) || s[j] == '_')) j++;
            if (j < n && s[j] == '$' && !(j > i + 1 && isdigit((unsigned char)s[i + 1]))) {
                int64_t tl = j - i + 1, e = j + 1;
                while (e + tl <= n && memcmp(s + e, s + i, (size_t)tl) != 0) e++;
                e = e + tl <= n ? e + tl : n;
                memcpy(out + o, s + i, (size_t)(e - i)); o += e - i; i = e;
            } else {
                out[o++] = ch; i++;
            }
        } else if (ch == '?') {
            if (k == 65535) db_panic("db_query()", "more than 65535 ? placeholders");
            o += snprintf(out + o, 7, "$%d", ++k);
            i++;
        } else {
            out[o++] = ch; i++;
        }
    }
    out[o] = 0;
    *count = k;
    return out;
}

// Send the SQL with its values and wait for the whole result. Returns the result, or NULL when the wait was cut
// short (the connection is then in an unknown state and must be closed). fn == NULL: also NULL instead of a
// panic when the connection broke (used while giving a connection back).
static PGresult *pg_send(PGconn *c, const char *sql, int np, const char *const *vals, const char *fn) {
    if (!pq.sendQueryParams(c, sql, np, NULL, vals, NULL, NULL, 0)) {
        if (!fn) return NULL;
        pg_conn_panic(fn, c, "cannot send the query");
    }
    int f;
    while ((f = pq.flush(c)) == 1)
        if (!pg_wait(c, 1, INT64_MAX / 4)) return NULL;
    if (f < 0) {
        if (!fn) return NULL;
        pg_conn_panic(fn, c, "cannot send the query");
    }
    PGresult *keep = NULL, *res;
    for (;;) {
        while (pq.isBusy(c)) {
            if (!pg_wait(c, 0, INT64_MAX / 4)) { if (keep) pq.clear(keep); return NULL; }
            if (!pq.consumeInput(c)) {
                if (keep) pq.clear(keep);
                if (!fn) return NULL;
                pg_conn_panic(fn, c, "the connection broke");
            }
        }
        if (!(res = pq.getResult(c))) break;
        if (keep && pq.resultStatus(keep) == PG_FATAL_ERROR) pq.clear(res);   // the first error is the one to show
        else { if (keep) pq.clear(keep); keep = res; }
    }
    if (!keep && fn) pg_conn_panic(fn, c, "no result");
    return keep;
}

typedef struct { PGresult *res; char *sql; char *buf; const char **vals; } PgRun;   // freed by pg_run_free

static void pg_run_free(PgRun *p) {
    if (p->res) pq.clear(p->res);
    xfree(p->sql); xfree(p->buf); xfree(p->vals);
}

static void pg_close_conn(Db *d);

// Run one statement with its ? values (kind: 0 int, 1 float, 3 string). Returns the PGresult; a constraint
// violation sets d->err and *constraint; any other error panics.
static PGresult *pg_query(Db *d, Str *sql, List *params, int64_t kind, const char *fn, int *constraint) {
    if (!d->pg) db_panic(fn, "the database is closed");
    PgRun p = {0};
    int want;
    p.sql = pg_placeholders(sql, &want);
    if (want != params->len) {
        char msg[160];
        snprintf(msg, sizeof msg, "the SQL has %d ? placeholder(s) but %lld value(s) were given", want, (long long)params->len);
        pg_run_free(&p);
        db_panic(fn, msg);
    }
    int64_t np = params->len, size = 1;
    if (kind == 3) for (int64_t i = 0; i < np; i++) size += ((Str **)params->data)[i]->len + 1;
    else size += np * 32;
    p.buf = xmalloc(checked_size(size, 1, 0));
    p.vals = xmalloc(checked_size(np, (int64_t)sizeof(char *), 1));
    char *w = p.buf;
    for (int64_t i = 0; i < np; i++) {
        p.vals[i] = w;
        if (kind == 3) {
            Str *s = ((Str **)params->data)[i];
            if (memchr(s->data, 0, (size_t)s->len)) {
                pg_run_free(&p);
                db_panic(fn, "a value contains a NUL byte, which PostgreSQL text cannot hold");
            }
            memcpy(w, s->data, (size_t)s->len); w += s->len; *w++ = 0;
        } else if (kind == 1) {
            w += snprintf(w, 32, "%.17g", ((double *)params->data)[i]) + 1;
        } else {
            w += snprintf(w, 32, "%lld", (long long)((int64_t *)params->data)[i]) + 1;
        }
    }
    p.res = pg_send(d->pg, p.sql, (int)np, p.vals, fn);
    if (!p.res) {                                    // cancelled mid-query: the connection cannot be reused
        pg_run_free(&p);
        pg_close_conn(d);
        db_panic(fn, pg_why_stopped());
    }
    PGresult *res = p.res;
    p.res = NULL;
    pg_run_free(&p);
    int st = pq.resultStatus(res);
    if (st == PG_COMMAND_OK || st == PG_TUPLES_OK) { d->err[0] = 0; return res; }
    if (st == PG_EMPTY_QUERY) { pq.clear(res); db_panic(fn, "the SQL is empty"); }
    // db_error() gets the server's one-line message (like SQLite's: it names the constraint, not the value);
    // a panic also shows the detail
    const char *code = pq.resultErrorField(res, PG_DIAG_SQLSTATE), *m = pq.resultErrorField(res, PG_DIAG_MESSAGE);
    const char *detail = pq.resultErrorField(res, PG_DIAG_DETAIL);
    snprintf(d->err, sizeof d->err, "%s", m ? m : pq.resultErrorMessage(res));
    pg_trim(d->err);
    char msg[600];
    snprintf(msg, sizeof msg, "%s%s%s", d->err, detail ? " (" : "", detail ? detail : "");
    if (detail) snprintf(msg + strlen(msg), sizeof msg - strlen(msg), ")");
    int multi = code && strcmp(code, "42601") == 0 && strstr(d->err, "multiple commands");
    int refused = code && strncmp(code, "23", 2) == 0;     // class 23: a constraint said no
    pq.clear(res);                                         // (code, m and detail point into res)
    if (constraint && refused) { *constraint = 1; return NULL; }
    if (multi) db_panic(fn, "one SQL statement per call (call it again for the next statement)");
    db_panic(fn, msg);
}

// ---------------------------------------------------------------- the pool (G4)
typedef struct PgIdle { PGconn *c; int64_t since; struct PgIdle *next; } PgIdle;
typedef struct { char url[256]; PgIdle *idle; int open; } PgSlot;
static __thread PgSlot pg_slots[POOL_KEYS];
static __thread int pg_nslots;
static int pg_checked;                               // the max_connections check ran (once per process)

static int pg_slot(const char *url) {
    if (strlen(url) >= 256) return -1;
    for (int i = 0; i < pg_nslots; i++) if (strcmp(pg_slots[i].url, url) == 0) return i;
    if (pg_nslots == POOL_KEYS) return -1;
    PgSlot *sl = &pg_slots[pg_nslots];
    snprintf(sl->url, sizeof sl->url, "%s", url);
    sl->idle = NULL; sl->open = 0;
    return pg_nslots++;
}

static void pg_scan(int64_t now) {
    for (int i = 0; i < pg_nslots; i++) {
        PgSlot *sl = &pg_slots[i];
        int idle_ms = pool_config(sl->url).idle_ms;
        for (PgIdle **pp = &sl->idle; *pp;) {
            PgIdle *c = *pp;
            if (now - c->since < idle_ms) { pp = &c->next; continue; }
            *pp = c->next;
            pq.finish(c->c);
            free(c);
            sl->open--;
        }
    }
}
static void pg_close_all(void) {
    for (int i = 0; i < pg_nslots; i++)
        for (PgIdle *c = pg_slots[i].idle, *n; c; c = n) { n = c->next; pq.finish(c->c); free(c); }
    pg_nslots = 0;
}

// Run SQL with no values; 1 = it worked (out: the first value), 0 = the server refused it, -1 = the connection
// broke or the wait was cut short (it must then be closed). Never panics (used while giving a connection back).
static int pg_simple(PGconn *c, const char *sql, char *out, size_t n) {
    PGresult *r = pg_send(c, sql, 0, NULL, NULL);
    if (!r) return -1;
    int st = pq.resultStatus(r), ok = st == PG_COMMAND_OK || st == PG_TUPLES_OK;
    if (out && ok && pq.ntuples(r) == 1) snprintf(out, n, "%s", pq.getvalue(r, 0, 0));
    pq.clear(r);
    return ok;
}

// Threads x connections per thread must stay under the server's max_connections (G4): say so once if not.
// 0 = the check could not run (the connection must be closed).
static int pg_check_limit(PGconn *c, const char *url) {
    if (__atomic_exchange_n(&pg_checked, 1, __ATOMIC_ACQ_REL)) return 1;
    char v[32] = "";
    int got = pg_simple(c, "SHOW max_connections", v, sizeof v);
    if (got < 0) return 0;
    if (!got || !v[0]) return 1;
    int server_max = atoi(v), threads = serve_threads(), per = pool_config(url).max;
    if ((int64_t)threads * per > server_max - 3) {
        char shown[300];
        pg_shown(url, shown, sizeof shown);
        fprintf(stderr, "warning: %s: up to %d threads x %d connections = %d, but the server allows %d "
                "(max_connections, 3 kept for admins). Lower it with db_pool(url, per_thread, idle_ms) or PN_THREADS\n",
                shown, threads, per, threads * per, server_max);
    }
    return 1;
}

static void pg_close_conn(Db *d) {                   // close, never return to the pool
    if (d->pg) { pq.finish(d->pg); if (d->slot >= 0) pg_slots[d->slot].open--; }
    d->pg = NULL;
}

static void pg_release(Db *d) {
    PGconn *c = d->pg;
    PgSlot *sl = &pg_slots[d->slot];
    int keep = !(cur_req && (cur_req->cancelled || cur_req->panicked)) && pq.status(c) == PG_CONNECTION_OK;
    int ts = keep ? pq.transactionStatus(c) : -1;
    if (keep && (ts == PG_TRANS_INTRANS || ts == PG_TRANS_INERROR))   // never let a transaction reach the next user
        keep = pg_simple(c, "ROLLBACK", NULL, 0) > 0 && pq.transactionStatus(c) == PG_TRANS_IDLE;
    else if (keep && ts != PG_TRANS_IDLE) keep = 0;
    PgIdle *n = keep ? malloc(sizeof *n) : NULL;
    if (!n) { pg_close_conn(d); return; }
    n->c = c; n->since = now_ms(); n->next = sl->idle;
    sl->idle = n;
    d->pg = NULL;
}

static Db *pg_open(Str *url) {
    const char *u = url->data;
    pthread_once(&pool_once, pool_init);
    int slot = pg_slot(u);
    PGconn *c = NULL;
    if (slot >= 0) {
        int max = pool_config(u).max;
        int64_t give_up = now_ms() + POOL_WAIT_MS;
        for (;;) {
            PgSlot *sl = &pg_slots[slot];
            while (sl->idle && !c) {                 // a connection the server closed while it was idle is dropped
                PgIdle *i = sl->idle;
                sl->idle = i->next;
                if (pq.status(i->c) == PG_CONNECTION_OK && pq.consumeInput(i->c) && pq.status(i->c) == PG_CONNECTION_OK) c = i->c;
                else { pq.finish(i->c); sl->open--; }
                free(i);
            }
            if (c) break;
            if (sl->open < max) { c = pg_connect(u, "db_open()"); sl->open++; break; }
            if (cancelled_now()) db_panic("db_open()", "the request ended while waiting for a connection");
            if (now_ms() >= give_up) {
                char shown[300], msg[500];
                pg_shown(u, shown, sizeof shown);
                snprintf(msg, sizeof msg, "all %d connections of this thread to %s stayed in use for %d s "
                         "(raise the limit with db_pool(), or close a $db before waiting)", max, shown, POOL_WAIT_MS / 1000);
                db_panic("db_open()", msg);
            }
            pn_sleep(2);
        }
    } else {
        c = pg_connect(u, "db_open()");
    }
    Db *d = xmalloc(sizeof(Db));
    d->h = NULL; d->pg = c; d->slot = slot; d->err[0] = 0;
    d->res = pn_res_add(db_res_close, d);
    if (!pg_check_limit(c, u)) { pg_close_conn(d); db_panic("db_open()", pg_why_stopped()); }
    return d;
}

// ---------------------------------------------------------------- results (G5)
enum { OID_BOOL = 16, OID_INT8 = 20, OID_INT2 = 21, OID_INT4 = 23, OID_OID = 26, OID_JSON = 114,
       OID_FLOAT4 = 700, OID_FLOAT8 = 701, OID_NUMERIC = 1700, OID_JSONB = 3802 };

static int64_t pg_exec(Db *d, Str *sql, List *params, int64_t kind) {
    int constraint = 0;
    PGresult *r = pg_query(d, sql, params, kind, "db_exec()", &constraint);
    if (!r) return -1;
    const char *t = pq.cmdTuples(r);
    int64_t n = t && t[0] ? strtoll(t, NULL, 10) : 0;
    pq.clear(r);
    return n;
}

static Rows *pg_rows(Db *d, Str *sql, List *params, int64_t kind) {
    PGresult *res = pg_query(d, sql, params, kind, "db_query()", NULL);
    Rows *r = xmalloc(sizeof(Rows));
    r->st = NULL; r->pg = res; r->row = -1; r->state = 0;
    r->res = pn_res_add(rows_res_close, r);
    return r;
}

static int64_t pg_next(Rows *r) {
    if (!r->pg || r->state == 2) return 0;
    if (++r->row < pq.ntuples(r->pg)) { r->state = 1; return 1; }
    r->state = 2;
    return 0;
}

static int pg_col(Rows *r, Str *name, const char *fn) {
    if (r->state != 1) db_panic(fn, "no current row: read columns only after db_next($rows) returned true");
    int n = pq.nfields(r->pg);
    for (int i = 0; i < n; i++) {
        const char *c = pq.fname(r->pg, i);
        if ((int64_t)strlen(c) == name->len && memcmp(c, name->data, (size_t)name->len) == 0) return i;
    }
    char msg[512];
    int off = snprintf(msg, sizeof msg, "no column named \"%s\" (the query has:", name->data);
    for (int i = 0; i < n && off < (int)sizeof msg - 40; i++)
        off += snprintf(msg + off, sizeof msg - (size_t)off, " %s", pq.fname(r->pg, i));
    snprintf(msg + off, sizeof msg - (size_t)off, ")");
    db_panic(fn, msg);
}

__attribute__((noreturn)) static void pg_not_a(const char *fn, Rows *r, int i, const char *what) {
    char msg[200];
    snprintf(msg, sizeof msg, "column \"%s\" holds \"%.40s\", not %s", pq.fname(r->pg, i), pq.getvalue(r->pg, r->row, i), what);
    db_panic(fn, msg);
}

// Strict: the whole text must be the number (an int column read with db_float is fine; "12abc" is not).
static int64_t pg_int(Rows *r, Str *col) {
    int i = pg_col(r, col, "db_int()");
    if (pq.getisnull(r->pg, r->row, i)) return 0;
    const char *v = pq.getvalue(r->pg, r->row, i);
    if (pq.ftype(r->pg, i) == OID_BOOL) return v[0] == 't';
    char *end;
    errno = 0;
    long long x = strtoll(v, &end, 10);
    if (end == v || *end || errno) pg_not_a("db_int()", r, i, "an integer that fits in int");
    return x;
}

static double pg_float(Rows *r, Str *col) {
    int i = pg_col(r, col, "db_float()");
    if (pq.getisnull(r->pg, r->row, i)) return 0;
    const char *v = pq.getvalue(r->pg, r->row, i);
    char *end;
    double x = strtod(v, &end);
    if (end == v || *end) pg_not_a("db_float()", r, i, "a number");
    return x;
}

// JSON's number syntax exactly (numeric "NaN" / float "Infinity" are not numbers in JSON: they become strings).
static int json_number_text(const char *s) {
    if (*s == '-') s++;
    if (*s == '0') s++;
    else if (*s >= '1' && *s <= '9') while (isdigit((unsigned char)*s)) s++;
    else return 0;
    if (*s == '.') { s++; if (!isdigit((unsigned char)*s)) return 0; while (isdigit((unsigned char)*s)) s++; }
    if (*s == 'e' || *s == 'E') {
        s++;
        if (*s == '+' || *s == '-') s++;
        if (!isdigit((unsigned char)*s)) return 0;
        while (isdigit((unsigned char)*s)) s++;
    }
    return *s == 0;
}

static Str *pg_json(Db *d, Str *sql, List *params, int64_t kind) {
    Rows *r = pg_rows(d, sql, params, kind);         // a heap Rows, so a panic below can free the result
    PGresult *res = r->pg;
    int rows = pq.ntuples(res), n = pq.nfields(res);
    Str *out = str_add_bytes(NULL, "[", 1);
    for (int row = 0; row < rows; row++) {
        out = str_add_bytes(out, row ? ",{" : "{", row ? 2 : 1);
        for (int i = 0; i < n; i++) {
            if (i) out = str_add_bytes(out, ",", 1);
            const char *name = pq.fname(res, i);
            out = json_text(out, name, (int64_t)strlen(name));
            out = str_add_bytes(out, ":", 1);
            if (pq.getisnull(res, row, i)) { out = str_add_bytes(out, "null", 4); continue; }
            const char *v = pq.getvalue(res, row, i);
            int64_t len = pq.getlength(res, row, i);
            switch (pq.ftype(res, i)) {
            case OID_BOOL: out = v[0] == 't' ? str_add_bytes(out, "true", 4) : str_add_bytes(out, "false", 5); break;
            case OID_INT2: case OID_INT4: case OID_INT8: case OID_OID: case OID_FLOAT4: case OID_FLOAT8: case OID_NUMERIC:
                out = json_number_text(v) ? str_add_bytes(out, v, len) : json_text(out, v, len);
                break;
            case OID_JSON: case OID_JSONB: out = add_text(out, v, len); break;
            default: out = json_text(out, v, len);
            }
        }
        out = str_add_bytes(out, "}", 1);
    }
    pn_rows_free(r);
    return str_add_bytes(out, "]", 1);
}

static void pq_clear_rows(Rows *r) { if (r->pg) pq.clear(r->pg); r->pg = NULL; }
