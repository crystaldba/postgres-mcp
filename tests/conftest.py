import asyncio
from typing import Generator

import pytest
from dotenv import load_dotenv
from utils import create_postgres_container

load_dotenv()


# Define a custom event loop policy that handles cleanup better
@pytest.fixture(scope="session")
def event_loop_policy():
    """Create and return a custom event loop policy for tests."""
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="class", params=["postgres:15", "postgres:16"])
def test_postgres_connection_string(request) -> Generator[tuple[str, str], None, None]:
    yield from create_postgres_container(request.param)
