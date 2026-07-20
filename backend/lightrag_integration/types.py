"""Shared types and input normalization for the LightRAG boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence
from urllib.parse import quote, unquote, urlencode, urlparse, parse_qs

from .errors import LightRAGValidationError


TARGET_LIGHTRAG_VERSION = "1.5.4"


class Engine(str, Enum):
    LEGACY = "legacy"
    LIGHTRAG = "lightrag"


class LightRAGMode(str, Enum):
    LOCAL = "local"
    GLOBAL = "global"
    HYBRID = "hybrid"
    MIX = "mix"
    NAIVE = "naive"

    @classmethod
    def parse(cls, value: "LightRAGMode | str | None", *, default: str = "mix") -> "LightRAGMode":
        raw = str(value.value if isinstance(value, cls) else value or default).strip().lower()
        try:
            return cls(raw)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise LightRAGValidationError(f"Unsupported LightRAG mode; expected one of: {allowed}") from exc


@dataclass(frozen=True)
class PageInput:
    """One already-parsed, one-based source page."""

    page: int
    content: str


def coerce_pages(pages: Sequence[Any]) -> list[PageInput]:
    """Accept PageText, mappings, or strings and return non-empty pages.

    ``PageText.page_idx`` is zero-based in the existing MinerU assembler, while
    an explicit ``page``/``page_number`` is treated as the public one-based page.
    """

    if isinstance(pages, (str, bytes)) or not isinstance(pages, Sequence):
        raise LightRAGValidationError("pages must be a sequence of parsed pages")
    normalized: list[PageInput] = []
    seen_pages: set[int] = set()
    for position, item in enumerate(pages, start=1):
        if isinstance(item, str):
            page_number, content = position, item
        elif isinstance(item, Mapping):
            if "page" in item:
                page_number = int(item["page"])
            elif "page_number" in item:
                page_number = int(item["page_number"])
            elif "page_idx" in item:
                page_number = int(item["page_idx"]) + 1
            else:
                page_number = position
            content = str(item.get("content", item.get("text", "")))
        else:
            page_idx = getattr(item, "page_idx", None)
            page_number = int(page_idx) + 1 if page_idx is not None else position
            content = str(getattr(item, "text", getattr(item, "content", "")))
        content = content.strip()
        if not content:
            continue
        if page_number < 1:
            raise LightRAGValidationError("page numbers must be positive")
        if page_number in seen_pages:
            raise LightRAGValidationError("page numbers must be unique")
        seen_pages.add(page_number)
        normalized.append(PageInput(page=page_number, content=content))
    if not normalized:
        raise LightRAGValidationError("document has no indexable page text")
    return normalized


def page_document_id(workspace: str, doc_id: str, page: int) -> str:
    """Return a deterministic opaque LightRAG child-document identifier."""

    raw_doc_id = str(doc_id).strip()
    if not raw_doc_id or page < 1:
        raise LightRAGValidationError("doc_id and a positive page are required")
    digest = hashlib.sha256(f"{workspace}\x00{raw_doc_id}\x00{page}".encode("utf-8")).hexdigest()
    return f"lrpg_{digest[:40]}"


def source_path(doc_id: str, filename: str, page: int) -> str:
    """Encode a source reference without including tenant/workspace data."""

    if page < 1:
        raise LightRAGValidationError("page must be positive")
    safe_doc_id = quote(str(doc_id).strip(), safe="")
    query = urlencode({"filename": str(filename).strip() or "document"})
    return f"graphrag://document/{safe_doc_id}/{page}?{query}"


def parse_source_path(value: str | None) -> dict[str, Any]:
    """Decode our reference URI, with a conservative legacy fallback."""

    raw = str(value or "").strip()
    if raw.startswith("graphrag://document/"):
        parsed = urlparse(raw)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            try:
                page = int(parts[-1])
            except ValueError:
                page = 0
            filename = parse_qs(parsed.query).get("filename", [""])[0]
            return {
                "doc_id": unquote("/".join(parts[:-1])),
                "filename": filename or "document",
                "page": page,
                "file_path": raw,
            }
    # LightRAG installations predating this adapter may only retain a filename.
    filename, _, fragment = raw.partition("#")
    page = 0
    for item in fragment.split("&"):
        key, _, val = item.partition("=")
        if key == "page":
            try:
                page = int(val)
            except ValueError:
                pass
    return {"doc_id": "", "filename": filename or "document", "page": page, "file_path": raw}
