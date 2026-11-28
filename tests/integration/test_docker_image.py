"""Docker tests for the image built from the repository's Dockerfile.

These run the real image and check what README.md says about using the SSE and
Streamable HTTP transports in Docker (the "SSE Transport", "Security" and
"Allowed Host headers (HTTP 421)" sections):

- the entrypoint adds --sse-host=0.0.0.0 for SSE only, and the startup warning
  about the missing authentication appears even when the port is published on
  127.0.0.1 only;
- the Host header check (HTTP 421) through a port published on loopback, with
  and without --allowed-hosts, and the usage error for an invalid entry;
- the Docker Compose example: a container on the same network reaches the
  server by its service name without any published port, and needs
  --allowed-hosts=postgres-mcp-server to get past the Host check;
- Streamable HTTP needs --streamable-http-host=0.0.0.0 to be reachable through
  a published port.

The image is built once per module. Every container, the network and the
image are removed afterwards. The tests are skipped when Docker is not
available.
"""

import http.client
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Optional

import docker
import pytest
from docker import errors as docker_errors

from postgres_mcp.network_exposure import exposure_warning

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
SUFFIX = uuid.uuid4().hex[:8]

# The database settings and the service name from the README's Docker Compose example.
DB_ALIAS = "db"
DATABASE_URI = "postgresql://username:password@db:5432/dbname"
SERVICE_NAME = "postgres-mcp-server"

STARTUP_TIMEOUT = 90
HTTP_TIMEOUT = 5

# Rich (the MCP SDK's log handler) wraps log lines at 80 columns when stderr is not a
# terminal; a wide console keeps each log message on one line in the container logs.
SERVER_ENV = {"DATABASE_URI": DATABASE_URI, "COLUMNS": "1000"}

INITIALIZE_REQUEST = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "docker-test", "version": "0"}},
    }
)
STREAMABLE_HTTP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

# Runs inside a client container on the test network. argv[1] is a JSON list of
# requests; it prints a JSON list of {"status", "body"} (or {"error"}). It retries
# connection errors until the deadline, since the server may still be starting.
CLIENT_SCRIPT = """
import http.client, json, sys, time
results = []
for req in json.loads(sys.argv[1]):
    deadline = time.monotonic() + 30
    while True:
        conn = http.client.HTTPConnection(req["host"], req["port"], timeout=5)
        try:
            conn.request("GET", req["path"], headers=req.get("headers", {}))
            resp = conn.getresponse()
            body = resp.read().decode() if resp.status != 200 else ""
            results.append({"status": resp.status, "body": body})
            break
        except (OSError, http.client.HTTPException) as e:
            if time.monotonic() > deadline:
                results.append({"error": repr(e)})
                break
            time.sleep(0.5)
        finally:
            conn.close()
print(json.dumps(results))
"""


def expected_warning(transport: str, restricted: bool) -> str:
    """The startup warning for a transport bound to 0.0.0.0:8000, as the server formats it."""
    warning = exposure_warning(transport, "0.0.0.0", 8000, restricted=restricted)
    assert warning is not None
    return warning


def container_logs(container: Any) -> str:
    try:
        return container.logs().decode("utf-8", errors="replace")
    except docker_errors.DockerException as e:
        return f"<could not read logs: {e}>"


def normalized_logs(container: Any) -> str:
    """Container logs with all whitespace runs collapsed, so a wrapped log line still matches."""
    return re.sub(r"\s+", " ", container_logs(container))


def remove_container(container: Any) -> None:
    try:
        container.remove(force=True, v=True)
    except docker_errors.NotFound:
        pass
    except docker_errors.DockerException as e:
        logger.warning(f"Could not remove container {container.name}: {e}")


def wait_for_log(container: Any, text: str, timeout: float = STARTUP_TIMEOUT) -> None:
    """Wait until the container's logs contain text; fail with the logs if it exits or times out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if text in container_logs(container):
            return
        container.reload()
        if container.status in ("exited", "dead"):
            pytest.fail(f"Container {container.name} exited before logging {text!r}. Logs:\n{container_logs(container)}")
        time.sleep(0.5)
    pytest.fail(f"Timed out after {timeout}s waiting for {text!r} in the logs of {container.name}. Logs:\n{container_logs(container)}")


@dataclass
class Response:
    status: int
    content_type: str
    body: str


def http_request(
    port: int,
    path: str,
    host_header: str,
    method: str = "GET",
    body: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> Response:
    """Send one request to 127.0.0.1:port with the given Host header.

    An event stream (a 200 on /sse) does not end, so only its first event is read;
    other responses are read in full.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HTTP_TIMEOUT)
    try:
        conn.request(method, path, body=body, headers={"Host": host_header, **(headers or {})})
        resp = conn.getresponse()
        content_type = resp.getheader("Content-Type", "")
        if resp.status == 200 and content_type.startswith("text/event-stream"):
            # Read up to the end of the first event.
            data = b""
            while b"\n\n" not in data.replace(b"\r\n", b"\n"):
                chunk = resp.read1(65536)
                if not chunk:
                    break
                data += chunk
        else:
            data = resp.read()
        return Response(resp.status, content_type, data.decode("utf-8", errors="replace"))
    finally:
        conn.close()


