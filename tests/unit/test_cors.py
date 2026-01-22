"""Tests for CORS support in SSE transport."""

import multiprocessing
import socket
import time

import pytest
import requests
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from postgres_mcp.server import mcp


def find_free_port():
    """Find a free port to use for testing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def run_server(port: int, cors_origins: list[str]):
    """Run the MCP server in a subprocess."""
    import asyncio

    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    from postgres_mcp.server import mcp

    starlette_app = mcp.sse_app()
    if cors_origins:
        starlette_app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    config = uvicorn.Config(
        starlette_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


@pytest.fixture
def app_with_cors():
    """Create an SSE app with CORS middleware configured."""
    app = mcp.sse_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://claude.ai", "https://example.com"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    return app


@pytest.fixture
def app_without_cors():
    """Create an SSE app without CORS middleware."""
    return mcp.sse_app()


class TestCorsPreflightRequests:
    """Test CORS preflight (OPTIONS) requests."""

    def test_preflight_allowed_origin_returns_cors_headers(self, app_with_cors):
        """OPTIONS preflight from allowed origin should return CORS headers."""
        client = TestClient(app_with_cors, raise_server_exceptions=False)
        response = client.options(
            "/sse",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "https://claude.ai"
        assert "GET" in response.headers.get("access-control-allow-methods", "")

    def test_preflight_second_allowed_origin(self, app_with_cors):
        """OPTIONS preflight from second allowed origin should also work."""
        client = TestClient(app_with_cors, raise_server_exceptions=False)
        response = client.options(
            "/sse",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "https://example.com"

    def test_preflight_disallowed_origin_no_cors_header(self, app_with_cors):
        """OPTIONS preflight from non-allowed origin should not return CORS header."""
        client = TestClient(app_with_cors, raise_server_exceptions=False)
        response = client.options(
            "/sse",
            headers={
                "Origin": "https://malicious.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # The response may be 200 or 400, but should NOT have the allow-origin header
        assert response.headers.get("access-control-allow-origin") is None

    def test_preflight_messages_endpoint(self, app_with_cors):
        """OPTIONS preflight on /messages/ endpoint should also work."""
        client = TestClient(app_with_cors, raise_server_exceptions=False)
        response = client.options(
            "/messages/",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "https://claude.ai"
        assert "POST" in response.headers.get("access-control-allow-methods", "")


class TestCorsOnActualRequests:
    """Test CORS headers on actual (non-preflight) requests."""

    def test_post_request_with_allowed_origin(self, app_with_cors):
        """POST request from allowed origin should include CORS header in response."""
        client = TestClient(app_with_cors, raise_server_exceptions=False)
        # Send a POST to /messages/ - it will fail (no valid session) but CORS headers should be present
        response = client.post(
            "/messages/",
            headers={"Origin": "https://claude.ai"},
            content="test",
        )
        # Even if the request fails, CORS headers should be present
        assert response.headers.get("access-control-allow-origin") == "https://claude.ai"

    def test_post_request_with_disallowed_origin(self, app_with_cors):
        """POST request from non-allowed origin should not have CORS header."""
        client = TestClient(app_with_cors, raise_server_exceptions=False)
        response = client.post(
            "/messages/",
            headers={"Origin": "https://malicious.com"},
            content="test",
        )
        assert response.headers.get("access-control-allow-origin") is None


class TestCorsDisabled:
    """Test behavior when CORS middleware is not configured."""

    def test_preflight_without_cors_middleware(self, app_without_cors):
        """App without CORS middleware should not handle preflight specially."""
        client = TestClient(app_without_cors, raise_server_exceptions=False)
        response = client.options(
            "/sse",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.headers.get("access-control-allow-origin") is None

    def test_request_without_cors_middleware(self, app_without_cors):
        """App without CORS middleware should not return CORS headers."""
        client = TestClient(app_without_cors, raise_server_exceptions=False)
        response = client.post(
            "/messages/",
            headers={"Origin": "https://claude.ai"},
            content="test",
        )
        assert response.headers.get("access-control-allow-origin") is None


class TestCorsEndToEnd:
    """End-to-end tests that start an actual server process."""

    def test_server_with_cors_enabled(self):
        """Test that a real server with CORS returns correct headers."""
        port = find_free_port()
        cors_origins = ["https://claude.ai", "https://example.com"]

        # Start server in subprocess
        proc = multiprocessing.Process(target=run_server, args=(port, cors_origins))
        proc.start()

        try:
            # Wait for server to start
            for _ in range(50):
                try:
                    requests.options(f"http://127.0.0.1:{port}/sse", timeout=0.1)
                    break
                except requests.exceptions.ConnectionError:
                    time.sleep(0.1)
            else:
                pytest.fail("Server did not start in time")

            # Test allowed origin
            response = requests.options(
                f"http://127.0.0.1:{port}/sse",
                headers={
                    "Origin": "https://claude.ai",
                    "Access-Control-Request-Method": "GET",
                },
                timeout=5,
            )
            assert response.headers.get("access-control-allow-origin") == "https://claude.ai"

            # Test disallowed origin
            response = requests.options(
                f"http://127.0.0.1:{port}/sse",
                headers={
                    "Origin": "https://malicious.com",
                    "Access-Control-Request-Method": "GET",
                },
                timeout=5,
            )
            assert response.headers.get("access-control-allow-origin") is None

        finally:
            proc.terminate()
            proc.join(timeout=5)

    def test_server_without_cors(self):
        """Test that a server without CORS does not return CORS headers."""
        port = find_free_port()

        # Start server without CORS
        proc = multiprocessing.Process(target=run_server, args=(port, []))
        proc.start()

        try:
            # Wait for server to start
            for _ in range(50):
                try:
                    requests.options(f"http://127.0.0.1:{port}/sse", timeout=0.1)
                    break
                except requests.exceptions.ConnectionError:
                    time.sleep(0.1)
            else:
                pytest.fail("Server did not start in time")

            # Test that no CORS headers are returned
            response = requests.options(
                f"http://127.0.0.1:{port}/sse",
                headers={
                    "Origin": "https://claude.ai",
                    "Access-Control-Request-Method": "GET",
                },
                timeout=5,
            )
            assert response.headers.get("access-control-allow-origin") is None

        finally:
            proc.terminate()
            proc.join(timeout=5)
