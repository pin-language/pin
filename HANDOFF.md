# HANDOFF — state as of 2026-10-24 (v0.1.0 preview)

**Main users are AI coding agents** (owner, 2026-10-24: "nobody writes code by hand any more"). Keep
`docs/ai-guide.md` complete and correct for every release: it is what agents read. Error messages should
say what to write instead; `pin check --json` is the agent loop.

The language is **Pin** (was "Pina"), extension `.pn`. Entry point for users: `./pin` (run/build/check);
the compiler itself is still `pinac.py`. **v0.1.0 is prepared for outside testers** (README.md in English,
README.th.md, docs/language.md, docs/ai-guide.md, examples/, LICENSE MIT, CHANGELOG.md, CI workflow) and
tag `v0.1.0`. **Not published yet**: creating the public repository was blocked by Claude Code's
permission check, so the owner does it. The READMEs already point at https://github.com/hugo8xx/pin-lang
(clone URL, issues, CI badge). Planned push: branch `release/v0.1.0` + tag `v0.1.0` (the owner's rule is
never to push to `main` without saying so), then check that the first CI run is green.

Owner's rule for every design decision, in order: **memory safety > speed > clarity > looking like PHP.**
The owner has said "decide for me, just go": make design calls by that rule, write them down, and only
ask when the rule cannot decide or a decision would change project direction.

Design specs (agreed with the owner, not all implemented): `docs/design/` — ownership-v2, error-handling,
int-overflow, null, strings, concurrency.

## 1. What works (verified)

