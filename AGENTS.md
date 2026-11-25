# AGENTS.md

Instructions for AI coding agents that open this repository.

**Writing Pin code?** Read the AI guide first. It covers the whole language on one page, with idioms, a
PHP-to-Pin table and an error-to-fix table. If a feature is not in the guide, Pin does not have it:
https://raw.githubusercontent.com/pin-language/docs/HEAD/ai-guide.md

Then loop: write → `pin check --json file.pn` (one JSON error per line, `{"file":…,"ok":true}` when clean) →
fix the reported line → repeat → `pin file.pn` to run. Every built-in, with examples:
https://github.com/pin-language/docs/blob/HEAD/reference/README.md

Current version: `VERSION` in `pinac.py`, and `CHANGELOG.md`. Pin has classes (no inheritance), closures,
`?T` null safety, `throws` / `try` / `catch`, `go` / `await`, a Hub, server-sent events, an http:// client,
`run()`, modules (`import` / `export`), Git packages (`pin get`, `pin.lock`), `json_decode` into classes,
and SQLite and PostgreSQL (`db_*`). It does not have inheritance or interfaces, `finally`, or a `bytes` type
yet. TLS is left to a proxy by design (pin-language/docs design/tls.md).

**Changing the compiler?** Read `CLAUDE.md` for the conventions, invariants and how to test, and
`HANDOFF.md` for the current state and task queue. `python3 tests/run_tests.py` must stay green.
