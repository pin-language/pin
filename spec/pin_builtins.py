#!/usr/bin/env python3
"""Source of truth for Pin's built-in functions, constants and casts.

After editing:  python3 tools/gen_docs.py --record   runs every example and stores its output in
spec/outputs.json (review the diff), then  python3 tools/gen_docs.py --docs ../pin-docs  regenerates the
reference pages, the AI guide's built-in list and the VS Code grammar. tests/run_tests.py checks that this
list matches the compiler and that every example still prints exactly its recorded output.
"""

ITEMS = []


def f(name, cat, sig, summary, example, notes=(), php=None, run=True, kind="function"):
    ITEMS.append(dict(name=name, kind=kind, category=cat, signature=sig, summary=summary, notes=list(notes),
                      php=php, example=example.strip("\n"), run=run))


S, L, N, V, J, H = "strings", "lists-and-maps", "numbers", "conversions", "json", "http"

# ----------------------------------------------------------------------------------------- strings
f("strlen", S, "strlen(string $s): int", "Length of a string in bytes.",
  r'''echo strlen("hello"), " ", strlen("ไทย"), " ", strlen(""), "\n";''',
  ["Counts bytes, not characters: each Thai character is 3 bytes in UTF-8."])
f("substr", S, "substr(string $s, int $start, int $length = rest): string", "Part of a string, by byte offsets.",
  r'''echo substr("hello world", 6), "|", substr("hello", -3), "|", substr("hello", 1, 3), "|", substr("hello", 1, -1), "|", substr("hi", 10), "|\n";''',
  ["A negative `$start` counts from the end; a negative `$length` stops that many bytes before the end.",
   "Out-of-range offsets give a shorter or empty string, never a panic.",
   "Offsets are bytes: cutting inside a Thai character gives broken text."])
f("strpos", S, "strpos(string $haystack, string $needle): int", "Byte position of the first match, or -1.",
  r'''echo strpos("hello", "ll"), " ", strpos("hello", "z"), " ", strpos("abc", ""), "\n";''',
  ["An empty needle matches at 0."],
  php="PHP returns `false` when not found; Pin returns `-1`, so write `strpos($s, $x) < 0`. There is no offset argument yet.")
f("str_contains", S, "str_contains(string $haystack, string $needle): bool", "Whether `$needle` occurs in `$haystack`.",
  r'''echo str_contains("pineapple", "apple") ? "yes" : "no", "\n";''')
f("str_starts_with", S, "str_starts_with(string $s, string $prefix): bool", "Whether `$s` begins with `$prefix`.",
  r'''echo str_starts_with("/user/42", "/user/") ? "yes" : "no", "\n";''')
f("str_ends_with", S, "str_ends_with(string $s, string $suffix): bool", "Whether `$s` ends with `$suffix`.",
  r'''echo str_ends_with("photo.png", ".png") ? "yes" : "no", "\n";''')
f("str_repeat", S, "str_repeat(string $s, int $times): string", "`$s` repeated `$times` times.",
  r'''echo str_repeat("ab", 3), "|", str_repeat("x", 0), "|\n";''',
  ["A negative count panics; a result too large to allocate panics."])
f("strtolower", S, "strtolower(string $s): string", "Lower-case copy; only A–Z change.",
  r'''echo strtolower("Hello ÄB ไทย"), "\n";''',
  ["Non-ASCII letters are left as they are (PHP 8.2+ behaves the same)."])
f("strtoupper", S, "strtoupper(string $s): string", "Upper-case copy; only a–z change.",
  r'''echo strtoupper("mixed Case"), "\n";''')
f("trim", S, "trim(string $s): string", "Copy without leading and trailing whitespace.",
  r'''echo "[", trim("  padded \n"), "]\n";''',
  [r"Removes spaces, tabs, `\n`, `\r` and `\v`. There is no second argument yet."])
f("explode", S, "explode(string $separator, string $s): list<string>", "Split a string into a list of strings.",
  r'''echo json_encode(explode(",", "a,b,,c")), " ", count(explode(",", "")), "\n";''',
  ["Empty pieces are kept. Splitting an empty string gives one empty piece.",
   "An empty separator panics (PHP throws a ValueError)."],
  php="Same argument order as PHP. There is no `$limit` argument yet.")
f("implode", S, "implode(string $glue, list<string|int|float> $xs): string", "Join a list into one string.",
  r'''echo implode(", ", ["tea", "cake"]), " | ", implode("-", [1, 2, 3]), "\n";''',
  ["Works on `list<string>`, `list<int>` and `list<float>`, not `list<bool>`."],
  php="Only the `implode($glue, $list)` order is accepted.")
f("PHP_EOL", S, "PHP_EOL: string", r'The string `"\n"`.', r'''echo "line", PHP_EOL;''', kind="constant")

# ----------------------------------------------------------------------------------------- lists and maps
f("count", L, "count(list|map $xs): int", "Number of items in a list or entries in a map.",
  r'''echo count([1, 2, 3]), " ", count(["a" => 1]), "\n";''')
f("fill", L, "fill(int $n, int|float|bool|string $value): list", "A new list with `$n` copies of `$value`.",
  r'''$flags = fill(3, false);
$flags[1] = true;
echo json_encode($flags), " ", json_encode(fill(2, "x")), "\n";''',
  ["A negative or impossibly large `$n` panics."], php="Pin's version of `array_fill(0, $n, $value)`.")
f("copy", L, "copy(list|map|object|string $x): same type", "An independent copy you own, with everything inside it.",
  r'''$a = [1, 2];
$b = copy($a);
$b[] = 3;
$grid = [[1], [2]];
$g2 = copy($grid);
$g2[0][] = 9;
echo count($a), " ", count($b), " ", json_encode($grid), " ", json_encode($g2), "\n";''',
  ["Lists, maps and objects move on `$b = $a`; use `copy()` when you need two of them.",
   "A function parameter is read-only; `copy()` it before changing it (or declare it `&`).",
   "Copies deeply: nested lists, maps and objects are copied too. A value holding a `db` or `rows` cannot be copied."])
