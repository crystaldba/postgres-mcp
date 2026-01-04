# DML_ONLY Mode Implementation Plan

## Goal

Add a new access mode to postgres-mcp that allows DML operations (INSERT, UPDATE, DELETE, UPSERT) while blocking DDL operations (CREATE TABLE, ALTER TABLE, DROP TABLE, CREATE INDEX, etc.).

## Current State

The postgres-mcp server currently has two access modes:

- **Unrestricted Mode** (`--access-mode=unrestricted`): Allows all SQL operations (DDL + DML)
- **Restricted Mode** (`--access-mode=restricted`): Only allows SELECT and read-only operations (blocks all writes)

## Required Functionality

We need a third mode that sits between these two extremes:

- **DML_ONLY Mode** (`--access-mode=dml_only`): Allows data manipulation (INSERT, UPDATE, DELETE) but blocks schema changes

### Allowed Operations in DML_ONLY Mode
- SELECT (all read operations)
- INSERT
- UPDATE  
- DELETE
- UPSERT (INSERT ... ON CONFLICT ... DO UPDATE)
- EXPLAIN (for query analysis)
- SHOW (for system information)
- Transaction control within read-write transactions

### Blocked Operations in DML_ONLY Mode
- CREATE TABLE / CREATE INDEX / CREATE EXTENSION
- ALTER TABLE / ALTER INDEX / ALTER EXTENSION
- DROP TABLE / DROP INDEX / DROP EXTENSION
- TRUNCATE
- VACUUM (can be dangerous in production)
- CREATE/ALTER/DROP SCHEMA
- CREATE/ALTER/DROP DATABASE
- Any other DDL operations

## Implementation Steps

### 1. Add DML_ONLY Access Mode

**File**: `src/postgres_mcp/server.py`

Modify the `AccessMode` enum to add the new mode:

```python
class AccessMode(str, Enum):
    """SQL access modes for the server."""
    UNRESTRICTED = "unrestricted"
    RESTRICTED = "restricted" 
    DML_ONLY = "dml_only"  # New: allow DML, block DDL
```

### 2. Create DmlOnlySqlDriver Class

**File**: `src/postgres_mcp/sql/dml_only_sql.py` (new file)

Create a new driver class similar to `SafeSqlDriver` but with different allowed statement types:

```python
from typing import ClassVar
from pglast import parse_sql
from pglast.ast import (
    SelectStmt,
    InsertStmt,
    UpdateStmt,
    DeleteStmt,
    ExplainStmt,
    VariableShowStmt,
    # ... other allowed types
)

class DmlOnlySqlDriver(SqlDriver):
    """
    A wrapper around SqlDriver that allows DML operations but blocks DDL.
    
    Allows: SELECT, INSERT, UPDATE, DELETE, and other read operations
    Blocks: CREATE, ALTER, DROP, TRUNCATE, and other DDL operations
    """
    
    ALLOWED_STMT_TYPES: ClassVar[set[type]] = {
        SelectStmt,        # SELECT queries
        InsertStmt,        # INSERT
        UpdateStmt,        # UPDATE
        DeleteStmt,        # DELETE
        ExplainStmt,       # EXPLAIN
        VariableShowStmt,  # SHOW statements
        # Add other safe statement types...
    }
    
    # Reuse allowed functions from SafeSqlDriver
    ALLOWED_FUNCTIONS: ClassVar[set[str]] = SafeSqlDriver.ALLOWED_FUNCTIONS
    
    # Reuse allowed node types from SafeSqlDriver
    ALLOWED_NODE_TYPES: ClassVar[set[type]] = SafeSqlDriver.ALLOWED_NODE_TYPES
    
    def _validate(self, query: str) -> None:
        """Validate query allows DML but blocks DDL"""
        # Parse and validate using pglast (similar to SafeSqlDriver)
        parsed = parse_sql(query)
        for stmt in parsed:
            # Check statement type is allowed
            # Recursively validate all nodes
            # Reject DDL operations
```

**Key Implementation Notes**:
- Inherit from `SqlDriver` 
- Reuse the validation logic from `SafeSqlDriver` but with different `ALLOWED_STMT_TYPES`
- Use pglast library for SQL parsing and validation
- Include timeout support (like SafeSqlDriver uses 30 seconds)
- Ensure `force_readonly=False` since we're allowing writes

### 3. Update get_sql_driver() Function

**File**: `src/postgres_mcp/server.py`

Modify the `get_sql_driver()` function to return the appropriate driver:

```python
async def get_sql_driver() -> Union[SqlDriver, SafeSqlDriver, DmlOnlySqlDriver]:
    """Get the appropriate SQL driver based on the current access mode."""
    base_driver = SqlDriver(conn=db_connection)

    if current_access_mode == AccessMode.RESTRICTED:
        logger.debug("Using SafeSqlDriver with restrictions (RESTRICTED mode)")
        return SafeSqlDriver(sql_driver=base_driver, timeout=30)
    elif current_access_mode == AccessMode.DML_ONLY:
        logger.debug("Using DmlOnlySqlDriver (DML_ONLY mode)")
        return DmlOnlySqlDriver(sql_driver=base_driver, timeout=30)
    else:
        logger.debug("Using unrestricted SqlDriver (UNRESTRICTED mode)")
        return base_driver
```

### 4. Update execute_sql Tool Description

**File**: `src/postgres_mcp/server.py`

Update the dynamic tool registration to include DML_ONLY mode:

