"""Privacy-safe operational event aggregation and optional webhook alerts."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from identity import RequestIdentity, clerk_configuration_profile
from observability import get_request_id
from storage.account_repository import get_account_repository


logger = logging.getLogger("graphrag.operations")


def _alert_webhook_provider(webhook_url: str) -> str:
    configured = os.getenv("OPS_ALERT_WEBHOOK_PROVIDER", "auto").strip().lower()
    if configured not in {"", "auto"}:
        return "feishu" if configured == "lark" else configured

    hostname = (urlparse(webhook_url).hostname or "").lower()
    if hostname in {"open.feishu.cn", "open.larksuite.com"}:
        return "feishu"
    return "generic"


def _alert_text(log_payload: dict[str, Any], event_id: str | None) -> str:
    """Render a concise alert without request bodies, credentials, or answers."""
    lines = [
        "GraphRAG 生产告警",
        f"时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"级别：{str(log_payload['level']).upper()}",
        f"事件：{log_payload['event']}",
        f"来源：{log_payload['source']}",
        f"说明：{log_payload['message']}",
        f"Request ID：{log_payload['request_id']}",
    ]
    if event_id:
        lines.append(f"Event ID：{event_id}")
    if log_payload.get("tenant_id"):
        lines.append(f"Tenant ID：{log_payload['tenant_id']}")
    if log_payload.get("context"):
        context = json.dumps(log_payload["context"], ensure_ascii=False, default=str, separators=(",", ":"))
        lines.append(f"上下文：{context[:1500]}")
    return "\n".join(lines)


def _alert_webhook_payload(
    webhook_url: str,
    log_payload: dict[str, Any],
    event_id: str | None,
) -> tuple[str, dict[str, Any]]:
    provider = _alert_webhook_provider(webhook_url)
    if provider == "feishu":
        return provider, {
            "msg_type": "text",
            "content": {"text": _alert_text(log_payload, event_id)},
        }
    return provider, {**log_payload, "event_id": event_id}


def _validate_alert_response(response: requests.Response, provider: str) -> None:
    response.raise_for_status()
    if provider != "feishu":
        return

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Feishu alert returned an invalid response") from exc

    code = body.get("code", body.get("StatusCode")) if isinstance(body, dict) else None
    if code not in {0, "0"}:
        raise RuntimeError("Feishu alert delivery was rejected")


def operational_readiness() -> dict[str, Any]:
    """Describe production controls without exposing any secret values."""
    auth = clerk_configuration_profile()
    try:
        retention_hours = max(0, int(os.getenv("DATABASE_BACKUP_RETENTION_HOURS", "0") or 0))
    except ValueError:
        retention_hours = 0
    pitr_ready = os.getenv("DATABASE_PITR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"} and retention_hours > 0
    alert_webhook_url = os.getenv("OPS_ALERT_WEBHOOK_URL", "").strip()
    checks = {
        "authentication": {
            "ready": bool(auth["production_ready"]),
            "mode": auth["mode"],
            "message": "正式身份服务已启用" if auth["production_ready"] else "仍在使用测试身份服务，请切换 Clerk Production 密钥",
        },
        "alert_delivery": {
            "ready": bool(alert_webhook_url),
            "provider": _alert_webhook_provider(alert_webhook_url) if alert_webhook_url else None,
            "message": "异常告警 webhook 已配置" if alert_webhook_url else "异常会记录到运维面板，但尚未配置外部告警 webhook",
        },
        "database_recovery": {
            "ready": pitr_ready,
            "retention_hours": retention_hours,
            "message": "数据库恢复窗口已登记" if pitr_ready else "尚未登记云数据库的时间点恢复窗口",
        },
        "index_recovery": {
            "ready": bool(os.getenv("INDEX_DISPATCH_SECRET", "").strip()),
            "message": "索引队列定时恢复已配置" if os.getenv("INDEX_DISPATCH_SECRET", "").strip() else "索引队列租约已启用，但定时唤醒密钥尚未配置",
        },
    }
    return {
        "status": "ready" if all(check["ready"] for check in checks.values()) else "action_required",
        "checks": checks,
    }


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
    log_method = logger.warning if severity == "warning" else logger.error
    log_method(json.dumps(log_payload, ensure_ascii=False, separators=(",", ":")))

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
            provider, webhook_payload = _alert_webhook_payload(webhook_url, log_payload, event_id)
            response = requests.post(webhook_url, json=webhook_payload, timeout=3)
            _validate_alert_response(response, provider)
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