f("sort", L, "sort(list $xs): void", "Sort a list variable in place, smallest first.",
  r'''$xs = [3, 1, 2];
sort($xs);
$names = ["bob", "Alice", "carol"];
sort($names);
echo json_encode($xs), " ", json_encode($names), "\n";''',
  ["Works on lists of int, float, bool and string; strings sort by bytes, so upper case comes before lower case.",
   "The argument must be a list variable you own (not a parameter, not an expression), and not one a `foreach` is walking."],
  php="There are no sort flags or custom comparisons (`usort`) yet.")
f("rsort", L, "rsort(list $xs): void", "Sort a list variable in place, largest first.",
  r'''$xs = [3, 1, 2];
rsort($xs);
echo json_encode($xs), "\n";''',
  ["Same rules as `sort()`."])
f("isset", L, "isset($list[$i] | $map[$key]): bool", "Whether a list index is in range or a map key exists.",
  r'''$m = ["tea" => 40];
$xs = [10, 20];
echo isset($m["tea"]) ? "y" : "n", isset($m["cake"]) ? "y" : "n", isset($xs[1]) ? "y" : "n", isset($xs[5]) ? "y" : "n", "\n";''',
  ["Use it (or `??`) before reading a key that may be missing: reading a missing map key panics."],
  php="Works only on one list item or map entry; there is no `null`, so there is no `isset($x)` for a plain variable.")
f("array_key_exists", L, "array_key_exists(key, map|list $m): bool", "Same as `isset($m[$key])`.",
  r'''$m = ["a" => 1];
echo array_key_exists("a", $m) ? "yes" : "no", "\n";''',
  ["The container must be a variable or a function result, not a literal."])
f("unset", L, "unset($map[$key]): void", "Remove a map entry (nothing happens if it is missing).",
  r'''$m = ["a" => 1, "b" => 2];
unset($m["a"]);
unset($m["zzz"]);
echo json_encode($m), "\n";''',
  ["Maps only. To drop items from a list, build a new list without them.",
   "Not allowed while a `foreach` walks the map."])

# ----------------------------------------------------------------------------------------- numbers
f("intdiv", N, "intdiv(int $a, int $b): int", "Integer division, rounding toward zero.",
  r'''echo intdiv(7, 2), " ", intdiv(-7, 2), "\n";''',
  ["`/` always gives a float, like PHP; use `intdiv()` for list indexes and counts.",
   "Division by zero panics; `intdiv(PHP_INT_MIN, -1)` panics (there is no int answer)."])
f("sqrt", N, "sqrt(int|float $x): float", "Square root.",
  r'''echo sqrt(16), " ", sqrt(2.0), "\n";''',
  ["`sqrt` of a negative number gives NAN."])
f("wrapping_add", N, "wrapping_add(int $a, int $b): int", "`$a + $b`, wrapping around on 64-bit overflow on purpose.",
  r'''echo wrapping_add(PHP_INT_MAX, 1) == PHP_INT_MIN ? "wrapped" : "no", "\n";''',
  ["Plain `+ - *` also wrap today, but only these functions promise it: use them in hashes and checksums so the "
   "code stays correct when overflow checks are added."])
f("wrapping_sub", N, "wrapping_sub(int $a, int $b): int", "`$a - $b` with 64-bit wrap-around.",
  r'''echo wrapping_sub(PHP_INT_MIN, 1) == PHP_INT_MAX ? "wrapped" : "no", "\n";''')
f("wrapping_mul", N, "wrapping_mul(int $a, int $b): int", "`$a * $b` with 64-bit wrap-around.",
  r'''$h = 0;
foreach ([104, 105] as $c) { $h = wrapping_mul(wrapping_add($h, $c), 1099511628211); }
echo $h, "\n";''')
f("PHP_INT_MAX", N, "PHP_INT_MAX: int", "The largest int, 9223372036854775807.",
  r'''echo PHP_INT_MAX, "\n";''', kind="constant")
f("PHP_INT_MIN", N, "PHP_INT_MIN: int", "The smallest int, -9223372036854775808.",
  r'''echo PHP_INT_MIN, "\n";''', kind="constant")

# ----------------------------------------------------------------------------------------- conversions
f("intval", V, "intval(string|float|bool|int $x): int", "Convert to int.",
  r'''echo intval("42"), " ", intval("  -7kg"), " ", intval("abc"), " ", intval(3.9), " ", intval("99999999999999999999"), "\n";''',
  ['From a string: optional spaces and sign, then digits; anything else stops the number, so `"abc"` is 0.',
   "Too big for an int: `PHP_INT_MAX` / `PHP_INT_MIN`, like PHP — range-check numbers that come from requests.",
   "From a float: drops the fraction; NAN, INF or out-of-range floats panic."])
f("floatval", V, "floatval(string|int|float $x): float", "Convert to float.",
  r'''echo floatval("2.5kg"), " ", floatval("-1.25e2"), " ", floatval("abc"), " ", floatval(3), "\n";''',
  ['From a string: the leading number; `"abc"` is 0.'])
f("strval", V, "strval(int|float|bool|string $x): string", "Convert to string, the way `echo` prints it.",
  r'''echo strval(42) . "|" . strval(2.50) . "|" . strval(true) . "|" . strval(false) . "|\n";''',
  ['Floats print like PHP: whole floats lose `.0` (`2.0` prints `2`). `true` is `"1"`, `false` is `""`.'])
f("(int)", V, "(int) $x", "Cast to int: the same as `intval()`.",
  r'''echo (int)"12" + (int)2.9, "\n";''', kind="cast")
