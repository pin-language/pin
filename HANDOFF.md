# HANDOFF — state as of 2026-10-25 (v0.8.0)

## 0. Next session (updated 2026-10-25, after the framework brainstorm)

The framework was talked through: name **Tack** (owner agreed), design in pin-language/docs branch
`design/tack`, `design/tack.md` — **on hold** until the language prerequisites P1–P5 (§4) are built.
Start with **P1 connection pool**, `design/pool.md` (owner agreed Q1–Q3; step 0 measured: SQLite ~6.8×
faster per request with a pool). The owner creates every new GitHub repository (pin-language/tack too);
Claude pushes to it.

Releases (decided 2026-10-25, owner: "you decide"): **no 0.8.1**. The use-after-free fix (B12) is on
`release/v0.1.0` (new installs get it, install.sh takes HEAD) and on `next`; it ships in **0.9.0** with
P1–P5. Old tags v0.1.0–v0.8.0 stay (docs refer to those numbers). Release less often: one per set.
`release/v0.1.0` is protected now: `test (ubuntu-latest)` and `test (macos-latest)` must pass (admins may
bypass); auto-merge is allowed on the repo.

State at the end of 2026-10-25: released **0.8.0** (pin, docs tags). On `next`, not released: `<?pin` / `?>`
ignored by the lexer, a clearer message for a missing `;` after `try ... catch { }`, the README's github.com
guide link. pin-mcp **v0.1.0** published (github.com/pin-language/mcp), checked with the official MCP Python
SDK and by Google Antigravity (`agy`), which wrote a working MCP server from the org URL alone with zero
compile errors.

Lessons from testing outside agents (keep in mind for docs):
- Chat models without tools (Gemini in the web chat) cannot open raw.githubusercontent.com and then write
  plain PHP. Agents with tools (agy, Claude) read the guide and compile on the first try.
- When agents build JSON by hand in strings they break it (missing `}`, unescaped text): docs push classes +
  `json_encode`; packages that do the protocol (pin-mcp) avoid it entirely.
- A "JSON literal inside json_encode([...]) may mix types and null" relaxation was proposed and not decided.

**Main users are AI coding agents** (owner, 2026-10-24: "nobody writes code by hand any more"). Keep
`ai-guide.md` (pin-language/docs) complete and correct for every release: it is what agents read. Error messages should
say what to write instead; `pin check --json` is the agent loop.

