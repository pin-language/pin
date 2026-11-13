# Changelog

## 0.9.0 — 2026-11-12

Breaking, on purpose (strings are always UTF-8): `chr()` above 127 panics (use `mb_chr()`), `substr()` with an
offset inside a character panics, a request whose path or body is not UTF-8 gets 400.

### Memory safety and robustness

- **Fix (memory safety):** every HTTP request wrote into freed memory after its answer (`request_done` left
  `cur_req` pointing at the freed request; the task switch copied it back and `task_release` wrote through it).
  Under load with database handlers the server crashed (SIGSEGV). Most likely there since 0.7.0 (tasks); not bisected.
- **Fixed a use-after-free:** a list literal whose items were lists or maps from `try f()` (or a borrowed
  `$maybe ?? [...]`) freed them while the new list still held them. List and map literals now take owned items,
  copy borrowed ones, and free what they hold when a later item fails (that used to leak).
- **A recursion that goes too deep no longer kills the server**: it is a panic, so that request answers 500 and the
  others go on (before, the whole process crashed: one request could take a server down). Outside a server it is a
  clear message and exit code 1. Guard pages below task stacks grew from one page to 64 KB. Throughput unchanged.
- `python3 tests/run_tests.py --sanitize` builds the HTTP servers with ASAN + UBSAN and fails on any report;
  CI runs it (tools/sanitize.sh only runs programs that exit, so the servers were never sanitized since 0.7.0).
- A request whose `db_open()` finds no free pooled connection within 5 s, or a PostgreSQL server that does not
  answer, now gets **503** instead of 500 (B15). It is still a panic; only the status changed.
### Null

- **null** (design/null.md steps 1–4): `?string`, `?list<T>`, `?map<K,V>` and `?ClassName` for variables,
  parameters, returns and properties (a `?T` property starts as null). A `?T` variable must be checked before use;
  `if ($u !== null)`, `if ($u === null) { return; }`, `&&`, `||`, `!`, `isset($u)`, `while` and `?:` narrow it to
  `T`. `$u?->name`, `$x ?? "default"`, `find($id) ?? throw error(404, "...")`. `json_encode` writes `null`;
  `json_decode` gives null for a `?T` field that is null or missing. A class may hold `?Itself` (linked lists).
  Same pointer as before with null = 0: programs without `?T` compile to the same code. `=== null` on a value that is never null
  is a compile error.
- **`?int`, `?float`, `?bool`** (design/null.md step 5): the value and a "has a value" flag side by side in
  registers, two 8-byte slots in an object. Checks, `??`, `?? throw`, `?->`, `?:`, `&` parameters, defaults and
  JSON work as for `?string`. Programs without them compile to the same code as before (all 51 tests, examples
  and benchmarks checked).
- **`list<?T>` and `map<K,?T>`** (design/null.md step 6): items that may be null, e.g. `[1, null, 3]` or
  `map<string,?string>`. An item is `?T` (check it, or `$xs[$i] ?? 0`, which also covers a null item);
  `isset()` is false for a null item as in PHP; `fill($n, null)`; JSON both ways. Existing programs compile to the
  same code (one new `declare` line).
- **A checked property counts as not null** (null.md N2): `if ($u->email !== null) { strlen($u->email); }`,
  `$u->addr !== null && $u->addr->zip !== null`, `$this->email`, until the object is changed (assignment, a
  changing method, `&`, or a loop that does so).
- `go` tasks take and give `?int`, `?float`, `?bool` (also through `await_all` and `await_timeout`).
- Fix: `list<?string>` lexed `<?` as the PHP open tag.

### Strings

- **Strings are always valid UTF-8** (design/strings.md ST1, ST3, ST4). A request whose path or body is not UTF-8
  gets 400; other text from outside (headers, `%XX` values, database columns, other servers' answers, `run()`,
  `read_line()`, `env()`) has bad bytes replaced by U+FFFD. `substr()` panics instead of cutting a character in
  half. **Breaking:** `chr()` above 127 panics; use the new `mb_chr()`. New: `mb_strlen`, `mb_substr`,
  `mb_strpos`, `mb_chr`, `mb_ord`, `grapheme_strlen`, `grapheme_substr` (UAX #29, Unicode 17, checked against
  Python's `unicodedata.iter_graphemes()` on 100,000 strings). HTTP throughput unchanged (`ab`, 215k–218k req/s
  new vs 202k–219k old).