f("(float)", V, "(float) $x", "Cast to float from int or string: the same as `floatval()`.",
  r'''echo (float)7 / 2, " ", (float)"0.5", "\n";''', kind="cast")
f("(string)", V, "(string) $x", "Cast to string: the same as `strval()`.",
  r'''echo (string)5 . "!", "\n";''', kind="cast",
  notes=["There is no `(bool)` cast: compare instead, e.g. `$n != 0`."])

# ----------------------------------------------------------------------------------------- json
f("json_encode", J, "json_encode(int|float|bool|string|list|map|object $x): string", "JSON text for a value.",
  r'''class Line {
    public function __construct(public string $item, public int $qty, private int $cost) {}
}
echo json_encode(["name" => "ไทย", "note" => "a\"b"]), " ", json_encode([1.5, 2.0]), " ", json_encode("x"), "\n";
echo json_encode([new Line("tea", 2, 80)]), " ", json_encode([[1, 2], [3]]), "\n";''',
  ["UTF-8 text is kept as is (like `JSON_UNESCAPED_UNICODE`); whole floats keep `.0`.",
   "Objects become JSON objects of their public properties, in declaration order; private properties are left out.",
   "A map has one value type; for a JSON object with mixed values, use a class."],
  php=r"PHP escapes non-ASCII as `\uXXXX` and drops `.0` unless `JSON_PRESERVE_ZERO_FRACTION` is set.")
f("json_raw", J, "json_raw(string $json, string $key): string",
  "The JSON text of one value of an object (an object, an array or anything), to read further with the json_* functions.",
  r'''$body = "{\"params\": {\"name\": \"add\", \"arguments\": {\"a\": 5, \"b\": 10}}}";
$params = json_raw($body, "params");
$args = json_raw($params, "arguments");
echo json_string($params, "name"), " ", json_int($args, "a") + json_int($args, "b"), "\n";''',
  ['`""` when the key is missing or the text is not a JSON object.',
   'The key `""` means "the text itself": `json_string(json_at($list, 0), "")` reads an array item.',
   "The text is copied as it was sent, so `json_raw($body, \"id\")` can be written back into an answer as is."],
  php="Instead of `json_decode($body, true)['params']['arguments']`: one level at a time.")
f("json_len", J, "json_len(string $json): int",
  "The number of items of a JSON array (or keys of an object); -1 when the text is neither.",
  r'''echo json_len("[1, 2, 3]"), " ", json_len("{\"a\": 1}"), " ", json_len("7"), "\n";''')
f("json_at", J, "json_at(string $json, int $i): string",
  "The JSON text of item `$i` (from 0) of a JSON array, or `\"\"` when there is no such item.",
  r'''$tags = "[\"tea\", \"cake\"]";
for ($i = 0; $i < json_len($tags); $i++) {
    echo json_string(json_at($tags, $i), ""), "\n";
}''',
  ['Read an item with key `""`: `json_int(json_at($nums, 0), "")`; an item that is an object with its keys.'])
f("json_has", J, "json_has(string $json, string $key): bool", "Whether a JSON object has a top-level key.",
  r'''$b = "{\"title\": \"tea\", \"qty\": null}";
echo json_has($b, "title") ? "y" : "n", json_has($b, "qty") ? "y" : "n", json_has($b, "x") ? "y" : "n", json_has("not json", "title") ? "y" : "n", "\n";''',
  ["True even when the value is `null`. False when the text is not a JSON object."])
f("json_string", J, "json_string(string $json, string $key): string", 'A top-level string value, or `""`.',
  r'''echo json_string("{\"name\": \"Som\\u0e01\", \"n\": 5}", "name"), "|", json_string("{\"n\": 5}", "n"), "|\n";''',
  ['`""` when the key is missing, the value is not a string, or the text is not a JSON object.',
   r'Escapes are decoded (`\n`, `\"`, `\uXXXX` including surrogate pairs).'],
  php="Instead of `json_decode($body, true)['name']`. Pin reads top-level keys of a JSON object with these "
      "functions; nested objects and arrays are skipped safely (a full `json_decode` for nested data comes later).")
f("json_int", J, "json_int(string $json, string $key): int", "A top-level integer value, or 0.",
  r'''echo json_int("{\"qty\": 3}", "qty"), " ", json_int("{\"qty\": 1.5}", "qty"), " ", json_int("{\"qty\": \"3\"}", "qty"), " ", json_int("{\"qty\": 99999999999999999999}", "qty"), "\n";''',
  ['0 when missing, not an integer (`1.5`, `"3"`), or not a JSON object — use `json_has` to tell missing from 0.',
   "Too big for an int: `PHP_INT_MAX` / `PHP_INT_MIN`."])
f("json_float", J, "json_float(string $json, string $key): float", "A top-level number as a float, or 0.0.",
  r'''echo json_float("{\"price\": 12.5}", "price"), " ", json_float("{\"price\": 3}", "price"), " ", json_float("{\"price\": 1e999}", "price"), "\n";''',
  ["Integers are accepted. Out-of-range numbers like `1e999` give 0."])
f("json_bool", J, "json_bool(string $json, string $key): bool", "Whether a top-level value is `true`.",
  r'''echo json_bool("{\"ok\": true}", "ok") ? "y" : "n", json_bool("{\"ok\": 1}", "ok") ? "y" : "n", "\n";''',
  ["Only the JSON literal `true` counts."])

# ----------------------------------------------------------------------------------------- http
f("serve", H, 'serve(int $port, "handler"): void', "Run an HTTP/1.1 server; never returns.",
  r'''function handle(string $method, string $path, string $body): string {
    return json_encode(["you_asked_for" => $path]);
}
serve(8080, "handle");''',
  ["The handler must be `function NAME(string $method, string $path, string $body): string`; its return value "
   "is the response body, sent as `application/json`.",
   "One thread per CPU core (`PN_THREADS=n` to change), keep-alive and pipelining. A panic in the handler "
   "answers 500 and the server keeps running.",
   "Speaks HTTP/1.1 without TLS, by design: put a reverse proxy (Caddy, nginx, a load balancer) in front for "
   "https. It has request/response headers, timeouts and connection limits (see the language guide)."], run=False)
