# Writing Pin — guide for AI coding agents

Pin v0.1. Give this file to the agent (or point it here). It is complete for v0.1: if a feature is not
in this file, Pin does not have it yet. Pin looks like PHP 8 but is statically typed and has no garbage
collector, so a lot of PHP habits do not compile. The compiler explains each error and the fix.

## Loop

```bash
pin check --json app.pn   # one JSON line per error: {"file","line","severity","message"}; {"ok":true} if clean
pin run app.pn            # compile and run
pin build app.pn -o app   # native binary
```

Write → `pin check --json` → fix the reported line → repeat → `pin run`. Errors stop at the first one,
so fix and re-check. Exit code 0 = ok, 1 = compile error or run-time panic, 2 = toolchain problem.

## Program shape and output

- A program is one `.pn` file. `<?php` at the top is optional. Statements outside functions run from top
  to bottom; functions can be declared anywhere in the file (before or after use).
- Top-level variables are local to the top level: functions cannot see them (there are no globals).
- Print with `echo`, several values separated by commas: `echo "total: ", $t, "\n";`. Double-quoted
  strings interpolate `"$name"` and `"{$name}x"`; single-quoted strings do not. `PHP_EOL` is `"\n"`.
- Printing a float uses PHP's format: `380.5` → `380.5`, `380.0` → `380`, `1/3` → `0.33333333333333`.
  `json_encode` of a whole float keeps `.0` (`380.0`; PHP would print `380`). `true` prints `1`, `false` prints nothing.
- Maps keep insertion order (like PHP arrays), in `foreach` and in `json_encode`.

## The rules that matter

1. **Types.** `int float bool string list<T> map<K,V> void`. `T` ∈ int/float/bool. `K` ∈ int/string,
   `V` ∈ int/float/bool/string. No nesting (`list<list<int>>`, `list<string>`, map of lists: no).
2. **Every parameter and return type is written.** `function f(int $n, list<int> $xs): string`.
3. **A variable keeps the type of its first value.** Empty containers need the type:
   `list<int> $ids = [];` `map<string,int> $count = [];`
4. **Conditions are bool.** `if ($n != 0)`, `if (count($xs) > 0)`, `if (strlen($s) > 0)` — never `if ($n)`.
5. **A variable must have a value on every path before use.** Initialise before `if` / loops.
6. **Lists and maps move on `=`.** After `$b = $a;` do not use `$a`. Want two? `$b = copy($a);`
7. **Parameters are read-only loans.** Do not change, move or return a list/map parameter; copy it:
   `$out = copy($xs);` then change `$out` and `return $out;`.
8. **Do not add to / remove from / replace a list or map inside a `foreach` over it.** Collect changes in
   another variable, apply after the loop.
9. **Strings copy on `=`** (PHP-like). Join with `.`; `+` on strings is an error.
10. **No globals.** Pass everything as parameters. Functions may be declared anywhere in the file.
11. **Non-void functions must return on every path.**
12. **Missing map key / bad index panic.** Use `isset($m[$k])` or `$m[$k] ?? $default`.
13. **A map literal has one value type.** `["a" => 1, "b" => 2.5]` becomes `map<string,float>`;
    `["a" => 1, "b" => "x"]` is an error — build mixed JSON objects as strings (idiom below).
14. **Both sides of `?:` and `??` have the same type.** `$n < 10 ? "0" . $n : strval($n)`, not `... : $n`.
15. **`unset()` works on maps only.** To drop items from a list, build a new list without them.
16. **`/` always gives a float** — for list indexes and counts use `intdiv($a, $b)`.
17. **`intval()` of text that is not a number gives 0; of a number too big for an int it panics** (in a
    server: a 500). Validate input ranges after `intval()`.

## Idioms

