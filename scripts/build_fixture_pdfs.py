"""Build deterministic, page-addressable synthetic PDF fixtures from authored Markdown."""

import argparse
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (ROOT / "data" / "raw" / "policies", ROOT / "data" / "raw" / "attachments")
PAGE_MARKER = re.compile(r"^<!-- PAGE (\d+) -->$", re.MULTILINE)
FONT = "STSong-Light"
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 48


def read_pages(source: Path) -> list[str]:
    content = source.read_text(encoding="utf-8")
    markers = list(PAGE_MARKER.finditer(content))
    if not markers or [int(item.group(1)) for item in markers] != list(range(1, len(markers) + 1)):
        raise ValueError(f"page markers must start at 1 and be sequential: {source}")
    pages = [
        content[
            item.end() : markers[index + 1].start() if index + 1 < len(markers) else None
        ].strip()
        for index, item in enumerate(markers)
    ]
    if any(not page for page in pages):
        raise ValueError(f"empty authored page: {source}")
    return pages


def wrapped_lines(text: str, font_size: float, max_width: float | None = None) -> list[str]:
    lines: list[str] = []
    current = ""
    max_width = max_width or PAGE_WIDTH - 2 * MARGIN
    for character in text:
        candidate = current + character
        if current and pdfmetrics.stringWidth(candidate, FONT, font_size) > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def draw_table(pdf: canvas.Canvas, rows: list[str], y: float, document_id: str, page: int) -> float:
    cells = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]
    cells = [
        row
        for row in cells
        if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in row)
    ]
    column_count = max(len(row) for row in cells)
    column_width = (PAGE_WIDTH - 2 * MARGIN) / column_count
    for row_index, row in enumerate(cells):
        row += [""] * (column_count - len(row))
        wrapped = [wrapped_lines(cell, 8, column_width - 12) for cell in row]
        row_height = max(len(lines) for lines in wrapped) * 12 + 10
        if y - row_height < 75:
            raise ValueError(f"authored table overflow on {document_id} page {page}")
        if row_index == 0:
            pdf.setFillColor(colors.HexColor("#E8EEF5"))
            pdf.rect(MARGIN, y - row_height, PAGE_WIDTH - 2 * MARGIN, row_height, fill=1, stroke=0)
        pdf.setStrokeColor(colors.HexColor("#94A3B8"))
        pdf.rect(MARGIN, y - row_height, PAGE_WIDTH - 2 * MARGIN, row_height, fill=0, stroke=1)
        for column in range(1, column_count):
            x = MARGIN + column * column_width
            pdf.line(x, y, x, y - row_height)
        pdf.setFillColor(colors.HexColor("#0F172A"))
        pdf.setFont(FONT, 8)
        for column, lines in enumerate(wrapped):
            x = MARGIN + column * column_width + 6
            for line_index, visual_line in enumerate(lines):
                pdf.drawString(x, y - 15 - line_index * 12, visual_line)
        y -= row_height
    return y - 10


def draw_authored_page(
    pdf: canvas.Canvas, text: str, document_id: str, page: int, total: int
) -> None:
    layout = re.match(r"<!-- LAYOUT (TABLE|RECEIPT|LETTER) -->\n", text)
    if layout:
        text = text[layout.end() :]
    pdf.setStrokeColor(colors.HexColor("#CBD5E1"))
    pdf.line(MARGIN, PAGE_HEIGHT - 43, PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 43)
    pdf.setFillColor(colors.HexColor("#475569"))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(MARGIN, PAGE_HEIGHT - 31, document_id)

    if document_id.startswith("DOC-") and layout and layout.group(1) == "RECEIPT":
        pdf.setStrokeColor(colors.HexColor("#64748B"))
        pdf.rect(MARGIN - 12, PAGE_HEIGHT - 460, PAGE_WIDTH - 2 * MARGIN + 24, 405, fill=0)
    elif document_id.startswith("DOC-") and layout and layout.group(1) == "LETTER":
        pdf.setFillColor(colors.HexColor("#E8EEF5"))
        pdf.rect(MARGIN - 12, PAGE_HEIGHT - 112, PAGE_WIDTH - 2 * MARGIN + 24, 56, fill=1, stroke=0)
    elif document_id.startswith("DOC-") and not layout:
        pdf.setFillColor(colors.HexColor("#F8FAFC"))
        pdf.setStrokeColor(colors.HexColor("#CBD5E1"))
        pdf.roundRect(MARGIN - 12, PAGE_HEIGHT - 246, PAGE_WIDTH - 2 * MARGIN + 24, 190, 8, fill=1)

    y = PAGE_HEIGHT - 82
    raw_lines = text.splitlines()
    line_index = 0
    while line_index < len(raw_lines):
        raw_line = raw_lines[line_index]
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|"):
            table_rows: list[str] = []
            while line_index < len(raw_lines):
                candidate = raw_lines[line_index].strip()
                if not (candidate.startswith("|") and candidate.endswith("|")):
                    break
                table_rows.append(candidate)
                line_index += 1
            y = draw_table(pdf, table_rows, y, document_id, page)
            continue
        line_index += 1
        if not line:
            y -= 11
            continue
        if line.startswith("### "):
            font_size, step = 12, 20
            line = line[4:]
        elif line.startswith("## "):
            font_size, step = 14, 23
            line = line[3:]
        elif line.startswith("# "):
            font_size, step = 18, 28
            line = line[2:]
        else:
            font_size, step = 10, 18
        pdf.setFillColor(
            colors.HexColor("#1E3A5F") if font_size > 10 else colors.HexColor("#0F172A")
        )
        pdf.setFont(FONT, font_size)
        for visual_line in wrapped_lines(line, font_size):
            if y < 75:
                raise ValueError(f"authored content overflow on {document_id} page {page}")
            pdf.drawString(MARGIN, y, visual_line)
            y -= step

    pdf.setStrokeColor(colors.HexColor("#CBD5E1"))
    pdf.line(MARGIN, 58, PAGE_WIDTH - MARGIN, 58)
    pdf.setFillColor(colors.HexColor("#64748B"))
    pdf.setFont(FONT, 9)
    pdf.drawString(MARGIN, 42, "项目合成资料 - 不代表真实企业制度或票据")
    pdf.setFont("Helvetica", 9)
    pdf.drawRightString(PAGE_WIDTH - MARGIN, 42, f"{page} / {total}")
    pdf.showPage()


def build_pdf(source: Path) -> Path:
    pages = read_pages(source)
    output = source.with_suffix(".pdf")
    pdf = canvas.Canvas(str(output), pagesize=A4, invariant=1)
    pdf.setTitle(source.stem)
    pdf.setAuthor("Agentic RAG expense pre-review project")
    for index, page in enumerate(pages, start=1):
        draw_authored_page(pdf, page, source.stem, index, len(pages))
    pdf.save()
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    args = parser.parse_args()
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))
    directories = (args.source_dir.resolve(),) if args.source_dir else SOURCE_DIRS
    sources = sorted(source for directory in directories for source in directory.glob("*.md"))
    if not sources:
        raise ValueError("no authored PDF fixtures found")
    for source in sources:
        print(build_pdf(source).relative_to(ROOT))
    print(f"built {len(sources)} PDF fixtures")


if __name__ == "__main__":
    main()
