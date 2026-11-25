# Pin

**A memory-safe programming language for web backends, with PHP-like syntax, compiled to native code.**

[![test](https://github.com/pin-language/pin/actions/workflows/test.yml/badge.svg)](https://github.com/pin-language/pin/actions/workflows/test.yml)
[![version](https://img.shields.io/badge/version-0.9.0_preview-orange)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

English · [ภาษาไทย](README.th.md)

Pin is a new programming language. Its source files end in `.pn`. A PHP developer can read Pin code
straight away, but Pin is compiled ahead of time through LLVM into a single native binary. It has no
garbage collector: memory is managed by ownership rules that the compiler checks, similar to Rust but
simpler. Pin is built for JSON APIs and other web backends, and it is designed to be written by people
and by AI coding agents.

> **Status: public preview (0.9.0).** Pin is intended for evaluation and feedback. The language is still
> changing between versions. **Do not use it in production yet.**

> **Why GitHub lists Python and C for this repository.** The Pin compiler (`pinac.py`) is a prototype
> written in Python, and the runtime library is written in C. Programs written in Pin do not run on
> Python: they are compiled to native machine code. GitHub does not recognise the Pin language yet, so
> `.pn` files are highlighted as PHP, which has nearly the same syntax.

## Contents

1. [Design principles](#design-principles)
2. [Getting started](#getting-started)
3. [The language at a glance](#the-language-at-a-glance)
4. [Building a web backend](#building-a-web-backend)
5. [Safety and security](#safety-and-security)
6. [Performance](#performance)
7. [How Pin works](#how-pin-works)
8. [Project status](#project-status)
9. [Documentation](#documentation)
10. [Using Pin with AI coding agents](#using-pin-with-ai-coding-agents)
11. [Contributing and feedback](#contributing-and-feedback)
12. [License](#license)

## Design principles

Every design decision in Pin follows one order of priorities:

1. **Memory safety.** A Pin program cannot read or write memory it does not own. Bounds checks and size
   checks are always on, and no flag or `unsafe` block turns them off.
2. **Speed.** Programs compile to native code with no garbage collector and no interpreter.
3. **Clarity.** Each variable has one type, and conditions must be `bool`. Compiler errors explain how to
   fix the problem.
4. **Familiarity.** The syntax stays as close to PHP 8 as the first three rules allow.

## Getting started

### Requirements

- macOS or Linux (Windows through WSL with Ubuntu, untested)
- Python 3.10 or newer, used to run the compiler
- clang, used to produce the native binary

### Install

The installer needs no `sudo`. It checks the requirements, installs Pin into `~/.pin`, links the `pin`
command into `~/.local/bin` and runs a test program:

```bash
curl -fsSL https://raw.githubusercontent.com/pin-language/pin/HEAD/install.sh | sh
```

If clang is missing, install it with `xcode-select --install` on macOS or `sudo apt install clang` on
Ubuntu/Debian. If the shell then reports `pin: command not found`, the installer has printed the line to
add to your shell configuration.

<details><summary>Manual installation</summary>

```bash
git clone https://github.com/pin-language/pin.git ~/.pin
mkdir -p ~/.local/bin && ln -sf ~/.pin/pin ~/.local/bin/pin
pin ~/.pin/examples/hello.pn
```

</details>

To update, run the installer again. To uninstall, run `rm -rf ~/.pin ~/.local/bin/pin`.

### Your first program

```bash
pin -v
printf '%s\n' 'echo "Hello from Pin\n";' > hello.pn
pin hello.pn
```

### Commands

| Command | Description |
|---|---|
| `pin file.pn` | Compile and run (same as `pin run file.pn`) |
| `pin build file.pn [-o out]` | Compile to a native binary |
| `pin check file.pn` | Check types and ownership without building |
| `pin check --json file.pn` | The same, with one JSON object per error |
| `pin get github.com/user/repo@v1.0.0` | Add a package, pinned in `pin.lock` |
| `pin version` | Print the version |

The [`examples/`](examples/) folder has short programs to start from: `hello`, `fizzbuzz`, `wordcount`,
`ownership`, `primes`, `api` (a JSON server) and `notes_api` (a server backed by SQLite).

## The language at a glance

```php
function squares(int $n): list<int> {   // returns a new list; the caller owns it
    list<int> $out = [];
    for ($i = 1; $i <= $n; $i++) { $out[] = $i * $i; }
    return $out;
}

function total(list<int> $xs): int {    // borrows the list read-only: nothing is copied
    $sum = 0;
    foreach ($xs as $x) { $sum += $x; }
    return $sum;
}

$name = "world";
echo "Hello, $name!\n";
echo total(squares(10)), "\n";          // 385
```

### Types

| Kind | Examples |
|---|---|
| Scalars | `int`, `float`, `bool`, `string` (always valid UTF-8) |
| Containers | `list<int>`, `map<string, User>` (ordered like PHP arrays), nested freely |
| Classes | `class User { ... }`, without inheritance |
| Values that may be null | `?User`, `?int`: they must be checked before use |
| Functions | `fn(int): string`, for closures and named functions |

A variable's type comes from its first value and does not change. Parameters and return values are
always typed.

### Ownership

```php
$a = [1, 2, 3];
echo total($a);      // a function call lends the list read-only; $a still owns it
$b = $a;             // lists and maps MOVE: $b owns the list now
echo count($a);      // compile error: $a was moved on line 3. Use copy($a) ...
$c = copy($b);       // an explicit copy
$s = "text";
$t = $s;             // strings are copied, as in PHP
```

Each list, map and string has exactly one owner and is freed when its owner goes out of scope. The
compiler checks each path through a function separately, and its errors say how to fix the code:

```
ownership.pn:20: error: $a was moved on line 18 and can no longer be used here. Use copy($a) if you need two separate values
```

### Errors

A function that can fail is declared with `throws`, and every call to it must handle the failure with
`try`, either passing it on or catching it:

```php
$body = try http_get("http://127.0.0.1:8080/health", [], 2000) catch ($e) { "down: " . $e->code; };
```

Errors are compiled to ordinary return values, so they cost nothing when nothing fails.

### Main differences from PHP

| | PHP | Pin |
|---|---|---|
| Variable types | Can change at any time | Fixed, inferred from the first value |
| Parameter and return types | Optional | Required |
| Arrays | One `array` type for lists and maps | `list<T>` and `map<K, V>` |
| Missing map key / index out of range | Warning, then `null` | Run-time error (`panic`); use `isset()` or `??` |
| `if ($n)` | Truthy / falsy | The condition must be a `bool` |
| Changing an array passed to a function | Changes a copy | Compile error; call `copy()` first |
| Memory management | Reference counting and garbage collection | Ownership checked at compile time |
| Integer overflow | Becomes a float | Wraps around (64-bit) |
| Strings | Any bytes | Always valid UTF-8; `mb_*` and `grapheme_*` built in |

Messages are written for people who know PHP. Writing `extends`, `finally`, `switch`, `array()` or
`include` gives an explanation of what to write in Pin instead, not a bare syntax error.

### Modules and packages

```php
import prices;                                   // prices.pn in the project folder
import github.com/someone/strutil/words;         // a package added with: pin get github.com/someone/strutil@v1.0.0
echo prices::price("tea"), " ", words::greet("pin"), "\n";
```

Only functions marked `export` can be called from other files. A package is any Git repository:
`pin get` records its commit and a SHA-256 of its code in `pin.lock`, and every build checks them. The
result is still one binary.

## Building a web backend

```php
function handle(string $method, string $path, string $body): string {
    if ($path == "/hello") { return json_encode(["message" => "hello"]); }
    http_status(404);
    return json_encode(["error" => "not found"]);
}
serve(8080, "handle");
```

| Area | What Pin provides |
|---|---|
| HTTP server | HTTP/1.1 with keep-alive and pipelining, one thread per CPU core (`PN_THREADS` to change), epoll on Linux and kqueue on macOS, timeouts and connection limits, server-sent events |
| Requests | `url_path`, `query_param`, `form_param`, `request_header`, `json_decode` into classes, `json_string` / `json_int` and similar |
| Databases | SQLite and PostgreSQL through the same `db_*` functions, with a connection pool; rows can be read into classes |
| Concurrency | Structured `go` / `await` tasks, a publish/subscribe Hub |
| HTTP client | `http_get`, `http_post`, `http_stream` for `http://` URLs |
| Security helpers | `password_hash`, `hash_hmac`, `hash_equals`, `random_token`, `random_int` |

Pin has no global variables, so request handlers share nothing and need no locks. If a handler fails at
run time, only that client receives `500 Internal Server Error`: everything the request allocated is
freed and the server continues. TLS (https) is handled by a reverse proxy such as Caddy or nginx, by
design ([design/tls.md](https://github.com/pin-language/docs/blob/HEAD/design/tls.md)).

## Safety and security

**Checked at compile time:** types, ownership (use after move, changing a borrowed value, changing a list
while `foreach` walks it), and that every variable has a value on every path.

**Checked at run time, always:** list bounds, missing map keys, division by zero,
`intdiv(PHP_INT_MIN, -1)`, invalid float-to-int conversions, and the size of every allocation.

**Built into the server and libraries:**

- SQL must be a string literal with `?` placeholders, so SQL injection does not compile.
- Response headers that contain CR or LF are rejected, which prevents header injection.
- Requests whose headers do not follow RFC 9112 exactly are refused with `400`, which prevents request
  smuggling behind a proxy.
- Request paths and bodies must be valid UTF-8.
- Map hashing uses a random key chosen at start-up, so attackers cannot send keys that all collide.
- The HTTP client accepts only plain IPv4 addresses (`a.b.c.d`) as numeric hosts.

**How it is tested:** a test suite of more than 300 checks runs on every change, on Linux and macOS,
both normally and under AddressSanitizer and UndefinedBehaviorSanitizer. Fuzzers in [`tools/`](tools/)
feed the compiler and runtime with generated programs, mutated JSON, HTTP requests and responses, text
and SQL.

**Known limitation:** integer `+ - *` wrap around on overflow instead of failing. Checking them cost up
to 23% on integer loops, so the check is postponed until the compiler can remove checks it can prove
unnecessary ([design/int-overflow.md](https://github.com/pin-language/docs/blob/HEAD/design/int-overflow.md)).
There is no per-request memory limit yet.

**Reporting a vulnerability:** please do not open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/pin-language/pin/security/advisories/new)
(the *Security* tab, then *Report a vulnerability*).

## Performance

The same algorithm was run in every language, and all outputs were checked to be identical. Apple M2
(8 cores), macOS 14, best of 3 runs.

| Benchmark | Pin | Rust 1.95 | Go 1.26 | PHP 8.5 + JIT | PHP 8.5 | Python 3.15 |
|---|---|---|---|---|---|---|
| sieve (20M) | 0.042 s | 0.043 s | 0.041 s | 0.652 s | 1.337 s | 4.283 s |
| collatz (1M) | 0.165 s | 0.131 s | 0.175 s | 0.714 s | 3.204 s | 6.302 s |
| pi (100M terms) | 0.090 s | 0.090 s | 0.091 s | 0.930 s | 2.235 s | 13.239 s |
| list build + sum | 0.114 s | 0.131 s | 0.183 s | 0.440 s | 1.192 s | 4.754 s |

**Caveats.**

- These are small CPU loops, not real applications. macOS does not allow CPU pinning, so differences
  below about 20% should be treated as noise.
- `fib` is excluded, because clang optimises that recursion in ways that say little about the language.
- An earlier HTTP test on a 2-core Linux container measured 132k requests/s for Pin, 42k for Go
  `net/http` and 10k for FrankenPHP on a "hello" endpoint. Pin's server does much less (no TLS, no
  HTTP/2), so this is not a fair comparison of the languages. On a CPU-bound endpoint Pin was 1.2× Go.

Programs, method and raw results: [pin-language/benchmarks](https://github.com/pin-language/benchmarks).

## How Pin works

```
file.pn ──► pinac.py ───────────────────────────────────► LLVM IR ──► clang -O2 ──► native binary
            lexer → parser → type and ownership checker              + runtime (C)
```

| Path | Contents |
|---|---|
| [`pinac.py`](pinac.py) | The compiler (prototype, written in Python) |
| [`runtime/`](runtime/) | The runtime library in C: memory, strings, maps, JSON, tasks, HTTP server and client, SQLite, PostgreSQL, Unicode, cryptography |
| [`pin`](pin) | The `pin` command-line tool |
| [`spec/`](spec/) | The description of every built-in function, from which the reference documentation is generated |
| [`tests/`](tests/) | The test suite |
| [`tools/`](tools/) | Fuzzers, the sanitizer script and the documentation generator |
| [`examples/`](examples/) | Example programs |
| [`editors/vscode/`](editors/vscode/) | Syntax highlighting for Visual Studio Code |

## Project status

**Available in 0.9.0:** classes, closures, `?T` null safety, `throws` / `try` / `catch`, `go` / `await`,
modules and Git packages, `json_decode` into classes, SQLite and PostgreSQL, server-sent events, an HTTP
client, Unicode text (`mb_*`, `grapheme_*`, normalisation), and 210 built-in functions and constants.

**Not available yet:** inheritance and interfaces, `finally`, a `bytes` type for binary data,
overflow-checked integers, a per-request memory limit, HTTP/2.

Planned features are designed in
[pin-language/docs/design](https://github.com/pin-language/docs/blob/HEAD/design/), and every change is
listed in [CHANGELOG.md](CHANGELOG.md).

## Documentation

| Document | For |
|---|---|
| [Learn Pin](https://github.com/pin-language/docs/blob/HEAD/learn/README.md) | Fourteen short chapters for newcomers |
| [Language guide](https://github.com/pin-language/docs/blob/HEAD/language.md) | The complete language |
| [Built-in reference](https://github.com/pin-language/docs/blob/HEAD/reference/README.md) | Every built-in function, with examples |
| [Design documents](https://github.com/pin-language/docs/blob/HEAD/design/) | Why the language works the way it does |
| [AI guide](https://github.com/pin-language/docs/blob/HEAD/ai-guide.md) | The whole language on one page, for AI coding agents |

All documentation lives in [pin-language/docs](https://github.com/pin-language/docs).

## Using Pin with AI coding agents

AI coding agents are trained mostly on PHP, so many PHP habits do not compile in Pin. Before an agent
writes Pin code, it should read the [AI guide](https://github.com/pin-language/docs/blob/HEAD/ai-guide.md),
which covers the whole language on one page, with a PHP-to-Pin table and an error-to-fix table.

1. Add a line such as this to your project's `AGENTS.md` or `CLAUDE.md`: *"This project is written in
   Pin. Read https://raw.githubusercontent.com/pin-language/docs/HEAD/ai-guide.md before writing code,
   and run `pin check --json` after every change."*
2. Let the agent loop on `pin check --json file.pn`. It prints one object per error
   (`{"file", "line", "severity", "message"}`), or `{"file", "ok": true}` when the file is clean, and exits
   with code 1 on error.
3. Run the program with `pin file.pn`. A run-time error prints `panic: ...` with the line number and
   exits with code 1.

See also [AGENTS.md](AGENTS.md) and [llms.txt](llms.txt).

## Contributing and feedback

This preview exists to learn what people and AI agents find natural or confusing. Please
[open an issue](https://github.com/pin-language/pin/issues) for:

- code you expected to compile that did not, and whether the error message helped,
- anything that crashed instead of printing `panic:`, which is always a bug,
- what Pin would need before you would try it for a real API.

Before sending a change to the compiler or runtime, read [CLAUDE.md](CLAUDE.md) for the conventions and
the rules the language must keep, then run the test suite (about one minute):

```bash
python3 tests/run_tests.py
```

## License

Pin is released under the [MIT License](LICENSE).
