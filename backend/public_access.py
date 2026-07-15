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


def visible_document_ids(header_value: str | None, owner_id: str | None = None) -> set[str] | None:
    """Combine the curated public corpus with documents owned by this caller."""
    allowed_ids = public_document_ids(header_value)
    if allowed_ids is None or not owner_id:
        return allowed_ids

    from storage import app_repository as app_store

    owned_ids = {
        str(doc.get("doc_id"))
        for doc in app_store.get_app_repository().list_documents()
        if doc.get("doc_id") and str(doc.get("owner_id") or "default") == owner_id
    }
    return allowed_ids | owned_ids
