# Changelog

## 0.1.0 — 2026-10-24 (first preview for testers)

### Language
- Variables are checked on every path: using a variable that is not assigned on all paths is a compile
  error (this used to compile and could crash on a null list).
- Moves are tracked per branch: moving a value in one `if` branch no longer blocks the other branch, and
  moving right before `break` or `return` is allowed.
- `wrapping_add`, `wrapping_sub`, `wrapping_mul`.
- `intdiv(PHP_INT_MIN, -1)` panics (it was undefined behaviour and crashed x86 machines); `x % -1` is 0.
- `(int)` of NAN, INF or a float outside the int range panics; `intval()` of a number that does not fit
  panics instead of giving a wrong value.
- `+ - *` on ints wrap around. (Overflow panics were tried and removed for now: up to 23% slower.)

### Runtime and server
- Builds and serves on macOS (kqueue) as well as Linux (epoll).
- A panic inside an HTTP handler answers `500 {"error":"Internal Server Error"}`, frees everything that
  request allocated, and the server keeps running (it used to exit).
- Every allocation size coming from a program value is checked; `fill()` with a huge count could
  previously produce a buffer smaller than the list claimed (heap overflow).

### Tools
- `pin` command: `pin run`, `pin build`, `pin check`, `pin version`. A missing file or a missing clang
  gives a one-line message.
- Builds no longer leave `.ll` files next to the source.
- `examples/` with six runnable programs; English README and language guide.
- Test suite: 48 checks, run on macOS and Linux, plus sanitizer runs (ASAN/UBSAN, LeakSanitizer on Linux).
