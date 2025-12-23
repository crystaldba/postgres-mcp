import logging
from typing import List
from typing import Optional

import mcp.types as types

from .sql import SafeSqlDriver
from .utils.reponse import format_error_response
from .utils.reponse import format_text_response
from .utils.sql_driver import get_sql_driver
from .utils.sql_driver import get_sql_driver_for_database

logger = logging.getLogger(__name__)

# Type alias for response format
ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]


def dynamically_register_resources(mcp_instance, database_name: Optional[str] = None):  # type: ignore
    """
    Register consolidated resource handlers with the MCP instance.

    Args:
        mcp_instance: The FastMCP instance to register resources with
        database_name: Optional specific database name. If None, registers dynamic resources.
    """

    if database_name:
        logger.info(f"Registering static resources for database: {database_name}")
        _register_static_resources(mcp_instance, database_name)
    else:
        logger.info("Registering dynamic resources with database name parameter")
        _register_dynamic_resources(mcp_instance)


def _register_static_resources(mcp_instance, db_name: str):  # type: ignore
    """Register static resource paths for a specific database."""

    tables = f"postgres://{db_name}/"
    views = f"postgres://{db_name}/"

    tables_uri = tables + "{schema_name}/tables"
    views_uri = views + "{schema_name}/views"

    logger.info(f"Registering static resource: {tables_uri}")
    logger.info(f"Registering static resource: {views_uri}")

    @mcp_instance.resource(tables_uri)  # type: ignore
    async def get_database_tables_static(schema_name: str) -> ResponseType:
        """
        Get comprehensive information about all tables in the configured database.

        Returns complete table information including schemas, columns with comments,
        constraints, indexes, and statistics.
        """
        return await _get_tables_impl(db_name, schema_name)

    @mcp_instance.resource(views_uri)  # type: ignore
    async def get_database_views_static(schema_name: str) -> ResponseType:
        """
        Get comprehensive information about all views in the configured database.

        Returns complete view information including schemas, columns with comments,
        view definitions, and dependencies.
        """
        return await _get_views_impl(db_name, schema_name)


def _register_dynamic_resources(mcp_instance):  # type: ignore
    """Register dynamic resource paths with database name parameter."""

    tables_uri = "postgres://{database_name}/{schema_name}/tables"
    views_uri = "postgres://{database_name}/{schema_name}/views"
    databases_uri = "postgres://databases"
    schemas_uri = "postgres://{database_name}/schemas"

    logger.info(f"Registering dynamic resource: {tables_uri}")
    logger.info(f"Registering dynamic resource: {views_uri}")
    logger.info(f"Registering dynamic resource: {databases_uri}")
    logger.info(f"Registering dynamic resource: {schemas_uri}")

    @mcp_instance.resource(tables_uri)  # type: ignore
    async def get_database_tables_dynamic(database_name: str, schema_name: Optional[str] = None) -> ResponseType:
        """
        Get comprehensive information about all tables in a specific database.

        Args:
            database_name: Name of the database to query
            schema_name: Name of the schema to query

        Returns complete table information including schemas, columns with comments,
        constraints, indexes, and statistics.
        """
        return await _get_tables_impl(database_name, schema_name)

    @mcp_instance.resource(views_uri)  # type: ignore
    async def get_database_views_dynamic(database_name: str, schema_name: Optional[str] = None) -> ResponseType:
        """
        Get comprehensive information about all views in a specific database.

        Args:
            database_name: Name of the database to query
            schema_name: Name of the schema to query

        Returns complete view information including schemas, columns with comments,
        view definitions, and dependencies.
        """
        return await _get_views_impl(database_name, schema_name)

    @mcp_instance.resource(databases_uri)  # type: ignore
    async def get_all_databases_dynamic() -> ResponseType:
        """
        List all databases in the PostgreSQL server.

        Returns a list of all user databases excluding system templates.
        Each database entry includes:
          - database_name: Name of the database
          - owner: Database owner
          - encoding: Character encoding
          - collation: Collation setting
          - ctype: Character classification
          - size: Formatted database size
        """
        return await _get_databases_info_impl(None)

    @mcp_instance.resource(schemas_uri)  # type: ignore
    async def get_all_schemas_dynamic(database_name: str) -> ResponseType:
        """
        List all schemas in a specific PostgreSQL database.

        Args:
            database_name: Name of the database to query

        Returns a list of all user schemas excluding system schemas (pg_* and information_schema).
        Each schema entry includes:
          - schema_name: Name of the schema
          - schema_owner: Owner of the schema
          - schema_type: Type of schema ('user' or 'system')
        """
        return await _get_all_schemas_impl(database_name)