- **`mb_strtolower`, `mb_strtoupper`** (full Unicode case, final sigma) and **`normalizer_normalize()`** with
  `Normalizer::FORM_C` / `FORM_D` / `FORM_KC` / `FORM_KD` (strings.md ST5, ST6): every code point and 200,000
  strings give what Python gives. Binaries are now linked with dead stripping: a program carries only the parts
  of the runtime it uses (hello: 148 KB → 54 KB).
- `ord()`, `chr()` and `rawurlencode()` (PHP's): bytes to numbers and back, and percent-encoding for values in URLs
  (used by Tack's MCP tools to build request paths).

### Databases

- **PostgreSQL** with the same `db_*` functions: `db_open(env("DATABASE_URL"))` with a `postgres://` URL
  (design/postgres.md). libpq does the protocol, SCRAM and TLS; it is loaded at run time only by programs that
  open PostgreSQL, so SQLite programs need nothing new. Queries never block the thread (4 × `pg_sleep(0.4)` on one
  thread: 0.42 s), `request_deadline()` cuts them with 504, connections are pooled per thread and returned with no
  transaction open. `?` placeholders stay; `INSERT ... RETURNING id` replaces `db_last_id`. New compile errors: a
  `postgres://` literal in `db_open`/`db_pool`, and SQL that would change a pooled connection (`SET` without
  `LOCAL`, `RESET`, `LISTEN`, `PREPARE`, `DISCARD`, `LOAD`, `set_config(..., false)`, session advisory locks).
  Tested in CI against PostgreSQL 16 on Linux and macOS, also under ASAN + UBSAN.
- **Connection pool** (pin-language/docs design/pool.md, P1): `db_open()` takes a connection from a pool the
  runtime keeps per server thread, and the end of `$db`'s scope gives it back — clean only (an open transaction
  is rolled back; after a panic or a cancelled request it is closed). Same programs, ~7× more requests per
  second for a one-query SQLite handler (181k–189k vs 24.6k–26.0k req/s, M2, load generator on the same
  machine). `db_pool($path, $per_thread, $idle_ms)` at the top level sets the limits (default 4, 60 s); a full
  pool makes `db_open()` wait up to 5 s, then panic with 503. `:memory:` is never pooled.
- **SQL from literals in a variable** (postgres.md G6): `$sql = "SELECT ... WHERE 1 = 1"; if ($q != "") { $sql .=
  " AND name = ?"; $ps[] = $q; }` now compiles — a variable whose every write is literal pieces is accepted as SQL,
  so search endpoints with optional filters and sorting need no SQL injection risk. Anything else still does not
  compile, and the connection rule (K4) checks the variable's pieces too.
- **`db_all()` / `db_one()`**: rows straight into objects, `list<Order> $os = db_all($db, "SELECT ...", [])`,
  `?Order $o = db_one(...)`. The class comes from where the value goes; a row that does not fit panics. Tack
  checks the columns against the class at build time (SQLite and PostgreSQL).
- New compile errors: a `db` / `rows` passed to `go` (open one inside the task); SQL that changes the
  connection for its next user (`PRAGMA foreign_keys = ...`, `ATTACH`, `CREATE TEMP`); `db_pool()` inside a
  function.

### Language and built-ins

- **Closures** (design/closures.md, all four steps): `fn($x) => ...` and `function ($x) use ($a, &$b) { ... }`.
  Passed to `array_map`, `array_filter`, `usort`, the new `array_reduce` or your own `fn(T): R` parameters they take
  their parameter types from there and borrow what they read (nothing copied or allocated; as fast as a named
  function). `use (&$x)` changes `$x`; every call of such a closure is checked as a change of `$x` (no `foreach`
  over it, no loan of it used later). Closures can be returned, stored in properties, lists and maps, and given to
  `go`: those own what they use (strings and numbers copied, lists / maps / objects moved; `copy()` to keep yours).
  `by_price(...)` makes a function a value; `$f($x)`, `adder(1)(2)` and `($this->onClick)($x)` call one.
- **`filter_var()`** with `FILTER_VALIDATE_EMAIL`, `_URL`, `_IP` (`?string`), `_INT` (`?int`), `_FLOAT`, `_BOOL`:
  null where PHP gives false, same answers as PHP; `ctype_*`, `nl2br`, `json_validate`, `array_key_first` / `_last`.
- `ksort` / `krsort` / `asort` / `arsort` (stable), `range`, `array_count_values`, `array_flip`, `array_combine`,
  `mb_str_split`, the `**` operator and `pow`, `fmod`, `log`, `log10`, `exp`, trig functions, `pi`, `is_nan` /
  `is_finite` / `is_infinite`, and `M_PI`, `M_E`, `NAN`, `INF`, `PHP_FLOAT_*`.
- **`array_map("label", $xs)`** and **`array_filter($xs, "cheap")`** with a function name (checked signature;
  the compiler writes the loop); `array_filter` renumbers from 0.
- **`usort($xs, "by_price")`**: sort lists of objects (or ints, floats, strings, lists, maps) with a function of
  the same file named as a string, `(T $a, T $b): int` checked at compile time; stable, as PHP 8.
- **`<=>`** (each side evaluated once) for `usort` comparisons; `array_map` / `array_filter` also take one-argument
  built-ins by name (`array_map("strtoupper", $xs)`); the closure messages say how to pass a named function.
- **Bit operators** `| & ^ ~ << >>` and `|= &= ^= <<= >>=` on ints (PHP 8 precedence; a shift of 64+ is 0, a
  negative shift panics), and `0xFF` / `0b1010` / `0o17` / `1_000_000` literals. `array_pop` / `array_shift` (give
  `?T`), `json_encode($v, JSON_PRETTY_PRINT)`; the other `JSON_` flags are accepted and change nothing.
- **`sprintf` / `printf`** with a literal format (each value's type is checked against it at compile time: `%d`
  with a float does not build), and **`date($format, $ts)`** with PHP's letters, in UTC. Both match PHP's output.
- `array_slice`, `array_merge`, `array_reverse` (items copied, any copyable type), `array_search` (-1 when not found),
  `array_unique`, `is_numeric`, `str_pad` (+ `STR_PAD_*`), `mb_str_pad`, `ucwords`, and `strrev` by grapheme (PHP's breaks UTF-8).
- **21 PHP helpers:** `str_replace`, `ltrim`, `rtrim`, `ucfirst`, `lcfirst`, `htmlspecialchars`, `base64_encode`,
  `base64_decode` (throws for non-base64 or non-UTF-8), `rawurldecode`, `urldecode`, `number_format`, `round` (PHP's
  half-away-from-zero, `round(1.955, 2)` is 1.96), `floor`, `ceil`, `abs`, `max` / `min` (two numbers or a list),
  `in_array` (strict), `array_keys`, `array_values`, `array_sum`.
- **Parameter defaults** (P3, first half): `function page(string $q, int $limit = 20)`; literals only, last in the
  list, not on `&` parameters; works for functions, methods, constructors and `go`. In `json_decode()` a
  constructor parameter with a default is an optional field.
- **`json_decode()` into a class** (P2): `Order $o = try json_decode($body);` builds the object and checks
  it strictly — a missing, unknown or repeated field, a wrong type, invalid JSON or nesting past 64 levels is
  `error(400, "$.lines[1].qty: want an integer, got a string")`. A class with `__construct` is built through it
  (its checks run); fields can be classes, `list<T>` and `map<string,T>` to any depth. The compiler writes one
  decoder per class in Pin (`gen_decoders`) on top of `runtime/jdecode.c`, which reads the text by position.
- **`time()`**: Unix seconds, like PHP. Tack's sessions use it instead of SQLite's clock, so their SQL runs on
  PostgreSQL too. `date()` / `microtime()` are still compile errors that point at it.
- `env("NAME")`: an environment variable (`""` when unset), for secrets like database URLs.
- Import paths may have `[name]` / `[...name]` parts (route files of the Tack framework): `import
  routes/users/[id] as user;` (a name with `as` is required for them). The built-in `Error` type can be named in
  modules too (it was "unknown type" outside the main file).
- `request_ip()`: the client's address (like `$_SERVER['REMOTE_ADDR']`).

### HTTP server

- **`request_deadline($ms)`**: the whole request — its waits and its tasks' waits (`await`, `sleep_ms`, `hub_next`,
  `http_*`, `run`) — gets at most that long; then every wait fails with 504 and the client gets `504 Gateway
  Timeout`, whatever the handler returns afterwards. Built on the same cancellation as a client that leaves;
  computing loops are not interrupted. Checked under load and with ASAN (tests/server/deadline.pn).
- Fix: an HTTP/1.0 client asking for keep-alive is now told `Connection: keep-alive` (`ab -k` hung).

### Security

- **Security built-ins** (P5): `hash("sha256", $s)`, `hash_hmac("sha256", $data, $key)`, `hash_equals()` (constant
  time), `random_token($bytes)` (hex), `random_int($min, $max)` (OS randomness, no bias), `password_hash()` /
  `password_verify()` (PBKDF2-HMAC-SHA256, 600,000 rounds, random salt). SHA-256 / HMAC / PBKDF2 are written in
  `runtime/crypto.c` (no new library) and checked against NIST, RFC 4231 and RFC 7914 vectors. Known cost:
  ~0.5 s per password hash with the portable code; the CPU's SHA-256 instructions are used on arm64 and x86 (below).
  `md5`, `sha1`, `rand`, `mt_rand`, `random_bytes`, `uniqid` and `PASSWORD_DEFAULT` now say what to write instead.
- `password_hash` / `password_verify` use the CPU's SHA-256 instructions on arm64 (Apple, and Linux when the CPU
  has them): ~0.04 s instead of ~0.5 s on an M2, same results (checked against the portable code on 100,000
  random blocks and the published vectors).
