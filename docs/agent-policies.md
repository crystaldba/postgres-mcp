# Per-Agent RBAC — Agent Policies

postgres-mcp supports fine-grained **per-agent Role-Based Access Control (RBAC)**.
Each AI agent is identified by a bearer token and gets its own:

- **Access mode** (`unrestricted` / `restricted` / `readonly`)
- **Allowed schemas** (list or `["*"]` for all)
- **Allowed MCP tools** (list or `["*"]` for all)
- **Audit logging** (structured JSON log per query)

This is different from the global `--access-mode` flag: with per-agent policies,
a read-only reporting bot and a privileged migration agent can connect to the
same server instance with different levels of access.

## Quick Start

### 1. Create `agents.yaml`

```yaml
agents:
  - name: reporting-bot
    token: "rpt-abc123"          # plaintext — hashed at load time
    access_mode: readonly
    allowed_schemas: ["analytics", "public"]
    allowed_tools: ["execute_sql", "list_objects", "get_object_details"]
    audit: true

  - name: schema-inspector
    token: "ins-def456"
    access_mode: restricted       # pglast-validated, read-only tx
    allowed_schemas: ["*"]        # all schemas
    allowed_tools: ["list_schemas", "list_objects", "get_object_details"]
    audit: true

  - name: migration-agent
    token: "mig-xyz789"
    access_mode: unrestricted
    allowed_schemas: ["*"]
    allowed_tools: ["*"]
    audit: true

# Optional: access mode for requests without a token
default_access_mode: readonly
```

### 2. Start the server

```bash
postgres-mcp "postgresql://user:pass@host/db" --agent-policies ./agents.yaml
```

Or via environment variable (useful for Docker / Kubernetes):

```bash
export AGENT_POLICIES_JSON='[
  {"name":"bot","token":"tok1","access_mode":"readonly","audit":true}
]'
postgres-mcp "postgresql://user:pass@host/db"
```

### 3. Agents send their token

Agents identify themselves via the `X-Agent-Token` HTTP header:

```bash
curl -H "X-Agent-Token: rpt-abc123" http://localhost:8000/mcp
```

For stdio transport, include the token in the MCP `_meta` field:

```json
{
  "method": "tools/call",
  "_meta": {"x-agent-token": "rpt-abc123"},
  "params": {"name": "execute_sql", "arguments": {"sql": "SELECT 1"}}
}
```

## Access Modes

| Mode | SQL Validation | DB Enforcement | Best For |
|---|---|---|---|
| `unrestricted` | None | None | Trusted migration agents |
| `restricted` | pglast AST | `READ ONLY` tx | Production read agents |
| `readonly` | None | `READ ONLY` tx | Analytics agents with complex queries |

## Schema Guards

When `allowed_schemas` is set to a specific list, the RBAC driver rejects
any query that references an unlisted schema:

```
Agent 'reporting-bot' is not permitted to access schema 'secrets'.
Allowed schemas: ['analytics', 'public']
```

Schema detection covers `schema.table` qualified references and
`SET search_path TO schema` statements.

## Tool Guards

When `allowed_tools` is a specific list, calling a non-allowed MCP tool
returns an error before the query reaches the database:

```
Agent 'reporting-bot' is not permitted to use tool 'run_health_check'.
Allowed tools: ['execute_sql', 'get_object_details', 'list_objects']
```

## Audit Logs

When `audit: true`, every query emits a structured log line:

```json
{"event": "agent_query", "agent": "reporting-bot", "access_mode": "readonly",
 "sql_preview": "SELECT * FROM analytics.sales WHERE ...",
 "has_params": false, "duration_ms": 12.4, "error": null}
```

Route these to your log aggregator (Loki, CloudWatch, Datadog) for a full
audit trail of every AI agent action.

## Security Notes

- Tokens are **SHA-256 hashed** at load time — plaintext secrets never live
  in process memory after startup.
- Token comparison uses **`hmac.compare_digest`** to prevent timing attacks.
- Schema guards are **best-effort** (token-based) for now. For strict isolation
  use dedicated PostgreSQL roles per agent in addition to RBAC policies.
- Per-agent policies **layer on top of** the server's global `--access-mode`.
  If the server is started with `--access-mode restricted`, an agent with
  `access_mode: unrestricted` still gets `restricted` (the stricter wins).
