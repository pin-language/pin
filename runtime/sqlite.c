// runtime/sqlite.c — the database built-ins (compiled in only with -DPIN_SQLITE). Part of runtime.c (one translation unit: see runtime.c).

// ============================================================================ SQLite (db_* built-ins)
// Compiled only for programs that use the database (the compiler adds -DPIN_SQLITE -lsqlite3).
// `db` and `rows` are ordinary owned values: the compiler frees them like lists, which closes the
// connection / finalizes the statement. Inside a request they are also registered as resources, so a
// panic closes them before the request's memory is freed. A connection is closed with sqlite3_close_v2,
// which waits for its statements to be finalized, so freeing a db before its rows is safe.
#ifdef PIN_SQLITE
#include <sqlite3.h>

typedef struct { sqlite3 *h; void *res; int slot; char err[256]; } Db;  // slot: its pool slot, -1 = not pooled
typedef struct { sqlite3_stmt *st; void *res; int state; } Rows;      // state: 0 before first row, 1 row, 2 done

// ---------------------------------------------------------------- the pool (pin-language/docs design/pool.md)
// Opening a connection costs ~80-200 µs (file, WAL, pragmas); a query by key ~3 µs. Pin has no globals, so
// every request opens its own `db`: the runtime keeps the connections instead, per thread and per path. Tasks
// never leave their thread, so the pool needs no locks. A connection comes back only clean (no transaction,
// no statement half-way); after a panic or a cancelled request it is closed. ":memory:" and temporary
// databases ("") are never pooled: each open must be a fresh, empty database.
#define POOL_KEYS 16
#define POOL_WAIT_MS 5000
typedef struct PoolConn { sqlite3 *h; int64_t since; struct PoolConn *next; } PoolConn;
typedef struct { char path[256]; int max, idle_ms; } PoolCfg;
typedef struct { char path[256]; PoolConn *idle; int open; } PoolSlot;
static PoolCfg pool_cfg[POOL_KEYS];                   // db_pool(): written by the top level before serve() starts threads
static int pool_ncfg;
static __thread PoolSlot pool_slots[POOL_KEYS];
static __thread int pool_nslots;

static int pool_poolable(const char *path) {
    return path[0] && !strstr(path, ":memory:") && !strstr(path, "mode=memory") && strlen(path) < 256;
}
static PoolCfg pool_config(const char *path) {
    for (int i = 0; i < pool_ncfg; i++) if (strcmp(pool_cfg[i].path, path) == 0) return pool_cfg[i];
    PoolCfg c = {"", 4, 60000};
    return c;
}
static void pool_scan(int64_t now) {                  // the server's once-a-second scan: close long-idle connections
    for (int i = 0; i < pool_nslots; i++) {
        PoolSlot *sl = &pool_slots[i];
        int idle_ms = pool_config(sl->path).idle_ms;
        for (PoolConn **pp = &sl->idle; *pp;) {
            PoolConn *c = *pp;
            if (now - c->since < idle_ms) { pp = &c->next; continue; }
            *pp = c->next;
            sqlite3_close_v2(c->h);
            free(c);
            sl->open--;
        }
    }
}
static void pool_close_all(void) {                    // at exit: this thread's idle connections (LeakSanitizer)
    for (int i = 0; i < pool_nslots; i++)
        for (PoolConn *c = pool_slots[i].idle, *n; c; c = n) { n = c->next; sqlite3_close_v2(c->h); free(c); }
    pool_nslots = 0;
}
static pthread_once_t pool_once = PTHREAD_ONCE_INIT;
static void pool_init(void) { atexit(pool_close_all); __atomic_store_n(&pool_scan_hook, pool_scan, __ATOMIC_RELEASE); }
static int pool_slot(const char *path) {
    for (int i = 0; i < pool_nslots; i++) if (strcmp(pool_slots[i].path, path) == 0) return i;
    if (pool_nslots == POOL_KEYS) return -1;          // more databases than slots: open and close as before
    PoolSlot *sl = &pool_slots[pool_nslots];
    snprintf(sl->path, sizeof sl->path, "%s", path);
    sl->idle = NULL; sl->open = 0;
    return pool_nslots++;
}
// Give a connection back, or close it when it may not be clean.
static void pool_release(int slot, sqlite3 *h) {
    PoolSlot *sl = &pool_slots[slot];
    int keep = !(cur_req && (cur_req->cancelled || cur_req->panicked));
    if (keep && !sqlite3_get_autocommit(h)) {         // a transaction left open: never let it reach the next user
        sqlite3_exec(h, "ROLLBACK", NULL, NULL, NULL);
        keep = sqlite3_get_autocommit(h);
    }
    for (sqlite3_stmt *st = keep ? sqlite3_next_stmt(h, NULL) : NULL; st; st = sqlite3_next_stmt(h, st))
        if (sqlite3_stmt_busy(st)) { keep = 0; break; }   // a result set still being read belongs to its owner
    if (!keep) { sqlite3_close_v2(h); sl->open--; return; }
    PoolConn *c = malloc(sizeof *c);
    if (!c) { sqlite3_close_v2(h); sl->open--; return; }
    c->h = h; c->since = now_ms(); c->next = sl->idle;
    sl->idle = c;
}

