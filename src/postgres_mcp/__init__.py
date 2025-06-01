import asyncio
import sys

from dotenv import load_dotenv

from . import server
from . import top_queries

load_dotenv()


def main():
    """Main entry point for the package."""
    # As of version 3.3.0 Psycopg on Windows is not compatible with the default
    # ProactorEventLoop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Check if we're already in an event loop
    try:
        asyncio.get_running_loop()
        # If we get here, we're already in a running loop
        raise RuntimeError(
            "Cannot run server.main() from within an event loop. "
            "Call server.main() directly as an async function."
        )
    except RuntimeError as e:
        if "no running event loop" in str(e).lower():
            # No running loop, safe to use asyncio.run()
            return asyncio.run(server.main())
        else:
            # Re-raise if it's the error about being in a loop
            raise


# Optionally expose other important items at package level
__all__ = [
    "main",
    "server",
    "top_queries",
]
