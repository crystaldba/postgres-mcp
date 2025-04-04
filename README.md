# Postgres Expert (MCP Server)

## Overview

*Postgres Expert* is a Model Context Protocol (MCP) server that helps you query, analyze, optimize, and monitor your PostgreSQL databases.
It allows AI assistants and agents such as Claude or Cursor to analyze query performance, recommend indexes, and perform health checks on your database.

[ [Quick Start](#quick-start) | [Intro Blog Post](https://www.crystaldba.ai/blog) | [Discord Server](https://discord.gg/4BEHC7ZM) ]

*DEMO VIDEO GOES HERE*

## Features

Postgres Expert provides a robust set of tools to help you query, analyze, and optimize your Postgres database.
It provides:

- **Schema Information**.
  Help your AI Agent generate SQL reliably and successfully with detailed schema information of your database objects—including tables, views, sequences, stored procedures, triggers.

- **Protected SQL Execution**.
  Work fast or safe, as needed:
  - *Unrestricted Mode:* Provide full read/write access in development environments. Let your AI agent modify data, change the schema, drop tables, whatever you need it to do.
  - *Restricted Mode:* Limit access in production environments with checks to ensure read-only operations and limits on resource consumption. These restrictions may become more elaborate in the future.

- **Index Tuning**.
  Ensure your SQL queries run efficiently and return quickly.
  Find tuning targets.
  Validate AI-generated suggestions or generate candidates using classical index optimization algorithms.
  Compare the query execution plans using [hypothetical indexes](https://hypopg.readthedocs.io/), to test how the Postgres query planner will perform with added indexes.

- **Database Health Checks**
  Check buffer cache hit rates, identify unused/duplicate indexes, monitor vacuum health, and more.


## Quick Start

### Prerequisites

Before getting started, ensure you have:
1. Access credentials for your database (as confirmed by ability to connect via `psql` or a GUI tool such as pgAdmin).
2. Python 3.12 or higher *or* Docker.
3. The `pg_statements` and `hypopg` extensions installed and enabled on your database (for full functionality).

### Installation

Choose one of the following methods to install Postgres Expert:

#### Option 1: Using Python

If you have `pipx` installed you can install Postgres Expert with:

```bash
pipx install postgres-mcp
```

Otherwise, install Postgres Expert with `uv`:

```bash
uv pip install postgres-mcp
```

#### Option 2: Using Docker

Pull the Postgres Expert MCP server Docker image.
This image contains all necessary dependencies, providing a reliable installation method for a range of systems:

```bash
docker pull crystaldba/postgres-mcp
```

### Configure Your AI Assistant


#### Claude Desktop Configuration

Edit your configuration file:
- MacOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%/Claude/claude_desktop_config.json`

You can also go to the `Settings` tab in Claude Desktop to locate the configuration file.

Add the following configuration to the `mcpServers` section:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "postgres-mcp",
      "args": [
        "postgresql://username:password@localhost:5432/dbname",
        "--access-mode=unrestricted"
      ]
    }
  }
}
```

Where you replace `postgresql://...` with your [Postgres database connection URI](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING-URIS).

You can also specify the access mode:
- **Unrestricted Mode**: Allows full read/write access to modify data and schema. It is suitable for development environments.
- **Restricted Mode**: Limits operations to read-only transactions with resource constraints. It is suitable for production environments.

## Usage Examples

### Get Database Health Overview

Ask Claude: "Check the health of my database and identify any issues."

### Analyze Slow Queries

Ask Claude: "What are the slowest queries in my database? And how can I speed them up?"

### Get Recommendations On How To Speed Things Up

Ask Claude: "My app is slow. How can I make it faster?"

### Generate Index Recommendations

Ask Claude: "Analyze my database workload and suggest indexes to improve performance."

### Optimize a Specific Query

Ask Claude: "Help me optimize this query: SELECT \* FROM orders JOIN customers ON orders.customer_id = customers.id WHERE orders.created_at > '2023-01-01';"

## MCP Server API

Postgres Expert exposes all of its functionality via MCP tools alone.
Some other servers use [MCP resources](https://modelcontextprotocol.io/docs/concepts/resources) to expose schema information, but we chose to use [MCP tools](https://modelcontextprotocol.io/docs/concepts/tools) because tools are better supported by the MCP client ecosystem.

Postgres Expert Tools:

| Tool Name | Description |
|-----------|-------------|
| `list_schemas` | Lists all database schemas available in the PostgreSQL instance |
| `list_objects` | Lists database objects (tables, views, sequences, extensions) within a specified schema |
| `get_object_details` | Provides information about a specific database object, for example, a table's columns, constraints, and indexes |
| `execute_sql` | Executes SQL statements on the database, with read-only limitations when connected in restricted mode. |
| `explain_query` | Gets the execution plan for a SQL query describing how PostgreSQL will process it and exposing the query planner's cost model. Can be invoked with hypothetical indexes to simulate the behavior after adding indexes. |
| `get_top_queries` | Reports the slowest SQL queries based on total execution time using `pg_stat_statements` data |
| `analyze_workload_indexes` | Analyzes the database workload to identify resource-intensive queries, then recommends optimal indexes for them |
| `analyze_query_indexes` | Analyzes a list of specific SQL queries (up to 10) and recommends optimal indexes for them |
| `analyze_db_health` | Performs comprehensive health checks including:<br>- Buffer cache hit rates<br>- Connection health<br>- Constraint validation<br>- Index health (duplicate/unused/invalid)<br>- Replication status<br>- Sequence limits<br>- Vacuum health |


## Related Projects

Postgres MCP Servers
- [Query MCP](https://github.com/alexander-zuev/supabase-mcp-server). An MCP server for Supabase Postgres with a three-tier safety architecture and Supabase management API support.
- [PG-MCP](https://github.com/stuzero/pg-mcp). An MCP server for PostgreSQL with flexible connection options, explain plans, extension context, and more.
- [Reference PostgreSQL MCP Server](https://github.com/modelcontextprotocol/servers/tree/main/src/postgres). A simple MCP Server implementation exposing schema information as MCP resources and executing read-only queries.
- [Supabase Postgres MCP Server](https://github.com/supabase-community/supabase-mcp). A MCP Server implementation with Supabase management features.
- [Nile MCP Server](https://github.com/niledatabase/nile-mcp-server). An MCP server providing access to the management API for the Nile's multi-tenant Postgres service.
- [Neon MCP Server](https://github.com/neondatabase-labs/mcp-server-neon). An MCP server providing access to the management API for Neon's serverless Postgres service.
- [Wren MCP Server](https://github.com/Canner/wren-engine). Provides a semantic engine powering business intelligence across Postgres and other databases.

DBA Tools (including commercial offerings)
- [Aiven Database Optimizer](https://aiven.io/solutions/aiven-ai-database-optimizer). A tool that provides holistic database workload analysis, query optimizations, and other performance improvements.
- [dba.ai](https://www.dba.ai/). An AI-powered database administration assistant that integrates with GitHub to resolve code issues.
- [pgAnalyze](https://pganalyze.com/). A comprehensive monitoring and analytics platform for identifying performance bottlenecks, optimizing queries, and real-time alerting.
- [Postgres.ai](https://postgres.ai/). An interactive chat experience combining an extensive Postgres knowledge base and GPT-4.
- [Xata Agent](https://github.com/xataio/agent). An open-source AI agent that automatically monitors database health, diagnoses issues, and provides recommendations using LLM-powered reasoning and playbooks.

Postgres Utilities
- [Dexter](https://github.com/DexterDB/dexter). A tool for generating and testing hypothetical indexes on PostgreSQL.
- [PgHero](https://github.com/ankane/pghero). A performance dashboard for Postgres, with recommendations.
Postgres Expert incorporates health checks from PgHero.
- [PgTune](https://github.com/le0pard/pgtune?tab=readme-ov-file). Heuristics for tuning Postgres configuration.

## Frequently Asked Questions

*How is Postgres Expert different from other Postgres MCP servers?*
There are many MCP servers allow an AI agent to run queries against a Postgres database.
Postgres Expert does that too, but also adds tools for understanding and improving the performance of your Postgres database.
For example, it implements version of the [Anytime Algorithm of Database Tuning Advisor for Microsoft SQL Server](https://www.microsoft.com/en-us/research/wp-content/uploads/2020/06/Anytime-Algorithm-of-Database-Tuning-Advisor-for-Microsoft-SQL-Server.pdf), a modern industrial-strength algorithm for automatic index tuning.

*Why are MCP tools needed when the LLM can reason, generate SQL, etc?*
LLMs are invaluable for tasks that involve ambiguity, reasoning, or natural language.
When compared to procedural code, however, they can be slow, expensive, non-deterministic, and sometimes produce unreliable results.
In the case of database tuning, we have well established algorithms, developed over decades, that are proven to work.
Postgres Expert lets you combine the best of both worlds by pairing LLMs with classical optimization algorithms and other procedural tools.

*How do you test Postgres Expert?*
Testing is critical to ensuring that Postgres Expert is reliable and accurate.
We are building out a suite of AI-generated adversarial workloads designed to challenge Postgres Expert and ensure it performs under a broad variety of scenarios.

*What Postgres versions are supported?*
We plan to support Postgres versions 13 through 17.
Our testing presently focuses on Postgres 15, 16, and 17.

*Who created this project?*
This project is created and maintained by [Crystal DBA](https://www.crystaldba.ai).

## Roadmap

There is nothing here yet.

Tell us what you want to see by opening an [issue](https://github.com/crystaldba/postgres-mcp/issues) or a [pull request](https://github.com/crystaldba/postgres-mcp/pulls).

You can also contact us on [Discord](https://discord.gg/4BEHC7ZM).



## Docker Usage Guide

The postgres-mcp Docker container is designed to work seamlessly across different platforms.

### Network Considerations

When connecting to services on your host machine from Docker, our entrypoint script automatically handles most network remapping:

```bash
# Works on all platforms - localhost is automatically remapped
docker run -it --rm crystaldba/postgres-mcp postgresql://username:password@localhost:5432/dbname
```

### Additional Options

### Connection Options

Connect using individual parameters instead of a URI:

```bash
docker run -it --rm crystaldba/postgres-mcp -h hostname -p 5432 -U username -d dbname
```

## Development

### Local Development Setup

1. **Install uv**:

   ```bash
   curl -sSL https://astral.sh/uv/install.sh | sh
   ```

2. **Clone the repository**:

   ```bash
   git clone https://github.com/crystaldba/postgres-mcp.git
   cd postgres-mcp
   ```

3. **Install dependencies**:

   ```bash
   uv pip install -e .
   uv sync
   ```

4. **Run the server**:
   ```bash
   uv run postgres-mcp "postgres://user:password@localhost:5432/dbname"
   ```
