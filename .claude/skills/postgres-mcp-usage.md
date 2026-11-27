---
name: postgres-mcp-usage
description: Guide for using postgres-mcp MCP tools to query, analyze, and optimize PostgreSQL databases. Use when connected to a postgres-mcp server instance and working with database performance, schema exploration, or health checks. Triggers: database query, slow query, index tuning, database health, schema exploration, explain plan, PostgreSQL optimization.
---

# Using postgres-mcp Tools

This skill teaches the recommended workflow for using the 9 postgres-mcp MCP tools. It covers the DTA (Database Tuning Advisor) method for index optimization. LLM-based optimization (`method="llm"`) is experimental and not covered here.

**Scope:** Using the MCP tools as a client. For contributing code to the server, see AGENTS.md and CLAUDE.md.

**Last verified:** 2026-03-19 against postgres-mcp v0.3.0 (9 tools). If tools have changed, verify against `server.py` `@mcp.tool` declarations.

## Access Modes

The server runs in one of two modes, set at startup via `--access-mode`:

- **UNRESTRICTED** — `execute_sql` runs any SQL (read/write/DDL). All other tools are read-only regardless of mode.
- **RESTRICTED** — `execute_sql` is limited to read-only queries with a 30-second timeout. Use this for production databases.

Always confirm which mode is active before running write operations.

## Recommended Workflow

Follow this sequence: **discover schema → analyze performance → optimize indexes**.

### 1. Schema Discovery

Understand the database structure before running analysis tools.

**`list_schemas`** — List all schemas in the database.
- No parameters. Start here to identify user schemas vs system schemas.

**`list_objects`** — List objects within a schema.
- `schema_name` (required): target schema
- `object_type`: `table` (default), `view`, `sequence`, or `extension`

**`get_object_details`** — Get columns, constraints, and indexes for a specific object.
- `schema_name` (required), `object_name` (required)
- `object_type`: `table` (default), `view`, `sequence`, or `extension`

Typical flow:
```
list_schemas → pick schema → list_objects(schema, "table") → get_object_details(schema, table)
```

### 2. Query Execution and Analysis

**`execute_sql`** — Run SQL queries against the database.
- `sql` (required): SQL statement to execute
- In UNRESTRICTED mode: any SQL (SELECT, INSERT, UPDATE, DELETE, DDL)
- In RESTRICTED mode: read-only queries only, 30-second timeout

**`explain_query`** — Get the execution plan for a SQL query.
- `sql` (required): the query to explain
- `analyze` (default: false): when true, actually executes the query to show real statistics (slower but more accurate)
- `hypothetical_indexes` (default: []): simulate indexes without creating them. Each entry: `{"table": "name", "columns": ["col1", "col2"], "using": "btree"}`
  - **Prerequisite:** HypoPG extension must be installed for hypothetical indexes
  - Cannot combine `analyze: true` with `hypothetical_indexes`

**`get_top_queries`** — Find the slowest or most resource-intensive queries.
- **Prerequisite:** pg_stat_statements extension must be installed and loaded
- `sort_by`: `resources` (default) for resource-intensive queries, `mean_time` for highest average latency, `total_time` for highest cumulative time
- `limit` (default: 10): number of queries to return (applies to `mean_time` and `total_time` only)

Typical flow:
```
get_top_queries(sort_by="resources") → identify slow queries → explain_query(sql) → see where time is spent
```

### 3. Index Optimization

**`analyze_workload_indexes`** — Analyze the full database workload and recommend indexes.
- **Prerequisites:** pg_stat_statements and HypoPG extensions
- `max_index_size_mb` (default: 10000): storage budget for recommended indexes
- `method`: `dta` (default) or `llm` (experimental, requires OPENAI_API_KEY)

**`analyze_query_indexes`** — Recommend indexes for specific queries (up to 10).
- **Prerequisites:** HypoPG extension
- `queries` (required): list of SQL query strings
- `max_index_size_mb` (default: 10000): storage budget
- `method`: `dta` (default) or `llm` (experimental)

Typical flow:
```
get_top_queries → pick problem queries → analyze_query_indexes(queries=[...]) → review recommendations
```

Or for full workload analysis:
```
analyze_workload_indexes → review recommendations → explain_query(sql, hypothetical_indexes=[...]) → verify improvement
```

### 4. Health Checks

**`analyze_db_health`** — Run database health checks.
- `health_type` (default: `all`): one or comma-separated list of:
  - `index` — invalid, duplicate, and bloated indexes
  - `connection` — connection count and utilization
  - `vacuum` — vacuum health, transaction ID wraparound risk
  - `sequence` — sequences at risk of exceeding max value
  - `replication` — replication lag, slot status
  - `buffer` — buffer cache hit rates for tables and indexes
  - `constraint` — invalid constraints
  - `all` — runs all checks

Start with `all` for a broad overview, then drill into specific areas.

## Extension Requirements

| Extension | Required for |
|-----------|-------------|
| pg_stat_statements | `get_top_queries`, `analyze_workload_indexes` |
| HypoPG | `explain_query` with hypothetical indexes, `analyze_workload_indexes`, `analyze_query_indexes` |

Install with: `CREATE EXTENSION IF NOT EXISTS <name>;`
Note: `pg_stat_statements` must also be in `shared_preload_libraries` in postgresql.conf.
