from fileinput import filename
import io
import logging
import os
import re
import uuid
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

from .helpers import (
    parse_inline_formatting_pdf,
    parse_table,
    process_list_items_pdf,
)

logger = logging.getLogger(__name__)


def markdown_to_pdf(markdown_content, page_size="letter"):
    """Convert Markdown to PDF document."""
    logger.info(f"Starting markdown_to_pdf conversion with page_size={page_size}")
    
    OUTPUT_DIR = "/app/output"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Generate filename
    filename = f"document_{uuid.uuid4().hex}.pdf"
    file_path = os.path.join(OUTPUT_DIR, filename)

    # Set page size
    pagesize = letter if page_size == "letter" else A4
    
    # Create PDF document
    doc = SimpleDocTemplate(
        file_path,
        pagesize=pagesize,
        rightMargin=72,
        leftMargin=72,
        topMargin=72,
        bottomMargin=72,
    )

    # Container for PDF elements
    story = []
    
    # Define styles
    styles = getSampleStyleSheet()
    
    # Custom styles
    styles.add(ParagraphStyle(
        name='CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=30,
        alignment=TA_LEFT
    ))
    
    styles.add(ParagraphStyle(
        name='CustomHeading1',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#2c2c2c'),
        spaceAfter=12,
        spaceBefore=12,
    ))
    
    styles.add(ParagraphStyle(
        name='CustomHeading2',
        parent=styles['Heading2'],
        fontSize=16,
        textColor=colors.HexColor('#2c2c2c'),
        spaceAfter=10,
        spaceBefore=10,
    ))
    
    styles.add(ParagraphStyle(
        name='CustomHeading3',
        parent=styles['Heading3'],
        fontSize=14,
        textColor=colors.HexColor('#2c2c2c'),
        spaceAfter=8,
        spaceBefore=8,
    ))
    
    styles.add(ParagraphStyle(
        name='CustomBody',
        parent=styles['BodyText'],
        fontSize=11,
        leading=14,
        alignment=TA_JUSTIFY,
        spaceAfter=12,
    ))
    
    styles.add(ParagraphStyle(
        name='CustomQuote',
        parent=styles['BodyText'],
        fontSize=11,
        leading=14,
        leftIndent=36,
        rightIndent=36,
        textColor=colors.HexColor('#555555'),
        borderColor=colors.HexColor('#cccccc'),
        borderWidth=1,
        borderPadding=10,
        spaceAfter=12,
        spaceBefore=12,
    ))

    # Split content into lines
    lines = markdown_content.split('\n')
    i = 0

    # Parsing counters
    headers_count = 0
    tables_count = 0
    ordered_lists = 0
    unordered_lists = 0
    quotes_count = 0
    paragraphs_count = 0

    try:
        while i < len(lines):
            line = lines[i]

            # Handle empty lines (spacing)
            if not line.strip():
                empty_line_count = 0
                while i < len(lines) and not lines[i].strip():
                    empty_line_count += 1
                    i += 1
                
                if empty_line_count >= 1:
                    story.append(Spacer(1, 0.2 * inch))
                continue

            line = line.strip()

            # Headers
            if line.startswith('#'):
                header_level = len(line) - len(line.lstrip('#'))
                header_text = line.lstrip('#').strip()
                
                if header_level == 1:
                    style = styles['CustomHeading1']
                elif header_level == 2:
                    style = styles['CustomHeading2']
                elif header_level == 3:
                    style = styles['CustomHeading3']
                else:
                    style = styles['CustomHeading3']
                
                formatted_text = parse_inline_formatting_pdf(header_text)
                story.append(Paragraph(formatted_text, style))
                headers_count += 1
                logger.debug(f"Header (level {header_level}): {header_text}")
                i += 1

            # Tables
            elif line.startswith('|'):
                table_data, i = parse_table(lines, i)
                if table_data:
                    # Create table
                    pdf_table = Table(table_data)
                    pdf_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, 0), 12),
                        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                        ('GRID', (0, 0), (-1, -1), 1, colors.black)
                    ]))
                    story.append(pdf_table)
                    story.append(Spacer(1, 0.2 * inch))
                    tables_count += 1
                    logger.debug(f"Added table with {len(table_data)} rows")

            # Ordered lists
            elif re.match(r'^\d+\.\s+', line):
                list_items, i = process_list_items_pdf(lines, i, True)
                for item_text, level in list_items:
                    formatted_text = parse_inline_formatting_pdf(item_text)
                    indent = level * 20
                    list_style = ParagraphStyle(
                        'ListItem',
                        parent=styles['CustomBody'],
                        leftIndent=indent,
                        bulletIndent=indent - 10,
                    )
                    story.append(Paragraph(f"• {formatted_text}", list_style))
                ordered_lists += 1
                story.append(Spacer(1, 0.1 * inch))

            # Unordered lists
            elif re.match(r'^[-*+]\s+', line):
                list_items, i = process_list_items_pdf(lines, i, False)
                for item_text, level in list_items:
                    formatted_text = parse_inline_formatting_pdf(item_text)
                    indent = level * 20
                    list_style = ParagraphStyle(
                        'ListItem',
                        parent=styles['CustomBody'],
                        leftIndent=indent,
                        bulletIndent=indent - 10,
                    )
                    story.append(Paragraph(f"• {formatted_text}", list_style))
                unordered_lists += 1
                story.append(Spacer(1, 0.1 * inch))

            # Horizontal rules
            elif line.startswith('---') or line.startswith('***'):
                story.append(Spacer(1, 0.1 * inch))
                story.append(PageBreak())
                i += 1

            # Block quotes
            elif line.startswith('>'):
                quote_text = line[1:].strip()
                formatted_text = parse_inline_formatting_pdf(quote_text)
                story.append(Paragraph(formatted_text, styles['CustomQuote']))
                quotes_count += 1
                i += 1

            # Regular paragraphs
            else:
                formatted_text = parse_inline_formatting_pdf(line)
                story.append(Paragraph(formatted_text, styles['CustomBody']))
                paragraphs_count += 1
                i += 1

    except Exception as e:
        logger.error(f"Error in parsing markdown: {e}", exc_info=True)
        return f"Error in parsing markdown: {e}"

    # Build PDF
    try:
        logger.info("Building PDF document")
        doc.build(story)

        # Build public URL
        public_url = f"http://localhost:8000/files/{filename}"

        logger.info(
            f"PDF document saved and available at: {public_url} "
            f"(headers={headers_count}, tables={tables_count}, ordered_lists={ordered_lists}, "
            f"unordered_lists={unordered_lists}, quotes={quotes_count}, paragraphs={paragraphs_count})"
        )
        
        return public_url

    except Exception as e:
        logger.error(f"Error building PDF document: {e}", exc_info=True)
        return f"Error building PDF document: {e}"