f("http_status", H, "http_status(int $code): void", "Set the status code of the current response (default 200).",
  r'''function handle(string $method, string $path, string $body): string {
    http_status(404);
    return "{\"error\":\"not found\"}";
}
serve(8080, "handle");''', run=False)
f("request_header", H, "request_header(string $name): string",
  'The value of a request header (name matched without case), or `""` when absent.',
  r'''function handle(string $method, string $path, string $body): string {
    $token = request_header("Authorization");          // "Bearer abc..." or ""
    if (!str_starts_with($token, "Bearer ")) { http_status(401); return "{\"error\":\"login first\"}"; }
    return json_encode(["agent" => request_header("User-Agent")]);
}
serve(8080, "handle");''',
  ["Spaces around the value are removed. With the same header twice, the first one is returned.",
   'Outside a `serve()` handler it returns `""`.'], run=False,
  php="Instead of `$_SERVER['HTTP_AUTHORIZATION']` or `getallheaders()['Authorization']`.")
f("request_has_header", H, "request_has_header(string $name): bool",
  "Whether the request has the header, also when its value is empty.",
  r'''function handle(string $method, string $path, string $body): string {
    if (!request_has_header("X-Api-Key")) { http_status(401); return "{\"error\":\"no key\"}"; }
    return "{\"ok\":true}";
}
serve(8080, "handle");''', run=False)
f("response_header", H, "response_header(string $name, string $value): void",
  "Add a header to the response (for example `Content-Type`, `Location`, `Cache-Control`, `Set-Cookie`).",
  r'''function handle(string $method, string $path, string $body): string {
    if (url_path($path) == "/old") {
        http_status(301);
        response_header("Location", "/new");
        return "";
    }
    response_header("Content-Type", "text/html; charset=utf-8");   // replaces the default application/json
    response_header("Cache-Control", "no-store");
    return "<h1>hello</h1>";
}
serve(8080, "handle");''',
  ["A value with CR, LF or another control character panics (it could inject headers), so it is safe to put "
   "request data in a header.",
   "`Content-Length`, `Connection`, `Transfer-Encoding` and `Date` are set by the server: setting them panics.",
   "Call it more than once to send a header several times (`Set-Cookie`). A panic answers 500 without these "
   "headers. Outside a handler it panics."], run=False,
  php="Instead of `header('Name: value')` and `setcookie()`.")
f("url_path", H, "url_path(string $path): string", "The request path without its query string.",
  r'''echo url_path("/orders?item=tea&qty=2"), " ", url_path("/health"), "\n";''')
f("query_param", H, "query_param(string $path, string $key): string", 'One value from the query string, decoded, or `""`.',
  r'''$p = "/search?q=green+tea&city=%E0%B8%81%E0%B8%97%E0%B8%A1";
echo query_param($p, "q"), "|", query_param($p, "city"), "|", query_param($p, "page"), "|\n";''',
  ["`%XX` and `+` are decoded. With a repeated key, the first one wins.",
   "Use `query_has` to tell a missing key from an empty value."],
  php="Pin's version of `$_GET[$key]`.")
f("query_has", H, "query_has(string $path, string $key): bool", "Whether the query string has `$key`, even with an empty value.",
  r'''echo query_has("/p?limit=", "limit") ? "y" : "n", query_has("/p?debug", "debug") ? "y" : "n", query_has("/p", "limit") ? "y" : "n", "\n";''')
f("form_param", H, "form_param(string $body, string $key): string", 'One value from a form body (`a=1&b=2`), decoded, or `""`.',
  r'''echo form_param("name=Somchai+K&qty=2", "name"), "\n";''',
  php="Pin's version of `$_POST[$key]` for form posts.")
f("form_has", H, "form_has(string $body, string $key): bool", "Whether a form body has `$key`.",
  r'''echo form_has("a=1&b=", "b") ? "y" : "n", form_has("a=1", "c") ? "y" : "n", "\n";''')

# ----------------------------------------------------------------------------------------- database
D = "database"
SETUP = ('$db = db_open(":memory:");\n'
         'db_exec($db, "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT UNIQUE, qty INTEGER, price REAL)", []);\n')
f("db_open", D, "db_open(string $path): db", "Open (or create) an SQLite database file.",
  r'''$db = db_open(":memory:");          // a file name like "shop.db", or ":memory:" for a temporary one
echo db_exec($db, "CREATE TABLE t (x INTEGER)", []), "\n";''',
  ["When `$db` goes out of scope (end of the function) the connection goes back to a pool the runtime keeps "
   "per server thread, so the next `db_open()` of the same file is almost free (~3 µs instead of ~80–200 µs). "
   "A connection comes back only clean: an open transaction is rolled back; after a handler panics it is closed.",
   "Open one per request in a server — that is the intended use. WAL mode lets requests read and write in "
   "parallel; writers wait up to 5 seconds for each other.",
   "`:memory:` and `\"\"` are never pooled: each open is a new, empty database.",
   "A file that cannot be opened panics. Foreign keys are enforced.",
   "`db` values move like lists; pass `$db` to functions (they borrow it). A task (`go`) cannot use the "
   "caller's `$db`: open one inside the task.",
   "SQL that would change the connection for its next user (`PRAGMA foreign_keys = ...`, `ATTACH`, "
   "`CREATE TEMP ...`) does not compile."],
  php="Pin's version of `new PDO('sqlite:shop.db')`. PostgreSQL is planned with the same functions.")
