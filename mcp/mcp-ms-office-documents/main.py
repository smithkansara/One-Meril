from fastmcp import FastMCP
from pydantic import BaseModel, Field
from typing import Annotated, List, Dict, Optional, Literal
from xlsx_tools import markdown_to_excel, create_excel_with_report
from docx_tools import markdown_to_word
from pptx_tools import create_presentation
from pdf_tools import markdown_to_pdf
from email_tools import create_eml
from email_tools.dynamic_email_tools import register_email_template_tools_from_yaml
from report_builder import create_report_in_same_format, SUPPORTED_FORMATS
from pathlib import Path
import logging
import asyncio
import functools
from config import get_config
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("MCP Office Documents")

# Initialize config and logging
config = get_config()
logger = logging.getLogger(__name__)

# Look for dynamic email templates in production and local locations.
# Production (container): /app/config/email_templates.yaml
# Local development: <project_root>/config/email_templates.yaml
APP_CONFIG_PATH = Path("/app/config") / "email_templates.yaml"
LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "email_templates.yaml"

# Prefer the production path when present, otherwise fall back to local config.
_primary_yaml = None
for candidate in (APP_CONFIG_PATH, LOCAL_CONFIG_PATH):
    if candidate.exists():
        _primary_yaml = candidate
        logger.info("[dynamic-email] Found email templates file: %s", candidate)
        break

if _primary_yaml:
    try:
        register_email_template_tools_from_yaml(mcp, _primary_yaml)
    except Exception as e:
        logger.exception("[dynamic-email] Failed to register email templates from %s: %s", _primary_yaml, e)
else:
    logger.info(
        "[dynamic-email] No dynamic email templates file found at /app/config/email_templates.yaml or config/email_templates.yaml - skipping"
    )

class PowerPointSlide(BaseModel):
    """PowerPoint slide - can be title, section, or content slide based on slide_type."""
    slide_type: Literal["title", "section", "content"] = Field(description="Type of slide: 'title' for presentation opening, 'section' for dividers, 'content' for slide with bullet points")
    slide_title: str = Field(description="Title text for the slide")

    # Optional fields based on slide type
    author: Optional[str] = Field(default="", description="Author name for title slides - appears in subtitle placeholder. Leave empty for section/content slides.")
    slide_text: Optional[List[Dict]] = Field(
        default=None,
        description="Array of bullet points for content slides. Each bullet point must have 'text' (string) and 'indentation_level' (integer 1-5). Leave empty/null for title and section slides."
    )

@mcp.tool(
    name="create_excel_from_markdown",
    description="Converts markdown content with tables and formulas to Excel (.xlsx) format.",
    tags={"excel", "spreadsheet", "data"},
    annotations={"title": "Markdown to Excel Converter"}
)
async def create_excel_document(
    markdown_content: Annotated[str, Field(description="Markdown content containing tables, headers, and formulas. Use T1.B[0] for cross-table references and B[0] for current row references. ALWAYS use [0], [1], [2] notation, NEVER use absolute row numbers like B2, B3. Do NOT count table header as first row, first row has index [0]. Supports cell formatting: **bold**, *italic*.")]
) -> str:
    """
    Converts markdown to Excel with advanced formula support.
    """

    logger.info("Converting markdown to Excel document")

    try:
        result = await asyncio.to_thread(markdown_to_excel, markdown_content)
        logger.info("Excel document uploaded successfully")
        return result
    except Exception as e:
        logger.error(f"Error creating Excel document: {e}")
        return f"Error creating Excel document: {str(e)}"

@mcp.tool(
    name="create_word_from_markdown",
    description="Converts markdown content to Word (.docx) format. Supports headers, tables, lists, formatting, hyperlinks, and block quotes.",
    tags={"word", "document", "text", "legal", "contract"},
    annotations={"title": "Markdown to Word Converter"}
)
async def create_word_document(
    markdown_content: Annotated[str, Field(description="Markdown content. For LEGAL CONTRACTS use numbered lists (1., 2., 3.) for sections and nested lists for provisions - DO NOT use headers (except for contract title). For other documents use headers (# ## ###).")]
) -> str:
    """
    Converts markdown to professionally formatted Word document.

    """

    logger.info("Converting markdown to Word document")

    try:
        result = await asyncio.to_thread(markdown_to_word, markdown_content)
        logger.info("Word document uploaded successfully")
        return result
    except Exception as e:
        logger.error(f"Error creating Word document: {e}")
        return f"Error creating Word document: {str(e)}"

