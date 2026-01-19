import logging
from typing import Dict
from typing import Optional
from typing import Union
from urllib.parse import urlparse
from urllib.parse import urlunparse

from ..moldes.model import AccessMode
from ..sql import DbConnPool
from ..sql import SafeSqlDriver
from ..sql import SqlDriver
from ..sql import obfuscate_password

logger = logging.getLogger(__name__)

db_connection: DbConnPool = DbConnPool()
current_access_mode: AccessMode = AccessMode.UNRESTRICTED
db_connections_cache: Dict[str, DbConnPool] = {}
shutdown_in_progress: bool = False


async def get_sql_driver(access_mode: Optional[AccessMode] = None, connection: Optional[DbConnPool] = None) -> Union[SqlDriver, SafeSqlDriver]:
    """
    Get the appropriate SQL driver based on the current access mode.

    Args:
        access_mode: Access mode to use. If None, uses global current_access_mode.
        connection: Database connection to use. If None, uses global db_connection.

    Returns:
        SqlDriver or SafeSqlDriver based on access mode
    """
    mode = access_mode if access_mode is not None else current_access_mode
    conn = connection if connection is not None else db_connection

    base_driver = SqlDriver(conn=conn)

    if mode == AccessMode.RESTRICTED:
        logger.debug("Using SafeSqlDriver with restrictions (RESTRICTED mode)")
        return SafeSqlDriver(sql_driver=base_driver, timeout=30)
    else:
        logger.debug("Using unrestricted SqlDriver (UNRESTRICTED mode)")
        return base_driver


async def get_current_database_name() -> str:
    """Get the name of the currently connected database."""
    if not db_connection:
        logger.error("Database connection not initialized")
        return ""

    try:
        sql_driver = await get_sql_driver()
        rows = await sql_driver.execute_query("SELECT current_database()")
        if rows and len(rows) > 0:
            return rows[0].cells.get("current_database", "")
        return ""
    except Exception as e:
        logger.error(f"Error getting current database name: {e}")
        return ""


async def get_sql_driver_for_database(database_name: str) -> Union[SqlDriver, SafeSqlDriver]:
    """
    Get SQL driver for a specific database.
    Reuses the main db_connection if database_name matches.
    Creates and caches new connections for other databases.

    Args:
        database_name: Name of the database to connect to

    Returns:
        SqlDriver or SafeSqlDriver for the specified database
    """
    if not db_connection:
        raise ValueError("Database connection not initialized")

    # Try to reuse main database connection
    current_db = await get_current_database_name()
    if database_name == current_db:
        logger.debug(f"Reusing main connection for database: {database_name}")
        return await get_sql_driver()

    # Check cached connection
    cached_driver = await _get_cached_driver(database_name)
    if cached_driver:
        return cached_driver

    # Create new connection
    return await _create_new_database_connection(database_name)


async def _get_cached_driver(database_name: str) -> Optional[Union[SqlDriver, SafeSqlDriver]]:
    """Retrieve driver from cache if valid."""
    if database_name not in db_connections_cache:
        return None

    cached_pool = db_connections_cache[database_name]

    if cached_pool.is_valid:
        logger.debug(f"Reusing cached connection for database: {database_name}")
        return _wrap_driver_for_access_mode(SqlDriver(conn=cached_pool))

    # Remove invalid cached connection
    logger.warning(f"Cached connection for {database_name} is invalid, removing from cache")
    await cached_pool.close()
    del db_connections_cache[database_name]
    return None


async def _create_new_database_connection(database_name: str) -> Union[SqlDriver, SafeSqlDriver]:
    """Create and cache a new database connection."""
    if not db_connection:
        raise ValueError("No base connection available")

    # Validate base connection URL exists
    if not db_connection.connection_url:
        raise ValueError("No base connection URL available")

    # Construct new database URL
    new_url = _build_database_url(database_name)
    logger.info(f"Creating new connection pool for database: {database_name}")

    try:
        # Create and cache new connection pool
        new_pool = DbConnPool()
        await new_pool.pool_connect(str(new_url))

        db_connections_cache[database_name] = new_pool
        return _wrap_driver_for_access_mode(SqlDriver(conn=new_pool))

    except Exception as e:
        logger.error(f"Error connecting to database {database_name}: {e}")
        raise ValueError(f"Cannot connect to database '{database_name}': {obfuscate_password(str(e))}") from e


def _build_database_url(database_name: str) -> str:
    """Build new database URL by replacing database name in base URL."""
    if not db_connection or not db_connection.connection_url:
        raise ValueError("No base connection URL available")

    parsed = urlparse(db_connection.connection_url)
    # Replace database name (URL path section)
    new_path = f"/{database_name}"
    return str(urlunparse(parsed._replace(path=new_path)))


def _wrap_driver_for_access_mode(driver: SqlDriver) -> Union[SqlDriver, SafeSqlDriver]:
    """Wrap driver with SafeSqlDriver if in restricted access mode."""
    if current_access_mode == AccessMode.RESTRICTED:
        return SafeSqlDriver(sql_driver=driver, timeout=30)
    return driver
