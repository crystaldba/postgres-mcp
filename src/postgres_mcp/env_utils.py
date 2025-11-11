"""Utility functions for environment variable handling."""

import os


def discover_database_connections() -> dict[str, str]:
    """
    Discover all DATABASE_URI_* environment variables.

    Returns:
        Dict mapping connection names to connection URLs
        - DATABASE_URI -> "default"
        - DATABASE_URI_APP -> "app"
        - DATABASE_URI_ETL -> "etl"
    """
    discovered = {}

    for env_var, url in os.environ.items():
        if env_var == "DATABASE_URI":
            discovered["default"] = url
        elif env_var.startswith("DATABASE_URI_"):
            # Extract postfix and lowercase it
            postfix = env_var[len("DATABASE_URI_") :]
            conn_name = postfix.lower()
            discovered[conn_name] = url

    return discovered


def discover_database_descriptions() -> dict[str, str]:
    """
    Discover all DATABASE_DESC_* environment variables.

    Returns:
        Dict mapping connection names to descriptions
        - DATABASE_DESC -> "default"
        - DATABASE_DESC_APP -> "app"
        - DATABASE_DESC_ETL -> "etl"
    """
    descriptions = {}

    for env_var, desc in os.environ.items():
        if env_var == "DATABASE_DESC":
            descriptions["default"] = desc
        elif env_var.startswith("DATABASE_DESC_"):
            # Extract postfix and lowercase it
            postfix = env_var[len("DATABASE_DESC_") :]
            conn_name = postfix.lower()
            descriptions[conn_name] = desc

    return descriptions