`python3 tests/run_tests.py` → **54 passed, 0 failed** on macOS arm64 (owner's MacBook) **and** on
Linux (Debian, clang 19, in Docker: `php:8.3-fpm` image + `apt-get install clang python3`).

- Everything from the first prototype (functions, int/float/bool/string, `list<T>`, ordered `map<K,V>`,
  control flow, ownership checker, ~20 PHP built-ins, HTTP/1.1 server with keep-alive + pipelining).
- **macOS**: server uses kqueue (epoll on Linux) behind a small `poll_*` interface in `runtime.c`. On
  macOS all threads share one listener (SO_REUSEPORT does not spread load there).
- **Int arithmetic**: `+ - *` wrap around (64-bit, defined). Overflow panics were built, cost up to 23%,
  and were **taken out on the owner's request** ("if something made the scores drop, cut it first").
  Kept because they cost nothing: `intdiv(INT_MIN, -1)` panics (was UB / x86 crash), `x % -1` is 0,
  `(int)` of NAN/INF/huge float panics, `intval()` of a too-big number panics. `wrapping_*()` exist.
- **Allocation sizes are checked** (`checked_size()`): `fill(2^61)` used to be a real heap overflow
  (ASAN-confirmed on the old runtime); now it panics.
- **Handler panics answer 500** and the server keeps going. Every heap block has a 16-byte header;
  during a request blocks are linked per thread, and a panic longjmps back and frees them all.
  Body is generic (`{"error":"Internal Server Error"}`), the message goes to stderr only.
- **Path-aware checking** (`class Flow` in `pinac.py`): each if-branch and loop is checked on its own
  copy of the state (assigned / moved / reachable) and the copies are merged where paths meet. Fixes B3,
  and adds *definite assignment*: using a variable that is not assigned on every path is a compile error.
  That closes a crash found while testing: `if ($c) { $xs = [1]; } echo count($xs);` read a null list
  (segfault) when `$c` was false. `break`, `continue` and `return` are followed correctly (a move just
  before `break` or `return` is fine). Tests: `tests/flow.pn`, `tests/errors/maybe_*.pn`,
  `loop_zero_times.pn`, `move_then_continue.pn`.
- ASAN+UBSAN clean on macOS and Linux (LeakSanitizer on Linux too): hello, strmap, arith, listsum, all panic tests, and both servers
  under load (pipelining, half-close, garbage, chunked bodies, ~1,450 panicking requests).

### AI-agent usability tests (2026-10-24)

Two fresh agents that had never seen Pin got only `docs/ai-guide.md` and three tasks each:
- Sonnet: order stats, word frequency top-3, JSON API (VAT, fizzbuzz map, 400/404). All correct, ≤2
  check rounds per task.
- Haiku: grades + self-written sort/median, satang ledger with rejected withdrawals, shop API with a
  query string. All correct, 4–6 check rounds per task.
No crash, hang or traceback in either. Their findings were fixed (messages for ternary types, float
index, unset on lists, PHP functions; json_encode(380.0) printing 3.8e+02; guide idioms). Repeat this
after each language change: it is the best signal for the main users.

## 2. Measured (owner's Apple M2 Mac, best of 5, pin only; full comparison in README.md)

| bench | original prototype | with overflow panics | now (panics removed) |
|---|---|---|---|
| sieve 20M | 0.049s | 0.054s | 0.045–0.050s |
| collatz 1M | 0.161s | 0.198s | 0.162s |
| pi 100M | 0.091s | 0.090s | 0.089s |
| listsum | 0.135–0.145s | 0.117–0.123s | 0.100–0.104s (faster since the allocator header change; not investigated) |

The Linux-container numbers in README.md (vs Rust/Go/PHP) are from before these changes. HTTP req/s was
not re-measured (no `wrk` on the Mac: `brew install wrk`).

## 3. Known bugs / limitations

| # | problem | where | severity |
|---|---|---|---|
| B5 | No request headers, query string, timeouts (slowloris), per-connection limits | server | security |
| B6 | `maplit` key type comes from a heuristic `expr_type_probe` | `pinac.py` | low |
| B8 | `.ll` files are written next to sources | `pinac.py main()` | cosmetic |
| B9 | `+ - *` wrap silently (overflow panics removed for speed until check elision exists, int-overflow.md O3) | codegen | correctness |
| B10 | Request-block tracking is per thread. When `go`/`await` arrives, tasks must use the request's list (or their own) — revisit then | `runtime.c` allocator | design |
| B11 | Panic outside a request still `exit(1)` (correct for CLI programs) | runtime | by design |

Fixed today: B1 (Linux-only server), B2 (panic kills server), B3 (branch-blind moves), B4 (silent int
overflow), B7 (Mac bench units), and the maybe-unassigned null-list crash.

## 4. Task queue (in order)

1. ~~Shared control-flow analysis~~ — **done** as `class Flow` (per-path assigned/moved/live, merged at
   joins; loops require the back-edge state to be no worse than the loop head, which avoids a fixpoint).
   Still to add on top of it when the features arrive: null-narrowed vars (null.md N2), live borrows
   (ownership-v2 R2 — needs "last use" liveness, a backward pass over the function body), pending tasks
   (concurrency.md C1). Each is one more field in `Flow` plus its merge rule.
2. **Error handling** (error-handling.md): `throws`, `try`, `catch`, `error(code, msg)`, handler `throws`.
3. **Strings** (strings.md): UTF-8 validation at the edges (invalid request → 400), `substr` boundary
   panic, `mb_*`, `grapheme_*`.
4. **Null** (null.md) together with **class** (ownership-v2 R1–R7).
5. **`list<string>` and nested containers**.
6. **Request headers + query string + timeouts** (B5) — needed before any real deployment.
7. **json_decode, database driver** — design not written yet; decide by the owner's rule when reached.
8. **Concurrency** (concurrency.md) — needs 1, 2 and the request allocator (B10).
9. Modules/imports — design not written yet.

After each: tests for the feature and every new compile error, ASAN/UBSAN on macOS, run the suite in the
Linux container, update this file.

## 5. Reproducing

- Tests: `python3 tests/run_tests.py` (≈1 min). Linux: run the same inside Docker (see §1).
- Sanitizers: see CLAUDE.md. On macOS use clang for the link step; LeakSanitizer only works on Linux.
- Server memory on macOS: use `footprint -p <pid>` (phys_footprint). RSS hides leaks because macOS
  compresses idle pages.
- Benchmarks vs other languages: `python3 bench/run.py` (needs rust, go, php, python). Load test:
  `python3 api/loadtest.py <wrk>` — on macOS there is no CPU pinning, say so next to any numbers.
- Raw results from the Linux container: `bench/results.json`, `api/loadtest.json`.
