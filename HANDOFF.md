# HANDOFF — state as of 2026-10-24

Built in a cloud Linux container (x86-64, 2 cores) over two sessions. You (Claude Code) are picking it up
on the owner's **MacBook Air (Apple Silicon, macOS)**. First thing: `python3 tests/run_tests.py`.
On macOS, expect everything green **except the HTTP server** (task 1).

## 1. What works (verified)

`python3 tests/run_tests.py` → **26 passed, 0 failed** on Linux at handoff.

- Compiler `pinac.py`: functions, int/float/bool/string, `list<T>`, `map<K,V>` (ordered like PHP),
  `if/elseif/else`, `while`, `for`, `foreach ($xs as $i => $v)`, `break/continue`, `?:`, `??`,
  string interpolation `"$x {$y}"`, `.` / `.=`, casts, ~20 PHP-named built-ins (list in README.md).
- Ownership checker: use-after-move, move inside loop, mutate/move borrowed param, mutate container
  during foreach — all rejected at compile time with plain-English fixes (see `tests/errors/`).
- Runtime: no GC, no refcount. ASAN+UBSAN clean on `tests/hello.pn`, `tests/strmap.pn`, `bench/listsum.pn`,
  and on the HTTP server under load + pipelining + malformed requests.
- HTTP server `serve($port, "handler")`: HTTP/1.1, keep-alive, pipelining, one thread per core
  (`PN_THREADS=n` overrides), 400 on garbage, answers pipelined requests even after client half-close.

## 2. Measured results (Linux container — will NOT reproduce exactly on an M-series Mac)

CPU benchmarks (best of 3, all languages produce identical output):

| | pina | Rust | Go | PHP 8.3+JIT | Python 3.13 |
|---|---|---|---|---|---|
| sieve 20M | 0.13s | 0.14s | 0.13s | 1.26s | 4.67s |
| collatz 1M | 0.17s | 0.17s | 0.25s | 1.25s | 9.63s |
| pi 100M | 0.13s | 0.10s | 0.12s | 0.82s | 11.1s |
| list build+sum | 0.28s / 40MB | 0.27s / 40MB | 1.20s / 162MB | 1.75s / 239MB | 7.31s / 312MB |

API load test (each server pinned to 1 core, wrk 64 conns on the other core, identical response bytes):

| endpoint | pina | Go net/http | FrankenPHP worker | php -S |
|---|---|---|---|---|
| /hello | 132k req/s* | 42k | 10k | 4.7k |
| /user/42 (JSON) | 95k | 38k | 11k | 4.4k |
| /primes/20000 | 17k | 14k | 3.5k | 2.3k |
| RSS | 2.3 MB | 13 MB | 102 MB | ~100 MB |

Caveats the owner already heard — keep repeating them with any new numbers:
- \* pina /hello was load-generator-bound (server at 80% CPU).
- pina's server does far less than Go net/http (no TLS, HTTP/2, timeouts, header API). Part of the
  /hello and /user gap is that. `/primes` is the fairest language comparison (pina 1.2× Go, 4.8× PHP).
- FrankenPHP 1.13.2 / PHP 8.5.12 crashes on /primes with `opcache.jit=tracing`; we used `jit=function`.
- `php -S` has no keep-alive (wrk shows read errors) — a weak stand-in for PHP-FPM.
- fib benchmark is excluded from claims (clang optimises recursion; not a language property).

## 3. Known bugs / limitations (verified unless marked)