f("db_pool", D, "db_pool(string $path, int $per_thread, int $idle_ms): void",
  "Set the connection pool for one database file (optional).",
  r'''db_pool("shop.db", 8, 30000);    // up to 8 connections per server thread; close after 30 s unused
echo "pool set\n";''',
  ["Without it each file gets 4 connections per thread, closed after 60 s unused.",
   "Only at the top level of the program, before `serve()` (compile error inside a function).",
   "When a thread's connections are all in use, `db_open()` waits (other requests keep running) for up to "
   "5 seconds, then panics — a 500 inside a handler. A handler holds a connection while it waits "
   "(`sleep_ms`, `await`, `http_get`), so raise the limit for handlers like that."],
  php="Like `PDO::ATTR_PERSISTENT` / `pg_pconnect`, but the connection is always returned clean.")
f("db_exec", D, "db_exec(db $db, string $sql, list $params): int",
  "Run INSERT, UPDATE, DELETE or CREATE; returns the rows changed.",
  SETUP + r'''echo db_exec($db, "INSERT INTO items (name, qty) VALUES (?, ?)", ["tea", "2"]), " ";
echo db_exec($db, "INSERT INTO items (name, qty) VALUES (?, ?)", ["tea", "5"]), " ", db_error($db), "\n";''',
  ["The SQL must be a string literal in your code; every value goes in a `?` placeholder and in the list "
   "(`list<string>`, `list<int>` or `list<float>`). SQL built from variables does not compile, so a client "
   "cannot inject SQL.",
   "Returns -1 when a constraint (UNIQUE, NOT NULL, CHECK, FOREIGN KEY) refuses the change — `db_error($db)` "
   "says which. Any other error (bad SQL, wrong number of values) is a bug and panics.",
   'One statement per call. Numbers can be passed as strings: SQLite stores `"2"` in an INTEGER column as 2.'])
f("db_query", D, "db_query(db $db, string $sql, list $params): rows", "Run a SELECT and get the rows one at a time.",
  SETUP + r'''db_exec($db, "INSERT INTO items (name, qty, price) VALUES (?, ?, ?)", ["tea", "2", "40"]);
db_exec($db, "INSERT INTO items (name, qty, price) VALUES (?, ?, ?)", ["cake", "1", "89.5"]);
$rows = db_query($db, "SELECT name, qty, price FROM items WHERE qty >= ? ORDER BY name", ["1"]);
while (db_next($rows)) {
    echo db_string($rows, "name"), " x", db_int($rows, "qty"), " at ", db_float($rows, "price"), "\n";
}''',
  ["Same SQL rules as `db_exec`. Read columns by name with `db_int`, `db_float`, `db_string`, `db_is_null` "
   "after `db_next` returns true.",
   "`rows` close by themselves when they go out of scope; they may outlive the `$db` they came from."],
  php="Like `$pdo->prepare($sql)->execute($params)` followed by a `fetch()` loop.")
f("db_next", D, "db_next(rows $rows): bool", "Move to the next row; false when there are no more.",
  SETUP + r'''$rows = db_query($db, "SELECT name FROM items", []);
echo db_next($rows) ? "a row" : "no rows", "\n";''')
f("db_int", D, "db_int(rows $rows, string $column): int", "An integer column of the current row (NULL gives 0).",
  SETUP + r'''db_exec($db, "INSERT INTO items (name, qty) VALUES (?, ?)", ["tea", "3"]);
$rows = db_query($db, "SELECT qty, qty * 10 AS big FROM items", []);
while (db_next($rows)) { echo db_int($rows, "qty"), " ", db_int($rows, "big"), "\n"; }''',
  ["A column name the query does not have, or reading before `db_next`, panics. Use `AS name` for computed "
   "columns."])
f("db_float", D, "db_float(rows $rows, string $column): float", "A numeric column of the current row (NULL gives 0.0).",
  SETUP + r'''db_exec($db, "INSERT INTO items (name, price) VALUES (?, ?)", ["cake", "89.5"]);
$rows = db_query($db, "SELECT price FROM items", []);
while (db_next($rows)) { echo db_float($rows, "price"), "\n"; }''')
f("db_string", D, "db_string(rows $rows, string $column): string",
  'A column of the current row as text (NULL gives `""`).',
  SETUP + r'''db_exec($db, "INSERT INTO items (name, qty) VALUES (?, ?)", ["ชาเย็น", "1"]);
$rows = db_query($db, "SELECT name, qty FROM items", []);
while (db_next($rows)) { echo db_string($rows, "name"), " ", db_string($rows, "qty"), "\n"; }''')
f("db_is_null", D, "db_is_null(rows $rows, string $column): bool", "Whether a column of the current row is NULL.",
  SETUP + r'''db_exec($db, "INSERT INTO items (name) VALUES (?)", ["tea"]);
$rows = db_query($db, "SELECT name, qty FROM items", []);
while (db_next($rows)) { echo db_is_null($rows, "qty") ? "qty is NULL" : "qty set", "\n"; }''',
  ['Pin has no `null` yet, so check this before trusting a 0 or `""` from a nullable column.'])
f("db_json", D, "db_json(db $db, string $sql, list $params): string",
  "Run a SELECT and get all rows as a JSON array of objects.",
  SETUP + r'''db_exec($db, "INSERT INTO items (name, qty, price) VALUES (?, ?, ?)", ["tea", "2", "40"]);
db_exec($db, "INSERT INTO items (name) VALUES (?)", ["cake \"special\""]);
echo db_json($db, "SELECT id, name, qty, price FROM items ORDER BY id", []), "\n";''',
  ["Column names become keys; INTEGER, REAL, TEXT and NULL become JSON numbers, strings and `null`.",
   "The easiest way to answer an API request with query results — no loop, no string building."])
