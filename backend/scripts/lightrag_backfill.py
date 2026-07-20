"""Plan or enqueue LightRAG backfill for documents already indexed by legacy.

The command is intentionally dry-run by default. Only ``--apply`` sends retry
requests. Listing documents is read-only in both modes.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from scripts.lightrag_ops_common import ApiError, JsonHttpClient, auth_headers_from_env, unwrap_data


SUCCESS_STATUSES = {"done", "indexed", "ready", "complete", "completed"}
ACTIVE_STATUSES = {"submitted", "queued", "parsing", "extracting", "indexing", "running"}


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


def _engine_status(document: dict[str, Any], engine: str) -> str:
    indexes = document.get("indexes")
    if isinstance(indexes, dict):
        info = indexes.get(engine)
        if isinstance(info, dict) and info.get("status") is not None:
            return _status(info.get("status"))
    if engine == "legacy":
        return _status(document.get("status"))
    return "missing"


def select_document(document: dict[str, Any], *, include_failed: bool = False) -> tuple[bool, str]:
    legacy_status = _engine_status(document, "legacy")
    lightrag_status = _engine_status(document, "lightrag")
    if legacy_status not in SUCCESS_STATUSES:
        return False, f"legacy_{legacy_status or 'unknown'}"
    if lightrag_status in SUCCESS_STATUSES:
        return False, "lightrag_done"
    if lightrag_status in ACTIVE_STATUSES:
        return False, "lightrag_active"
    if lightrag_status == "failed" and not include_failed:
        return False, "lightrag_failed_requires_include_failed"
    return True, f"lightrag_{lightrag_status or 'missing'}"


def list_all_documents(
    client: JsonHttpClient,
    *,
    documents_path: str,
    page_size: int,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = unwrap_data(
            client.request("GET", documents_path, query={"limit": page_size, "offset": offset})
        )
        if isinstance(response, list):
            items, total, total_is_explicit = response, len(response), False
        elif isinstance(response, dict):
            items = response.get("items") or response.get("documents") or []
            total = int(response.get("total", offset + len(items)))
            total_is_explicit = "total" in response
        else:
            raise ApiError("Document list response has an unsupported shape")
        if not isinstance(items, list):
            raise ApiError("Document list response does not contain an item list")
        documents.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        if not items or offset >= total or (not total_is_explicit and len(items) < page_size):
            break
    return documents


def backfill_documents(
    client: JsonHttpClient,
    *,
    apply: bool = False,
    include_failed: bool = False,
    max_documents: int = 0,
    page_size: int = 100,
    documents_path: str = "/documents",
    retry_path_template: str = "/indexing/{doc_id}/retry",
) -> dict[str, Any]:
    documents = list_all_documents(client, documents_path=documents_path, page_size=page_size)
    skipped: dict[str, int] = {}
    planned: list[dict[str, str]] = []
    for document in documents:
        selected, reason = select_document(document, include_failed=include_failed)
        if not selected:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        doc_id = str(document.get("doc_id") or document.get("id") or "").strip()
        if not doc_id:
            skipped["missing_doc_id"] = skipped.get("missing_doc_id", 0) + 1
            continue
        planned.append({
            "doc_id": doc_id,
            "filename": str(document.get("filename") or ""),
            "reason": reason,
        })
        if max_documents > 0 and len(planned) >= max_documents:
            break

    enqueued: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    if apply:
        for item in planned:
            path = retry_path_template.format(doc_id=item["doc_id"])
            try:
                response = unwrap_data(client.request("POST", path, payload={"engine": "lightrag"}))
                enqueued.append({"doc_id": item["doc_id"], "result": response})
            except ApiError as exc:
                failures.append({"doc_id": item["doc_id"], "error": str(exc)})

    return {
        "dry_run": not apply,
        "documents_scanned": len(documents),
        "documents_planned": len(planned),
        "documents_enqueued": len(enqueued),
        "documents_failed": len(failures),
        "planned": planned,
        "skipped": skipped,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan or enqueue legacy-to-LightRAG document backfill.")
    parser.add_argument("--apply", action="store_true", help="enqueue LightRAG retries; omitted means dry-run")
    parser.add_argument("--include-failed", action="store_true", help="also retry documents whose LightRAG index failed")
    parser.add_argument("--max-documents", type=int, default=0, help="maximum documents to plan; 0 means all")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--gateway-url", default=os.getenv("GRAPHRAG_GATEWAY_URL", "http://localhost:8000/api/v1"))
    parser.add_argument("--documents-path", default=os.getenv("LIGHTRAG_BACKFILL_DOCUMENTS_PATH", "/documents"))
    parser.add_argument(
        "--retry-path-template",
        default=os.getenv("LIGHTRAG_BACKFILL_RETRY_PATH_TEMPLATE", "/indexing/{doc_id}/retry"),
    )
    parser.add_argument("--timeout", type=float, default=float(os.getenv("LIGHTRAG_OPS_TIMEOUT_SECONDS", "30")))
    args = parser.parse_args()
    if args.max_documents < 0 or not 1 <= args.page_size <= 500:
        parser.error("--max-documents must be >= 0 and --page-size must be between 1 and 500")
    if "{doc_id}" not in args.retry_path_template:
        parser.error("--retry-path-template must contain {doc_id}")

    client = JsonHttpClient(args.gateway_url, timeout_seconds=args.timeout, headers=auth_headers_from_env())
    report = backfill_documents(
        client,
        apply=args.apply,
        include_failed=args.include_failed,
        max_documents=args.max_documents,
        page_size=args.page_size,
        documents_path=args.documents_path,
        retry_path_template=args.retry_path_template,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["documents_failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
