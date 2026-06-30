import io
import os
import uuid
import logging
from typing import List
from openpyxl import Workbook
from openpyxl.styles import Font

from .helpers import parse_table, add_table_to_sheet

logger = logging.getLogger(__name__)

OUTPUT_DIR = "/app/output"


def _populate_sheet_from_markdown(ws, markdown_content: str) -> None:
    """Parse markdown (headers, tables, plain text) and populate a worksheet."""
    lines: List[str] = markdown_content.split('\n')
    current_row = 1
    table_counter = 1
    table_positions = {}
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        if line.startswith('#'):
            header_level = len(line) - len(line.lstrip('#'))
            header_text = line.lstrip('#').strip()
            cell = ws.cell(row=current_row, column=1)
            cell.value = header_text
            if header_level == 1:
                cell.font = Font(size=16, bold=True, color="2F5597")
            elif header_level == 2:
                cell.font = Font(size=14, bold=True, color="4472C4")
            else:
                cell.font = Font(size=12, bold=True)
            current_row += 2
            i += 1

        elif line.startswith('|'):
            table_data, i = parse_table(lines, i)
            if table_data:
                table_key = f"T{table_counter}"
                table_positions[table_key] = current_row
                current_row = add_table_to_sheet(table_data, ws, current_row, table_positions)
                table_counter += 1
        else:
            cell = ws.cell(row=current_row, column=1)
            cell.value = line
            current_row += 1
            i += 1


def create_excel_with_report(
    data_content: str,
    report_markdown: str,
    data_sheet_name: str = "Data",
    report_sheet_name: str = "Analysis Report",
) -> str:
    """Create a two-sheet Excel: 'Data' sheet with original content, 'Analysis Report' sheet with report.

    Returns the public download URL.
    """
    logger.info("Creating Excel workbook with '%s' + '%s' sheets", data_sheet_name, report_sheet_name)

    wb = Workbook()

    # First sheet: original data
    ws_data = wb.active
    ws_data.title = data_sheet_name[:31]
    _populate_sheet_from_markdown(ws_data, data_content)

    # Second sheet: analysis report
    ws_report = wb.create_sheet(title=report_sheet_name[:31])
    _populate_sheet_from_markdown(ws_report, report_markdown)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"analysis_{uuid.uuid4().hex}.xlsx"
    file_path = os.path.join(OUTPUT_DIR, filename)

    try:
        wb.save(file_path)
        public_url = f"https://chatgpt.e-meril.in/files/{filename}"
        logger.info("Excel with report saved: %s", public_url)
        return public_url
    except Exception as e:
        logger.error("Error saving Excel with report: %s", str(e), exc_info=True)
        return f"Error saving Excel with report: {str(e)}"
