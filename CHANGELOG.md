# Changelog

## 0.2.0 — 2026-10-24

Writing a real JSON API no longer needs hand-written parsing loops.

- `list<string>`: lists own their strings (freed, copied and JSON-encoded correctly). Changing a string
  element or sorting while a `foreach` walks the list is a compile error, since the loop variable borrows it.
- `explode($sep, $s)`, `implode($glue, $list)`, `sort($xs)`, `rsort($xs)` (int, float, bool, string).
- Request helpers: `url_path($path)`, `query_param($path, $key)`, `form_param($body, $key)` (URL-decoded),
  and `json_has` / `json_string` / `json_int` / `json_float` / `json_bool` for top-level keys of a JSON
  body. Malformed or hostile input gives the default value; nesting deeper than 64 levels is rejected.
  `tests/json_hostile.pn` runs these parsers on broken input under ASAN in CI.
- `examples/api.pn` handles GET with a query string and POST with a JSON body; `examples/wordcount.pn`
  uses `explode`, `list<string>` and `sort`.
- Documentation moved to github.com/pin-language/docs; benchmarks to github.com/pin-language/benchmarks.
- Numbers from text never fail: `intval()` and `json_int()` saturate at `PHP_INT_MAX` / `PHP_INT_MIN` like
  PHP (0.1.0 panicked, which turned a bad client number into a 500); `floatval()` and `(float)"2.5"` read
  the leading number; `query_has()` / `form_has()` tell a missing key from an empty one.
- `pin FILE.pn` runs the program; `pin -v`; one-command installer (`install.sh`).

## 0.1.0 — 2026-10-24 (first preview for testers)

Pin is aimed at AI coding agents: `ai-guide.md` (pin-language/docs) is the whole language on one page, and
`pin check --json` gives machine-readable errors.

### Language
- No `<?php` tag: files are code from the first line. A leading `<?php` (from pasted PHP) is ignored.
- Variables are checked on every path: using a variable that is not assigned on all paths is a compile
  error (this used to compile and could crash on a null list).
- Moves are tracked per branch: moving a value in one `if` branch no longer blocks the other branch, and
  moving right before `break` or `return` is allowed.
- `wrapping_add`, `wrapping_sub`, `wrapping_mul`; constants `PHP_INT_MAX`, `PHP_INT_MIN`, `PHP_EOL`.
- A non-void function that can reach its end without `return` is a compile error (was a run-time panic).
- PHP features that Pin lacks (`class`, `new`, `try`, `switch`, `match`, `null`, closures, `array()`,
  `include`, `global`, ...) get a message saying so and what to write instead.
- `json_encode` of a whole float like 380.0 gives `380.0` (it gave `3.8e+02`).
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
- One-command install: `curl -fsSL https://raw.githubusercontent.com/pin-language/pin/HEAD/install.sh | sh`
  (checks Python and clang, installs into `~/.pin`, links `pin` into `~/.local/bin`, no sudo).
- VS Code extension in `editors/vscode`: syntax highlighting, comment toggling, brackets, auto-indent.
- `pin` command: `pin FILE.pn` (runs it), `pin run`, `pin build`, `pin check`, `pin version` / `-v`; `--json` for machine-readable
  errors. A missing file, a non-UTF-8 file or a missing clang gives a one-line message.
- Builds no longer leave `.ll` files next to the source.
- `examples/` with six runnable programs; English README, language guide, AI guide, `llms.txt`.
- Test suite: 57 checks, run on macOS and Linux, plus sanitizer runs (ASAN/UBSAN, LeakSanitizer on
  Linux) and a GitHub Actions workflow.
