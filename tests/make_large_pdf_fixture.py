"""Deterministically build the 210-page PDF fixture for large-document routing.

Every page carries two lines of 120 ASCII "1" characters each (240 characters
per page) in the PDF built-in Helvetica font (never embedded); page content
streams are compressed (``pageCompression=1``). The dense text keeps the
native-text layer "substantive" so Xberg's per-page OCR quality gate does not
route pages into the document-level OCR fallback under the fast (all-visual
steps off) config. Generated with ReportLab, pinned ``reportlab==5.0.1``
(BSD licenses, matching the open-fixture rule in AGENTS.md :ref:`§14`).

The output is byte-stable across regenerations: ReportLab's invariant mode
fixes CreationDate, ModDate and trailer ``/ID`` using its public API. This
mirrors the fixed-timestamp convention of ``make_embedded_fixtures.py``.

This script is a maintenance/regeneration tool. Tests and the conversion
pipeline only read the committed fixture (``tests/test_example/large_210_pages.pdf``)
and never import reportlab.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "tests" / "test_example" / "large_210_pages.pdf"
PAGE_COUNT = 210


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(OUT), pagesize=A4, pageCompression=1, invariant=1)
    c.setTitle("large_210_pages")
    for _ in range(PAGE_COUNT):
        c.setFont("Helvetica", 8)
        c.drawString(10, 10, "1" * 120)
        c.drawString(10, 25, "1" * 120)
        c.showPage()
    c.save()
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {PAGE_COUNT} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
