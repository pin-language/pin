# The Pin language guide (v0.1)

Pin reads like PHP 8 and compiles to a native binary. This guide covers everything the v0.1 compiler
accepts. Anything not listed here is not supported yet.

## Files and running

A program is one `.pn` file. It may start with `<?php` (optional). Top-level statements run in order;
functions can be declared anywhere in the file.

```bash
pin run hello.pn          # compile + run
pin build hello.pn -o hi  # native binary
pin check hello.pn        # errors only
```

## Types

| type | example | notes |
|---|---|---|
| `int` | `42`, `-7` | 64-bit; `+ - *` wrap around on overflow |
| `float` | `1.5`, `2.0e10` | 64-bit IEEE |
| `bool` | `true`, `false` | |
| `string` | `"hi $name"`, `'raw'` | bytes; Thai/UTF-8 text is kept as is |
| `list<T>` | `[1, 2, 3]` | `T` is `int`, `float` or `bool` |
| `map<K,V>` | `["a" => 1]` | `K` is `int` or `string`; `V` is `int`, `float`, `bool` or `string`; keeps insertion order |
| `void` | | return type only |

A variable gets its type from its first assignment and keeps it. Empty containers need the type
written out:

```php
list<int> $ids = [];
map<string,int> $count = [];
$n = 0;            // int
$n = 1.5;          // compile error: assigning to $n: expected int but got float
```

## Variables must have a value on every path

```php
if ($ok) { $msg = "yes"; }
echo $msg;         // compile error: $msg may be used before it is assigned ...
if ($ok) { $msg = "yes"; } else { $msg = "no"; }
echo $msg;         // fine
```

## Operators

- Arithmetic: `+ - * %`, `/` (always gives a float, like PHP), `intdiv($a, $b)` for int division.
  `%` and `intdiv` follow PHP (round toward zero). Division by zero panics.
- Compound: `+= -= *= /= %= .=`, `++`, `--`. `/=` needs a float variable.
- Comparison: `== != < <= > >= === !==` (no implicit conversions between types).
- Logic: `&& || !` — on `bool` only.
- Strings: `.` joins, `.=` appends. `"Hello $name"` and `"{$name}s"` interpolate.
- `$cond ? $a : $b`, and `$m[$k] ?? $default` / `$list[$i] ?? $default`.
- Casts: `(int)` (from float, bool or string), `(float)` (from int), `(string)` (from int, float or
  bool). `(int)` of a NAN, INF or out-of-range float panics. There is no `(bool)` cast — compare instead
  (`$n != 0`) — and no `(float)` of a string yet.
- `wrapping_add($a, $b)`, `wrapping_sub`, `wrapping_mul`: say "wrapping is intended". Today plain `+ - *`
  wrap too; once overflow checks arrive, only these will.

Conditions must be `bool`: `if ($n)` is an error, write `if ($n != 0)`.

## Control flow

`if / elseif / else`, `while`, `for`, `foreach ($xs as $x)`, `foreach ($xs as $i => $x)`,
`foreach ($map as $key => $value)`, `break`, `continue`, `return`. Braces are required.

## Functions

```php
function area(float $w, float $h): float { return $w * $h; }
function log_line(string $s): void { echo $s, "\n"; }
```

Parameter and return types are required. Recursion works. There are no default values, no named
arguments and no variadics yet.

## Lists and maps

```php
$xs = [3, 1, 2];
$xs[] = 4;                 // append
$xs[0] = 10;               // index out of range panics
echo count($xs);
$flags = fill(100, false); // 100 copies of a value

$price = ["tea" => 40];
$price["cake"] = 89;
$price["tea"] += 5;        // a missing key starts from 0 for += -= *=
echo $price["coffee"] ?? 0;
if (isset($price["tea"])) { unset($price["tea"]); }
echo array_key_exists("tea", $price);
echo json_encode($price);  // {"cake":89}
```

A map has one value type, so a JSON object with mixed values is built as a string — `json_encode()`
of a string gives a quoted, escaped JSON string:

```php
return "{\"item\":" . json_encode($name) . ",\"qty\":" . $qty . "}";
```

## Ownership

Lists, maps and strings live on the heap. Pin frees them without a garbage collector by following
these rules, all checked when you compile:

1. Each value has one owner (a variable). It is freed when the owner's function returns or the
   variable gets a new value.
2. `$b = $a;` **moves** a list or map: `$b` is the owner and `$a` cannot be used any more (until you
   assign it again). Use `copy($a)` for a second, independent value.
3. Strings **copy** on assignment, like PHP. String literals are never copied.
4. Passing a value to a function **lends** it read-only. The function cannot change it, move it or
   return it as is; the caller still owns it afterwards. To change it, make a copy inside.
5. `foreach` variables are loans too.
6. While `foreach` walks over a list or map, you cannot add to it, remove from it or replace it.
7. A function that returns a list or map hands ownership to the caller.
8. Moving a value inside a loop is an error if the next round would use it again — move it after the
   loop, `copy()` it, or `break` right after moving.
9. Each `if` branch is checked on its own: moving `$a` in one branch leaves it usable in the other.
   After the `if`, `$a` counts as moved if any branch that continues moved it.

There are no global variables. Functions only see their parameters.

## Strings

`strlen`, `substr` (negative start/length like PHP), `strpos` (returns **-1** when not found, not
`false`), `str_contains`, `str_starts_with`, `str_ends_with`, `str_repeat`, `strtolower`, `strtoupper`
(ASCII letters only, like PHP 8.2+), `trim`, `intval` (panics if the number does not fit in an int),
`strval`.

All of these count **bytes**, like PHP: `strlen("ไทย")` is 9. Character-aware functions (`mb_*`,
`grapheme_*`) are designed but not built yet.

## Other built-ins

`count`, `fill($n, $value)`, `copy`, `isset($m[$k])`, `array_key_exists`, `unset($m[$k])`,
`json_encode` (int, float, bool, string, list, map), `sqrt`, `intdiv`, `echo` with several arguments.

## Panics

A panic is a run-time error the compiler could not rule out: index out of range, missing map key,
division by zero, `intdiv(PHP_INT_MIN, -1)`, an allocation too large, `(int)` of a bad float,
`intval()` overflow. It prints `panic: ...` with the line and stops the program with exit code 1. Inside
an HTTP handler it answers 500 instead and the server keeps running. There is no way to catch a panic;
`try`/`catch` for expected errors is designed (`docs/design/error-handling.md`) but not built.

## HTTP server

```php
function handle(string $method, string $path, string $body): string { ... }
serve(8080, "handle");
```

- The handler must have exactly this signature. Its return value is the response body
  (`Content-Type: application/json`). `http_status(404)` sets the status for this response.
- `$path` includes the query string as sent; there is no header or query API yet.
- One thread per CPU core (`PN_THREADS=n` to change). Keep-alive and pipelining are supported.
- Limits: 64 KB of headers, 64 MB of body. No TLS, no HTTP/2, no timeouts: **for local testing only.**

## Not in v0.1

Classes and objects, `null` and `?Type`, `try`/`catch`/`throws`, `list<string>`, lists of lists, maps
of maps, closures, `use`/modules (one file per program), `explode`, `implode`, `sort`, `json_decode`,
request headers, database drivers, `go`/`await`. Designs for most of these (Thai) are in
[`docs/design/`](design/).
