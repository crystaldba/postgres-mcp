# Add DML_ONLY Access Mode

## Overview
Adds a new `DML_ONLY` access mode to postgres-mcp that allows data manipulation (INSERT, UPDATE, DELETE, SELECT) while blocking schema changes and other DDL operations. This provides a middle ground between `UNRESTRICTED` (allows everything) and `RESTRICTED` (read-only) modes.

## Motivation
Users often need to perform data modifications without having the ability to alter database schema. The existing access modes didn't support this use case:
- `UNRESTRICTED`: Too permissive, allows DDL operations
- `RESTRICTED`: Too restrictive, blocks all writes including DML

`DML_ONLY` mode enables safe data manipulation for use cases like:
- Application agents that need to insert/update data but shouldn't modify schema
- Data import/migration scripts that should be isolated from structural changes
- Development environments where schema changes require explicit approval

## Implementation Details

**New Components:**
- `DmlOnlySqlDriver` class in `src/postgres_mcp/sql/dml_only_sql.py`
  - Wraps underlying SqlDriver with validation layer
  - Uses pglast to parse and validate SQL AST before execution
  - Reuses `SafeSqlDriver.ALLOWED_FUNCTIONS` and extends `ALLOWED_NODE_TYPES`

**Allowed Operations:**
- ✅ `SELECT` - Read queries
- ✅ `INSERT` - Including UPSERT with `ON CONFLICT`
- ✅ `UPDATE` - With all standard clauses (WHERE, RETURNING, etc.)
- ✅ `DELETE` - With all standard clauses
- ✅ `EXPLAIN` - Query planning (but not `EXPLAIN ANALYZE`)
- ✅ `SHOW` - View configuration variables
- ✅ Complex queries (CTEs, subqueries, JOINs, CASE expressions)

**Blocked Operations:**
- ❌ `CREATE/ALTER/DROP TABLE`
- ❌ `CREATE/DROP INDEX`
- ❌ `CREATE/DROP VIEW/FUNCTION/SCHEMA/DATABASE`
- ❌ `TRUNCATE`
- ❌ `VACUUM`
- ❌ `CREATE/DROP EXTENSION`
- ❌ `SET` (configuration changes)
- ❌ `BEGIN/COMMIT/ROLLBACK` (transaction control)
- ❌ `EXPLAIN ANALYZE` (can impact performance)

**Usage:**
```bash
# Start server with DML_ONLY mode
mcp-server-postgres postgres://user:pass@localhost/dbname --access-mode dml_only

# With timeout (recommended)
mcp-server-postgres postgres://user:pass@localhost/dbname \
  --access-mode dml_only \
  --query-timeout 30
```

## Testing
- **46 unit tests** for DML_ONLY driver covering:
  - 13 tests for allowed DML operations
  - 18 tests for blocked DDL operations
  - 6 tests for complex queries (CTEs, subqueries, etc.)
  - 9 tests for error handling and edge cases
- **7 integration tests** for access mode selection
- All existing tests continue to pass (no regression)

## Design Decisions

1. **RawStmt Validation**: Validates inner statement (`stmt.stmt`) to properly check the actual SQL command, not just the wrapper node

2. **Function Allow-list**: Reuses `SafeSqlDriver.ALLOWED_FUNCTIONS` to maintain consistency with read-only mode's security model

3. **Error Messages**: Provides specific, actionable error messages (e.g., "Statement type CreateStmt not allowed in DML_ONLY mode") rather than generic failures

4. **Timeout Handling**: Returns `TimeoutError` (not `ValueError`) for query timeouts, enabling callers to distinguish timeout from validation failures

5. **DELETE/UPDATE without WHERE**: Required for safety. UPDATE and DELETE statements must include a WHERE clause to prevent accidental modification or deletion of all rows. This is a critical safety feature that helps prevent data loss from mistaken queries

## Files Changed
- `src/postgres_mcp/server.py` - Add DML_ONLY to AccessMode enum, update driver selection
- `src/postgres_mcp/sql/dml_only_sql.py` - New DmlOnlySqlDriver implementation
- `src/postgres_mcp/sql/__init__.py` - Export DmlOnlySqlDriver
- `tests/unit/sql/test_dml_only_sql.py` - New comprehensive test suite
- `tests/unit/test_access_mode.py` - Add DML_ONLY test cases
- `README.md` - Document new access mode and usage

## Related Documentation
Implementation follows the plan outlined in `DML_ONLY_MODE_IMPLEMENTATION.md`

## Breaking Changes
None. This is a purely additive feature that doesn't modify existing behavior.
