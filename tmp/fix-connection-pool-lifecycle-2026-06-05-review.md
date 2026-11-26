# Code Review Exchange — postgres-mcp connection-pool lifecycle fix

Independent reviewer: auggie / gpt-5.5 (whole-codebase index), 3 passes.

## Consolidated Review (from 3 independent passes)

- Pass 1: `ready` — LGTM, no issues.
- Pass 2: `ready` — LGTM, no issues.
- Pass 3: `needs work` — one finding (below).

### Finding 1
- **File**: src/postgres_mcp/server.py (557-578, run block + _exit_if_orphaned), src/postgres_mcp/sql/sql_driver.py (pool)
- **Lens**: completeness & coverage
- **Severity**: medium
- **Confidence**: Flagged in 1/3 passes (the other two passed it clean)
- **Issue**: The riskiest new behavior — a stdio-only orphan watchdog that closes the global pool then calls os._exit(0) — had no checked-in test. Existing transport tests only assert which run_*_async() is called; nothing proved the watchdog exits after getppid()==1, closes the pool before exiting, is cancelled on clean stdio return, and is never started for sse/streamable-http. Only manual/empirical verification existed.
- **Suggestion**: Add focused unit tests patching os.getppid/os._exit/db_connection.close to exercise _exit_if_orphaned (asserting close-before-exit), plus main() tests asserting the watchdog is created+cancelled for stdio and not created for sse/streamable-http, without invoking real os._exit.

## Response by Author

### Re: Finding 1
- **Status**: fixed
- **Response**: Valid — this is exactly the non-obvious lifecycle logic that warrants real coverage, especially heading into an upstream PR. Added tests/unit/test_lifecycle.py (5 tests): close-before-exit ordering, poll-until-orphaned (no premature close/exit), stdio starts exactly one watchdog and cancels it on clean exit, and sse/streamable-http start none. Also tightened the finally to await the cancelled watchdog (no dangling task) — confirmed it doesn't break the 6 existing transport tests.
- **Changes**: src/postgres_mcp/server.py (await watchdog cancellation in finally); tests/unit/test_lifecycle.py (new). `uv run pytest tests/unit/test_lifecycle.py tests/unit/test_transport.py` → 11 passed.

No findings disputed. The two LGTM passes plus the addressed coverage gap → resolved.

<!-- AGREED -->
