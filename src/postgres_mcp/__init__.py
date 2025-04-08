import asyncio

from . import server
from . import top_queries


def main():
    """Main entry point for the package."""
    asyncio.run(server.main())


# Optionally expose other important items at package level
__all__ = [
    "main",
    "server",
    "top_queries",
]
