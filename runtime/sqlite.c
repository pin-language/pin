// runtime/sqlite.c — the database built-ins (compiled in only with -DPIN_SQLITE). Part of runtime.c (one translation unit: see runtime.c).

// ============================================================================ SQLite (db_* built-ins)
// Compiled only for programs that use the database (the compiler adds -DPIN_SQLITE -lsqlite3).
// `db` and `rows` are ordinary owned values: the compiler frees them like lists, which closes the
// connection / finalizes the statement. Inside a request they are also registered as resources, so a
// panic closes them before the request's memory is freed. A connection is closed with sqlite3_close_v2,
// which waits for its statements to be finalized, so freeing a db before its rows is safe.
#ifdef PIN_SQLITE
#include <sqlite3.h>

typedef struct { sqlite3 *h; void *res; char err[256]; } Db;
typedef struct { sqlite3_stmt *st; void *res; int state; } Rows;      // state: 0 before first row, 1 row, 2 done

static void db_res_close(void *o) { Db *d = o; if (d->h) sqlite3_close_v2(d->h); d->h = NULL; }
static void rows_res_close(void *o) { Rows *r = o; if (r->st) sqlite3_finalize(r->st); r->st = NULL; }

__attribute__((noreturn)) static void db_panic(const char *what, const char *detail) {
    fflush(stdout);
    fprintf(stderr, "panic: %s: %s\n", what, detail);
    die();
}

Db *pn_db_open(Str *path) {
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
    Db *d = xmalloc(sizeof(Db));
    d->h = h; d->err[0] = 0;
    d->res = pn_res_add(db_res_close, d);
    return d;
}

void pn_db_close(Db *d) {
    if (!d) return;
    pn_res_remove(d->res);
    if (d->h) sqlite3_close_v2(d->h);
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
