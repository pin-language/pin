# CLAUDE.md — Pin prototype language (formerly "Pina")

Read `HANDOFF.md` first: it has the current state, verified numbers, known bugs and the task queue.

## What this is

A new programming language for PinaTo Co., Ltd. that **looks like PHP** but compiles to native code via
LLVM, with **Rust-style ownership instead of a garbage collector**. Target use: web APIs / backends.
Name **Pin** (was "pina"), file extension `.pn`. It is a prototype, not production.
Owner's rule for every design decision: **memory safety > speed > clarity > looking like PHP**.
Agreed design specs live in `docs/design/` (most not implemented yet — see HANDOFF.md §4).

```
.pn source → pinac.py (lexer → parser → type+ownership checker → LLVM IR) → clang -O2 + runtime.c → one native binary
```

## Commands

```bash
python3 tests/run_tests.py              # THE check. Must stay green. (~1 min, includes benches + HTTP)
python3 tests/run_tests.py --no-server  # skip the HTTP server check
python3 tests/run_tests.py --update     # regenerate expected outputs — only after reviewing the diff
python3 pinac.py file.pn [-o out]       # compile; writes file.ll next to the source
python3 pinac.py --emit-ll file.pn      # IR only (used for compile-error tests)
python3 bench/run.py                    # CPU benchmarks vs Rust/Go/PHP/Python (needs all 5 toolchains)
python3 api/loadtest.py <wrk>           # API load test; start the 4 servers first (see HANDOFF.md)
```

Needs: Python 3.10+ (tested 3.13), clang (tested 18). No Python packages.

Memory-safety check (do this after touching runtime.c or ownership/codegen):
```bash
clang -O1 -c -Wno-override-module X.ll -o X.o
gcc -g -fsanitize=address,undefined runtime.c X.o -lm -pthread -o X_asan && ./X_asan   # Linux
# macOS: use clang instead of gcc for the link step (LeakSanitizer only works on Linux)
# Linux check from the Mac: Docker image php:8.3-fpm + apt-get install clang python3, then run_tests.py
```

## Layout

| path | what |
|---|---|
| `pinac.py` | whole compiler, one file (~1850 lines). `Parser` → AST `Node`s; `FuncGen` does type checking, ownership checking and IR emission in one pass |
| `runtime.c` | allocator (16-byte block header, per-request tracking), lists, strings, ordered hash maps, json_encode, printing, panics, HTTP/1.1 server (epoll on Linux, kqueue on macOS) |
| `tests/` | `*.pn` + `expected/*.out`; `errors/*.pn` must fail to compile; `panics/*.pn` must exit 1; `server/panics.pn` = handler panics give 500 |
| `bench/` | same 5 programs in pn/rust/go/php/python + `run.py`, `memrun.c` |
| `api/` | same 3-endpoint JSON API in `server.pn`, `server.go`, PHP (FrankenPHP worker + classic) + `loadtest.py` |

## Language invariants — do not break these without the owner's OK

1. Every variable has one fixed type, inferred from its first assignment. Parameters and returns are typed.
2. Types: `int float bool string list<int|float|bool> map<int|string, int|float|bool|string> void`.
3. **Ownership**: each list/map/string has exactly one owner; freed when the owner's function returns or
   the variable is overwritten. Lists/maps **move** on `$b = $a`; strings **copy** (literals never copied).
   Function arguments are **read-only borrows**. Borrowed values (params, foreach vars) cannot be changed,
   moved or returned. No mutation of a container while `foreach` walks it.
4. **No global variables** — this is what lets `serve()` run handlers on many threads with no locks.
5. Conditions must be `bool` (no truthiness). `/` always returns float (like PHP); `intdiv()` for ints.
6. Runtime errors (index out of range, missing map key, div by zero, int overflow, size too large)
   **panic** = print + `exit(1)`; inside an HTTP handler a panic frees the request's blocks and answers
   500 instead. Bounds checks and overflow checks are never optional (no unsafe, no flag).
7. int `+ - *` panic on overflow; `wrapping_add/sub/mul()` wrap on purpose. Every allocation size that
   comes from a program value goes through `checked_size()` in `runtime.c`.
8. Syntax should stay as close to PHP as the rules above allow (PHP likeness is the lowest priority). When unsure, do what PHP 8 does.

Owner decisions so far (in Thai conversation): 1A web API focus · 2C ownership like Rust · 5B LLVM backend ·
3A concurrency **revised 2026-10-24** to structured `go` + `await` (`docs/design/concurrency.md`) ·
4 errors **answered 2026-10-24**: Swift/Zig-style `throws`/`try`/`catch` compiled to return values
(`docs/design/error-handling.md`) · null, strings, int overflow, ownership v2: see `docs/design/`.
Changing any of these = change of project direction → ask first.

## Codegen conventions (pinac.py)

- `expr()` returns `(llvm_value, type, owned)`. `owned=True` means the caller must free or store it.
- `borrow(e)` = evaluate for reading; owned results go to `self.temps`, freed at end of statement.
- `owned_value(e)` = evaluate for storing; moves lists/maps out of variables, dups strings.
- String literals are constants `{len, cap=-1, bytes}`; runtime never frees/writes `cap < 0`.
- Map values live in 8-byte slots; strings stored as pointers, bools as i64.
- Every new heap type needs: `is_heap`, `free_fn`, init in `declare`, and ASAN-clean tests.
- Language heap memory must come from `xmalloc/xcalloc/xrealloc/xfree` in `runtime.c` (never plain
  malloc/free): the block header is what lets a panicking request be cleaned up. Server-internal buffers
  (`Conn`) use plain malloc on purpose — they outlive requests.
- New panics call `die()` (not `exit`) so they become a 500 inside handlers.
- Add a test for every feature (`tests/*.pn` + expected) and every new compile error (`tests/errors/`).

## Working with the owner

The owner runs PinaTo and mostly uses AI instead of staff. Write to them **in Thai**, plain and short.
- Act first, then report. Ask only when: irreversible, changes project direction, or needs info only they have.
- Offer at most 3 options, always say which you recommend and why.
- Never claim "first / nobody has done this / no competitor" without searching first.
- Separate verified facts from guesses. If you change a previous recommendation, say what was wrong.
- Benchmarks: always verify outputs match across languages, and state the caveats next to the numbers.