```php
<?php
// build and return a list (caller owns it)
function evens(int $n): list<int> {
    list<int> $out = [];
    for ($i = 0; $i < $n; $i++) { if ($i % 2 == 0) { $out[] = $i; } }
    return $out;
}

// read a list without copying
function sum(list<int> $xs): int {
    $t = 0;
    foreach ($xs as $x) { $t += $x; }
    return $t;
}

// "modify" a parameter: copy, change, return
function bump(list<int> $xs): list<int> {
    $out = copy($xs);
    for ($i = 0; $i < count($out); $i++) { $out[$i] += 1; }
    return $out;
}

// counting with a map (a missing key starts at 0 for += -= *= ++ --)
function tally(list<int> $xs): map<int,int> {
    map<int,int> $c = [];
    foreach ($xs as $x) { $c[$x] += 1; }
    return $c;
}

// "not found" without null: return -1 / "" and check it
function find(list<int> $xs, int $want): int {
    foreach ($xs as $i => $x) { if ($x == $want) { return $i; } }
    return -1;
}

// mixed JSON object: build the string; json_encode() a string to quote/escape it
function user_json(int $id, string $name): string {
    return "{\"id\":" . $id . ",\"name\":" . json_encode($name) . "}";
}
```

Sort a list (no `sort` yet): insertion sort on a copy — fine up to a few thousand items.

```php
function sorted(list<int> $xs): list<int> {
    $a = copy($xs);
    for ($i = 1; $i < count($a); $i++) {
        $v = $a[$i];
        $j = $i - 1;
        while ($j >= 0 && $a[$j] > $v) { $a[$j + 1] = $a[$j]; $j--; }
        $a[$j + 1] = $v;
    }
    return $a;
}

function median(list<int> $xs): float {     // assumes count($xs) > 0
    $s = sorted($xs);
    $n = count($s);
    $mid = intdiv($n, 2);
    return $n % 2 == 1 ? (float)$s[$mid] : ($s[$mid - 1] + $s[$mid]) / 2;
}
```

Money: keep integer satang/cents; format with `intdiv` and `%` (no `sprintf` / `number_format` yet).

```php
function baht(int $satang): string {        // 123450 -> "1234.50", -5 -> "-0.05"
    $sign = $satang < 0 ? "-" : "";
    $abs = $satang < 0 ? -$satang : $satang;
    $frac = $abs % 100;
    return $sign . intdiv($abs, 100) . "." . ($frac < 10 ? "0" . $frac : strval($frac));
}
```

Top N from a map (no `sort` yet): pick the best not yet used, N times.

```php
function top3(map<string,int> $counts): string {
    map<string,bool> $used = [];
    $out = "";
    for ($round = 0; $round < 3; $round++) {
        $best = "";
        $bestN = -1;
        foreach ($counts as $w => $n) {
            if (!isset($used[$w]) && $n > $bestN) { $best = $w; $bestN = $n; }
        }
        if ($bestN < 0) { break; }
        $used[$best] = true;
        $out .= "$best: $bestN\n";
    }
    return $out;
}
```

Splitting a string (no `explode` yet):

```php
function count_words(string $text): map<string,int> {
    map<string,int> $counts = [];
    $start = 0;
    $len = strlen($text);
    while ($start < $len) {
        $sp = strpos(substr($text, $start), " ");      // -1 when not found (not false)
        $end = $sp < 0 ? $len : $start + $sp;
        $w = substr($text, $start, $end - $start);
        if (strlen($w) > 0) { $counts[$w] += 1; }
        $start = $end + 1;
    }
    return $counts;
}
```

HTTP API:

```php
function handle(string $method, string $path, string $body): string {   // exactly this signature
    if ($method == "GET" && $path == "/health") { return "{\"ok\":true}"; }
    if (str_starts_with($path, "/items/")) {
        $id = intval(substr($path, 7));
        if ($id <= 0) { http_status(400); return "{\"error\":\"bad id\"}"; }
        return "{\"id\":" . $id . "}";
    }
    http_status(404);
    return "{\"error\":\"not found\"}";
}
serve(8080, "handle");    // last statement; blocks forever
```

