"""Build synthetic image/scan inputs for the P04 OCR smoke check."""

import shutil
import subprocess
import tempfile
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/attachments/DOC-GC-A-TAXI-001.pdf"
OUTPUT_DIR = ROOT / "data/raw/scans"
CLEAR_IMAGE = OUTPUT_DIR / "DOC-GC-A-TAXI-001-clear.png"
LOW_QUALITY_PDF = OUTPUT_DIR / "DOC-GC-A-TAXI-001-low-quality.pdf"


def run_pdftoppm(*args: str) -> None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        raise RuntimeError("pdftoppm is required to build scan fixtures")
    subprocess.run([executable, *args], check=True, capture_output=True)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        clear_prefix = temp / "clear"
        low_prefix = temp / "low"
        run_pdftoppm("-f", "1", "-l", "1", "-singlefile", "-r", "150", "-png",
                     str(SOURCE), str(clear_prefix))
        run_pdftoppm("-f", "1", "-l", "1", "-singlefile", "-r", "72", "-jpeg",
                     "-jpegopt", "quality=42", str(SOURCE), str(low_prefix))
        shutil.copyfile(clear_prefix.with_suffix(".png"), CLEAR_IMAGE)

        pdf = canvas.Canvas(str(LOW_QUALITY_PDF), pagesize=A4, invariant=1)
        pdf.setTitle("Synthetic low-quality scanned taxi receipt")
        pdf.setAuthor("Agentic RAG expense pre-review project")
        pdf.drawImage(str(low_prefix.with_suffix(".jpg")), 0, 0, width=A4[0], height=A4[1])
        pdf.showPage()
        pdf.save()

    print(CLEAR_IMAGE.relative_to(ROOT))
    print(LOW_QUALITY_PDF.relative_to(ROOT))


if __name__ == "__main__":
    main()
