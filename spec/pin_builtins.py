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
f("copy", L, "copy(list|map|string $x): same type", "An independent copy you own.",
  r'''$a = [1, 2];
$b = copy($a);
$b[] = 3;
echo count($a), " ", count($b), "\n";''',
  ["Lists and maps move on `$b = $a`; use `copy()` when you need two of them.",
   "A function parameter is read-only; `copy()` it before changing it."])
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
f("json_encode", J, "json_encode(int|float|bool|string|list|map $x): string", "JSON text for a value.",
  r'''echo json_encode(["name" => "ไทย", "note" => "a\"b"]), " ", json_encode([1.5, 2.0]), " ", json_encode("x"), "\n";''',
  ["UTF-8 text is kept as is (like `JSON_UNESCAPED_UNICODE`); whole floats keep `.0`.",
   r'A map has one value type; build mixed objects as strings: `"{\"id\":" . $id . ",\"name\":" . json_encode($name) . "}"`.'],
  php=r"PHP escapes non-ASCII as `\uXXXX` and drops `.0` unless `JSON_PRESERVE_ZERO_FRACTION` is set.")
f("json_has", J, "json_has(string $json, string $key): bool", "Whether a JSON object has a top-level key.",
  r'''$b = "{\"title\": \"tea\", \"qty\": null}";
echo json_has($b, "title") ? "y" : "n", json_has($b, "qty") ? "y" : "n", json_has($b, "x") ? "y" : "n", json_has("not json", "title") ? "y" : "n", "\n";''',
  ["True even when the value is `null`. False when the text is not a JSON object."])
f("json_string", J, "json_string(string $json, string $key): string", 'A top-level string value, or `""`.',
  r'''echo json_string("{\"name\": \"Som\\u0e01\", \"n\": 5}", "name"), "|", json_string("{\"n\": 5}", "n"), "|\n";''',
  ['`""` when the key is missing, the value is not a string, or the text is not a JSON object.',
   r'Escapes are decoded (`\n`, `\"`, `\uXXXX` including surrogate pairs).'],
  php="Pin has no `json_decode` yet; these functions read one top-level key at a time. Nested values are skipped safely.")
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
   "No TLS, no request headers and no timeouts yet: for local testing only."], run=False)
f("http_status", H, "http_status(int $code): void", "Set the status code of the current response (default 200).",
  r'''function handle(string $method, string $path, string $body): string {
    http_status(404);
    return "{\"error\":\"not found\"}";
}
serve(8080, "handle");''', run=False)
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

CATEGORIES = [{"id": S, "title": "Strings"}, {"id": L, "title": "Lists and maps"}, {"id": N, "title": "Numbers"},
              {"id": V, "title": "Conversions"}, {"id": J, "title": "JSON"}, {"id": H, "title": "HTTP"}]