async def _get_tables_impl(database_name: str, schema_name: Optional[str] = None) -> ResponseType:
    """
    Implementation for getting comprehensive table information.

    Args:
        database_name: Database name to query
        schema_name: Optional schema name to filter results. If provided, only returns tables from that schema.

    Returns:
        - List of all user schemas (or single schema if filtered)
        - Complete table information including:
          * Table metadata (schema, name, type)
          * Column details with comments
          * Constraints (primary key, foreign key, unique, check)
          * Indexes with statistics
          * Table size and row count
    """
    logger.info(f"Getting comprehensive table information for database: {database_name}, schema: {schema_name or 'all'}")
    if not schema_name:
        raise ValueError("schema_name must be provided")
    try:
        sql_driver = await get_sql_driver_for_database(database_name)
        schema_filter = f"AND schema_name = '{schema_name}'"
        schema_query = f"""
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'system'
                    WHEN schema_name = 'information_schema' THEN 'system'
                    ELSE 'user'
                END as schema_type
            FROM information_schema.schemata
            WHERE schema_name NOT LIKE 'pg_%'
              AND schema_name != 'information_schema'
              {schema_filter}
            ORDER BY schema_name
        """
        schema_rows = await sql_driver.execute_query(schema_query)  # type: ignore
        schemas = [row.cells for row in schema_rows] if schema_rows else []

        # If schema_name is provided but not found, return empty result
        if schema_name and not schemas:
            logger.warning(f"Schema '{schema_name}' not found in database '{database_name}'")
            return format_text_response(
                {"database": database_name, "schemas": [], "tables": [], "total_tables": 0, "message": f"Schema '{schema_name}' not found"}
            )
        table_schema_filter = f"AND t.table_schema = '{schema_name}'"

        # Get all tables with metadata (filtered by schema if provided)
        table_query = f"""
            SELECT
                t.table_schema,
                t.table_name,
                pg_size_pretty(pg_total_relation_size(quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))) as table_size,
                (SELECT reltuples::bigint
                 FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = t.table_schema AND c.relname = t.table_name) as estimated_rows,
                obj_description((quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass) as table_comment
            FROM information_schema.tables t
            WHERE t.table_type = 'BASE TABLE'
              AND t.table_schema NOT LIKE 'pg_%'
              AND t.table_schema != 'information_schema'
              {table_schema_filter}
            ORDER BY t.table_schema, t.table_name
        """
        table_rows = await sql_driver.execute_query(table_query)  # type: ignore

        if not table_rows:
            return format_text_response({"database": database_name, "schemas": schemas, "tables": [], "total_tables": 0})

        tables_info = []
        for row in table_rows:
            table_schema = row.cells["table_schema"]
            table_name = row.cells["table_name"]

            try:
                # Get columns with comments
                col_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT
                        c.column_name,
                        c.data_type,
                        c.is_nullable,
                        c.column_default,
                        c.ordinal_position,
                        c.character_maximum_length,
                        c.numeric_precision,
                        c.numeric_scale,
                        pgd.description as column_comment
                    FROM information_schema.columns c
                    LEFT JOIN pg_catalog.pg_statio_all_tables psat
                        ON c.table_schema = psat.schemaname AND c.table_name = psat.relname
                    LEFT JOIN pg_catalog.pg_description pgd
                        ON psat.relid = pgd.objoid AND c.ordinal_position = pgd.objsubid
                    WHERE c.table_schema = {} AND c.table_name = {}
                    ORDER BY c.ordinal_position
                    """,
                    [table_schema, table_name],
                )

                columns = []
                if col_rows:
                    for r in col_rows:
                        col_info = {
                            "name": r.cells["column_name"],
                            "data_type": r.cells["data_type"],
                            "is_nullable": r.cells["is_nullable"],
                            "default": r.cells["column_default"],
                            "position": r.cells["ordinal_position"],
                            "comment": r.cells.get("column_comment", ""),
                        }
                        # Add type-specific details
                        if r.cells.get("character_maximum_length"):
                            col_info["max_length"] = r.cells["character_maximum_length"]
                        if r.cells.get("numeric_precision"):
                            col_info["precision"] = r.cells["numeric_precision"]
                        if r.cells.get("numeric_scale"):
                            col_info["scale"] = r.cells["numeric_scale"]
                        columns.append(col_info)

                # Get constraints
                con_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT
                        tc.constraint_name,
                        tc.constraint_type,
                        kcu.column_name,
                        CASE
                            WHEN tc.constraint_type = 'FOREIGN KEY' THEN ccu.table_schema
                            ELSE NULL
                        END as foreign_table_schema,
                        CASE
                            WHEN tc.constraint_type = 'FOREIGN KEY' THEN ccu.table_name
                            ELSE NULL
                        END as foreign_table_name,
                        CASE
                            WHEN tc.constraint_type = 'FOREIGN KEY' THEN ccu.column_name
                            ELSE NULL
                        END as foreign_column_name
                    FROM information_schema.table_constraints AS tc
                    LEFT JOIN information_schema.key_column_usage AS kcu
                        ON tc.constraint_name = kcu.constraint_name
                        AND tc.table_schema = kcu.table_schema
                    LEFT JOIN information_schema.constraint_column_usage AS ccu
                        ON tc.constraint_name = ccu.constraint_name
                        AND tc.constraint_type = 'FOREIGN KEY'
                    WHERE tc.table_schema = {} AND tc.table_name = {}
                    ORDER BY tc.constraint_type, tc.constraint_name, kcu.ordinal_position
                    """,
                    [table_schema, table_name],
                )

                constraints = {}
                if con_rows:
                    for con_row in con_rows:
                        cname = con_row.cells["constraint_name"]
                        ctype = con_row.cells["constraint_type"]
                        col = con_row.cells["column_name"]

                        if cname not in constraints:
                            constraints[cname] = {"type": ctype, "columns": []}
                            # Add foreign key reference info
                            if ctype == "FOREIGN KEY" and con_row.cells.get("foreign_table_name"):
                                constraints[cname]["references"] = {
                                    "schema": con_row.cells["foreign_table_schema"],
                                    "table": con_row.cells["foreign_table_name"],
                                    "column": con_row.cells["foreign_column_name"],
                                }
                        if col:
                            constraints[cname]["columns"].append(col)

                constraints_list = [{"name": name, **data} for name, data in constraints.items()]

                # Get indexes with details
                idx_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT
                        i.indexname,
                        i.indexdef,
                        pg_size_pretty(pg_relation_size(quote_ident(i.schemaname) || '.' || quote_ident(i.indexname))) as index_size,
                        idx.indisunique as is_unique,
                        idx.indisprimary as is_primary
                    FROM pg_indexes i
                    JOIN pg_class c ON c.relname = i.indexname
                    JOIN pg_index idx ON idx.indexrelid = c.oid
                    WHERE i.schemaname = {} AND i.tablename = {}
                    ORDER BY i.indexname
                    """,
                    [table_schema, table_name],
                )

                indexes = []
                if idx_rows:
                    for idx_row in idx_rows:
                        indexes.append(
                            {
                                "name": idx_row.cells["indexname"],
                                "definition": idx_row.cells["indexdef"],
                                "size": idx_row.cells["index_size"],
                                "is_unique": idx_row.cells["is_unique"],
                                "is_primary": idx_row.cells["is_primary"],
                            }
                        )

                table_info = {
                    "schema": table_schema,
                    "name": table_name,
                    "type": "table",
                    "comment": row.cells.get("table_comment", ""),
                    "size": row.cells.get("table_size", ""),
                    "estimated_rows": row.cells.get("estimated_rows", 0),
                    "columns": columns,
                    "constraints": constraints_list,
                    "indexes": indexes,
                }

                tables_info.append(table_info)

            except Exception as e:
                logger.error(f"Error getting schema for table {database_name}.{table_schema}.{table_name}: {e}")
                # Continue with other tables even if one fails

        result = {
            "database": database_name,
            "schema_filter": schema_name or "all",
            "schemas": schemas,
            "tables": tables_info,
            "total_tables": len(tables_info),
        }

        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting tables information for database {database_name}: {e}")
        return format_error_response(str(e))


async def _get_views_impl(database_name: str, schema_name: Optional[str] = None) -> ResponseType:
    """
    Implementation for getting comprehensive view information.

    Args:
        database_name: Database name to query
        schema_name: Optional schema name to filter results. If provided, only returns views from that schema.

    Returns:
        - List of all user schemas (or single schema if filtered)
        - Complete view information including:
          * View metadata (schema, name, type)
          * Column details with comments
          * View definition (SQL)
          * Dependent objects
    """
    logger.info(f"Getting comprehensive view information for database: {database_name}, schema: {schema_name or 'all'}")
    if not schema_name:
        raise ValueError("schema_name must be provided")
    try:
        sql_driver = await get_sql_driver_for_database(database_name)
        schema_filter = f"AND schema_name = '{schema_name}'"

        # Get all user schemas (or specific schema if provided)
        schema_query = f"""
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'system'
                    WHEN schema_name = 'information_schema' THEN 'system'
                    ELSE 'user'
                END as schema_type
            FROM information_schema.schemata
            WHERE schema_name NOT LIKE 'pg_%'
              AND schema_name != 'information_schema'
              {schema_filter}
            ORDER BY schema_name
        """
        schema_rows = await sql_driver.execute_query(schema_query)  # type: ignore
        schemas = [row.cells for row in schema_rows] if schema_rows else []

        # If schema_name is provided but not found, return empty result
        if schema_name and not schemas:
            logger.warning(f"Schema '{schema_name}' not found in database '{database_name}'")
            return format_text_response(
                {"database": database_name, "schemas": [], "views": [], "total_views": 0, "message": f"Schema '{schema_name}' not found"}
            )

        # Build view filter condition
        view_schema_filter = ""
        if schema_name:
            view_schema_filter = f"AND t.table_schema = '{schema_name}'"

        # Get all views with metadata (filtered by schema if provided)
        view_query = f"""
            SELECT
                t.table_schema,
                t.table_name,
                v.view_definition,
                obj_description((quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass) as view_comment
            FROM information_schema.tables t
            LEFT JOIN information_schema.views v
                ON t.table_schema = v.table_schema AND t.table_name = v.table_name
            WHERE t.table_type = 'VIEW'
              AND t.table_schema NOT LIKE 'pg_%'
              AND t.table_schema != 'information_schema'
              {view_schema_filter}
            ORDER BY t.table_schema, t.table_name
        """
        view_rows = await sql_driver.execute_query(view_query)  # type: ignore

        if not view_rows:
            return format_text_response({"database": database_name, "schemas": schemas, "views": [], "total_views": 0})

        views_info = []
        for row in view_rows:
            view_schema = row.cells["table_schema"]
            view_name = row.cells["table_name"]

            try:
                # Get columns with comments
                col_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT
                        c.column_name,
                        c.data_type,
                        c.is_nullable,
                        c.column_default,
                        c.ordinal_position,
                        c.character_maximum_length,
                        c.numeric_precision,
                        c.numeric_scale,
                        pgd.description as column_comment
                    FROM information_schema.columns c
                    LEFT JOIN pg_catalog.pg_statio_all_tables psat
                        ON c.table_schema = psat.schemaname AND c.table_name = psat.relname
                    LEFT JOIN pg_catalog.pg_description pgd
                        ON psat.relid = pgd.objoid AND c.ordinal_position = pgd.objsubid
                    WHERE c.table_schema = {} AND c.table_name = {}
                    ORDER BY c.ordinal_position
                    """,
                    [view_schema, view_name],
                )

                columns = []
                if col_rows:
                    for r in col_rows:
                        col_info = {
                            "name": r.cells["column_name"],
                            "data_type": r.cells["data_type"],
                            "is_nullable": r.cells["is_nullable"],
                            "default": r.cells["column_default"],
                            "position": r.cells["ordinal_position"],
                            "comment": r.cells.get("column_comment", ""),
                        }
                        # Add type-specific details
                        if r.cells.get("character_maximum_length"):
                            col_info["max_length"] = r.cells["character_maximum_length"]
                        if r.cells.get("numeric_precision"):
                            col_info["precision"] = r.cells["numeric_precision"]
                        if r.cells.get("numeric_scale"):
                            col_info["scale"] = r.cells["numeric_scale"]
                        columns.append(col_info)

                # Get dependent objects (what tables this view depends on)
                dep_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT DISTINCT
                        source_ns.nspname as source_schema,
                        source_table.relname as source_table,
                        source_table.relkind as source_type
                    FROM pg_depend d
                    JOIN pg_rewrite r ON r.oid = d.objid
                    JOIN pg_class view_class ON view_class.oid = r.ev_class
                    JOIN pg_namespace view_ns ON view_ns.oid = view_class.relnamespace
                    JOIN pg_class source_table ON source_table.oid = d.refobjid
                    JOIN pg_namespace source_ns ON source_ns.oid = source_table.relnamespace
                    WHERE view_ns.nspname = {}
                      AND view_class.relname = {}
                      AND source_table.relkind IN ('r', 'v', 'm')
                      AND d.deptype = 'n'
                    """,
                    [view_schema, view_name],
                )

                dependencies = []
                if dep_rows:
                    for dep_row in dep_rows:
                        dep_type_map = {"r": "table", "v": "view", "m": "materialized view"}
                        dependencies.append(
                            {
                                "schema": dep_row.cells["source_schema"],
                                "name": dep_row.cells["source_table"],
                                "type": dep_type_map.get(dep_row.cells["source_type"], "unknown"),
                            }
                        )

                view_info = {
                    "schema": view_schema,
                    "name": view_name,
                    "type": "view",
                    "comment": row.cells.get("view_comment", ""),
                    "definition": row.cells.get("view_definition", ""),
                    "columns": columns,
                    "dependencies": dependencies,
                }

                views_info.append(view_info)

            except Exception as e:
                logger.error(f"Error getting schema for view {database_name}.{view_schema}.{view_name}: {e}")
                # Continue with other views even if one fails

        result = {
            "database": database_name,
            "schema_filter": schema_name or "all",
            "schemas": schemas,
            "views": views_info,
            "total_views": len(views_info),
        }

        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting views information for database {database_name}: {e}")
        return format_error_response(str(e))


async def _get_all_schemas_impl(database_name: str) -> ResponseType:
    """
    Implementation for getting all schemas in a database.

    Args:
        database_name: Database name to query

    Returns:
        List of all schemas in the database, excluding system schemas
    """
    logger.info(f"Getting all schemas for database: {database_name}")
    try:
        sql_driver = await get_sql_driver_for_database(database_name)

        rows = await sql_driver.execute_query(
            """
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'system'
                    WHEN schema_name = 'information_schema' THEN 'system'
                    ELSE 'user'
                END as schema_type
            FROM information_schema.schemata
            WHERE schema_name NOT LIKE 'pg_%'
              AND schema_name != 'information_schema'
            ORDER BY schema_name
            """
        )
        schemas = [row.cells for row in rows] if rows else []
        return format_text_response({"database": database_name, "schemas": schemas, "total_schemas": len(schemas)})
    except Exception as e:
        logger.error(f"Error getting schemas for database {database_name}: {e}")
        return format_error_response(str(e))


async def _get_databases_info_impl(database_name: Optional[str] = None) -> ResponseType:
    """
    Implementation for getting database information.

    Args:
        database_name: Optional database name. If None, returns all databases.
                      If provided, returns information for that specific database.

    Returns:
        - If database_name is None: List of all databases
        - If database_name is provided: Detailed information about the specific database
    """
    try:
        if database_name:
            logger.info(f"Getting information for database: {database_name}")
            sql_driver = await get_sql_driver()

            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT
                    datname as database_name,
                    pg_catalog.pg_get_userbyid(datdba) as owner,
                    pg_encoding_to_char(encoding) as encoding,
                    datcollate as collation,
                    datctype as ctype,
                    pg_size_pretty(pg_database_size(datname)) as size,
                    datconnlimit as connection_limit,
                    datistemplate as is_template,
                    datallowconn as allow_connections
                FROM pg_catalog.pg_database
                WHERE datname = {}
                """,
                [database_name],
            )

            if not rows or len(rows) == 0:
                return format_error_response(f"Database '{database_name}' not found")

            database_info = rows[0].cells
            return format_text_response(database_info)
        else:
            logger.info("Listing all databases")
            sql_driver = await get_sql_driver()

            rows = await sql_driver.execute_query(
                """
                SELECT
                    datname as database_name,
                    pg_catalog.pg_get_userbyid(datdba) as owner,
                    pg_encoding_to_char(encoding) as encoding,
                    datcollate as collation,
                    datctype as ctype,
                    pg_size_pretty(pg_database_size(datname)) as size
                FROM pg_catalog.pg_database
                WHERE datistemplate = false
                ORDER BY datname
                """
            )

            databases = [row.cells for row in rows] if rows else []
            return format_text_response(databases)
    except Exception as e:
        if database_name:
            logger.error(f"Error getting database info for {database_name}: {e}")
        else:
            logger.error(f"Error listing databases: {e}")
        return format_error_response(str(e))
