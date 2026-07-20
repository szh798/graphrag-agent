"""Backfill LightRAG indexes for every tenant from inside the trusted Worker.

Dry-run is the default. Applying requires an explicit acknowledgement and a
durable queue, so this command cannot accidentally run large indexes inside a
one-off shell process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

from scripts.lightrag_backfill import select_document
from services import indexing_service
from storage import app_repository as app_store
from storage import queue_repository as queue_store


def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _startup_state_id() -> str:
    release = os.getenv("LIGHTRAG_BACKFILL_RELEASE_ID", "initial-v1").strip() or "initial-v1"
    suffix = hashlib.sha256(release.encode("utf-8")).hexdigest()[:16]
    return f"maintenance_lightrag_backfill_{suffix}"


def backfill_all_tenants(
    documents: list[dict[str, Any]],
    *,
    enqueue: Callable[[str, set[str]], dict] | None = None,
    include_failed: bool = False,
    max_documents: int = 0,
) -> dict[str, Any]:
    skipped: dict[str, int] = {}
    planned: list[dict[str, str]] = []
    selected_total = 0
    owners: set[str] = set()
    for document in documents:
        owner_id = str(document.get("owner_id") or "default")
        owners.add(owner_id)
        selected, reason = select_document(document, include_failed=include_failed)
        if not selected:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        doc_id = str(document.get("doc_id") or "").strip()
        if not doc_id:
            skipped["missing_doc_id"] = skipped.get("missing_doc_id", 0) + 1
            continue
        selected_total += 1
        if max_documents > 0 and len(planned) >= max_documents:
            skipped["batch_deferred"] = skipped.get("batch_deferred", 0) + 1
            continue
        planned.append({
            "doc_id": doc_id,
            "owner_hash_prefix": hashlib.sha256(owner_id.encode()).hexdigest()[:12],
            "reason": reason,
        })

    enqueued: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    if enqueue is not None:
        for item in planned:
            try:
                job = enqueue(item["doc_id"], {"lightrag"})
                if not job:
                    raise RuntimeError("document disappeared before enqueue")
                enqueued.append({"doc_id": item["doc_id"], "job_id": str(job.get("job_id") or "")})
            except Exception as exc:
                failures.append({"doc_id": item["doc_id"], "error": type(exc).__name__})

    return {
        "dry_run": enqueue is None,
        "documents_scanned": len(documents),
        "tenants_scanned": len(owners),
        "documents_planned": len(planned),
        "documents_selected_total": selected_total,
        "has_more": selected_total > len(planned),
        "documents_enqueued": len(enqueued),
        "documents_failed": len(failures),
        "planned": planned,
        "skipped": skipped,
        "enqueued": enqueued,
        "failures": failures,
    }


def run_startup_backfill() -> dict[str, Any]:
    """Idempotently enqueue the release backfill from a trusted Railway worker.

    The feature is disabled by default. Production enablement requires an
    explicit acknowledgement, durable Upstash queue and a distributed lock.
    Progress/report state is stored in the existing durable indexing job table,
    allowing a restarted worker to continue without rescanning completed rows.
    """

    if not _truthy("LIGHTRAG_BACKFILL_ON_START"):
        return {"status": "disabled", "enabled": False}
    if os.getenv("LIGHTRAG_BACKFILL_ALL_TENANTS_ACK", "") != "YES":
        raise RuntimeError("LIGHTRAG_BACKFILL_ALL_TENANTS_ACK=YES is required")

    queue = queue_store.get_queue_repository()
    if not bool(getattr(queue, "is_durable", lambda: False)()):
        raise RuntimeError("startup backfill requires the durable Upstash queue")

    app_repo = app_store.get_app_repository()
    state_id = _startup_state_id()
    previous = app_repo.load_job_meta(state_id)
    if previous and previous.get("status") == "done" and not _truthy("LIGHTRAG_BACKFILL_FORCE"):
        return {"status": "already_done", "state_id": state_id}

    lock_owner = "__lightrag_backfill_maintenance__"
    acquire = getattr(queue, "acquire_index_owner_lock", None)
    release = getattr(queue, "release_index_owner_lock", None)
    if not callable(acquire) or not acquire(lock_owner, state_id):
        return {"status": "locked", "state_id": state_id}

    now = datetime.now(timezone.utc).isoformat()
    try:
        app_repo.save_job_meta(state_id, {
            "job_id": state_id,
            "doc_id": "__maintenance_lightrag_backfill__",
            "status": "running",
            "stage": "Scanning legacy documents for LightRAG backfill",
            "progress": previous.get("progress", {}) if previous else {},
            "created_at": previous.get("created_at", now) if previous else now,
            "updated_at": now,
        })
        max_documents = max(0, int(os.getenv("LIGHTRAG_BACKFILL_BATCH_SIZE", "25")))
        report = backfill_all_tenants(
            app_repo.list_documents(),
            enqueue=indexing_service.start_indexing,
            include_failed=_truthy("LIGHTRAG_BACKFILL_INCLUDE_FAILED"),
            max_documents=max_documents,
        )
        # Every selected row is now either durably queued or reported failed.
        # A failure keeps the state retryable on the next worker interval.
        status = (
            "failed" if report["documents_failed"]
            else "pending" if report["has_more"]
            else "done"
        )
        app_repo.save_job_meta(state_id, {
            "job_id": state_id,
            "doc_id": "__maintenance_lightrag_backfill__",
            "status": status,
            "stage": (
                "Backfill jobs durably queued" if status == "done"
                else "Backfill has another batch" if status == "pending"
                else "Backfill enqueue failed"
            ),
            "progress": {
                "scanned": report["documents_scanned"],
                "planned": report["documents_planned"],
                "enqueued": report["documents_enqueued"],
                "failed": report["documents_failed"],
                "last_doc_id": (report["planned"][-1]["doc_id"] if report["planned"] else None),
            },
            "report": report,
            "created_at": previous.get("created_at", now) if previous else now,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": status, "state_id": state_id, **report}
    except Exception as exc:
        # A crash after the maintenance lock is acquired must not leave a
        # permanently green or ambiguous "running" state in system settings.
        try:
            app_repo.save_job_meta(state_id, {
                "job_id": state_id,
                "doc_id": "__maintenance_lightrag_backfill__",
                "status": "failed",
                "stage": "Backfill maintenance failed",
                "progress": previous.get("progress", {}) if previous else {},
                "error_type": type(exc).__name__,
                "created_at": previous.get("created_at", now) if previous else now,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            # Preserve the original maintenance error when the business store
            # is also unavailable; app_database readiness reports that outage.
            pass
        raise
    finally:
        if callable(release):
            release(lock_owner, state_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or enqueue an all-tenant LightRAG backfill inside Railway Worker."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-failed", action="store_true")
    parser.add_argument("--max-documents", type=int, default=0)
    args = parser.parse_args()
    if args.max_documents < 0:
        parser.error("--max-documents must be >= 0")

    enqueue = None
    if args.apply:
        if os.getenv("LIGHTRAG_BACKFILL_ALL_TENANTS_ACK", "") != "YES":
            parser.error("set LIGHTRAG_BACKFILL_ALL_TENANTS_ACK=YES before --apply")
        queue = queue_store.get_queue_repository()
        if not bool(getattr(queue, "is_durable", lambda: False)()):
            parser.error("--apply requires the durable Upstash queue")
        enqueue = indexing_service.start_indexing

    report = backfill_all_tenants(
        app_store.get_app_repository().list_documents(),
        enqueue=enqueue,
        include_failed=args.include_failed,
        max_documents=args.max_documents,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["documents_failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
