"""Excel formatter for query results."""

import os
import tempfile
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def format_to_excel(rows: list[dict], columns: list[str], output_dir: str | None = None) -> str:
    """Format query result rows to an Excel file.

    Args:
        rows: List of row dictionaries from query results.
        columns: List of column names.
        output_dir: Output directory (default: system temp / postgres-mcp-results).

    Returns:
        Path to the created Excel file.
    """
    from openpyxl import Workbook

    if output_dir is None:
        output_dir = os.path.join(tempfile.gettempdir(), "postgres-mcp-results")

    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"query_{timestamp}.xlsx"
    filepath = os.path.join(output_dir, filename)

    wb = Workbook()
    ws = wb.active
    ws.title = "Query Results"

    # Write header row
    ws.append(columns)

    # Write data rows
    for row in rows:
        ws.append([row.get(col) for col in columns])

    # Auto-adjust column widths
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if cell.value is not None:
                    max_length = max(max_length, len(str(cell.value)))
            except Exception:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column_letter].width = adjusted_width

    wb.save(filepath)
    return filepath
