"""Lightweight local document parser for offline demo indexing."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup

ProgressCallback = Callable[[str, dict[str, Any]], None]

SUPPORTED_LOCAL_EXTENSIONS = {"pdf", "html", "htm", "txt", "md", "markdown"}


class LocalParserError(RuntimeError):
    pass


def parse_local_file(
    file_path: Path,
    output_dir: Path,
    *,
    data_id: str,
    language: str = "ch",
    enable_formula: bool = True,
    enable_table: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Parse a local selectable-text document into MinerU-compatible content_list JSON."""
    file_path = Path(file_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ext = file_path.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_LOCAL_EXTENSIONS:
        raise LocalParserError(
            "Local parser supports selectable-text pdf/html/txt/md files only. "
            "Use MinerU cloud or a local OCR parser for scanned PDFs, images, or Office files."
        )

    if progress_callback:
        progress_callback("local_parsing", {"extracted_pages": 0, "total_pages": 0})

    if ext == "pdf":
        blocks = _parse_pdf(file_path)
    elif ext in {"html", "htm"}:
        blocks = [_text_block(_parse_html(file_path), page_idx=0)]
    else:
        blocks = [_text_block(_read_text_file(file_path), page_idx=0)]

    blocks = [block for block in blocks if block["text"].strip()]
    if not blocks:
        raise LocalParserError(
            "Local parser found no selectable text. OCR is required for scanned PDFs or image-only documents."
        )

    content_list_path = output_dir / f"{data_id}_content_list.json"
    content_list_path.write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")

    if progress_callback:
        progress_callback("done", {"extracted_pages": len(blocks), "total_pages": len(blocks)})

    return content_list_path


def _parse_pdf(file_path: Path) -> list[dict]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise LocalParserError("pypdf is required for local PDF parsing. Install backend requirements first.") from exc

    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:
        raise LocalParserError(f"Local PDF parser failed to read {file_path.name}: {exc}") from exc

    blocks: list[dict] = []
    for page_idx, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            blocks.append(_text_block(text, page_idx=page_idx))

    if not blocks:
        raise LocalParserError(
            "Local parser found no selectable text in this PDF. OCR is required for scanned PDFs."
        )
    return blocks


def _parse_html(file_path: Path) -> str:
    html = _read_text_file(file_path)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _read_text_file(file_path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return file_path.read_text(encoding="utf-8", errors="replace")


def _text_block(text: str, *, page_idx: int) -> dict:
    return {
        "type": "text",
        "text": text.strip(),
        "page_idx": page_idx,
        "bbox": [0, 0, 0, 0],
        "source": "local_parser",
    }