f("db_last_id", D, "db_last_id(db $db): int", "The id of the last row inserted on this connection.",
  SETUP + r'''db_exec($db, "INSERT INTO items (name) VALUES (?)", ["tea"]);
db_exec($db, "INSERT INTO items (name) VALUES (?)", ["cake"]);
echo db_last_id($db), "\n";''')
f("db_error", D, "db_error(db $db): string", 'Why the last `db_exec` returned -1, or `""`.',
  SETUP + r'''db_exec($db, "INSERT INTO items (name) VALUES (?)", ["tea"]);
if (db_exec($db, "INSERT INTO items (name) VALUES (?)", ["tea"]) < 0) { echo "refused: ", db_error($db), "\n"; }''')

E = "errors"
f("error", E, "error(int $code, string $message): Error", "A new error, to throw from a function declared `throws`.",
  r'''function findUser(int $id): string throws {
    if ($id <= 0) { throw error(400, "bad id $id"); }
    return "user$id";
}
echo try findUser(7) catch ($e) { "?"; }, "\n";
echo try findUser(-1) catch ($e) { "failed with " . $e->code . ": " . $e->message; }, "\n";''',
  ["An `Error` has two public properties, `$e->code` and `$e->message`; `json_encode($e)` gives both.",
   "A function that can fail is declared `: T throws`; every call to it needs `try` (pass the error on) or "
   "`try ... catch ($e) { ... }` (handle it). An error escaping the top level prints `error: ... (code N)` and "
   "exits 1.",
   "An error leaving a `serve()` handler answers its code if it is 400–599 (else 500), with the standard status "
   "text as the body; the message goes to the log only."],
  php="Instead of `throw new InvalidArgumentException(...)`. Pin has one error type, and errors are values "
      "returned next to the result, not exceptions that unwind the stack.")

T = "tasks"
f("sleep_ms", T, "sleep_ms(int $ms): void", "Wait this many milliseconds. Other tasks (and other requests) keep running.",
  r'''function late(string $s, int $ms): string {
    sleep_ms($ms);
    return $s;
}
$a = go late("second", 40);
$b = go late("first", 10);
echo await $b, " ", await $a, "\n";''',
  ["Counts milliseconds, not seconds like PHP's `sleep()`: `sleep_ms(1000)` is one second.",
   "In a `serve()` handler it waits without holding the thread: the same thread answers other requests meanwhile."],
  php="Instead of `sleep()` / `usleep()`, which block the whole process.")
f("await_all", T, "await_all(list<Task<T>> $tasks): list<T>",
  "Wait for every task in the list and return their results, in the list's order.",
  r'''function square(int $n): int {
    sleep_ms(10);
    return $n * $n;
}
list<Task<int>> $ts = [];
foreach ([1, 2, 3, 4] as $n) { $ts[] = go square($n); }
echo json_encode(await_all($ts)), "\n";''',
  ["The tasks run at the same time: four 10 ms tasks take about 10 ms.",
   "The list is used up (moved). When the tasks' function is `throws`, write `try await_all($ts)`: the first "
   "error is the one you get, and the results of the others are freed."])

f("await_timeout", T, "await_timeout(Task<T> $t, int $ms): T throws",
  "Like `await $t`, but give up after `$ms` milliseconds: the task is cancelled and the call fails with 504.",
  r'''function slow(int $ms): int {
    sleep_ms($ms);
    return $ms;
}
$quick = go slow(5);
$late = go slow(1000);
echo try await_timeout($quick, 500) catch ($e) { -1; }, " ", try await_timeout($late, 20) catch ($e) { $e->code; }, "\n";''',
  ["Cancelling is cooperative: the task's next wait (`sleep_ms`, `hub_next`) ends at once, so it stops soon; a "
   "long computing loop runs to its end.",
   "What a cancelled task returned is freed. Always needs `try` (running out of time is an error)."])
HB = "hub"
f("hub_publish", HB, "hub_publish(string $topic, string $message): void",
  "Send a message to everyone subscribed to the topic — also requests on other threads.",
  r'''$inbox = hub_subscribe("orders");
hub_publish("orders", "{\"id\":7}");
echo try hub_next($inbox, 100) catch ($e) { "none"; }, "\n";''',
  ["Requests share no variables; the Hub is how one request tells another (a long-poll or a stream waiting for "
   "news). The text is copied: usually `json_encode` of an object.",
   "Each topic keeps its last 100 messages for `hub_resume`. Messages live in memory: a restart loses them, so "
   "store what must not be lost (SQLite) and use the Hub to wake readers."])
f("hub_subscribe", HB, "hub_subscribe(string $topic): sub",
  "Start receiving the topic's messages (those published from now on).",
  r'''$a = hub_subscribe("t");
$b = hub_subscribe("t");
hub_publish("t", "hi");
echo try hub_next($a, 10) catch ($e) { "?"; }, " ", try hub_next($b, 10) catch ($e) { "?"; }, "\n";''',
  ["The `sub` is an owned value: when it goes out of scope (or the request ends or panics) it unsubscribes.",
   "A reader more than 100 messages behind is dropped: its next `hub_next` fails with 507."])
f("hub_resume", HB, "hub_resume(string $topic, int $last_id): sub",
  "Subscribe and also receive the kept messages with an id above `$last_id` (a reader catching up).",
  r'''hub_publish("log", "one");
hub_publish("log", "two");
$r = hub_resume("log", 1);
echo try hub_next($r, 10) catch ($e) { "?"; }, "\n";''',
  ["For Server-Sent Events: `hub_resume($topic, intval(request_header(\"Last-Event-ID\")))` gives a reconnecting "
   "client what it missed."])
f("hub_next", HB, "hub_next(sub $s, int $ms): string throws",
  "The next message, waiting at most `$ms` milliseconds (other requests keep running meanwhile).",
  r'''$s = hub_subscribe("quiet");
echo try hub_next($s, 20) catch ($e) { "error " . $e->code; }, "\n";''',
  ["Fails with 504 when nothing arrives in time, 507 when this reader fell behind, 499 when the request was "
   "cancelled (its client left). The time limit is required: nothing waits forever."])
