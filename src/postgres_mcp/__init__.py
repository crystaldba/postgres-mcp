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
    # See: https://www.psycopg.org/psycopg3/docs/advanced/async.html#async
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Simply run the server main function
    # All shutdown logic is handled inside server.main()
    try:
        return asyncio.run(server.main())
    except KeyboardInterrupt:
        # This might not be reached if server.main() handles signals properly
        print("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error running server: {e}")
        sys.exit(1)


# Optionally expose other important items at package level
__all__ = [
    "main",
    "server",
    "top_queries",
]
