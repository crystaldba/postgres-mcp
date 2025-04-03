# postgres-mcp: PostgreSQL Tuning and Analysis Tool

postgres-mcp is a Model Context Protocol (MCP) server that helps you analyze, optimize, and monitor your PostgreSQL databases. It allows AI assistants like Claude to analyze query performance, recommend indexes, and perform health checks on your database.

## Features

postgres-mcp provides several powerful tools for database optimization:

- **Database Health Analysis**: Check buffer cache hit rates, identify unused/duplicate indexes, monitor vacuum health, and more
- **Query Performance Analysis**: Find your slowest SQL queries and get optimization suggestions
- **Index Recommendations**: Analyze frequently executed queries and get optimal index suggestions
- **Schema Exploration**: Explore database tables, columns, and relationships

## Quick Start

#### Using Docker

```bash
docker pull crystaldba/postgres-mcp
docker run -it --rm crystaldba/postgres-mcp "postgres://user:password@host:5432/dbname"
```

When using Docker with a localhost database, our image automatically handles the connection:

- MacOS/Windows: Uses `host.docker.internal` automatically
- Linux: Uses `172.17.0.1` or the appropriate host address automatically

Example with automatic localhost remapping:

```bash
docker run -it --rm crystaldba/postgres-mcp "postgres://user:password@localhost:5432/dbname"
```

### Setting Up with AI Assistants

#### Claude Desktop

Configure Claude Desktop to use postgres-mcp:

1. Edit your Claude Desktop configuration file:

   - MacOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%/Claude/claude_desktop_config.json`

2. Add the following to the "mcpServers" section:

If you want "restricted mode" -- all queries are wrapped in a read-only transaction, with a query timeout of 30 seconds, and additional scrubbing:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "crystaldba/postgres-mcp",
        "postgresql://username:password@localhost:5432/dbname",
        "--access-mode=restricted"
    }
  }
}
```

If you want "unrestricted mode" -- you can do anything:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "crystaldba/postgres-mcp",
        "postgresql://username:password@localhost:5432/dbname"
    }
  }
}
```

#### Cursor

Configure Cursor to use postgres-mcp:

1. Create or edit `~/.cursor/mcp.json` (or run Ctrl-Shift-P and type "Developer: Open Logs Folder")
2. Add the same `mcpServers` configuration as above.

```json
{
  "mcpServers": {
    "postgres-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/home/greg/code/postgres-mcp",
        "run",
        "postgres-mcp",
        "postgresql://postgres:mysecretpassword@localhost:5432/postgres"
      ]
    }
  }
}
```

Or, if you have installed the project locally with `uv`:

```json
{
  "mcpServers": {
    "postgres-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/PATH/TO/PROJECT/postgres-mcp",
        "run",
        "postgres-mcp",
        "postgresql://postgres:mysecretpassword@localhost:5432/postgres"
      ]
    }
  }
}
```

## Examples

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
