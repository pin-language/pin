# Pin

> **🤖 AI coding agents: before writing any Pin code, read the complete guide —
> https://github.com/pin-language/docs/blob/HEAD/ai-guide.md** (one page; plain text:
> https://raw.githubusercontent.com/pin-language/docs/HEAD/ai-guide.md). Pin looks like PHP but
> many PHP habits do not compile: no `null`, a map holds one value type (use a class for mixed JSON), empty
> containers need a type, calls that can fail need `try`. Loop on `pin check --json file.pn`. See also
> [AGENTS.md](AGENTS.md) and [llms.txt](llms.txt).

[![test](https://github.com/pin-language/pin/actions/workflows/test.yml/badge.svg)](https://github.com/pin-language/pin/actions/workflows/test.yml)

**A language for AI coding agents to write fast, memory-safe web backends.**
PHP-like syntax, compiled to native code, no garbage collector.

Most code is now written by AI agents, and they already know PHP. Pin keeps that syntax but moves the
mistakes from production to compile time: every variable has one type, memory is managed by
compile-time ownership (like Rust, simplified), every path through a function is checked, and each
error says how to fix it — in plain text or as JSON for the agent's loop. The result compiles through
LLVM into a single native binary, including a multi-threaded HTTP server.



> **Status: preview (0.8) for early testers.** The language is small and still changing. Do not use it
> in production. TLS (https) is left to a proxy by design, in both directions: see
> [design/tls.md](https://github.com/pin-language/docs/blob/HEAD/design/tls.md).
> 🇹🇭 ภาษาไทย: [README.th.md](README.th.md)

## Install

macOS or Linux, one command (no sudo; it checks for Python 3.10+ and clang, puts Pin in `~/.pin`, links
the `pin` command into `~/.local/bin`, and runs a hello-world to prove it works):

```bash
curl -fsSL https://raw.githubusercontent.com/pin-language/pin/HEAD/install.sh | sh
```

If it says clang is missing: macOS `xcode-select --install`, Ubuntu/Debian `sudo apt install clang`.
If `pin` is then "command not found", the installer prints the one line to add to your shell config.
Windows: use WSL with Ubuntu (untested).

Check it, and run your first program:

```bash
pin -v
printf '%s\n' 'echo "Hello from Pin\n";' > hello.pn && pin hello.pn
```

Update later by running the same `curl … | sh` again. To uninstall: `rm -rf ~/.pin ~/.local/bin/pin`.

<details><summary>Install by hand instead</summary>

```bash
git clone https://github.com/pin-language/pin.git ~/.pin
mkdir -p ~/.local/bin && ln -sf ~/.pin/pin ~/.local/bin/pin
pin ~/.pin/examples/hello.pn
```

</details>

```php
function squares(int $n): list<int> {   // returns a list the caller will own
    list<int> $out = [];
    for ($i = 1; $i <= $n; $i++) { $out[] = $i * $i; }
    return $out;
}

function total(list<int> $xs): int {    // borrows the list read-only: no copy
    $sum = 0;
    foreach ($xs as $x) { $sum += $x; }
    return $sum;
}

$name = "world";
echo "Hello, $name!\n";
echo total(squares(10)), "\n";          // 385
```

## Using Pin with an AI coding agent

1. Point the agent at the [AI guide](https://github.com/pin-language/docs/blob/HEAD/ai-guide.md) — the whole language on one page, with
   idioms, a PHP → Pin table and an error → fix table. For example, add this line to your project's
   `AGENTS.md` / `CLAUDE.md`: *"This project is written in Pin. Read
   https://raw.githubusercontent.com/pin-language/docs/HEAD/ai-guide.md before writing code, and run `pin check --json` after every change."*
2. Let it loop on `pin check --json file.pn`: one JSON object per error
   (`{"file","line","severity","message"}`), `{"file","ok":true}` when clean, exit code 1 on error.
3. Run with `pin run file.pn`. Run-time errors print `panic: ...` with the line and exit 1 (inside an
   HTTP handler they answer 500 and the server keeps going).

Messages are written for agents trained on PHP: reaching for `extends`, `finally`, `switch`, `null`,
`array()`, closures or `include` gets "not in Pin yet — write this instead", not a parse error.

## Commands

| command | what it does |
|---|---|
| `pin file.pn` | compile and run (same as `pin run file.pn`) |
| `pin run file.pn` | compile and run (the binary is thrown away) |
| `pin build file.pn [-o out]` | compile to a native binary |
| `pin check file.pn` | type- and ownership-check only |
| `pin version` | print the version |

**Editor:** VS Code syntax highlighting is in [`editors/vscode`](editors/vscode/) — after installing,
`cd ~/.pin/editors/vscode && npx @vscode/vsce package && code --install-extension pin-0.5.0.vsix`.

Start with [`examples/`](examples/) (installed in `~/.pin/examples`): `hello`, `fizzbuzz`, `wordcount`, `ownership`, `primes`, and
`api` (a small JSON server). New to Pin? [Learn Pin](https://github.com/pin-language/docs/blob/HEAD/learn/README.md) — fourteen short chapters.
Every built-in function with examples: the [reference](https://github.com/pin-language/docs/blob/HEAD/reference/README.md).
The full guide is the [language guide](https://github.com/pin-language/docs/blob/HEAD/language.md);
all documentation lives in [pin-language/docs](https://github.com/pin-language/docs).

## How Pin differs from PHP

| | PHP | Pin |
|---|---|---|
| variable types | can change at any time | one type per variable, inferred from the first value |
| parameters / return | types optional | types required |
| arrays | one `array` for lists and maps | `list<int>` and `map<string,int>` (maps keep insertion order like PHP) |
| strings | `.`, `.=`, `"Hello $name"` | the same |
| missing map key | warning, then `null` | panic — check with `isset()` or use `$m[$k] ?? 0` |
| index out of range | warning, then `null` | panic |
| `if ($n)` | truthy/falsy | the condition must be a `bool`: write `$n != 0` |
| changing an array you were passed | changes a copy (copy-on-write) | compile error — `copy()` it first |
| using a variable that may not be set | warning | compile error |
| memory | reference counting + GC at run time | ownership checked at compile time, no GC |
| int overflow | becomes a float | wraps around (64-bit) |
| `json_encode` of Thai text | `ไ...` | UTF-8 as is (like `JSON_UNESCAPED_UNICODE`) |

## Ownership in one minute

```php
$a = [1, 2, 3];
echo total($a);      // passing to a function lends it read-only; $a is still yours
$b = $a;             // lists and maps MOVE: $b owns it now
echo count($a);      // compile error: $a was moved on line 3. Use copy($a) ...
$c = copy($b);       // an explicit copy
$s = "text";
$t = $s;             // strings COPY, like PHP
```

Each list, map and string has exactly one owner and is freed when the owner goes away — no garbage
collector, no reference counting. The compiler checks every path through your code separately, so a
value moved in one `if` branch can still be used in the other branch. Errors explain the fix:

```
ownership.pn:20: error: $a was moved on line 18 and can no longer be used here. Use copy($a) if you need two separate values
loop.pn:2: error: $last may be used before it is assigned: on some paths to this line it never gets a value (an if without else, or a loop that can run zero times). Give $last a value before the if/loop
```

## Modules and packages

```php
import prices;                                   // prices.pn in the project folder
import github.com/someone/strutil/words;         // a package, after: pin get github.com/someone/strutil@v1.0.0
echo prices::price("tea"), " ", words::greet("pin"), "\n";
```

Only functions marked `export` can be called from other files; imported files contain only functions. A
package is any Git repository: `pin get` pins its commit and a SHA-256 of its code in `pin.lock`, checked on
every build, so code that changes after you locked it does not compile. Everything still builds into one
binary. ([Learn chapter 12](https://github.com/pin-language/docs/blob/HEAD/learn/12-modules-and-packages.md))

## A web server in one file

```php
function handle(string $method, string $path, string $body): string {
    if ($path == "/hello") { return json_encode(["message" => "hello"]); }
    http_status(404);
    return json_encode(["error" => "not found"]);
}
serve(8080, "handle");
```

Store data in SQLite with `db_open`, `db_exec`, `db_query` and `db_json` — the SQL must be a string literal with
`?` placeholders, so SQL injection does not compile ([example](examples/notes_api.pn)). Read requests with `url_path($path)`, `query_param($path, "k")`, `form_param($body, "k")` and
`json_string($body, "k")` / `json_int` / `json_float` / `json_bool` / `json_has`. `pin run` it and you get an HTTP/1.1 server with keep-alive and pipelining, one thread per CPU core
(`PN_THREADS=4` to change), on Linux (epoll) and macOS (kqueue). There are no global variables, so
handlers share nothing and need no locks. If a handler panics, that client gets
`500 {"error":"Internal Server Error"}`, everything the request allocated is freed, and the server keeps
serving.

## What is checked for you

- **At compile time:** types, ownership (use after move, moving inside a loop, changing a borrowed
  value, changing a list while `foreach` walks it), and that every variable has a value on every path.
- **At run time (always on, no flag to turn them off):** list bounds, missing map keys, division by zero,
  `intdiv(PHP_INT_MIN, -1)`, `(int)` of NAN/INF/huge floats, and
  every allocation size (a huge `fill()` panics instead of producing a too-small buffer).
- **Not checked yet:** `+ - *` on ints wrap around on overflow. Checking them cost up to 23% on integer
  loops, so it is postponed until the compiler can skip the checks it can prove unnecessary.

## Benchmarks

Same algorithm in every language, same output checked for all of them. Apple M2 (8 cores), macOS 14,
best of 3 runs. No CPU pinning on macOS, so treat differences under ~20% as noise; memory numbers
and the full method are in the benchmarks repository.

| benchmark | Pin | Rust 1.95 | Go 1.26 | PHP 8.5 + JIT | PHP 8.5 | Python 3.15 |
|---|---|---|---|---|---|---|
| sieve (20M) | 0.042s | 0.043s | 0.041s | 0.652s | 1.337s | 4.283s |
| collatz (1M) | 0.165s | 0.131s | 0.175s | 0.714s | 3.204s | 6.302s |
| pi (100M terms) | 0.090s | 0.090s | 0.091s | 0.930s | 2.235s | 13.239s |
| list build + sum | 0.114s | 0.131s | 0.183s | 0.440s | 1.192s | 4.754s |

Caveats: these are small CPU loops, not real applications. `fib` is left out because clang optimises
that recursion in ways that say little about the language. Pin's ints are not overflow-checked, while
Rust's release build is not either and Go also wraps. Programs, the runner and raw numbers:
[pin-language/benchmarks](https://github.com/pin-language/benchmarks).

An earlier HTTP load test on a 2-core Linux container (each server pinned to one core, wrk with 64
connections) gave Pin 132k req/s for a hello endpoint vs 42k for Go `net/http` and 10k for FrankenPHP.
Pin's server does far less (no TLS or HTTP/2), so that gap is not a language
comparison; the CPU-bound endpoint (`/primes`) was 1.2× Go and 4.8× FrankenPHP. Details:
[pin-language/benchmarks](https://github.com/pin-language/benchmarks) (`http/`).

## Not there yet

Inheritance and interfaces, `null` / nullable types, `finally`, default parameter values, closures,
PostgreSQL (SQLite is there, with a connection pool). `json_decode()` into a class and `go`/`await` are built.
Most of these are already designed in [pin-language/docs/design](https://github.com/pin-language/docs/blob/HEAD/design/); see
[CHANGELOG.md](CHANGELOG.md) for what changed.

## Feedback wanted

This preview exists to learn what AI agents and PHP developers find natural or confusing. Please
[open an issue](https://github.com/pin-language/pin/issues) with:

- code that you expected to work but got a compile error for (and whether the message helped),
- anything that crashed instead of printing `panic:` — that is a bug we want to see,
- what you would need before trying Pin for a real API.

Run the test suite with `python3 tests/run_tests.py` (≈1 minute).

## License

MIT — see [LICENSE](LICENSE).