@mcp.tool(
    name="create_powerpoint_presentation",
    description="Creates PowerPoint presentations with professional templates using structured slide models.",
    tags={"powerpoint", "presentation", "slides"},
    annotations={"title": "PowerPoint Presentation Creator"}
)
async def create_powerpoint_presentation(
    slides: List[PowerPointSlide],
    format: Annotated[Literal["4:3", "16:9"], Field(
        default="4:3",
        description="Presentation formating: '4:3' for traditional or '16:9' for widescreen"
    )]
) -> str:
    """Creates PowerPoint presentations with structured slide models and professional templates."""

    logger.info(f"Creating PowerPoint presentation with {len(slides)} slides in {format} format")

    try:
        slides_data = [slide.model_dump() for slide in slides]
        result = await asyncio.to_thread(create_presentation, slides_data, format)
        logger.info(f"PowerPoint presentation created: {result}")
        return result
    except Exception as e:
        logger.error(f"Error creating PowerPoint presentation: {e}")
        return f"Error creating PowerPoint presentation: {str(e)}"

@mcp.tool(
    name="create_pdf_from_markdown",
    description="Converts markdown content to PDF format. Supports headers, tables, lists, formatting, hyperlinks, and block quotes. Use this for final, non-editable documents that need to be shared or printed.",
    tags={"pdf", "document", "text", "final", "print"},
    annotations={"title": "Markdown to PDF Converter"}
)
async def create_pdf_document(
    markdown_content: Annotated[str, Field(description="Markdown content. For LEGAL CONTRACTS use numbered lists (1., 2., 3.) for sections and nested lists for provisions - DO NOT use headers (except for contract title). For other documents use headers (# ## ###).")],
    page_size: Annotated[Literal["letter", "A4"], Field(
        default="letter",
        description="Page size: 'letter' for US Letter (8.5x11 inches) or 'A4' for international A4 size"
    )] = "letter"
) -> str:
    """
    Converts markdown to professionally formatted PDF document.
    PDF documents are final/non-editable format, ideal for distribution.
    """

    logger.info(f"Converting markdown to PDF document with {page_size} page size")

    try:
        result = await asyncio.to_thread(markdown_to_pdf, markdown_content, page_size)
        logger.info("PDF document uploaded successfully")
        return result
    except Exception as e:
        logger.error(f"Error creating PDF document: {e}")
        return f"Error creating PDF document: {str(e)}"

@mcp.tool(
    name="create_email_draft",
    description="Creates an email draft in EML format with HTML content using preset professional styling.",
    tags={"email", "eml", "communication"},
    annotations={"title": "Email Draft Creator"}
)
async def create_email_draft(
    content: Annotated[str, Field(description="BODY CONTENT ONLY - Do NOT include HTML structure tags like <html>, <head>, <body>, or <style>. Do NOT include any CSS styling. Use <p> for greetings and for signatures, never headers. Use <h2> for section headers (will be bold), <h3> for subsection headers (will be underlined). HTML tags allowed: <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <div>.")],
    subject: Annotated[str, Field(description="Email subject line")],
    to: Annotated[Optional[List[str]], Field(description="List of recipient email addresses", default=None)],
    cc: Annotated[Optional[List[str]], Field(description="List of CC recipient email addresses", default=None)],
    bcc: Annotated[Optional[List[str]], Field(description="List of BCC recipient email addresses", default=None)],
    priority: Annotated[str, Field(description="Email priority: 'low', 'normal', or 'high'", default="normal")],
    language: Annotated[str, Field(description="Language code for proofreading in Outlook (e.g., 'cs-CZ' for Czech, 'en-US' for English, 'de-DE' for German, 'sk-SK' for Slovak)", default="cs-CZ")]
) -> str:
    """
    Creates professional email drafts in EML format with preset styling and language settings.
    """

    logger.info(f"Creating email draft with subject: {subject}")

    try:
        result = await asyncio.to_thread(
            functools.partial(create_eml,
                to=to,
                cc=cc,
                bcc=bcc,
                re=subject,
                content=content,
                priority=priority,
                language=language
            )
        )
        logger.info(f"Email draft created: {result}")
        return result
    except Exception as e:
        logger.error(f"Error creating email draft: {e}")
        return f"Error creating email draft: {str(e)}"

