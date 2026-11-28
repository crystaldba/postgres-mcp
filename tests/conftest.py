import asyncio
import os
from typing import Generator

import pytest
from dotenv import load_dotenv
from utils import create_postgres_container

from postgres_mcp.sql import reset_postgres_version_cache

load_dotenv()


# Define a custom event loop policy that handles cleanup better
@pytest.fixture(scope="session")
def event_loop_policy():
    """Create and return a custom event loop policy for tests."""
    return asyncio.DefaultEventLoopPolicy()


ALL_POSTGRES_VERSIONS = ["postgres:12", "postgres:15", "postgres:16", "postgres:17", "postgres:18", "postgres:19"]


def _selected_postgres_versions() -> list[str]:
    """Versions to test, from PG_TEST_VERSIONS (e.g. "15,19" or "postgres:15,postgres:19"); default is all."""
    raw = os.environ.get("PG_TEST_VERSIONS", "").strip()
    if not raw:
        return ALL_POSTGRES_VERSIONS
    selected = [v if v.startswith("postgres:") else f"postgres:{v}" for v in (item.strip() for item in raw.split(",")) if v]
    unknown = [v for v in selected if v not in ALL_POSTGRES_VERSIONS]
    if unknown:
        raise pytest.UsageError(f"PG_TEST_VERSIONS has unsupported versions {unknown}; choose from {ALL_POSTGRES_VERSIONS}")
    return selected


@pytest.fixture(scope="class", params=_selected_postgres_versions())
def test_postgres_connection_string(request) -> Generator[tuple[str, str], None, None]:
    yield from create_postgres_container(request.param)


@pytest.fixture(autouse=True)
def reset_pg_version_cache():
    """Reset the PostgreSQL version cache before each test."""
    reset_postgres_version_cache()
    yield
