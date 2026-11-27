# AGENTS.md — postgres-mcp

Universal instructions for AI agents contributing to this project.

**Scope:** Development conventions, CI commands, and safety rules for the postgres-mcp codebase. Does NOT cover how to use the MCP tools as an end user (see `.claude/skills/postgres-mcp-usage.md` for that).

## Safety Guardrails

**Hard rule: every new tool MUST call `get_sql_driver()` to obtain its SQL driver.** This is the only mechanism that enforces the server's access mode (UNRESTRICTED vs RESTRICTED). Bypassing it — by instantiating `SqlDriver` directly — silently disables safety protections.

- **UNRESTRICTED mode** — full read/write SQL access. Intended for development.
- **RESTRICTED mode** — read-only transactions via `SafeSqlDriver`, 30-second query timeout. Intended for production.
- `get_sql_driver()` returns a `SqlDriver` or `SafeSqlDriver` depending on `current_access_mode`.
- `execute_sql` is dynamically registered with different MCP annotations per mode. Follow this pattern for any new tool whose behavior changes with access mode.

## CI Commands

Run these locally before pushing. They mirror `.github/workflows/build.yml`:

```bash
uv sync                         # install dependencies
uv run ruff format --check .    # format check
uv run ruff check .             # lint
uv run pyright                  # type check
uv run pytest -v --log-cli-level=INFO  # tests (unit + integration)
```

All five must pass for CI to be green.

## Project Structure

```
src/postgres_mcp/
  server.py          # MCP server entry point, tool definitions (~700 lines)
  sql/               # SQL driver, SafeSqlDriver, parameterized queries, extensions
  index/             # Index tuning (DTA algorithm, LLM optimizer)
  explain/           # EXPLAIN plan analysis
  database_health/   # Health checks (index, connection, vacuum, sequence, replication, buffer, constraint)
  top_queries/       # pg_stat_statements query analysis
  artifacts.py       # Response types (ExplainPlanArtifact, ErrorResult)

tests/
  unit/              # Fast, no database required
  integration/       # Requires a running PostgreSQL instance (Docker)
  conftest.py        # Shared fixtures
```

Key entry point: `server.py` — contains all `@mcp.tool` definitions and the `main()` function.

## Code Conventions

See `pyproject.toml` for full ruff/pyright configuration. Key points:

- **Async throughout** — all tool handlers and SQL operations are `async`. Use `await` for database calls.
- **Line length:** 150 characters.
- **Quotes:** double quotes (ruff format).
- **Imports:** force-single-line (`from x import y`, one per line). Enforced by ruff isort.
- **Type checking:** pyright in standard mode, Python 3.12. Ruff lint targets Python 3.9 for compatibility.
- **Lint rules:** E, F, I, B, W, N, UP, RUF. See `pyproject.toml [tool.ruff]` for active ignores.

### Patterns to Follow

- Use `Field(description=..., default=...)` from pydantic for tool parameter definitions.
- Return `ResponseType` (list of `TextContent | ImageContent | EmbeddedResource`) from tool handlers.
- Use `format_text_response()` and `format_error_response()` helpers in server.py.
- Parameterized queries: use `SafeSqlDriver.execute_param_query(driver, sql, params)` with `{}` placeholders (not `%s` or `$1`).

## Testing

- **Unit tests** (`tests/unit/`): test logic without a database. Mock SQL drivers as needed.
- **Integration tests** (`tests/integration/`): run against a real PostgreSQL instance via Docker. See `tests/Dockerfile.postgres-hypopg` for the test image with pg_stat_statements and HypoPG pre-installed.
- Test runner: `uv run pytest -v --log-cli-level=INFO`
- Async tests use `pytest-asyncio`.

## Dependencies

- **Package manager:** `uv` (not pip/pipx for development).
- **Build system:** hatchling.
- **Key libraries:** FastMCP (`mcp` package), psycopg3 (async PostgreSQL), pglast (SQL parsing for safety), pydantic (validation), instructor (LLM integration).
- Add dev dependencies to `[dependency-groups] dev` in `pyproject.toml`.