| # | problem | where | severity |
|---|---|---|---|
| B1 | **HTTP server is Linux-only**: epoll, accept4, SOCK_NONBLOCK, sched_getaffinity/CPU_COUNT, memmem needs _GNU_SOURCE | `runtime.c` server section | blocks Mac dev |
| B2 | **A panic inside a request handler kills the whole server** (`exit(1)`), e.g. missing map key | `runtime.c` panics | must fix before any real use |
| B3 | Move tracking is not branch-aware: moving `$a` in the `then` branch makes `$a` "moved" in the `else` branch too → false compile error. (Using it *after* the if is correctly rejected.) | `FuncGen.moved`, `if_()` | annoying |
| B4 | int overflow wraps silently (`PHP_INT_MAX + 1` → negative). PHP turns it into float. | `binary()` | correctness |
| B5 | No request headers, query string, timeouts (slowloris), or per-connection limits; 64 KB header / 64 MB body caps only | server | security |
| B6 | `maplit` infers key type with a heuristic `expr_type_probe` — keys from unlisted built-ins may get the wrong type (not seen, inferred from code) | `pinac.py` | low |
| B7 | `bench/memrun.c` reads `ru_maxrss` as KB; on macOS it is **bytes**. `taskset` does not exist on macOS. | bench/api scripts | Mac only |
| B8 | `.ll` files are written next to sources | `pinac.py main()` | cosmetic |

Not built yet: class/object, null/nullable, try/catch, `list<string>`, nested containers, `explode`,
`json_decode`, database access, the `go` keyword for general concurrency, closures, modules/imports.

## 4. Task queue (in order)

**Task 1 — Make it build and pass on macOS (do this first, no need to ask).**
Port the server loop in `runtime.c` to kqueue behind `#ifdef __APPLE__` (keep epoll for Linux):
accept + `fcntl(O_NONBLOCK)` instead of accept4, `sysconf(_SC_NPROCESSORS_ONLN)` instead of
sched_getaffinity, SO_REUSEPORT works on macOS but does not load-balance like Linux — use one listener
shared by all threads (or one thread) on macOS. Fix B7 too.
Done when: `python3 tests/run_tests.py` is fully green on the Mac AND still green on Linux
(check Linux via Docker or CI), and the ASAN run of the server is clean on macOS.

**Task 2 — Handler panics return 500 instead of killing the server (B2).**
Suggested: per-request arena or setjmp/longjmp in the worker around `pn_user_handler`; the request's
heap values must still be freed (ownership makes the set knowable — or use a per-request arena, which
also matches "free everything at end of request" from the original design discussion).
Done when: a test request that hits a missing key returns 500, the next request still works, ASAN clean.

**Task 3 — Owner's next feature pick (recommended to them, NOT yet confirmed):**
request headers + query string, `json_decode` into typed maps, and a database driver (start with
SQLite or PostgreSQL). Goal: rewrite one real PinaTo API endpoint in pina and benchmark against the
current implementation. The alternative the owner was offered: pick their slowest real endpoint first.
→ Ask the owner which one (this is a direction choice), but do tasks 1–2 while waiting.

Backlog after that: B3 (branch-aware moves: track moved-sets per branch and merge), B4 (overflow →
panic or float; ask owner which), try/catch (decision 4 default), `list<string>`, structs/classes,
`go` keyword (decision 3A), split `pinac.py` into modules once it passes ~2500 lines.

## 5. Reproducing the comparisons

- wrk: build from github.com/wg/wrk (`make`), or `brew install wrk` on Mac.
- FrankenPHP: release binary `frankenphp-linux-x86_64` was used (v1.13.2); Mac: `brew install frankenphp`.
  PHP ini used: `opcache.enable=1, opcache.enable_cli=1, opcache.jit=function, opcache.jit_buffer_size=64M,
  opcache.validate_timestamps=0, memory_limit=512M` via `PHP_INI_SCAN_DIR`.
- Ports: 8081 pina, 8082 Go, 8083 FrankenPHP (`frankenphp php-server --root api/php-worker --listen :8083
  --worker api/php-worker/index.php --no-compress`), 8084 `php -S 0.0.0.0:8084 api/php-classic/index.php`.
- Linux pinned servers with `taskset -c 0` and wrk with `taskset -c 1`. macOS has no CPU pinning:
  results there are less controlled — say so when reporting.
- Raw results: `bench/results.json`, `api/loadtest.json`.