@mcp.tool(
    name="create_report_with_source_data",
    description=(
        "ALWAYS call this tool at the end of any file analysis or web-data analysis workflow. "
        "It combines the original source data (uploaded file content OR web-fetched data) with the full analysis report "
        "into a single downloadable file in the SAME FORMAT as the source. "
        "Supports: xlsx (two sheets), docx (two sections), pdf (two sections), pptx (two slide sections), csv, txt. "
        "For uploaded files: pass the file content and the file's extension as file_format. "
        "For web data: pass the scraped/fetched content and choose docx, pdf, or xlsx as the output format. "
        "The user receives one file containing both their data and the analysis — ready to download locally."
    ),
    tags={"report", "analysis", "download", "excel", "word", "pdf", "pptx", "csv"},
    annotations={"title": "Create Report With Source Data"}
)
async def create_report_with_source_data(
    data_content: Annotated[str, Field(
        description=(
            "The original source data as text or markdown. "
            "For uploaded files: pass the full extracted content of the file. "
            "For web data: pass the full scraped/fetched text or table content. "
            "This becomes the first section/sheet labelled by data_section_title."
        )
    )],
    report_markdown: Annotated[str, Field(
        description=(
            "The analysis/report in markdown format (headers, bullet points, tables). "
            "This becomes the second section/sheet labelled by report_section_title."
        )
    )],
    file_format: Annotated[str, Field(
        description=(
            "Output file format — MUST match the original file's extension when processing an uploaded file. "
            f"Accepted values: {', '.join(sorted(SUPPORTED_FORMATS))}. "
            "Examples: 'xlsx' for Excel, 'docx' for Word, 'pdf' for PDF, 'pptx' for PowerPoint, "
            "'csv' for CSV, 'txt' for plain text. "
            "For web data with no source file, choose the most suitable format (xlsx or docx recommended)."
        )
    )],
    data_section_title: Annotated[str, Field(
        default="Source Data",
        description="Label for the original data section or sheet. Defaults to 'Source Data'."
    )] = "Source Data",
    report_section_title: Annotated[str, Field(
        default="Analysis Report",
        description="Label for the analysis report section or sheet. Defaults to 'Analysis Report'."
    )] = "Analysis Report",
) -> str:
    """Builds a combined file (data + report) in the requested format and returns a download URL."""

    logger.info(
        "Building combined report: format=%s data_len=%d report_len=%d",
        file_format, len(data_content), len(report_markdown),
    )

    try:
        result = await asyncio.to_thread(
            create_report_in_same_format,
            data_content,
            report_markdown,
            file_format,
            data_section_title,
            report_section_title,
        )
        logger.info("Combined report created: %s", result)
        return result
    except Exception as e:
        logger.error("Error creating combined report: %s", e)
        return f"Error creating combined report: {str(e)}"


@mcp.custom_route("/build-report", methods=["POST"])
async def build_report_route(request: Request):
    """Plain HTTP endpoint so other services (e.g. mcp-analytics) can combine
    original source data + an analysis report into a single downloadable file
    in the same format as the source, without going through the MCP protocol.

    Body (JSON): data_content, report_markdown, file_format,
                 data_section_title?, report_section_title?
    Returns: {"url": <download_url>, "file_format": <fmt>}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    data_content = body.get("data_content", "") or ""
    report_markdown = body.get("report_markdown", "") or ""
    file_format = (body.get("file_format") or "xlsx").lower()
    data_section_title = body.get("data_section_title", "Source Data")
    report_section_title = body.get("report_section_title", "Analysis Report")
    charts = body.get("charts") or []
    charts_heading = body.get("charts_heading") or ""

    if not data_content and not report_markdown:
        return JSONResponse(
            {"error": "at least one of data_content or report_markdown is required"},
            status_code=400,
        )

    try:
        url = await asyncio.to_thread(
            create_report_in_same_format,
            data_content,
            report_markdown,
            file_format,
            data_section_title,
            report_section_title,
            charts,
            charts_heading,
        )
        logger.info("[build-report] created %s file: %s", file_format, url)
        return JSONResponse({"url": url, "file_format": file_format})
    except Exception as e:
        logger.exception("[build-report] failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8958,
        log_level=config.logging.mcp_level_str,
        path="/mcp"
    )