f("hub_last_id", HB, "hub_last_id(sub $s): int", "The id of the message `hub_next` last returned.",
  r'''$s = hub_subscribe("ids");
hub_publish("ids", "a");
hub_publish("ids", "b");
$x = try hub_next($s, 10) catch ($e) { ""; };
$y = try hub_next($s, 10) catch ($e) { ""; };
echo $y, " has id ", hub_last_id($s), "\n";''',
  ["Send it as the SSE event id (`sse_send_id`) so a client can resume from it."])
f("sse_start", H, "sse_start(): void throws",
  "Turn the response into a Server-Sent Events stream (`text/event-stream`), sent as it is written.",
  r'''function handle(string $method, string $path, string $body): string throws {
    $inbox = hub_subscribe("news");
    try sse_start();
    while (true) {
        $m = try hub_next($inbox, 25000) catch ($e) {
            if ($e->code != 504) { return ""; }   // the client left
            try sse_comment("ping");             // keeps proxies from closing a quiet stream
            continue;
        };
        try sse_send($m);
    }
}
serve(8080, "handle");''',
  ["Headers set with `response_header()` before it are sent. The stream ends when the handler returns; the "
   "connection then closes.",
   "When the client leaves, the next `sse_*` call (or `hub_next`) fails with 499, so the handler ends and its "
   "subscription is closed."], run=False)
f("sse_send", H, "sse_send(string $data): void throws", "Send one event (`data:` lines; a newline in the text starts a new line).",
  r'''function handle(string $method, string $path, string $body): string throws {
    try sse_start();
    try sse_send(json_encode(["n" => 1]));
    return "";
}
serve(8080, "handle");''',
  ["Fails with 499 when the client is gone, 503 when it reads too slowly (over 1 MB waiting)."], run=False)
f("sse_send_id", H, "sse_send_id(int $id, string $data): void throws",
  "Send one event with an id; a reconnecting browser sends it back as the `Last-Event-ID` header.",
  r'''function handle(string $method, string $path, string $body): string throws {
    try sse_start();
    $inbox = hub_resume("news", intval(request_header("Last-Event-ID")));
    $msg = try hub_next($inbox, 25000);
    try sse_send_id(hub_last_id($inbox), $msg);
    return "";
}
serve(8080, "handle");''', run=False)
f("sse_comment", H, "sse_comment(string $text): void throws",
  "Send a comment line (`: text`): clients ignore it; it keeps an idle stream from being closed.",
  r'''function handle(string $method, string $path, string $body): string throws {
    try sse_start();
    try sse_comment("ping");
    return "";
}
serve(8080, "handle");''', run=False)

CL = "client"
f("http_get", CL, "http_get(string $url, map<string,string> $headers, int $ms): string throws",
  "GET an http:// URL and return the body (waits without holding the thread).",
  r'''$health = try http_get("http://127.0.0.1:8080/health", ["Authorization" => "Bearer abc"], 2000) catch ($e) {
    "down: " . $e->code;
};
echo $health, "\n";''',
  ["A status of 400 or more fails with that status as the code (the start of the body is in the message); "
   "502 when the connection fails, 504 when there is no answer within `$ms`.",
   "Only `http://`, by design: for an https API, call a local egress proxy (Caddy) on 127.0.0.1 that opens the "
   "https connection — see design/tls.md. An https:// URL fails with 400.",
   "Header values with CR/LF panic. The name lookup itself still blocks the thread briefly."], run=False,
  php="Instead of `file_get_contents($url)` or curl.")
f("http_post", CL, "http_post(string $url, map<string,string> $headers, string $body, int $ms): string throws",
  "POST a body (sent as application/json) and return the response body.",
  r'''$r = try http_post("http://127.0.0.1:8080/orders", [], json_encode(["item" => "tea"]), 2000);
echo $r, "\n";''', ["Errors as for `http_get`."], run=False)
f("http_stream", CL, "http_stream(string $url, map<string,string> $headers, int $last_id, int $ms): stream throws",
  "Open a Server-Sent Events stream (for example a board an agent listens to) and keep it open.",
  r'''$s = try http_stream("http://127.0.0.1:8080/events", ["X-Api-Key" => "k1"], 0, 5000);
while (true) {
    $event = try stream_next($s, 60000);
    echo "got ", $event, " (id ", stream_last_id($s), ")\n";
}''',
  ["With `$last_id` above 0 it sends `Last-Event-ID`, so the server can send what was missed.",
   "The `stream` closes when it goes out of scope. `$ms` limits connecting and the response head."], run=False)
f("stream_next", CL, "stream_next(stream $s, int $ms): string throws",
  "The data of the next event of the stream (several `data:` lines joined with newlines; comments skipped).",
  r'''$s = try http_stream("http://127.0.0.1:8080/events", [], 0, 5000);
$e1 = try stream_next($s, 30000) catch ($e) { "none (" . $e->code . ")"; };
echo $e1, "\n";''',
  ["504 when no event comes within `$ms` (the stream stays usable); 502 when the server ended the stream: "
   "reconnect with `http_stream($url, $headers, stream_last_id($s), $ms)`."], run=False)
f("stream_last_id", CL, "stream_last_id(stream $s): int", "The id of the last event read (0 before any had one).",
  r'''$s = try http_stream("http://127.0.0.1:8080/events", [], 0, 5000);
$e1 = try stream_next($s, 30000);
echo stream_last_id($s), "\n";''', run=False)
f("read_line", CL, "read_line(): string throws",
  "One line of standard input, without its line ending. Fails with code 1 at the end of the input.",
  r'''while (true) {
    $line = try read_line() catch ($e) { break; };    // the input ended
    echo strtoupper($line), "\n";
}''',
  ["What was echoed so far is flushed before it waits, so a program answering line by line (MCP over stdio) "
   "is never stuck with its answer in a buffer.",
   "Waiting does not hold the thread: other tasks keep running."], run=False,
  php="Instead of `fgets(STDIN)`.")
