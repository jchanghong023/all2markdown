"""Deterministically build OOXML fixtures with embedded Office children.

Xberg v1.0.14 recursively extracts every recognizable file under
word/embeddings/ (DOCX) and ppt/embeddings/ (PPTX); OLE compound
files (oleObject*.bin) are skipped with a warning. Word's UI embedding
produces OLE bins, so a real Office-embedded-Office fixture for the
supported path is built by injecting open-source OOXML fixtures into an
existing open fixture's embeddings directory.

Pure standard library; fixed timestamps -> byte-stable output.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OFFICE = REPO / "tests" / "test_example"
FIXED_DATE = (2024, 1, 1, 0, 0, 0)


def build(parent: Path, prefix: str, children: list[Path], out: Path) -> None:
    with zipfile.ZipFile(parent) as src, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            info = zipfile.ZipInfo(item.filename, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, src.read(item.filename))
        for child in children:
            info = zipfile.ZipInfo(
                f"{prefix}{child.name}", date_time=FIXED_DATE
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, child.read_bytes())
    print(f"wrote {out} ({out.stat().st_size} bytes)")


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "tests" / "test_example"
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx = OFFICE / "merged_header.xlsx"
    pptx = OFFICE / "merged_table.pptx"
    build(
        OFFICE / "merged_cells.docx",
        "word/embeddings/",
        [xlsx, pptx],
        out_dir / "docx_with_embedded_office.docx",
    )
    build(
        pptx,
        "ppt/embeddings/",
        [xlsx],
        out_dir / "pptx_with_embedded_office.pptx",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
