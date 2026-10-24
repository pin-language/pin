# Pin

[![test](https://github.com/pin-language/pin/actions/workflows/test.yml/badge.svg)](https://github.com/pin-language/pin/actions/workflows/test.yml)

**A language for AI coding agents to write fast, memory-safe web backends.**
PHP-like syntax, compiled to native code, no garbage collector.

Most code is now written by AI agents, and they already know PHP. Pin keeps that syntax but moves the
mistakes from production to compile time: every variable has one type, memory is managed by
compile-time ownership (like Rust, simplified), every path through a function is checked, and each
error says how to fix it — in plain text or as JSON for the agent's loop. The result compiles through
LLVM into a single native binary, including a multi-threaded HTTP server.

> **Status: v0.1 preview for early testers.** The language is small and still changing. Do not use it
> in production, and do not expose its HTTP server to the internet (no TLS, no timeouts yet).
> 🇹🇭 ภาษาไทย: [README.th.md](README.th.md)

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

1. Point the agent at [docs/ai-guide.md](docs/ai-guide.md) — the whole v0.1 language on one page, with
   idioms, a PHP → Pin table and an error → fix table. For example, add this line to your project's
   `AGENTS.md` / `CLAUDE.md`: *"This project is written in Pin. Read `<path to pin>/docs/ai-guide.md`
   before writing code, and run `pin check --json` after every change."*
2. Let it loop on `pin check --json file.pn`: one JSON object per error
   (`{"file","line","severity","message"}`), `{"file","ok":true}` when clean, exit code 1 on error.
3. Run with `pin run file.pn`. Run-time errors print `panic: ...` with the line and exit 1 (inside an
   HTTP handler they answer 500 and the server keeps going).

Messages are written for agents trained on PHP: reaching for `class`, `try`, `switch`, `null`,
`array()`, closures or `include` gets "not in Pin v0.1 — write this instead", not a parse error.

## Quick start

You need **Python 3.10+** and **clang**.

- macOS: `xcode-select --install`
- Ubuntu / Debian: `sudo apt install clang python3`
- Windows: not supported yet (WSL with Ubuntu should work but is untested).

```bash
git clone https://github.com/pin-language/pin.git
cd pin
./pin run examples/hello.pn
```

Put `pin` on your PATH (no sudo needed; `~/.local/bin` is on the PATH of most setups — if `pin -v`
then says "command not found", add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`):

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/pin" ~/.local/bin/pin && pin -v
```

| command | what it does |
|---|---|
| `pin run file.pn` | compile and run (the binary is thrown away) |
| `pin build file.pn [-o out]` | compile to a native binary |
| `pin check file.pn` | type- and ownership-check only |
| `pin version` | print the version |

**Editor:** VS Code syntax highlighting is in [`editors/vscode`](editors/vscode/) — from the repository,
`cd editors/vscode && npx @vscode/vsce package && code --install-extension pin-0.1.0.vsix`.

Start with [`examples/`](examples/): `hello`, `fizzbuzz`, `wordcount`, `ownership`, `primes`, and
`api` (a small JSON server). The full guide is [docs/language.md](docs/language.md).

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

## A web server in one file

```php
function handle(string $method, string $path, string $body): string {
    if ($path == "/hello") { return json_encode(["message" => "hello"]); }
    http_status(404);
    return json_encode(["error" => "not found"]);
}
serve(8080, "handle");
```

`pin run` it and you get an HTTP/1.1 server with keep-alive and pipelining, one thread per CPU core
(`PN_THREADS=4` to change), on Linux (epoll) and macOS (kqueue). There are no global variables, so
handlers share nothing and need no locks. If a handler panics, that client gets
`500 {"error":"Internal Server Error"}`, everything the request allocated is freed, and the server keeps
serving.

## What is checked for you

- **At compile time:** types, ownership (use after move, moving inside a loop, changing a borrowed
  value, changing a list while `foreach` walks it), and that every variable has a value on every path.
- **At run time (always on, no flag to turn them off):** list bounds, missing map keys, division by zero,
  `intdiv(PHP_INT_MIN, -1)`, `(int)` of NAN/INF/huge floats, `intval()` of numbers that do not fit, and
  every allocation size (a huge `fill()` panics instead of producing a too-small buffer).
- **Not checked yet:** `+ - *` on ints wrap around on overflow. Checking them cost up to 23% on integer
  loops, so it is postponed until the compiler can skip the checks it can prove unnecessary.

## Benchmarks

Same algorithm in every language, same output checked for all of them. Apple M2 (8 cores), macOS 14,
best of 3 runs, peak memory. No CPU pinning on macOS, so treat small differences as noise.

| benchmark | Pin | Rust 1.95 | Go 1.26 | PHP 8.5 + JIT | PHP 8.5 | Python 3.15 |
|---|---|---|---|---|---|---|
| sieve (20M) | 0.047s / 20 MB | 0.047s / 20 MB | 0.046s / 23 MB | 0.640s / 330 MB | 1.351s / 326 MB | 4.412s / 165 MB |
| collatz (1M) | 0.166s / 1 MB | 0.132s / 1 MB | 0.180s / 4 MB | 0.721s / 25 MB | 3.235s / 21 MB | 6.440s / 11 MB |
| pi (100M terms) | 0.092s / 1 MB | 0.090s / 1 MB | 0.091s / 4 MB | 0.932s / 25 MB | 2.237s / 21 MB | 12.352s / 12 MB |
| list build + sum | 0.104s / 59 MB | 0.135s / 399 MB | 0.182s / 124 MB | 0.494s / 230 MB | 1.170s / 175 MB | 4.869s / 104 MB |

Caveats: these are small CPU loops, not real applications. `fib` is left out because clang optimises
that recursion in ways that say little about the language. Pin's ints are not overflow-checked, while
Rust's release build is not either and Go also wraps. Reproduce with `python3 bench/run.py` (needs all
toolchains); raw numbers in `bench/results.json`.

An earlier HTTP load test on a 2-core Linux container (each server pinned to one core, wrk with 64
connections) gave Pin 132k req/s for a hello endpoint vs 42k for Go `net/http` and 10k for FrankenPHP.
Pin's server does far less (no TLS, HTTP/2, header API or timeouts), so that gap is not a language
comparison; the CPU-bound endpoint (`/primes`) was 1.2× Go and 4.8× FrankenPHP. Details:
`api/loadtest.json`, `api/loadtest.py`.

## Not there yet

Classes, `null` / nullable types, `try`/`catch`, `list<string>` and nested containers, closures,
modules/imports, request headers and query strings, `json_decode`, database access, `go`/`await`.
Most of these are already designed (in Thai) in [`docs/design/`](docs/design/); see
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