f("run", CL, "run(string $program, list<string> $args, int $ms): string throws",
  "Run a program and return what it printed. There is no shell: each argument reaches the program as it is.",
  r'''echo try run("echo", ["hello", "world; rm -rf /"], 2000);
echo try run("sh", ["-c", "exit 3"], 2000) catch ($e) { "exit code " . $e->code; }, "\n";''',
  ["Arguments are never interpreted by a shell, so text from a request cannot inject commands. Write "
   "`run(\"sh\", [\"-c\", ...])` only with text you wrote yourself.",
   "A non-zero exit fails with the exit code as the error code and the start of stderr as the message; 127 when "
   "the program cannot be started; 504 when it runs longer than `$ms` (it is killed).",
   "The program reads nothing on stdin; output above 64 MB is cut."],
  php="Instead of `exec()` / `shell_exec()` / `proc_open()`, which go through a shell.")

# ----------------------------------------------------------------------------------------- security
SEC = "security"
f("hash", SEC, 'hash(string $algo, string $data): string', "SHA-256 of a string, as 64 lowercase hex characters.",
  r'''echo hash("sha256", "abc"), "\n";''',
  ['`$algo` must be the literal `"sha256"`: MD5 and SHA-1 are broken, so they do not compile.',
   "For passwords use `password_hash()`, not a plain hash: it is salted and slow on purpose."],
  php="`hash('sha256', $data)`; no `md5()` / `sha1()`.")
f("hash_hmac", SEC, 'hash_hmac(string $algo, string $data, string $key): string',
  "HMAC-SHA256 of `$data` with `$key`, as hex: a signature only someone with the key can make.",
  r'''$sig = hash_hmac("sha256", "user=7", "server-secret");
echo strlen($sig), " ", hash_equals($sig, hash_hmac("sha256", "user=7", "server-secret")) ? "valid" : "forged", "\n";''',
  ['`$algo` must be the literal `"sha256"`.',
   "Compare a signature that came from a client with `hash_equals()`, never `==`."],
  php="Same argument order as PHP: algorithm, data, key.")
f("hash_equals", SEC, "hash_equals(string $known, string $user): bool",
  "Compare two strings in a time that does not depend on where they differ.",
  r'''echo hash_equals("abc", "abc") ? "same" : "differ", " ", hash_equals("abc", "abd") ? "same" : "differ", "\n";''',
  ["Use it for tokens, API keys and signatures: `==` stops at the first different byte, which lets a client "
   "guess a secret one byte at a time by measuring."])
f("random_token", SEC, "random_token(int $bytes): string",
  "`$bytes` random bytes from the operating system, as hex (twice as many characters).",
  r'''$t = random_token(16);
echo strlen($t), "\n";                    // 32 hex characters: a 128-bit session token''',
  ["Secure for session tokens, API keys, password-reset links. 16 bytes is enough for a token.",
   "`$bytes` must be 1 to 1024 (else panic)."],
  php="`bin2hex(random_bytes($bytes))`.", run=True)
f("random_int", SEC, "random_int(int $min, int $max): int",
  "A random integer from `$min` to `$max` (both included), from the operating system's secure source.",
  r'''$d = random_int(1, 6);
echo $d >= 1 && $d <= 6 ? "a dice roll\n" : "impossible\n";''',
  ["Every value is equally likely (no modulo bias). `$min > $max` panics."],
  php="Same as PHP's `random_int`. There is no `rand()` / `mt_rand()`: use this.")
f("password_hash", SEC, "password_hash(string $password): string",
  "A salted, slow hash of a password, to store instead of the password.",
  r'''$h = password_hash("correct horse");
echo substr($h, 0, 22), "...\n";''',
  ["PBKDF2-HMAC-SHA256 with 600,000 rounds (OWASP's figure for this algorithm) and a random 16-byte salt, "
   "stored as `$pbkdf2-sha256$600000$<salt>$<hash>`; two calls give different strings.",
   "Slow on purpose: about 0.5 s on an Apple M2 (portable C; hardware SHA instructions are not used yet). It "
   "keeps the server thread busy meanwhile, so call it only at sign-up and password change.",
   "Store the whole string (it fits in TEXT); check with `password_verify()`."],
  php="`password_hash($password, PASSWORD_DEFAULT)` without the second argument; PHP uses bcrypt, Pin PBKDF2, so "
      "hashes do not move between them.")
f("password_verify", SEC, "password_verify(string $password, string $hash): bool",
  "Whether `$password` matches a hash made by `password_hash()`.",
  r'''$h = password_hash("correct horse");
echo password_verify("correct horse", $h) ? "yes" : "no", " ", password_verify("wrong", $h) ? "yes" : "no", "\n";''',
  ["A string that is not such a hash gives `false` (never a panic): it usually comes from a database.",
   "As slow as `password_hash()`; the comparison takes constant time."])

CATEGORIES = [{"id": S, "title": "Strings"}, {"id": L, "title": "Lists and maps"}, {"id": N, "title": "Numbers"},
              {"id": V, "title": "Conversions"}, {"id": J, "title": "JSON"}, {"id": H, "title": "HTTP"},
              {"id": "database", "title": "Database (SQLite)"}, {"id": E, "title": "Errors"}, {"id": T, "title": "Tasks (go / await)"},
              {"id": HB, "title": "Hub (messages between requests)"}, {"id": CL, "title": "Other servers and programs"},
              {"id": SEC, "title": "Security (hashes, passwords, random)"}]
