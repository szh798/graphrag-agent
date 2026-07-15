"""Public-demo dataset scoping shared by API routes and QA execution."""
from __future__ import annotations

import os


PUBLIC_DEMO_HEADER = "X-GraphRAG-Public-Demo"


def public_document_ids(header_value: str | None) -> set[str] | None:
    """Return ``None`` for trusted full access, otherwise the public allowlist.

    The marker is only trusted after ProxyAuthMiddleware has authenticated the
    edge proxy. A public request fails closed when no allowlist is configured.
    """
    if (header_value or "").strip() != "1":
        return None
    return {
        item.strip()
        for item in os.getenv("PUBLIC_DOCUMENT_IDS", "").split(",")
        if item.strip()
    }


def document_is_visible(doc_id: str, allowed_ids: set[str] | None) -> bool:
    return allowed_ids is None or doc_id in allowed_ids
