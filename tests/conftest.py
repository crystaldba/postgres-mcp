import asyncio
from typing import Generator

import pytest
from dotenv import load_dotenv
from utils import create_postgres_container
from utils import selected_postgres_versions
from utils import strict_environment

from postgres_mcp.sql import reset_postgres_version_cache

load_dotenv()

# Skips that are intentional and allowed even under PG_TEST_STRICT=1. Any other skip fails
# the run in strict mode, so a new silent skip cannot hide a coverage hole. To add one,
# match the skip reason text here and say why in the PR.
ALLOWED_STRICT_SKIPS = (
    "Skipping multi-column indexes test for now",
    "Skipping storage cost tradeoff test for now",
    "Skipping pareto optimal index selection test for now",
)


# Define a custom event loop policy that handles cleanup better
@pytest.fixture(scope="session")
def event_loop_policy():
    """Create and return a custom event loop policy for tests."""
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="class", params=selected_postgres_versions())
def test_postgres_connection_string(request) -> Generator[tuple[str, str], None, None]:
    yield from create_postgres_container(request.param)


@pytest.fixture(autouse=True)
def reset_pg_version_cache():
    """Reset the PostgreSQL version cache before each test."""
    reset_postgres_version_cache()
    yield


def pytest_collection_modifyitems(items):
    """Mark everything under tests/integration/dta as `dta`.

    The fast CI tier deselects these (-m "not dta"); the full tier runs them.
    Run them yourself (uv run pytest -m dta) when you change the index advisor.
    """
    for item in items:
        if "integration/dta/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.dta)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Under PG_TEST_STRICT=1, turn any skip that is not explicitly allowed into a failure."""
    outcome = yield
    report = outcome.get_result()
    if not (strict_environment() and report.skipped) or hasattr(report, "wasxfail"):
        return
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    if any(allowed in reason for allowed in ALLOWED_STRICT_SKIPS):
        return
    report.outcome = "failed"
    report.longrepr = f"Unexpected skip under PG_TEST_STRICT=1 (add it to ALLOWED_STRICT_SKIPS in tests/conftest.py only if intentional): {reason}"