The language is **Pin** (was "Pina"), extension `.pn`. Entry point for users: `pin` (run/build/check/get);
the compiler itself is `pinac.py`. **Public** since 2026-10-25 in the GitHub organisation `pin-language`:
`pin` (this repo), `docs`, `benchmarks`, `.github` (the organisation page). Default branch of each is
`release/v0.1.0` (the owner's rule: never push to `main` without being told; the owner may rename it).
Install: `curl -fsSL https://raw.githubusercontent.com/pin-language/pin/HEAD/install.sh | sh` — checked on a
clean Debian container. Every release: bump `VERSION` in pinac.py, CHANGELOG, tag in `pin` and `docs`.

Outside reviews (Gemini, GPT, 2026-10-25): stale "v0.1" wording made a reviewer think modules and request
reading were missing — keep version wording out of messages ("not in Pin yet") and keep README, llms.txt and
the organisation page's Status section current with every release.

Owner's rule for every design decision, in order: **memory safety > speed > clarity > looking like PHP.**
The owner has said "decide for me, just go": make design calls by that rule, write them down, and only
ask when the rule cannot decide or a decision would change project direction.

Design specs (agreed with the owner, not all implemented): `design/` in the pin-language/docs repo (local clone: ../pin-docs) — ownership-v2, error-handling,
int-overflow, null, strings, concurrency.

## 1. What works (verified)

`python3 tests/run_tests.py` → **150 passed, 0 failed** (0.6.0) on macOS arm64 (owner's MacBook) **and** on
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
  (that server claim was from before 0.7.0 and had stopped being true: see B12; servers are sanitized by `run_tests.py --sanitize` now)
  under load (pipelining, half-close, garbage, chunked bodies, ~1,450 panicking requests).

### Classes (0.5.0)

- `class`, properties, `__construct` (promoted params for scalars/strings only), methods, `$this`, `new`,
  `export class` / `mod::Name`. Objects are 8-byte-slot blocks (`pn_obj_new`); per-type `free:` / `copy:` /
  `json:` functions are generated on demand (`NEEDED`, `gen_helper`) and handed to the runtime's `*_with`
  functions — always generated wrappers, never C functions directly (UBSan `-fsanitize=function`).
- Any type nests (`list<T>` / `map<K,V>` of anything but db/rows/void). Paths: `place()` / `base_value()` /
  `assign_path()`; var-rooted list/map writes keep their old fast code (benchmark IR unchanged).
- Ownership: `mutating(root, props, ...)` is the one gate for every change (writable? foreach walking that
  path? live borrow?). `&` params pass the address of the caller's slot (`slot_over`); `foreach &` uses the
  item's address as the variable's slot. `$u = $xs[0]` (list/map/object) is a borrow var
  (`borrow_vars`); liveness is approximated with postorder node numbers from `prepare()` plus loop ranges
  and loop-top kills — conservative (may reject code that is safe, never the reverse). Changing methods
  (R6) are inferred by compiling all methods repeatedly until no new one is marked (`mod.inferring`).
  Constructor rule N5 uses pseudo-variables `$this->f` in `Flow`.
- Not built: R2's string borrow (strings still copy), null, inheritance, parameter defaults, `&` on bool
  items, `??` on container-valued entries.
- Fresh-agent test (Sonnet, ai-guide only): library system, grades with `foreach &` and `&$xs[1]`, matrix
  transpose — all correct, one compile error in total (its message gave the fix).

### AI-agent usability tests (2026-10-24)

Two fresh agents that had never seen Pin got only `ai-guide.md` (pin-language/docs) and three tasks each:
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
| B5 | ~~headers, timeouts, connection limit~~ done in 0.6.0. No TLS / HTTP/2 by design (design/tls.md: a proxy does it) | server | security |
| B6 | `maplit` key type comes from a heuristic `expr_type_probe` | `pinac.py` | low |
| B8 | `.ll` files are written next to sources | `pinac.py main()` | cosmetic |
| B9 | `+ - *` wrap silently (overflow panics removed for speed until check elision exists, int-overflow.md O3) | codegen | correctness |
| B10 | ~~per-thread request blocks~~ done: `struct Req` per request, `cur_req` follows the running task | `runtime.c` | done |
| B11 | Panic outside a request still `exit(1)` (correct for CLI programs) | runtime | by design |
| B12 | ~~use-after-free on every HTTP request (`request_done` / `cur_req`), crashed under load~~ fixed 2026-10-25; found by the pool measurement (pin-language/docs design/pool.md step 0). CI had not sanitized any server since 0.7.0: now `run_tests.py --sanitize` | runtime | memory safety |
| B13 | HTTP/1.0 keep-alive: the server keeps the connection open but does not answer `Connection: keep-alive`, so `ab -k` hangs | server | low |

Fixed today: B1 (Linux-only server), B2 (panic kills server), B3 (branch-blind moves), B4 (silent int
overflow), B7 (Mac bench units), and the maybe-unassigned null-list crash.

## 4. Task queue (in order)

**Next set (owner, 2026-10-25), before the web framework Tack** (pin-language/docs design/tack.md, on hold):
P1 a database connection pool that outlives requests (design/pool.md; per worker thread; decide this first, it shapes
PostgreSQL) → P2 `json_decode` into a class → P3 `null` + parameter defaults → P4 PostgreSQL on the pool with
a safe way to build filter/sort queries → P5 crypto built-ins. Then Tack M1 only, used for one real app. The
numbered list below is the older order; items 3, 4 and 7 are folded into P1–P5, strings (3) stays after P3.

1. ~~Shared control-flow analysis~~ — **done** as `class Flow` (per-path assigned/moved/live, merged at
   joins; loops require the back-edge state to be no worse than the loop head, which avoids a fixpoint).
   Still to add on top of it when the features arrive: null-narrowed vars (null.md N2), live borrows
   (ownership-v2 R2 — needs "last use" liveness, a backward pass over the function body), pending tasks
   (concurrency.md C1). Each is one more field in `Flow` plus its merge rule.
2. ~~Error handling~~ done in 0.6.0: `throws`, `try`, `catch`, `error(code, msg)`, handler `throws`.
   How: `fn_llret` returns `{T, ptr}`; `failing_call` / `error_branch` / `route_error` send the error to the
   innermost `Catch` (freeing temporaries past its marks) or out of the function. Catch blocks inside an
   expression set the statement's temporaries aside (`temp_stack`). Known gap: owned items already moved into
   a list literal under construction leak if a later item fails (error path only).
   Owner, 2026-10-25: release cadence was too fast — batch features; work happens on branch `next`, release
   when a set is done. Next set: async (concurrency.md C1–C11).
3. **Strings** (strings.md): UTF-8 validation at the edges (invalid request → 400), `substr` boundary
   panic, `mb_*`, `grapheme_*`.
4. **Null** (null.md). Classes (ownership-v2 R1–R7) done in 0.5.0 — null needs `?T` properties now.
5. ~~`list<string>`~~ done in 0.2.0 (plus explode/implode/sort, query/form/JSON request helpers).
   Nested containers done in 0.5.0.
6. ~~Request headers + timeouts~~ done in 0.6.0 (`tests/server/limits.pn`: slowloris, smuggling, injection,
   connection cap). Timer: each worker keeps its connections in a list and scans it once a second.
7. ~~SQLite~~ done in 0.3.0 (`db_*`, literal-SQL rule, resources closed on panic). **PostgreSQL next**
   (owner, 2026-10-24: "SQLite first, then PostgreSQL") with the same `db_*` functions; then a full
   `json_decode`.
8. ~~Concurrency~~ done in 0.7.0 (C1–C11; see concurrency.md status for what differs). Runtime: tasks +
   scheduler + timer heap + wait_fds in runtime.c ("tasks"), Hub ("hub"), client ("HTTP client and run()").
   Compiler: go_expr / await_expr / tasks_done / task_borrows in pinac.py. TLS: decided no (owner, 2026-10-25, design/tls.md). Next ideas: loop-head
   cancellation checks, stack-overflow → panic message, durable await (research first).
9. ~~Modules/imports~~ done in 0.4.0: `import`/`export`/`mod::fn()`, packages via `pin get` + `pin.lock`
   (commit + sha256, checked every build). Not yet: transitive package resolution (a package's own package
   imports must be `pin get`-ed by the project), package versions by semver ranges, a package index.

After each: tests for the feature and every new compile error, ASAN/UBSAN on macOS, run the suite in the
Linux container, update this file.

## 4b. Where things live (owner, 2026-10-25)

- HTTP stays in the language (server, client, Hub, SSE are tied to the scheduler and the per-request
  allocator). `runtime.c` is split into `runtime/*.c` but still built as one translation unit (object code
  identical to the single file).
- Frameworks on top (routing, middleware, auth helpers, apps like the PACT board) go in their own repos as
  Pin packages (`pin get`), not into the language. Packages are Pin source only: no C in packages.

## 5. Reproducing

- Big test round = GitHub Actions (`.github/workflows/test.yml`: the suite + sanitizers on ubuntu-latest x86_64
  and macos-latest, on every push to any branch, ~4 min). Docker Desktop on the owner's Mac hangs when the
  disk is nearly full, so it is not the release gate any more; `tools/linux_test.sh` is still there for arm64:
  the full suite + sanitizers on arm64 and x86_64 in kept images `pin-test:*`
  (built once from tools/linux/Dockerfile). Owner, 2026-10-25: run it for big rounds (before a release), not
  after every change — installing clang in a fresh container each time filled the Mac's disk.
- Tests: `python3 tests/run_tests.py` (≈2 min). Server stderr goes to `build/server_*.log` (a pipe
  nobody read filled after ~64 KB of panic lines and blocked the server: looked like a hang). Linux: run the same inside Docker (see §1).
- Sanitizers: see CLAUDE.md. On macOS use clang for the link step; LeakSanitizer only works on Linux.
- Server memory on macOS: use `footprint -p <pid>` (phys_footprint). RSS hides leaks because macOS
  compresses idle pages.
- Benchmarks vs other languages and the HTTP load test live in github.com/pin-language/benchmarks
  (`python3 cpu/run.py`, `python3 http/loadtest.py <wrk>`). On macOS there is no CPU pinning and peak
  memory for listsum varies 2x between runs — say so next to any numbers.
