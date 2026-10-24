# AGENTS.md

Instructions for AI coding agents that open this repository.

**Writing Pin code?** Read the AI guide first — the whole language on one page, with idioms, a PHP → Pin
table and an error → fix table. If a feature is not in it, Pin does not have it:
https://raw.githubusercontent.com/pin-language/docs/HEAD/ai-guide.md

Then loop: write → `pin check --json file.pn` (one JSON error per line, `{"file":…,"ok":true}` when clean) →
fix the reported line → repeat → `pin file.pn` to run. Every built-in with examples:
https://github.com/pin-language/docs/blob/HEAD/reference/README.md

Current version: see `VERSION` in `pinac.py` and `CHANGELOG.md`. Pin has modules (`import`/`export`),
Git packages (`pin get`, `pin.lock`), `list<string>`, request helpers (`query_param`, `json_string`, …) and
SQLite (`db_*`). It does not yet have classes, `null`, `try`/`catch`, closures, nested containers, request
headers or PostgreSQL.

**Changing the compiler?** Read `CLAUDE.md` (conventions, invariants, how to test) and `HANDOFF.md` (state and
task queue). `python3 tests/run_tests.py` must stay green.