def build_context_tar() -> bytes:
    """The repository's tracked and untracked, non-ignored files as a tar, like a clean checkout.

    This keeps a local .venv (which CI creates in the checkout) and caches out of
    the build context; the repository has no .dockerignore.
    """
    files = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in sorted({f.decode() for f in files if f}):
            path = REPO_ROOT / name
            if path.is_file():
                tar.add(path, arcname=name, recursive=False)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def docker_client() -> Iterator[Any]:
    try:
        client = docker.from_env()
        client.ping()
    except (docker_errors.DockerException, ConnectionError) as e:
        pytest.skip(f"Docker is not available: {e}")
    if shutil.which("docker") is None:
        pytest.skip("The docker CLI is not installed (needed to build the image with BuildKit)")
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("Building the image needs git and a git checkout of the repository")
    yield client
    client.close()


@pytest.fixture(scope="module")
def image(docker_client: Any) -> Iterator[str]:
    """Build the image from the repository's Dockerfile once for this module."""
    tag = f"postgres-mcp-docker-test:{SUFFIX}"
    started = time.monotonic()
    # The Dockerfile uses RUN --mount, which needs BuildKit; the docker SDK's build API
    # uses the legacy builder, so build with the CLI.
    result = subprocess.run(
        ["docker", "build", "--tag", tag, "-"],
        input=build_context_tar(),
        capture_output=True,
        timeout=1800,
        env={**os.environ, "DOCKER_BUILDKIT": "1"},
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        pytest.fail(f"docker build failed with status {result.returncode}:\n{output[-5000:]}")
    logger.info(f"Built {tag} in {time.monotonic() - started:.1f}s")
    yield tag
    try:
        docker_client.images.remove(tag, force=True)
    except docker_errors.DockerException as e:
        logger.warning(f"Could not remove image {tag}: {e}")


@pytest.fixture(scope="module")
def network(docker_client: Any) -> Iterator[str]:
    name = f"postgres-mcp-docker-test-{SUFFIX}"
    net = docker_client.networks.create(name, driver="bridge")
    yield name
    try:
        net.remove()
    except docker_errors.DockerException as e:
        logger.warning(f"Could not remove network {name}: {e}")


def endpoint(docker_client: Any, network: str, alias: str) -> dict[str, Any]:
    return {network: docker_client.api.create_endpoint_config(aliases=[alias])}


@pytest.fixture(scope="module")
def postgres(docker_client: Any, network: str) -> Iterator[Any]:
    """Postgres on the test network as `db`, with the credentials from the README's example URI."""
    container = docker_client.containers.run(
        "postgres:16",
        name=f"postgres-mcp-docker-test-db-{SUFFIX}",
        environment={"POSTGRES_USER": "username", "POSTGRES_PASSWORD": "password", "POSTGRES_DB": "dbname"},
        network=network,
        networking_config=endpoint(docker_client, network, DB_ALIAS),
        detach=True,
    )
    try:
        # Over TCP, so the temporary server the image's init runs on the Unix socket does not count.
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while container.exec_run(["pg_isready", "-h", "127.0.0.1", "-U", "username", "-d", "dbname"]).exit_code != 0:
            container.reload()
            if container.status != "running" or time.monotonic() > deadline:
                pytest.fail(f"Postgres did not become ready (status {container.status}). Logs:\n{container_logs(container)}")
            time.sleep(1)
        yield container
    finally:
        remove_container(container)


@pytest.fixture(scope="module")
def client_container(docker_client: Any, image: str, network: str) -> Iterator[Any]:
    """A container on the test network to make requests from, as another Compose service would."""
    container = docker_client.containers.run(
        image,
        entrypoint=["sleep", "infinity"],
        name=f"postgres-mcp-docker-test-client-{SUFFIX}",
        network=network,
        detach=True,
    )
    try:
        yield container
    finally:
        remove_container(container)


def request_from_network(client_container: Any, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exit_code, output = client_container.exec_run(["python", "-c", CLIENT_SCRIPT, json.dumps(requests)])
    text = output.decode("utf-8", errors="replace")
    assert exit_code == 0, f"Client script failed:\n{text}"
    return json.loads(text.strip().splitlines()[-1])


@dataclass
class Server:
    container: Any
    port: Optional[int]  # host port on 127.0.0.1, if published


StartServer = Callable[..., Server]


@pytest.fixture(scope="module")
def start_server(docker_client: Any, image: str, network: str, postgres: Any) -> Iterator[StartServer]:
    """Start server containers from the image; all of them are removed at the end of the module."""
    started: list[Any] = []

    def start(*args: str, publish: bool = True, alias: Optional[str] = None, wait: bool = True) -> Server:
        container = docker_client.containers.run(
            image,
            command=list(args),
            name=f"postgres-mcp-docker-test-server-{SUFFIX}-{len(started)}",
            environment=SERVER_ENV,
            network=network,
            networking_config=endpoint(docker_client, network, alias) if alias else None,
            # Like `docker run -p 127.0.0.1:<random>:8000`.
            ports={"8000/tcp": ("127.0.0.1",)} if publish else None,
            detach=True,
        )
        started.append(container)
        port = None
        if publish:
            container.reload()
            bindings = container.ports.get("8000/tcp") or []
            assert bindings, f"Port 8000 was not published: {container.ports}"
            assert {b["HostIp"] for b in bindings} == {"127.0.0.1"}, f"Port not published on loopback only: {bindings}"
            port = int(bindings[0]["HostPort"])
        if wait:
            wait_for_log(container, "Uvicorn running on")
        return Server(container, port)

    try:
        yield start
    finally:
        for container in started:
            remove_container(container)


@pytest.fixture(scope="module")
def sse_server(start_server: StartServer) -> Server:
    """The README's `docker run -p 127.0.0.1:8000:8000 ... --access-mode=unrestricted --transport=sse`."""
    return start_server("--access-mode=unrestricted", "--transport=sse")


@pytest.fixture(scope="module")
def sse_server_allowed_hosts(start_server: StartServer) -> Server:
    """SSE with --allowed-hosts given twice, with a NAME, a NAME:PORT and a NAME:* entry."""
    return start_server(
        "--access-mode=unrestricted",
        "--transport=sse",
        "--allowed-hosts=mcp.example.com",
        "--allowed-hosts=proxy.internal:9443, Other.Example:*",
    )


def test_sse_entrypoint_binds_all_interfaces_and_warns(sse_server: Server) -> None:
    logs = normalized_logs(sse_server.container)
    assert "SSE transport detected, adding --sse-host=0.0.0.0" in logs
    assert "Uvicorn running on http://0.0.0.0:8000" in logs
    # The warning appears although the port is published on 127.0.0.1 only.
    warning = expected_warning("sse", restricted=False)
    assert warning in logs, logs
    assert "has no authentication" in warning
    assert "Inside a container this warning appears even when the port is published on loopback only" in warning
    assert "Accepting Host headers: 127.0.0.1:*, localhost:*, [::1]:*" in logs
    assert "Successfully connected to database" in logs


def test_sse_published_port_default_host_check(sse_server: Server) -> None:
    port = sse_server.port
    assert port is not None
    for host in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"):
        resp = http_request(port, "/sse", host)
        assert resp.status == 200, f"Host {host}: {resp}"
        assert resp.content_type.startswith("text/event-stream"), resp
        assert "event: endpoint" in resp.body, resp

    rejected = ("localhost", f"host.docker.internal:{port}", "mcp.example.com", f"{SERVICE_NAME}:8000")
    for host in rejected:
        resp = http_request(port, "/sse", host)
        assert (resp.status, resp.body) == (421, "Invalid Host header"), f"Host {host}: {resp}"


def test_sse_published_port_origin_check(sse_server: Server) -> None:
    port = sse_server.port
    assert port is not None
    assert http_request(port, "/sse", f"localhost:{port}", headers={"Origin": f"http://localhost:{port}"}).status == 200
    resp = http_request(port, "/sse", f"localhost:{port}", headers={"Origin": "http://evil.example"})
    assert (resp.status, resp.body) == (403, "Invalid Origin header"), resp


def test_sse_allowed_hosts_through_entrypoint(sse_server_allowed_hosts: Server) -> None:
    server = sse_server_allowed_hosts
    port = server.port
    assert port is not None
    logs = normalized_logs(server.container)
    # The example log line in the README, followed by the other entries.
    assert (
        "Accepting Host headers: 127.0.0.1:*, localhost:*, [::1]:*, mcp.example.com, mcp.example.com:*, proxy.internal:9443, other.example:*" in logs
    ), logs
    assert expected_warning("sse", restricted=False) in logs

    accepted = ("mcp.example.com", "mcp.example.com:443", "proxy.internal:9443", "other.example:1", f"localhost:{port}")
    for host in accepted:
        resp = http_request(port, "/sse", host)
        assert resp.status == 200, f"Host {host}: {resp}"

    # Unlisted names, other ports, a NAME:* entry without a port, and a different case.
    rejected = ("evil.example.com", "proxy.internal", "proxy.internal:9444", "other.example", "OTHER.EXAMPLE:1", "MCP.EXAMPLE.COM")
    for host in rejected:
        resp = http_request(port, "/sse", host)
        assert (resp.status, resp.body) == (421, "Invalid Host header"), f"Host {host}: {resp}"


def test_invalid_allowed_hosts_entry_exits_with_usage_error(start_server: StartServer) -> None:
    server = start_server("--transport=sse", "--allowed-hosts=*", publish=False, wait=False)
    status = server.container.wait(timeout=STARTUP_TIMEOUT)["StatusCode"]
    logs = normalized_logs(server.container)
    assert status == 2, logs
    assert "usage: postgres-mcp" in logs, logs
    assert "error: argument --allowed-hosts: invalid entry '*': wildcards are only supported as the port (NAME:*)" in logs, logs


def test_compose_service_name_needs_allowed_hosts(start_server: StartServer, client_container: Any) -> None:
    """The README's Compose example: another container connects to http://postgres-mcp-server:8000/sse."""
    request = {"host": SERVICE_NAME, "port": 8000, "path": "/sse"}  # the client sends Host: postgres-mcp-server:8000

    # Without --allowed-hosts, the service name is rejected.
    server = start_server("--access-mode=restricted", "--transport=sse", publish=False, alias=SERVICE_NAME)
    try:
        assert not server.container.attrs["HostConfig"]["PortBindings"]
        [result] = request_from_network(client_container, [request])
        assert result == {"status": 421, "body": "Invalid Host header"}, result
    finally:
        remove_container(server.container)

    # The README's command: no published port, reachable by the service name from the network.
    server = start_server("--access-mode=restricted", "--transport=sse", "--allowed-hosts=postgres-mcp-server", publish=False, alias=SERVICE_NAME)
    assert not server.container.attrs["HostConfig"]["PortBindings"]
    results = request_from_network(
        client_container,
        [
            request,
            {**request, "headers": {"Host": SERVICE_NAME}},
            {**request, "headers": {"Host": "other-service:8000"}},
        ],
    )
    assert [r.get("status") for r in results] == [200, 200, 421], results

    logs = normalized_logs(server.container)
    assert "Successfully connected to database" in logs, logs
    assert expected_warning("sse", restricted=True) in logs, logs
    assert "Accepting Host headers: 127.0.0.1:*, localhost:*, [::1]:*, postgres-mcp-server, postgres-mcp-server:*" in logs, logs


def test_streamable_http_unreachable_without_host_option(start_server: StartServer) -> None:
    server = start_server("--transport=streamable-http")
    port = server.port
    assert port is not None
    logs = normalized_logs(server.container)
    assert "Uvicorn running on http://localhost:8000" in logs, logs
    assert "SSE transport detected" not in logs
    assert "not a loopback address" not in logs  # bound to loopback inside the container: no warning
    for _ in range(3):
        with pytest.raises((OSError, http.client.HTTPException)):
            http_request(port, "/mcp", f"localhost:{port}", method="POST", body=INITIALIZE_REQUEST, headers=STREAMABLE_HTTP_HEADERS)
        time.sleep(0.5)


def test_streamable_http_reachable_with_host_option(start_server: StartServer) -> None:
    server = start_server("--transport=streamable-http", "--streamable-http-host=0.0.0.0")
    port = server.port
    assert port is not None
    logs = normalized_logs(server.container)
    assert "Uvicorn running on http://0.0.0.0:8000" in logs, logs
    assert expected_warning("streamable-http", restricted=False) in logs, logs

    resp = http_request(port, "/mcp", f"localhost:{port}", method="POST", body=INITIALIZE_REQUEST, headers=STREAMABLE_HTTP_HEADERS)
    assert resp.status == 200, resp
    assert '"result"' in resp.body and '"serverInfo"' in resp.body, resp

    resp = http_request(port, "/mcp", "mcp.example.com", method="POST", body=INITIALIZE_REQUEST, headers=STREAMABLE_HTTP_HEADERS)
    assert (resp.status, resp.body) == (421, "Invalid Host header"), resp