```python
if current_access_mode == AccessMode.UNRESTRICTED:
    mcp.add_tool(execute_sql, description="Execute any SQL query")
elif current_access_mode == AccessMode.DML_ONLY:
    mcp.add_tool(execute_sql, description="Execute DML operations (INSERT, UPDATE, DELETE) and read queries")
else:
    mcp.add_tool(execute_sql, description="Execute a read-only SQL query")
```

### 5. Update __init__.py Export

**File**: `src/postgres_mcp/sql/__init__.py`

Add the new driver to exports:

```python
from .dml_only_sql import DmlOnlySqlDriver

__all__ = [
    # ... existing exports ...
    "DmlOnlySqlDriver",
]
```

## Testing Requirements

### Unit Tests

**File**: `tests/unit/sql/test_dml_only_sql.py` (new file)

Create comprehensive tests covering:

1. **Allowed DML Operations**:
   - `test_insert_statement` - INSERT should be allowed
   - `test_update_statement` - UPDATE should be allowed
   - `test_delete_statement` - DELETE should be allowed
   - `test_insert_on_conflict` - UPSERT should be allowed
   - `test_select_statement` - SELECT should be allowed

2. **Blocked DDL Operations**:
   - `test_create_table_blocked` - CREATE TABLE should be rejected
   - `test_alter_table_blocked` - ALTER TABLE should be rejected
   - `test_drop_table_blocked` - DROP TABLE should be rejected
   - `test_create_index_blocked` - CREATE INDEX should be rejected
   - `test_drop_index_blocked` - DROP INDEX should be rejected
   - `test_truncate_blocked` - TRUNCATE should be rejected
   - `test_create_extension_blocked` - CREATE EXTENSION should be rejected
   - `test_vacuum_blocked` - VACUUM should be rejected

3. **Complex Queries**:
   - `test_insert_with_select` - INSERT ... SELECT should work
   - `test_update_with_join` - UPDATE with JOIN should work
   - `test_delete_with_subquery` - DELETE with WHERE IN (SELECT...) should work
   - `test_cte_with_dml` - CTEs with DML should work

**File**: `tests/unit/test_access_mode.py`

Add DML_ONLY to existing access mode tests:

```python
@pytest.mark.parametrize(
    "access_mode,expected_driver_type",
    [
        (AccessMode.UNRESTRICTED, SqlDriver),
        (AccessMode.RESTRICTED, SafeSqlDriver),
        (AccessMode.DML_ONLY, DmlOnlySqlDriver),  # Add this
    ],
)
```

### Integration Tests

**File**: `tests/integration/test_dml_only_integration.py` (new file)

Test against a real PostgreSQL database:

1. Set up test tables
2. Execute INSERT/UPDATE/DELETE operations successfully
3. Verify DDL operations are blocked
4. Verify data is actually modified in the database
5. Test transaction handling

## Documentation Updates

### 1. README.md

Add DML_ONLY mode to the documentation:

- Update "Access Mode" section
- Add usage examples
- Update CLI help text
- Add to comparison table

### 2. Command-line Help

**File**: `src/postgres_mcp/server.py`

Update the argparse help text:

```python
parser.add_argument(
    "--access-mode",
    type=str,
    choices=[mode.value for mode in AccessMode],
    default=AccessMode.UNRESTRICTED.value,
    help="Set SQL access mode: unrestricted (full access), dml_only (allow DML, block DDL), or restricted (read-only)",
)
```

### 3. Examples

Add example configurations showing DML_ONLY mode usage in:
- Claude Desktop config
- VS Code MCP settings
- Docker examples

## Validation Checklist

- [ ] `AccessMode` enum includes `DML_ONLY`
- [ ] `DmlOnlySqlDriver` class created with proper validation
- [ ] `get_sql_driver()` returns correct driver for DML_ONLY mode
- [ ] All unit tests pass
- [ ] Integration tests pass with real database
- [ ] Documentation updated (README, CLI help)
- [ ] All existing tests still pass
- [ ] Code follows project style (ruff, pyright)
- [ ] Example configurations added

## Running Tests

```bash
# Run all tests
pytest

# Run only DML_ONLY tests
pytest tests/unit/sql/test_dml_only_sql.py

# Run with coverage
pytest --cov=src/postgres_mcp --cov-report=html

# Check code style
ruff check src/ tests/
pyright src/
```

## Key Files to Modify/Create

### New Files
- `src/postgres_mcp/sql/dml_only_sql.py` - DmlOnlySqlDriver implementation
- `tests/unit/sql/test_dml_only_sql.py` - Unit tests for DML_ONLY mode
- `tests/integration/test_dml_only_integration.py` - Integration tests

### Modified Files
- `src/postgres_mcp/server.py` - Add AccessMode.DML_ONLY, update get_sql_driver()
- `src/postgres_mcp/sql/__init__.py` - Export DmlOnlySqlDriver
- `tests/unit/test_access_mode.py` - Add DML_ONLY to parametrized tests
- `README.md` - Document the new mode

## Reference Implementation

The implementation should closely follow the pattern established by `SafeSqlDriver` in `src/postgres_mcp/sql/safe_sql.py`:

1. Use pglast for SQL parsing
2. Maintain allowed statement types, functions, and node types
3. Implement recursive node validation
4. Apply timeout to prevent long-running queries
5. Use proper error messages for blocked operations
6. Follow the existing code style and patterns

## Success Criteria

The implementation is complete when:

1. All tests pass (existing + new)
2. Documentation is updated
3. An agent can successfully use the DML_ONLY mode to:
   - Insert data into tables
   - Update existing records
   - Delete records
   - But is blocked from creating/altering/dropping tables or indexes
4. The mode works correctly in both Claude Desktop and VS Code MCP configurations
