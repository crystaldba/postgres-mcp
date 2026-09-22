"""ASGI worker for keyed TenantCloud Restate workflows."""

from __future__ import annotations

import asyncio
import os

from hypercorn.asyncio import serve
from hypercorn.config import Config

from .server import build_runtime
from .server import build_tenantcloud_auth_gate
from .tenantcloud_delivery import TenantCloudDeliveryCoordinator
from .tenantcloud_delivery import build_restate_app


async def _serve() -> None:
    runtime = await build_runtime()
    coordinator = TenantCloudDeliveryCoordinator(
        store=runtime.store,
        service=runtime.service,
        auth=build_tenantcloud_auth_gate(),
        max_attempts=int(os.environ.get("OUTBOUND_MAX_ATTEMPTS", "5")),
    )
    config = Config()
    config.bind = [
        os.environ.get(
            "OUTBOUND_TENANTCLOUD_RESTATE_WORKER_BIND",
            "127.0.0.1:9083",
        )
    ]
    config.accesslog = None
    try:
        await serve(build_restate_app(coordinator), config, mode="asgi")
    finally:
        await runtime.pool.close()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
