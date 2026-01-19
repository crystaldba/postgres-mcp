from typing import Any
from typing import List

import mcp.types as types

ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]


def format_text_response(text: Any) -> ResponseType:
    """Format a text response."""
    return [types.TextContent(type="text", text=str(text))]


def format_error_response(error: str) -> ResponseType:
    """Format an error response."""
    return format_text_response(f"Error: {error}")
