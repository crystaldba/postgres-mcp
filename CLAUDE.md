# CLAUDE.md — postgres-mcp

Claude-specific instructions for contributing to this project. Read AGENTS.md first for shared rules (CI commands, safety guardrails, code conventions).

**Scope:** Claude Code tool preferences, MCP-aware development workflows, and commit conventions. Does NOT duplicate content from AGENTS.md.

## Tool Preferences

Prefer Claude Code built-in tools over shell equivalents:

| Prefer | Over |
|--------|------|
| `Read` | `cat`, `head`, `tail` |
| `Edit` | `sed`, `awk` |
| `Grep` | `grep`, `rg` |
| `Glob` | `find`, `ls` for file search |
| `Write` | `echo >`, heredoc |

Use `Bash` only for commands that require shell execution (git, uv, pytest, etc.).

## MCP-Aware Development

When connected to a postgres-mcp instance (tools visible in MCP tool list):

- **Use MCP tools to validate changes.** After modifying a tool handler, call the tool through MCP to verify it works. For example, after editing `list_schemas`, call `list_schemas` via MCP to confirm the output.
- **Test access mode behavior.** If your change touches SQL execution paths, verify behavior in both UNRESTRICTED and RESTRICTED modes.
- **Use `execute_sql` to inspect schema** when writing tests or debugging query-related code.

When NOT connected to an MCP instance:

- Run unit tests: `uv run pytest tests/unit/ -v`
- For integration tests, start the Docker test database first (see `tests/Dockerfile.postgres-hypopg`).
- Review SQL strings manually — the MCP tools won't be available for live validation.

## Commit Style

This project uses a mixed commit style. Follow these conventions:

- **Prefix with type when applicable:** `feat:`, `fix:`, `refactor:`, `chore:`, `docs:` — lowercase, no scope.
- **Imperative mood for the subject line.** Example: `fix: Support PostgreSQL 12 in get_top_queries`
- **Include PR number** if merging via GitHub: `Add streamable HTTP transport support (#134)`
- **Keep subject line under 72 characters.**
- No body required for small changes. Add a body for non-obvious context.

## Development Workflow

1. Read the relevant source files before making changes. Start with `server.py` for tool definitions.
2. Run the full CI suite before considering work complete (see AGENTS.md for commands).
3. If adding a new MCP tool:
   - Define it in `server.py` with `@mcp.tool` decorator.
   - Use `get_sql_driver()` — this is a hard safety rule (see AGENTS.md).
   - Add unit tests in `tests/unit/` and integration tests in `tests/integration/`.

## Project-Specific Context

- The codebase is async throughout (psycopg3 async, FastMCP).
- `execute_sql` is dynamically registered (not decorated) — see `main()` in server.py.
- Index tuning uses the DTA algorithm by default. LLM optimization (`method="llm"`) is experimental and requires `OPENAI_API_KEY`.
- SQL safety parsing uses `pglast` to reject COMMIT/ROLLBACK in restricted mode.
- Parameterized queries use `{}` placeholders via `SafeSqlDriver.execute_param_query()`, not `%s` or `$1`.
