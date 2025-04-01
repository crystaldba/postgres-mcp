import os
import time
from pathlib import Path
from typing import Generator
from typing import Tuple

import docker
import pytest
from docker import errors as docker_errors


def create_postgres_container(version: str) -> Generator[Tuple[str, str], None, None]:
    """Create a PostgreSQL container of specified version and return its connection string."""
    try:
        client = docker.from_env()
        client.ping()
    except (docker_errors.DockerException, ConnectionError):
        pytest.skip("Docker is not available")

    container_name = (
        f"postgres-crystal-test-{version.replace(':', '_')}-{os.urandom(4).hex()}"
    )
    current_dir = Path(__file__).parent.absolute()

    postgres_password = "test_password"
    postgres_db = "test_db"

    container = client.containers.run(
        version,
        name=container_name,
        environment={
            "POSTGRES_PASSWORD": postgres_password,
            "POSTGRES_DB": postgres_db,
        },
        ports={"5432/tcp": ("127.0.0.1", 0)},  # Let Docker assign a random port
        command=[
            "-c",
            "shared_preload_libraries=pg_stat_statements",
            "-c",
            "pg_stat_statements.track=all",
        ],
        volumes=[f"{current_dir}/init.sql:/docker-entrypoint-initdb.d/init.sql:ro"],
        detach=True,
    )

    try:
        container.reload()
        port = container.ports["5432/tcp"][0]["HostPort"]

        deadline = time.time() + 30
        while time.time() < deadline:
            exit_code, _ = container.exec_run("pg_isready")
            if exit_code == 0:
                break
            time.sleep(1)
        else:
            raise Exception("Timeout waiting for PostgreSQL to start")

        connection_string = (
            f"postgresql://postgres:{postgres_password}@localhost:{port}/{postgres_db}"
        )

        yield connection_string, version

    finally:
        container.stop(timeout=1)
        container.remove(v=True)
