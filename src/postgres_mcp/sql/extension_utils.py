"""Utilities for working with PostgreSQL extensions."""

import logging
from typing import Literal, TypedDict

from postgres_mcp.sql import SqlDriver, SafeSqlDriver

logger = logging.getLogger(__name__)


class ExtensionStatus(TypedDict):
    """Status of an extension."""

    is_installed: bool
    is_available: bool
    name: str
    message: str
    default_version: str | None


async def check_extension(
    sql_driver: SqlDriver,
    extension_name: str,
    include_messages: bool = True,
    message_type: Literal["plain", "markdown"] = "plain",
) -> ExtensionStatus:
    """
    Check if a PostgreSQL extension is installed or available.

    Args:
        sql_driver: An instance of SqlDriver to execute queries
        extension_name: Name of the extension to check
        include_messages: Whether to include user-friendly messages in the result
        message_type: Format for messages - 'plain' or 'markdown'

    Returns:
        ExtensionStatus with fields:
            - is_installed: True if the extension is installed
            - is_available: True if the extension is available but not installed
            - name: The extension name
            - message: A user-friendly message about the extension status
            - default_version: The default version of the extension if available
    """
    # Check if the extension is installed
    installed_result = await SafeSqlDriver.execute_param_query(
        sql_driver,
        "SELECT extversion FROM pg_extension WHERE extname = {}",
        [extension_name],
    )

    # Initialize result
    result: ExtensionStatus = {
        "is_installed": False,
        "is_available": False,
        "name": extension_name,
        "message": "",
        "default_version": None,
    }

    if installed_result and len(installed_result) > 0:
        # Extension is installed
        version = installed_result[0].cells.get("extversion", "unknown")
        result["is_installed"] = True
        result["is_available"] = True

        if include_messages:
            if message_type == "markdown":
                result["message"] = (
                    f"The **{extension_name}** extension (version {version}) is already installed."
                )
            else:
                result["message"] = (
                    f"The {extension_name} extension (version {version}) is already installed."
                )
    else:
        # Check if the extension is available but not installed
        available_result = await SafeSqlDriver.execute_param_query(
            sql_driver,
            "SELECT default_version FROM pg_available_extensions WHERE name = {}",
            [extension_name],
        )

        if available_result and len(available_result) > 0:
            # Extension is available but not installed
            result["is_available"] = True
            result["default_version"] = available_result[0].cells.get("default_version")

            if include_messages:
                if message_type == "markdown":
                    result["message"] = (
                        f"The **{extension_name}** extension is available but not installed.\n\n"
                        f"You can install it by running: `CREATE EXTENSION {extension_name};`\n\n"
                        f"Or use the 'install_extension' tool."
                    )
                else:
                    result["message"] = (
                        f"The {extension_name} extension is available but not installed.\n"
                        f"You can install it by running: CREATE EXTENSION {extension_name};\n"
                        f"Or use the 'install_extension' tool."
                    )
        else:
            # Extension is not available
            if include_messages:
                if message_type == "markdown":
                    result["message"] = (
                        f"The **{extension_name}** extension is not available on this PostgreSQL server.\n\n"
                        f"To install it, you need to:\n"
                        f"1. Install the extension package on the server\n"
                        f"2. Run: `CREATE EXTENSION {extension_name};`"
                    )
                else:
                    result["message"] = (
                        f"The {extension_name} extension is not available on this PostgreSQL server.\n"
                        f"To install it, you need to:\n"
                        f"1. Install the extension package on the server\n"
                        f"2. Run: CREATE EXTENSION {extension_name};"
                    )

    return result


async def check_hypopg_installation_status(
    sql_driver: SqlDriver, message_type: Literal["plain", "markdown"] = "markdown"
) -> tuple[bool, str]:
    """
    Get a detailed status message for the HypoPG extension.

    Args:
        sql_driver: An instance of SqlDriver to execute queries
        message_type: Format for messages - 'plain' or 'markdown'

    Returns:
        A formatted message about the HypoPG extension status with installation instructions
    """
    status = await check_extension(sql_driver, "hypopg", include_messages=False)

    if status["is_installed"]:
        if message_type == "markdown":
            return True, "The **hypopg** extension is already installed."
        else:
            return True, "The hypopg extension is already installed."

    if status["is_available"]:
        if message_type == "markdown":
            return False, (
                "The **hypopg** extension is required to test hypothetical indexes, but it is not currently installed.\n\n"
                "You can ask me to install 'hypopg' using the 'install_extension' tool.\n\n"
                "**Is it safe?** Installing 'hypopg' is generally safe and a standard practice for index testing. "
                "It adds a virtual layer that simulates indexes without actually creating them in the database. "
                "It requires database privileges (often superuser) to install.\n\n"
                "**What does it do?** It allows you to create virtual indexes and test how they would affect query performance "
                "without the overhead of actually creating the indexes.\n\n"
                "**How to undo?** If you later decide to remove it, you can ask me to run 'DROP EXTENSION hypopg;'."
            )
        else:
            return False, (
                "The hypopg extension is required to test hypothetical indexes, but it is not currently installed.\n"
                "You can ask me to install it using the 'install_extension' tool.\n"
                "It is generally safe to install and allows testing indexes without creating them."
            )

    # Extension is not available
    if message_type == "markdown":
        return False, (
            "The **hypopg** extension is not available on this PostgreSQL server.\n\n"
            "To install HypoPG:\n"
            "1. For Debian/Ubuntu: `sudo apt-get install postgresql-hypopg`\n"
            "2. For RHEL/CentOS: `sudo yum install postgresql-hypopg`\n"
            "3. For MacOS with Homebrew: `brew install hypopg`\n"
            "4. For other systems, build from source: `git clone https://github.com/HypoPG/hypopg`\n\n"
            "After installing the extension packages, connect to your database and run: `CREATE EXTENSION hypopg;`"
        )
    else:
        return False, (
            "The hypopg extension is not available on this PostgreSQL server.\n"
            "To install HypoPG:\n"
            "1. For Debian/Ubuntu: sudo apt-get install postgresql-hypopg\n"
            "2. For RHEL/CentOS: sudo yum install postgresql-hypopg\n"
            "3. For MacOS with Homebrew: brew install hypopg\n"
            "4. For other systems, build from source: git clone https://github.com/HypoPG/hypopg\n"
            "After installing the extension packages, connect to your database and run: CREATE EXTENSION hypopg;"
        )
