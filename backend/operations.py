"""Privacy-safe operational event aggregation and optional webhook alerts."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from identity import RequestIdentity
from observability import get_request_id
from storage.account_repository import get_account_repository


logger = logging.getLogger("graphrag.operations")


def report_event(
    event_type: str,
    message: str,
    *,
    severity: str = "error",
    source: str = "backend",
    identity: RequestIdentity | None = None,
    request_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> str | None:
    correlation_id = request_id or get_request_id()
    safe_context = {
        key: value
        for key, value in (context or {}).items()
        if key not in {"authorization", "cookie", "token", "secret", "api_key", "question", "answer"}
    }
    log_payload = {
        "level": severity,
        "event": event_type,
        "source": source,
        "request_id": correlation_id,
        "tenant_id": identity.tenant_id if identity else None,
        "actor_id": identity.actor_id if identity and identity.authenticated else None,
        "message": " ".join(str(message).split())[:500],
        "context": safe_context,
    }
    logger.error(json.dumps(log_payload, ensure_ascii=False, separators=(",", ":")))

    event_id: str | None = None
    try:
        event_id = get_account_repository().record_ops_event(
            severity=severity,
            source=source,
            event_type=event_type,
            request_id=correlation_id,
            tenant_id=identity.tenant_id if identity else None,
            actor_id=identity.actor_id if identity and identity.authenticated else None,
            message=log_payload["message"],
            payload=safe_context,
        )
    except Exception as storage_error:
        logger.error(
            json.dumps(
                {
                    "level": "error",
                    "event": "ops_event_persistence_failed",
                    "request_id": correlation_id,
                    "error_type": type(storage_error).__name__,
                },
                separators=(",", ":"),
            )
        )

    webhook_url = os.getenv("OPS_ALERT_WEBHOOK_URL", "").strip()
    if webhook_url and severity == "error":
        try:
            requests.post(webhook_url, json={**log_payload, "event_id": event_id}, timeout=3).raise_for_status()
        except Exception as webhook_error:
            logger.error(
                json.dumps(
                    {
                        "level": "error",
                        "event": "ops_alert_delivery_failed",
                        "request_id": correlation_id,
                        "error_type": type(webhook_error).__name__,
                    },
                    separators=(",", ":"),
                )
            )
    return event_id


def report_exception(
    event_type: str,
    exc: Exception,
    *,
    source: str = "backend",
    identity: RequestIdentity | None = None,
    context: dict[str, Any] | None = None,
) -> str | None:
    return report_event(
        event_type,
        type(exc).__name__,
        severity="error",
        source=source,
        identity=identity,
        context=context,
    )