static void db_res_close(void *o) {                   // a panic: close, never return to the pool
    Db *d = o;
    if (d->h) { sqlite3_close_v2(d->h); if (d->slot >= 0) pool_slots[d->slot].open--; }
    d->h = NULL;
}
static void rows_res_close(void *o) { Rows *r = o; if (r->st) sqlite3_finalize(r->st); r->st = NULL; }

__attribute__((noreturn)) static void db_panic(const char *what, const char *detail) {
    fflush(stdout);
    fprintf(stderr, "panic: %s: %s\n", what, detail);
    die();
}

static sqlite3 *db_connect(Str *path) {
    sqlite3 *h = NULL;
    int rc = sqlite3_open_v2(path->data, &h, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_NOMUTEX, NULL);
    if (rc != SQLITE_OK) {
        char msg[300];
        snprintf(msg, sizeof msg, "cannot open %s (%s)", path->data, h ? sqlite3_errmsg(h) : sqlite3_errstr(rc));
        if (h) sqlite3_close_v2(h);
        db_panic("db_open()", msg);
    }
    sqlite3_busy_timeout(h, 5000);                       // wait for other writers instead of failing at once
    sqlite3_exec(h, "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;", NULL, NULL, NULL);
    return h;
}

Db *pn_db_open(Str *path) {
    sqlite3 *h = NULL;
    int slot = -1;
    if (pool_poolable(path->data)) {
        pthread_once(&pool_once, pool_init);
        slot = pool_slot(path->data);
    }
    if (slot >= 0) {
        int max = pool_config(path->data).max;
        int64_t give_up = now_ms() + POOL_WAIT_MS;
        for (;;) {
            PoolSlot *sl = &pool_slots[slot];
            if (sl->idle) {
                PoolConn *c = sl->idle;
                sl->idle = c->next;
                h = c->h;
                free(c);
                break;
            }
            if (sl->open < max) { h = db_connect(path); sl->open++; break; }
            if (cur_req && cur_req->cancelled) db_panic("db_open()", "the request ended while waiting for a connection");
            if (now_ms() >= give_up) {
                char msg[200];
                snprintf(msg, sizeof msg, "all %d connections of this thread to %s stayed in use for %d s "
                         "(raise the limit with db_pool(), or close a $db before waiting)", max, path->data, POOL_WAIT_MS / 1000);
                db_panic("db_open()", msg);
            }
            pn_sleep(2);                                  // other tasks on this thread run meanwhile
        }
    } else {
        h = db_connect(path);
    }
    Db *d = xmalloc(sizeof(Db));
    d->h = h; d->slot = slot; d->err[0] = 0;
    d->res = pn_res_add(db_res_close, d);
    return d;
}