The response is always `Content-Type: application/json`; the body format is yours. `$path` includes any
query string (`/health?x=1`), so strip it first if you route on exact paths:

```php
function route(string $path): string {
    $q = strpos($path, "?");
    return $q < 0 ? $path : substr($path, 0, $q);
}
```

There is no header access, no body parsing (`json_decode`), no database yet. Handlers cannot keep state
between requests (no globals): compute each answer from the request.

## PHP → Pin

| PHP | Pin |
|---|---|
| `array(1, 2)`, `[]` untyped | `[1, 2]`, `list<int> $a = [];` |
| `$a = ["x" => 1, "y" => "s"]` (mixed) | one value type per map; build JSON strings for mixed objects |
| `null`, `?int` | not yet — use `-1`, `""`, empty list, or a separate `bool` |
| `class`, `new`, `->` | not yet — functions + maps |
| `try`/`catch`/`throw` | not yet — check first (`isset`, `??`, range checks) |
| `switch`, `match` | `if / elseif / else` |
| `function () {}`, `fn() =>` | named functions |
| `include`, `use`, `namespace` | one file per program |
| `global`, `static`, `const`, `define` | parameters / local variables |
| `strpos(...) === false` | `strpos(...) < 0` |
| `explode`, `implode`, `sort`, `array_map`, `in_array`, `max`, `sprintf`… | write the loop (see idioms); the error message names the replacement |
| `json_decode` | not yet |
| `(bool)$x`, truthiness | compare explicitly |
| `$s + 1` on strings | `intval($s) + 1` |
| `list($a, $b) = ...` | assign separately |
| `foreach ($xs as &$x)` | `for ($i = 0; $i < count($xs); $i++) { $xs[$i] ... }` |

## Built-ins (complete list)

`count fill copy isset unset array_key_exists json_encode strlen substr strpos str_contains
str_starts_with str_ends_with str_repeat strtolower strtoupper trim intval strval sqrt intdiv
wrapping_add wrapping_sub wrapping_mul serve http_status` · constants `PHP_INT_MAX PHP_INT_MIN PHP_EOL`
· casts `(int)` from float/bool/string, `(float)` from int, `(string)` from int/float/bool.

String functions count bytes (`strlen("ไทย")` is 9). `/` always gives a float; use `intdiv($a, $b)`.
Ints wrap around on overflow.

## Errors → fixes

| message contains | fix |
|---|---|
| `was moved on line N` | use `copy($x)` at line N, or stop using the old name |
| `may have been moved on line N (on one path…)` | copy in that branch, or reassign after the `if` |
| `is moved inside a loop` | move after the loop, `copy()`, or `break` right after the move |
| `may be used before it is assigned` | give it a value before the `if`/loop |
| `is a parameter lent to this code, so it cannot be changed` | `$copy = copy($p);` and work on `$copy` |
| `while a foreach is looping over it` | collect into another list/map, apply after the loop |
| `an empty [] needs a type` | `list<int> $a = [];` / `map<string,int> $m = [];` |
| `condition must be bool` | write the comparison: `$n != 0` |
| `expected int but got float` | the variable is int: use a new variable, `(int)`, or start it as `0.0` |
| `cannot combine string and int` | join with `.`; convert with `intval()` / `strval()` |
| `can reach its end without returning` | add a final `return` |
| `both sides of ?: / ?? must have the same type` | convert one side with `strval()` / `intval()` / `(float)` |
| `list index must be int but got float` | use `intdiv($a, $b)` instead of `/` |
| `unset() works on maps` | build a new list without the items |
| `… is not in Pin v0.1` | use the replacement in the message / the table above |
| run time `panic: key not found` | `isset($m[$k])` or `$m[$k] ?? default` |
| run time `panic: index N out of range` | check `$i < count($xs)` |
