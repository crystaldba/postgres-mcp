# Postgres Pro | MCP Server


## Overview

*Postgres Pro* is a Model Context Protocol (MCP) server that helps you query, analyze, optimize, and monitor your PostgreSQL databases.
It allows AI assistants and agents such as Claude or Cursor to analyze query performance, recommend indexes, and perform health checks on your database.

Postgres Pro provides support along the full software development lifecycle—from code generation through testing, deployment, and production maintenance and tuning.

[ [Quick Start](#quick-start) | [Intro Blog Post](https://www.crystaldba.ai/blog) | [Discord Server](https://discord.gg/4BEHC7ZM) ]

*DEMO VIDEO PLACEHOLDER*

**Table of Contents:**
- [Features](#features)
- [Quick Start](#quick-start)
- [Usage Examples](#usage-examples)
- [MCP Server API](#mcp-server-api)
- [Related Projects](#related-projects)
- [Frequently Asked Questions](#frequently-asked-questions)
- [Postgres Pro Development](#postgres-pro-development)

## Features

Postgres Pro offers a set of tools to help you query, analyze, and optimize your Postgres database.
It provides:

- **Schema Information**.
  Help your AI Agent generate SQL reliably and successfully with detailed schema information of your database objects—including tables, views, sequences, stored procedures, triggers.

- **Protected SQL Execution**.
  Work fast or safe, as needed:
  - *Unrestricted Mode:* Provide full read/write access in development environments. Let your AI agent modify data, change the schema, drop tables, whatever you need it to do.
  - *Restricted Mode:* Limit access in production environments with checks to ensure read-only operations and limits on resource consumption. These restrictions may become more elaborate in the future.

- **Index Tuning**.
  Ensure your SQL queries run efficiently and return quickly.
  Find tuning targets, validate AI-generated suggestions, or generate candidates using classical index optimization algorithms.
  Simulate how Postgres will after adding indexes using the explain plans together with [hypothetical indexes](https://hypopg.readthedocs.io/).

- **Database Health**.
  Check buffer cache hit rates, identify unused/duplicate indexes, monitor vacuum health, and more.


## Quick Start

### Prerequisites

Before getting started, ensure you have:
1. Access credentials for your database.
2. Python 3.12 or higher *or* Docker.
3. The `pg_statements` and `hypopg` extensions loaded on your database (for full functionality).

#### Access Credentials
 You can confirm your access credentials are valid by using `psql` or a GUI tool such as [pgAdmin](https://www.pgadmin.org/).


#### Python  or Docker

The choice to use Docker or Python is yours.
We generally recommend using whichever is most familiar to you.

#### Postgres Extensions

Postgres Pro uses the `pg_statements` extension to analyze query execution statistics.
For example, this allows it to understand which queries are running slow or consuming significant resources.
It uses the `hypopg` extension to simulate the behavior of the Postgres query planner after adding indexes.
While Postgres Pro can run without these extensions, its capabilities will be limited.

If your Postgres database is running on a cloud provider managed service, the `pg_statements` and `hypopg` extensions will probably already be available on the system.
In this case, you can just run `CREATE EXTENSION` commands using a role with sufficient privileges.

If you are using a self-managed Postgres database, you may need to do additional work.
Before loading the `pg_statements` extension you must ensure that it is listed in the `shared_preload_libraries` in the Postgres configuration file.
The `hypopg` extension may also require additional system-level installation (e.g., via your package manager) because it does not always ship with Postgres.


### Installation

Choose one of the following methods to install Postgres Pro:

#### Option 1: Using Python

If you have `pipx` installed you can install Postgres Pro with:

```bash
pipx install postgres-mcp
```

Otherwise, install Postgres Pro with `uv`:

```bash
uv pip install postgres-mcp
```

If you need to install `uv`, see the [uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/).

#### Option 2: Using Docker

Pull the Postgres Pro MCP server Docker image.
This image contains all necessary dependencies, providing a reliable way to run Postgres Pro in a variety of environments.

```bash
docker pull crystaldba/postgres-mcp
```

### Configure Your AI Assistant

We provide full instructions for configuring Postgres Pro with Claude Desktop.
Many MCP clients have similar configuration files, you can adapt these steps to work with the client of your choice.

#### Claude Desktop Configuration

You will need to edit the Claude Desktop configuration file to add Postgres Pro.
The location of this file depends on your operating system:
- MacOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%/Claude/claude_desktop_config.json`

You can also use `Settings` menu item in Claude Desktop to locate the configuration file.

You will now edit the `mcpServers` section of the configuration file.

##### If you are using `pipx`

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

##### If you are using `uv`

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "run",
        "postgres-mcp",
        "postgresql://username:password@localhost:5432/dbname",
        "--access-mode=unrestricted"
      ]
    }
  }
}
```

##### If you are using the Docker

```json
{
  "mcpServers": {
    "postgres": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "postgres-mcp",
        "postgresql://username:password@localhost:5432/dbname",
        "--access-mode=unrestricted"
      ]
    }
  }
}
```

The Postgres Pro Docker image will automatically remap the hostname `localhost` to work from inside of the container.

- MacOS/Windows: Uses `host.docker.internal` automatically
- Linux: Uses `172.17.0.1` or the appropriate host address automatically


##### Connection URI

Replace `postgresql://...` with your [Postgres database connection URI](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING-URIS).

You can also use `psql`-style connection parameters:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "postgres-mcp",
      "args": [
        "-h", "localhost",
        "-p", "5432",
        "-U", "username",
        "-d", "dbname",
        "--access-mode=unrestricted"
      ]
    }
  }
}
```

##### Access Mode

Postgres Pro supports multiple *access modes* to give you control over the operations that the AI agent can perform on the database:
- **Unrestricted Mode**: Allows full read/write access to modify data and schema. It is suitable for development environments.
- **Restricted Mode**: Limits operations to read-only transactions with resource constraints. It is suitable for production environments.

To use restricted mode, replace `--access-mode=unrestricted` with `--access-mode=restricted` in the configuration examples above.


#### Other MCP Clients

Many MCP clients have similar configuration files to Claude Desktop, and you can adapt the examples above to work with the client of your choice.

- If you are using Cursor, you can use navigate from the `Command Palette` to `Cursor Settings`, then open the `MCP` tab to access the configuration file.
- If you are using Windsurf, you can navigate to from the `Command Palette` to `Open Windsurf Settings Page` to access the configuration file.
- If you are using Goose run `goose configure`, then select `Add Extension`.

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

The MCP standard defines various types of endpoints: Tools, Resources, Prompts, and others.

Postgres Pro exposes all of its functionality via MCP tools alone.
While, some other servers use [MCP resources](https://modelcontextprotocol.io/docs/concepts/resources) to expose schema information, we chose to use [MCP tools](https://modelcontextprotocol.io/docs/concepts/tools) because they are better supported by the MCP client ecosystem.

Postgres Pro Tools:

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
- [Wren MCP Server](https://github.com/Canner/wren-engine). Provides a semantic engine powering business intelligence for Postgres and other databases.

DBA Tools (including commercial offerings)
- [Aiven Database Optimizer](https://aiven.io/solutions/aiven-ai-database-optimizer). A tool that provides holistic database workload analysis, query optimizations, and other performance improvements.
- [dba.ai](https://www.dba.ai/). An AI-powered database administration assistant that integrates with GitHub to resolve code issues.
- [pgAnalyze](https://pganalyze.com/). A comprehensive monitoring and analytics platform for identifying performance bottlenecks, optimizing queries, and real-time alerting.
- [Postgres.ai](https://postgres.ai/). An interactive chat experience combining an extensive Postgres knowledge base and GPT-4.
- [Xata Agent](https://github.com/xataio/agent). An open-source AI agent that automatically monitors database health, diagnoses issues, and provides recommendations using LLM-powered reasoning and playbooks.

Postgres Utilities
- [Dexter](https://github.com/DexterDB/dexter). A tool for generating and testing hypothetical indexes on PostgreSQL.
- [PgHero](https://github.com/ankane/pghero). A performance dashboard for Postgres, with recommendations.
Postgres Pro incorporates health checks from PgHero.
- [PgTune](https://github.com/le0pard/pgtune?tab=readme-ov-file). Heuristics for tuning Postgres configuration.

## Frequently Asked Questions

*How is Postgres Pro different from other Postgres MCP servers?*
There are many MCP servers allow an AI agent to run queries against a Postgres database.
Postgres Pro does that too, but also adds tools for understanding and improving the performance of your Postgres database.
For example, it implements a version of the [Anytime Algorithm of Database Tuning Advisor for Microsoft SQL Server](https://www.microsoft.com/en-us/research/wp-content/uploads/2020/06/Anytime-Algorithm-of-Database-Tuning-Advisor-for-Microsoft-SQL-Server.pdf), a modern industrial-strength algorithm for automatic index tuning.

*Why are MCP tools needed when the LLM can reason, generate SQL, etc?*
LLMs are invaluable for tasks that involve ambiguity, reasoning, or natural language.
When compared to procedural code, however, they can be slow, expensive, non-deterministic, and sometimes produce unreliable results.
In the case of database tuning, we have well established algorithms, developed over decades, that are proven to work.
Postgres Pro lets you combine the best of both worlds by pairing LLMs with classical optimization algorithms and other procedural tools.

*How do you test Postgres Pro?*
Testing is critical to ensuring that Postgres Pro is reliable and accurate.
We are building out a suite of AI-generated adversarial workloads designed to challenge Postgres Pro and ensure it performs under a broad variety of scenarios.

*What Postgres versions are supported?*
We plan to support Postgres versions 13 through 17.
Our testing presently focuses on Postgres 15, 16, and 17.

*Who created this project?*
This project is created and maintained by [Crystal DBA](https://www.crystaldba.ai).

## Roadmap

There is nothing here yet.

Tell us what you want to see by opening an [issue](https://github.com/crystaldba/postgres-mcp/issues) or a [pull request](https://github.com/crystaldba/postgres-mcp/pulls).

You can also contact us on [Discord](https://discord.gg/4BEHC7ZM).



## Postgres Pro Development

The instructions below are for developers who want to work on Postgres Pro, or users who prefer to install Postgres Pro from source.

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