// db_pool($path, $per_thread, $idle_ms): only at the top level (the compiler checks), before serve().
void pn_db_pool(Str *path, int64_t per_thread, int64_t idle_ms) {
    if (per_thread < 1 || per_thread > 1000) db_panic("db_pool()", "connections per thread must be 1 to 1000");
    if (idle_ms < 0 || idle_ms > 86400000) db_panic("db_pool()", "the idle time must be 0 to 86400000 ms (one day)");
    if (!pool_poolable(path->data)) db_panic("db_pool()", "\":memory:\" and temporary databases are never pooled");
    int i = 0;
    while (i < pool_ncfg && strcmp(pool_cfg[i].path, path->data) != 0) i++;
    if (i == POOL_KEYS) db_panic("db_pool()", "at most 16 databases can be configured");
    snprintf(pool_cfg[i].path, sizeof pool_cfg[i].path, "%s", path->data);
    pool_cfg[i].max = (int)per_thread; pool_cfg[i].idle_ms = (int)idle_ms;
    if (i == pool_ncfg) pool_ncfg++;
}

void pn_db_close(Db *d) {
    if (!d) return;
    pn_res_remove(d->res);
    if (d->h) {
        if (d->slot >= 0) pool_release(d->slot, d->h);
        else sqlite3_close_v2(d->h);
    }
    xfree(d);
}

void pn_rows_free(Rows *r) {
    if (!r) return;
    pn_res_remove(r->res);
    if (r->st) sqlite3_finalize(r->st);
    xfree(r);
}

// prepare one statement and bind the list of values to its ? placeholders (kind: 0 int, 1 float, 3 string)
static sqlite3_stmt *db_prepare(Db *d, Str *sql, List *params, int64_t kind, const char *fn) {
    if (!d->h) db_panic(fn, "the database is closed");
    sqlite3_stmt *st = NULL;
    const char *tail = NULL;
    if (sqlite3_prepare_v2(d->h, sql->data, (int)sql->len, &st, &tail) != SQLITE_OK)
        db_panic(fn, sqlite3_errmsg(d->h));
    if (!st) db_panic(fn, "the SQL is empty");
    while (tail && tail < sql->data + sql->len && (*tail == ' ' || *tail == '\n' || *tail == '\t' || *tail == '\r' || *tail == ';'))
        tail++;
    if (tail && tail < sql->data + sql->len) {
        sqlite3_finalize(st);
        db_panic(fn, "one SQL statement per call (call it again for the next statement)");
    }
    int want = sqlite3_bind_parameter_count(st);
    if (want != params->len) {
        char msg[160];
        snprintf(msg, sizeof msg, "the SQL has %d ? placeholder(s) but %lld value(s) were given", want, (long long)params->len);
        sqlite3_finalize(st);
        db_panic(fn, msg);
    }
    for (int64_t i = 0; i < params->len; i++) {
        int rc;
        if (kind == 3) {
            Str *s = ((Str **)params->data)[i];
            rc = sqlite3_bind_text(st, (int)i + 1, s->data, (int)s->len, SQLITE_TRANSIENT);
        } else if (kind == 1) {
            rc = sqlite3_bind_double(st, (int)i + 1, ((double *)params->data)[i]);
        } else {
            rc = sqlite3_bind_int64(st, (int)i + 1, ((int64_t *)params->data)[i]);
        }
        if (rc != SQLITE_OK) { sqlite3_finalize(st); db_panic(fn, sqlite3_errmsg(d->h)); }
    }
    return st;
}

// INSERT / UPDATE / DELETE / CREATE ...: rows changed, or -1 when a constraint (UNIQUE, NOT NULL, CHECK,
// FOREIGN KEY) refused it — db_error() says which. Any other failure is a bug in the SQL: panic.
int64_t pn_db_exec(Db *d, Str *sql, List *params, int64_t kind) {
    sqlite3_stmt *st = db_prepare(d, sql, params, kind, "db_exec()");
    int rc;
    while ((rc = sqlite3_step(st)) == SQLITE_ROW) {}
    if (rc == SQLITE_DONE) {
        sqlite3_finalize(st);
        d->err[0] = 0;
        return sqlite3_changes(d->h);
    }
    snprintf(d->err, sizeof d->err, "%s", sqlite3_errmsg(d->h));
    sqlite3_finalize(st);
    if ((rc & 0xff) == SQLITE_CONSTRAINT) return -1;
    db_panic("db_exec()", d->err);
}

Rows *pn_db_query(Db *d, Str *sql, List *params, int64_t kind) {
    sqlite3_stmt *st = db_prepare(d, sql, params, kind, "db_query()");
    Rows *r = xmalloc(sizeof(Rows));
    r->st = st; r->state = 0;
    r->res = pn_res_add(rows_res_close, r);
    return r;
}