- SHA-256 uses the x86 SHA extensions when the CPU has them (as it already did on arm64): `password_hash()` on
  Linux x86 servers gets the same ~13x as on Apple M2.

## 0.8.0 — 2026-10-25

MCP servers in Pin: nested JSON, standard input, and the first package built on Pin.


- Nested JSON: `json_raw($json, $key)` (the JSON text of any value), `json_len($array)`, `json_at($array, $i)`;
  the key `""` reads the text itself. MCP's `params.arguments` can now be read.
- `read_line()` reads standard input (flushes output first; end of input fails with code 1) — what an MCP
  server over stdio needs. First package on top: `github.com/pin-language/mcp` (pin-mcp), checked against
  the official MCP Python SDK.
- No TLS by design (design/tls.md): a proxy does https both ways; the https:// error says so.
- A map literal with mixed value types now suggests a class (it still pointed to building JSON by hand).
- runtime.c split into runtime/*.c (one translation unit, object code unchanged).
- examples/mcp_http.pn: an MCP-style JSON-RPC server over HTTP.

## 0.7.0 — 2026-10-25

Tasks and real time (design: concurrency.md): a program can wait for several things at once, and requests
can talk to each other — what an agent board needs to wake agents without polling.

- `go f(...)` / `await` / `await_all` / `await_timeout`, `Task<T>`, `sleep_ms`. Green threads with their own
  stacks (guard page), switched in assembly; a task runs only on the thread that started it, so tasks cannot
  race. Checked when compiling: every task is awaited before its function returns; its arguments cannot
  change until then; `&` arguments are exclusive.
- Every server request runs as a task: a handler that waits no longer blocks the others on its thread (3 ms
  answers while another request waits). A panic in any task of a request answers 500 and frees all of them.
- Hub: `hub_publish`, `hub_subscribe`, `hub_resume` (the last 100 messages per topic), `hub_next` (time limit
  required), `hub_last_id` — across worker threads; slow readers are dropped instead of growing memory.
- Server-Sent Events from a handler: `sse_start`, `sse_send`, `sse_send_id`, `sse_comment`; a client that
  leaves cancels its request (499); reconnecting with `Last-Event-ID` resumes.
- Calling out (http:// only — TLS later): `http_get`, `http_post`, `http_stream` + `stream_next` /
  `stream_last_id`; `run($program, $args, $ms)` starts a program with no shell (nothing can be injected).
- PHP's `sleep`, `usleep`, `exec`, `shell_exec`, `proc_open`, `curl_init`, `file_get_contents` point to the new
  functions. Docs: Learn chapter 14, the AI guide's Tasks section, the language guide, three reference pages.
- Fixed while building: a request with no header lines at all made the server try to allocate a huge block
  (new code only; 0.6 was not affected).
- HTTP throughput about the same as 0.6 (Linux container, wrk; run-to-run spread larger than the difference).

## 0.6.0 — 2026-10-25

Errors you expect (`throws` / `try` / `catch`), and an HTTP server that reads headers and protects itself.

### Request and response headers, server limits

- `request_header("Authorization")` (case-insensitive, trimmed, `""` if absent), `request_has_header()`,
  `response_header("Location", "/x")` (repeatable for `Set-Cookie`; `Content-Type` replaces the default).
  A header value with CR/LF or another control byte panics, so request data cannot inject headers; the
  headers the server writes itself cannot be set.
- Limits against slow and hostile clients (B5): a request must arrive within 15 s of its first byte (408; a
  connection that sends nothing is closed too), idle keep-alive connections close after 60 s, unread
  responses after 30 s, at most 10 000 open connections (503). `PN_READ_TIMEOUT_MS`, `PN_IDLE_TIMEOUT_MS`,
  `PN_WRITE_TIMEOUT_MS`, `PN_MAX_CONNS` change them.
- Request smuggling closed: `Transfer-Encoding` is refused (501) instead of being misread, and a
  `Content-Length` that is not plain digits or appears twice with different values gets 400. More than
  64 KB of headers gets 431 (it was a silent close), a body over 64 MB gets 413.
- HTTP throughput unchanged (Linux container, wrk, 2 threads: 0.5 runtime 369k–395k req/s, 0.6 371k–386k;
  noise level).

### Errors

Errors you expect: `throws`, `try`, `catch` (design: error-handling.md), compiled to return values.

- `function f(): T throws` (methods and `__construct` too); `throw error($code, $message);` — one error type
  `Error` with public `$code` and `$message`.
- Every call that can fail needs `try`: `try f()` passes the error on (only in a `throws` function),
  `try f() catch ($e) { ...; value; }` handles it in an expression, `try { ... } catch ($e) { ... }` handles
  several lines. Forgetting `try`, a `try` with nothing that can fail, and a catch block without a value are
  compile errors that say what to write. `catch (Exception $e)` is accepted; `finally` is not in Pin yet.
- No stack unwinding: a failing call returns its error next to its result, in registers. Benchmark programs
  compile to exactly the same code as before.
- The error path frees what the failing code owned — temporaries, foreach temporaries, variables, a
  half-built object when its constructor fails (LeakSanitizer and macOS `leaks` clean).
- An error escaping the top level prints `error: <message> (code N)` and exits 1. A `serve()` handler may be
  declared `throws`: an escaping error answers its code (400–599, else 500) with the standard status text;
  the message goes to the log only, never to the client.
- Docs: Learn chapter 9, the AI guide's Errors section, the language guide, and `error()` in the reference.

## 0.5.0 — 2026-10-25

Classes, nested containers and `&`: data with mixed types and structure, without a garbage collector.

- `class` with typed `public` / `private` properties, literal defaults, `__construct` (with PHP 8 promoted
  parameters), methods and `$this`; `new Name(...)`; `$o->prop`, `$o->method()`. No inheritance,
  interfaces, `static` or `null` (each says so when used).
- The constructor must give every property a value on every path, and cannot read a property or use `$this`
  before that (checked when compiling).
- Objects are values with one owner, like lists: they move on `=`, `copy()` copies deeply, parameters are
  read-only loans. A method that assigns `$this->...` (or calls one that does) is inferred to be a changing
  method, and can only be called on something the caller may change.
- Any type nests: `list<Order>`, `list<list<int>>`, `map<string,list<string>>`, `map<int,User>`, objects
  holding lists and objects. Change values through paths: `$users[0]->name = "x"`, `$m["a"]["b"] += 1`,
  `$o->items[] = $x`; `sort()` and `unset()` work on paths too.
- `&` parameters change the caller's value, written at the call as well: `discount(&$order)`, also for paths
  (`&$orders[0]`). The call's other arguments may not use the same value. `foreach ($xs as &$x)` changes
  items in place.
- `$u = $users[0];` borrows read-only instead of copying; changing `$users` while `$u` is used again later
  is a compile error that names the line.
- `json_encode` writes objects (public properties, in order), lists and maps of them, and nested lists:
  mixed-type JSON objects no longer have to be built as strings.
- `"{$o->items[0]->name}"` and `"$o->name"` in strings; `export class` in modules, used as `mod::Name`.
- Error messages for read-only parameters now suggest `&`. Parameter default values get a clear error.
- Every heap object still belongs to its request: a panicking handler frees objects and nested lists too
  (tested with 300 panics holding 1,000 lists each). ASAN, UBSAN and LeakSanitizer clean.
- Docs: Learn chapter 13 (classes), the AI guide's Classes section, language guide, reference for `copy` and
  `json_encode`. VS Code highlights `class`, `new`, `public`/`private`, `$this`, `->` and `&`.

## 0.4.1 — 2026-10-25

- Fix: on Linux without SQLite's development files, every program failed to build — even hello world — so
  the installer stopped at its hello test. Since 0.3.0 the compiler linked SQLite whenever the program merely
  declared the `db_*` functions, which every program does; it now links SQLite only for programs that call
  them. Found by installing from the public URL on a clean Debian container (Mac and CI both had SQLite, so
  tests did not see it). A test now checks the decision.

## 0.4.0 — 2026-10-24

Modules and packages: a program can be many files, and can use code from other repositories.

- `import prices;` loads `prices.pn` from the project folder (the main file's folder) (`import util/text as t;` for folders and
  short names); call `prices::price()`. Only `export function` can be called from other files.
- Imported files may contain only functions and imports, so importing never runs code; import cycles, missing
  files, unexported functions and unknown module names are compile errors that name the fix. Errors and
  `--json` name the file they are in.
- `pin get github.com/owner/repo@version` downloads a package (any Git repository) and writes its commit and a
  SHA-256 of its `.pn` files to `pin.lock`; `import github.com/owner/repo/module;` uses it. The hash is checked
  on every build: code that changed after it was locked does not compile. The cache refills itself from the
  locked commit.
- PHP's `include`, `require`, `use` and `namespace` now point at `import`.
- VS Code: `import`, `export`, `as` and `module::fn()` are highlighted. Docs: Learn chapter 12.

## 0.3.0 — 2026-10-24

SQLite: a Pin API can now store data.

- `db_open`, `db_exec`, `db_query` + `db_next` + `db_int` / `db_float` / `db_string` / `db_is_null`,
  `db_json` (a whole result as a JSON array), `db_last_id`, `db_error`.
- **SQL injection does not compile:** the SQL must be a string literal; values go in `?` placeholders.
- `db` and `rows` are owned values: they close when they go out of scope, move on `=`, are lent to
  functions, and are closed before the 500 when a handler panics (300 panicking requests left 0 extra open
  files). Connections use WAL and a 5-second busy timeout; 200 parallel inserts on 4 threads all landed.
- Constraint violations make `db_exec` return -1 with `db_error`; other SQL errors panic with SQLite's
  message. One statement per call.
- Programs link SQLite only if they use it. Linux needs `libsqlite3-dev`; `install.sh` says so.
- Docs: `reference/database.md`, Learn chapter 11, `examples/notes_api.pn`.

## 0.2.0 — 2026-10-24

Writing a real JSON API no longer needs hand-written parsing loops.

- `list<string>`: lists own their strings (freed, copied and JSON-encoded correctly). Changing a string
  element or sorting while a `foreach` walks the list is a compile error, since the loop variable borrows it.
- `explode($sep, $s)`, `implode($glue, $list)`, `sort($xs)`, `rsort($xs)` (int, float, bool, string).
- Request helpers: `url_path($path)`, `query_param($path, $key)`, `form_param($body, $key)` (URL-decoded),
  and `json_has` / `json_string` / `json_int` / `json_float` / `json_bool` for top-level keys of a JSON
  body. Malformed or hostile input gives the default value; nesting deeper than 64 levels is rejected.
  `tests/json_hostile.pn` runs these parsers on broken input under ASAN in CI.
- `examples/api.pn` handles GET with a query string and POST with a JSON body; `examples/wordcount.pn`
  uses `explode`, `list<string>` and `sort`.
- Built-in reference (github.com/pin-language/docs, `reference/`): every function, constant and cast with a
  runnable example and its real output, generated from `spec/pin_builtins.py`; CI fails if the docs and the
  compiler disagree.
- Documentation moved to github.com/pin-language/docs; benchmarks to github.com/pin-language/benchmarks.
- Numbers from text never fail: `intval()` and `json_int()` saturate at `PHP_INT_MAX` / `PHP_INT_MIN` like
  PHP (0.1.0 panicked, which turned a bad client number into a 500); `floatval()` and `(float)"2.5"` read
  the leading number; `query_has()` / `form_has()` tell a missing key from an empty one.
- `pin FILE.pn` runs the program; `pin -v`; one-command installer (`install.sh`).

## 0.1.0 — 2026-10-24 (first preview for testers)

Pin is aimed at AI coding agents: `ai-guide.md` (pin-language/docs) is the whole language on one page, and
`pin check --json` gives machine-readable errors.

### Language
- No `<?php` tag: files are code from the first line. A leading `<?php` (from pasted PHP) is ignored.
- Variables are checked on every path: using a variable that is not assigned on all paths is a compile
  error (this used to compile and could crash on a null list).
- Moves are tracked per branch: moving a value in one `if` branch no longer blocks the other branch, and
  moving right before `break` or `return` is allowed.
- `wrapping_add`, `wrapping_sub`, `wrapping_mul`; constants `PHP_INT_MAX`, `PHP_INT_MIN`, `PHP_EOL`.
- A non-void function that can reach its end without `return` is a compile error (was a run-time panic).
- PHP features that Pin lacks (`class`, `new`, `try`, `switch`, `match`, `null`, closures, `array()`,
  `include`, `global`, ...) get a message saying so and what to write instead.
- `json_encode` of a whole float like 380.0 gives `380.0` (it gave `3.8e+02`).
- `intdiv(PHP_INT_MIN, -1)` panics (it was undefined behaviour and crashed x86 machines); `x % -1` is 0.
- `(int)` of NAN, INF or a float outside the int range panics; `intval()` of a number that does not fit
  panics instead of giving a wrong value.
- `+ - *` on ints wrap around. (Overflow panics were tried and removed for now: up to 23% slower.)

### Runtime and server
- Builds and serves on macOS (kqueue) as well as Linux (epoll).
- A panic inside an HTTP handler answers `500 {"error":"Internal Server Error"}`, frees everything that
  request allocated, and the server keeps running (it used to exit).
- Every allocation size coming from a program value is checked; `fill()` with a huge count could
  previously produce a buffer smaller than the list claimed (heap overflow).

### Tools
- One-command install: `curl -fsSL https://raw.githubusercontent.com/pin-language/pin/HEAD/install.sh | sh`
  (checks Python and clang, installs into `~/.pin`, links `pin` into `~/.local/bin`, no sudo).
- VS Code extension in `editors/vscode`: syntax highlighting, comment toggling, brackets, auto-indent.
- `pin` command: `pin FILE.pn` (runs it), `pin run`, `pin build`, `pin check`, `pin version` / `-v`; `--json` for machine-readable
  errors. A missing file, a non-UTF-8 file or a missing clang gives a one-line message.
- Builds no longer leave `.ll` files next to the source.
- `examples/` with six runnable programs; English README, language guide, AI guide, `llms.txt`.
- Test suite: 57 checks, run on macOS and Linux, plus sanitizer runs (ASAN/UBSAN, LeakSanitizer on
  Linux) and a GitHub Actions workflow.
