# CLAUDE.md — Pin prototype language (formerly "Pina")

Read `HANDOFF.md` first: it has the current state, verified numbers, known bugs and the task queue.

## What this is

A new programming language for PinaTo Co., Ltd. that **looks like PHP** but compiles to native code via
LLVM, with **Rust-style ownership instead of a garbage collector**. Target use: web APIs / backends.
Name **Pin** (was "pina"), file extension `.pn`. It is a prototype, not production.
Owner's rule for every design decision: **memory safety > speed > clarity > looking like PHP**.
Agreed design specs live in `design/` in the pin-language/docs repo (local clone: ../pin-docs) (most not implemented yet — see HANDOFF.md §4).

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
```

Needs: Python 3.10+ (tested 3.13), clang (tested 18). No Python packages.

Memory-safety check (do this after touching runtime.c or ownership/codegen):
`tools/sanitize.sh file.pn ...` does the steps below (CI runs it on Linux and macOS). By hand:
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
| `tests/bench/`, `tests/server/api.pn` | the benchmark programs and the JSON API, kept as tests |
| — | comparisons with Rust/Go/PHP/Python and the HTTP load test: github.com/pin-language/benchmarks (local clone ../pin-benchmarks); docs: github.com/pin-language/docs (../pin-docs) |

## Language invariants — do not break these without the owner's OK

1. Every variable has one fixed type, inferred from its first assignment. Parameters and returns are typed.
2. Types: `int float bool string list<int|float|bool> map<int|string, int|float|bool|string> void`.
3. **Ownership**: each list/map/string has exactly one owner; freed when the owner's function returns or
   the variable is overwritten. Lists/maps **move** on `$b = $a`; strings **copy** (literals never copied).
   Function arguments are **read-only borrows**. Borrowed values (params, foreach vars) cannot be changed,
   moved or returned. No mutation of a container while `foreach` walks it.
4. **No global variables** — this is what lets `serve()` run handlers on many threads with no locks.
5. Conditions must be `bool` (no truthiness). `/` always returns float (like PHP); `intdiv()` for ints.
6. Runtime errors (index out of range, missing map key, div by zero, `intdiv(INT_MIN,-1)`, size too large)
   **panic** = print + `exit(1)`; inside an HTTP handler a panic frees the request's blocks and answers
   500 instead. Bounds checks and overflow checks are never optional (no unsafe, no flag).
7. int `+ - *` wrap around (64-bit, defined behaviour). Overflow panics were built and then taken out on
   the owner's request because they cost up to 23% (see pin-language/docs design/int-overflow.md); bring them back only
   with check elision. Write `wrapping_add/sub/mul()` where wrapping is intended so that code stays right
   when checks return. Every allocation size that comes from a program value goes through
   `checked_size()` in `runtime.c` — that one is memory safety and stays.
8. Syntax should stay as close to PHP as the rules above allow (PHP likeness is the lowest priority). When unsure, do what PHP 8 does.

Owner decisions so far (in Thai conversation): 1A web API focus · 2C ownership like Rust · 5B LLVM backend ·
3A concurrency **revised 2026-10-24** to structured `go` + `await` (`pin-language/docs design/concurrency.md`) ·
4 errors **answered 2026-10-24**: Swift/Zig-style `throws`/`try`/`catch` compiled to return values
(`pin-language/docs design/error-handling.md`) · null, strings, int overflow, ownership v2: see `design/` in the pin-language/docs repo (local clone: ../pin-docs).
Changing any of these = change of project direction → ask first.

## Codegen conventions (pinac.py)

- Several files: `compile_file()` loads the main file and every import (depth first, cycle check); function
  table keys are `name` in the main file and `moduleid::name` elsewhere; `FuncGen.fkey()` resolves a call.
  LLVM names are `@"f.<key>"`. Packages: `package_file()` / `pin_get()` with `pin.lock` (commit + sha256).
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
- **Every built-in is described once, in `spec/pin_builtins.py`** (signature, summary, notes, PHP difference,
  runnable example). After adding or changing one: `python3 tools/gen_docs.py --record` (review
  `spec/outputs.json`), then `python3 tools/gen_docs.py --docs ../pin-docs` to regenerate the reference
  pages, the AI guide's built-in list and the VS Code grammar, and commit both repositories. run_tests.py
  fails if the spec and the compiler disagree or an example's output changes.

## Working on this repository

(Notes about working with the owner are in `CLAUDE.local.md`, which is not committed.)
- Never claim "first / nobody has done this / no competitor" without searching first.
- Separate verified facts from guesses. If you change a previous recommendation, say what was wrong.
- Benchmarks: always verify outputs match across languages, and state the caveats next to the numbers.