int64_t pn_db_next(Rows *r) {
    if (!r->st || r->state == 2) return 0;
    int rc = sqlite3_step(r->st);
    if (rc == SQLITE_ROW) { r->state = 1; return 1; }
    if (rc == SQLITE_DONE) { r->state = 2; return 0; }
    db_panic("db_next()", sqlite3_errmsg(sqlite3_db_handle(r->st)));
}

static int db_col(Rows *r, Str *name, const char *fn) {
    if (r->state != 1) db_panic(fn, "no current row: read columns only after db_next($rows) returned true");
    int n = sqlite3_column_count(r->st);
    for (int i = 0; i < n; i++) {
        const char *c = sqlite3_column_name(r->st, i);
        if (c && (int64_t)strlen(c) == name->len && memcmp(c, name->data, (size_t)name->len) == 0) return i;
    }
    char msg[512];
    int off = snprintf(msg, sizeof msg, "no column named \"%s\" (the query has:", name->data);
    for (int i = 0; i < n && off < (int)sizeof msg - 40; i++)
        off += snprintf(msg + off, sizeof msg - (size_t)off, " %s", sqlite3_column_name(r->st, i));
    snprintf(msg + off, sizeof msg - (size_t)off, ")");
    db_panic(fn, msg);
}

int64_t pn_db_int(Rows *r, Str *col) { return sqlite3_column_int64(r->st, db_col(r, col, "db_int()")); }
double pn_db_float(Rows *r, Str *col) { return sqlite3_column_double(r->st, db_col(r, col, "db_float()")); }
int64_t pn_db_is_null(Rows *r, Str *col) { return sqlite3_column_type(r->st, db_col(r, col, "db_is_null()")) == SQLITE_NULL; }

Str *pn_db_string(Rows *r, Str *col) {
    int i = db_col(r, col, "db_string()");
    const unsigned char *p = sqlite3_column_text(r->st, i);
    return p ? str_from((const char *)p, sqlite3_column_bytes(r->st, i)) : str_alloc(0);
}

int64_t pn_db_last_id(Db *d) { return d->h ? sqlite3_last_insert_rowid(d->h) : 0; }
Str *pn_db_error(Db *d) { return str_from(d->err, (int64_t)strlen(d->err)); }

// the whole result as a JSON array of objects, with JSON types from SQLite's (NULL -> null)
Str *pn_db_json(Db *d, Str *sql, List *params, int64_t kind) {
    Rows *r = pn_db_query(d, sql, params, kind);         // a heap Rows, so a panic below can finalize it
    sqlite3_stmt *st = r->st;
    Str *out = str_add_bytes(NULL, "[", 1);
    int rc, first = 1, n = sqlite3_column_count(st);
    while ((rc = sqlite3_step(st)) == SQLITE_ROW) {
        out = str_add_bytes(out, first ? "{" : ",{", first ? 1 : 2);
        first = 0;
        for (int i = 0; i < n; i++) {
            if (i) out = str_add_bytes(out, ",", 1);
            const char *name = sqlite3_column_name(st, i);
            out = json_string(out, name, (int64_t)strlen(name));
            out = str_add_bytes(out, ":", 1);
            switch (sqlite3_column_type(st, i)) {
            case SQLITE_INTEGER: out = pn_str_add_int(out, sqlite3_column_int64(st, i)); break;
            case SQLITE_FLOAT: out = json_float(out, sqlite3_column_double(st, i)); break;
            case SQLITE_NULL: out = str_add_bytes(out, "null", 4); break;
            default: {
                const char *p = (const char *)sqlite3_column_text(st, i);
                out = json_string(out, p ? p : "", sqlite3_column_bytes(st, i));
            }
            }
        }
        out = str_add_bytes(out, "}", 1);
    }
    if (rc != SQLITE_DONE) {
        char msg[256];
        snprintf(msg, sizeof msg, "%s", sqlite3_errmsg(d->h));
        db_panic("db_json()", msg);
    }
    pn_rows_free(r);
    return str_add_bytes(out, "]", 1);
}
#endif
