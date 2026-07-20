import importlib.util
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header

from identity import RequestIdentity, get_request_identity
from models.schemas import APIResponse
from public_access import PUBLIC_DEMO_HEADER, visible_document_ids
from pipeline.llm_config import LLM_API_KEY, LLM_BASE_URL, LLM_INDEX_MODEL, LLM_MODEL, LLM_PROVIDER
from services.local_parser import SUPPORTED_LOCAL_EXTENSIONS
from services import async_bridge
from storage import app_repository as app_store
from storage import blob_repository as blob_store
from storage import file_store as fs
from storage import graph_repository as graph_store
from storage import queue_repository as queue_store
from version import APP_VERSION

router = APIRouter(tags=["System"])

_START_TIME = time.time()
_STATS_CACHE_TTL_SECONDS = 30.0
_stats_cache: dict[tuple[tuple[str, ...] | None, str | None], tuple[float, dict]] = {}
_PUBLIC_COMPONENT_FIELDS = {
    "status",
    "backend",
    "durable",
    "persistent",
    "persistence",
    "mode",
    "active_parser",
    "version",
    "detail",
    "enabled",
    "configured",
    "queue_depth",
    "total",
    "done",
    "pending",
    "failed",
    "worker_id",
    "last_seen",
    "last_updated",
    "heartbeat_age_seconds",
    "heartbeat_ttl_seconds",
    "maintenance_status",
}


def _sanitize_component(component: dict) -> dict:
    """Reduce dependency health to the stable, non-sensitive public contract."""
    return {
        key: value
        for key, value in component.items()
        if key in _PUBLIC_COMPONENT_FIELDS
    }


def _backend_python_candidates(backend_dir: Path) -> list[Path]:
    """Return likely Python runtimes for a portable offline demo package."""
    candidates = [
        backend_dir / ".venv" / "bin" / "python",
        backend_dir / ".venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _check_python_import(module_name: str, backend_dir: Path) -> dict:
    # Importing a large dependency in a child process made every health poll
    # expensive and amplified cold starts. ``find_spec`` is a side-effect-free
    # availability check in the actual runtime that serves the request.
    try:
        available = importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        available = False
    return {
        "status": "ok" if available else "error",
        "path": str(Path(sys.executable)),
        "exists": available,
    }


def _normalized_parser_mode() -> str:
    mode = os.getenv("PARSER_MODE", "auto").strip().lower()
    if mode in {"cloud", "mineru", "mineru_cloud"}:
        return "mineru"
    if mode in {"auto", "local"}:
        return mode
    return "auto"


def _production_mode() -> bool:
    return any(
        os.getenv(name, "").strip().lower() in {"production", "prod"}
        for name in ("VERCEL_ENV", "ENVIRONMENT")
    )


def _production_dependency_issues(components: dict[str, dict]) -> list[str]:
    """Return unsafe production dependencies while allowing local demo mode."""
    if not _production_mode():
        return []

    issues: list[str] = []
    required_backends = {
        "graph_database": {"neo4j", "postgres"},
        "app_database": {"postgres"},
        "blob_storage": {"vercel_blob"},
    }
    for name, allowed in required_backends.items():
        component = components.get(name, {})
        if component.get("status") != "ok" or component.get("backend") not in allowed:
            issues.append(name)

    queue = components.get("task_queue", {})
    if queue.get("status") != "ok" or not queue.get("durable"):
        issues.append("task_queue")
    if os.getenv("LIGHTRAG_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        for name in ("lightrag", "lightrag_worker", "lightrag_graph_database", "lightrag_vector_database", "lightrag_reranker"):
            if components.get(name, {}).get("status") != "ok":
                issues.append(name)
    return issues


def _lightrag_health() -> dict:
    from services import lightrag_service

    if not lightrag_service.enabled():
        return {
            "status": "ok",
            "mode": "disabled",
            "configured": False,
            "version": os.getenv("LIGHTRAG_VERSION", "1.5.4"),
            "components": {},
        }
    try:
        result = async_bridge.run(lightrag_service.health(probe=True))
        core_status = str(result.get("status") or "").lower()
        ready = bool(result.get("ready")) or core_status in {"ok", "ready", "healthy"}
        return {
            **result,
            "status": "ok" if ready else "error",
            "mode": result.get("transport") or result.get("mode"),
            "configured": True,
            "version": result.get("target_version") or result.get("installed_version") or os.getenv("LIGHTRAG_VERSION", "1.5.4"),
            "detail": f"LightRAG runtime status: {core_status or 'unknown'}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "mode": os.getenv("LIGHTRAG_TRANSPORT", "remote"),
            "configured": True,
            "version": os.getenv("LIGHTRAG_VERSION", "1.5.4"),
            "detail": f"LightRAG health probe failed ({type(exc).__name__})",
            "components": {},
        }


def _lightrag_component(base: dict, *names: str, default_detail: str) -> dict:
    components = base.get("components") if isinstance(base.get("components"), dict) else {}
    for name in names:
        value = components.get(name)
        if isinstance(value, dict):
            return value
    disabled = base.get("mode") == "disabled" or base.get("enabled") is False
    return {
        "status": "ok" if disabled else "error",
        "mode": base.get("mode"),
        "configured": base.get("configured", False),
        "version": base.get("version"),
        "detail": default_detail,
    }


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _iso_timestamp(value: object) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _iso_age_seconds(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - observed).total_seconds()))


def _lightrag_worker_component(queue_repo, lightrag_health: dict) -> dict:
    """Report the real long-running Worker, never the API dependency probe."""
    enabled = bool(lightrag_health.get("configured")) and lightrag_health.get("mode") != "disabled"
    if not enabled:
        return {
            "status": "ok",
            "mode": "disabled",
            "enabled": False,
            "configured": False,
            "detail": "LightRAG is disabled; no indexing worker is required.",
        }

    try:
        profile = queue_repo.profile()
    except Exception:
        profile = {}
    backend = str(profile.get("backend") or "unknown")
    durable = bool(profile.get("durable"))
    base = {
        "backend": backend,
        "durable": durable,
        "enabled": True,
        "configured": durable,
    }
    if not durable:
        return {
            **base,
            "status": "error",
            "mode": "non_durable",
            "detail": "Worker readiness requires a durable shared queue heartbeat.",
        }

    reader = getattr(queue_repo, "get_worker_heartbeat", None)
    if not callable(reader):
        return {
            **base,
            "status": "error",
            "mode": "unsupported",
            "detail": "The configured queue backend cannot read Worker heartbeats.",
        }
    try:
        heartbeat = reader()
    except Exception as exc:
        return {
            **base,
            "status": "error",
            "mode": "unavailable",
            "detail": f"Worker heartbeat lookup failed ({type(exc).__name__}).",
        }
    if not heartbeat:
        return {
            **base,
            "status": "error",
            "mode": "missing",
            "detail": "No Railway Worker heartbeat is present.",
        }
    if heartbeat.get("malformed"):
        return {
            **base,
            "status": "error",
            "mode": "malformed",
            "heartbeat_ttl_seconds": heartbeat.get("ttl_seconds"),
            "detail": "The stored Railway Worker heartbeat is malformed.",
        }

    last_seen = _iso_timestamp(heartbeat.get("last_seen"))
    worker_id = str(heartbeat.get("worker_id") or "")
    version = str(heartbeat.get("version") or "")
    observed = {
        **base,
        "worker_id": hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:12] if worker_id else None,
        "version": version or None,
        "last_seen": last_seen,
        "heartbeat_age_seconds": heartbeat.get("age_seconds"),
        "heartbeat_ttl_seconds": heartbeat.get("ttl_seconds"),
    }
    if heartbeat.get("fresh") is not True or last_seen is None or not worker_id or not version:
        return {
            **observed,
            "status": "error",
            "mode": "stale",
            "detail": "The Railway Worker heartbeat has expired.",
        }
    if version != APP_VERSION:
        return {
            **observed,
            "status": "error",
            "mode": "version_mismatch",
            "detail": "The Railway Worker release does not match the public gateway.",
        }

    internal_worker = _lightrag_component(
        lightrag_health,
        "worker",
        "queue",
        default_detail="LightRAG runtime queue",
    )
    return {
        **observed,
        "status": "ok",
        "mode": "active",
        "queue_depth": internal_worker.get("queue_depth"),
        "detail": "Railway Worker heartbeat is current.",
    }


def _lightrag_backfill_component(
    app_repo,
    queue_repo,
    documents: list[dict],
    lightrag_health: dict,
) -> dict:
    """Expose the configured switch, durable maintenance state and failures."""
    from scripts.lightrag_backfill_worker import _startup_state_id

    states = [
        str(((document.get("indexes") or {}).get("lightrag") or {}).get("status") or "pending")
        for document in documents
    ]
    total = len(states)
    done = sum(1 for state in states if state == "done")
    pending = sum(1 for state in states if state in {"pending", "queued", "indexing", "disabled"})
    document_failures = sum(1 for state in states if state == "failed")

    enabled = _env_truthy("LIGHTRAG_BACKFILL_ON_START")
    acknowledged = os.getenv("LIGHTRAG_BACKFILL_ALL_TENANTS_ACK", "") == "YES"
    lightrag_enabled = bool(lightrag_health.get("configured")) and lightrag_health.get("mode") != "disabled"
    try:
        durable = bool(queue_repo.is_durable())
    except Exception:
        durable = False

    state = app_repo.load_job_meta(_startup_state_id())
    maintenance_status = str((state or {}).get("status") or "not_started")
    progress = (state or {}).get("progress") if isinstance((state or {}).get("progress"), dict) else {}
    report = (state or {}).get("report") if isinstance((state or {}).get("report"), dict) else {}
    reported_failures = max(
        _safe_nonnegative_int(progress.get("failed")),
        _safe_nonnegative_int(report.get("documents_failed")),
    )
    failed = max(document_failures, reported_failures)
    configured = enabled and acknowledged and lightrag_enabled and durable
    base = {
        "enabled": enabled,
        "configured": configured,
        "total": total,
        "done": done,
        "pending": pending,
        "failed": failed,
        "maintenance_status": maintenance_status,
        "last_updated": (state or {}).get("updated_at"),
    }

    if maintenance_status == "failed" or failed > 0:
        return {
            **base,
            "status": "error",
            "mode": "failed",
            "detail": "LightRAG backfill has failed documents or a failed maintenance run.",
        }
    if not enabled:
        return {
            **base,
            "status": "ok",
            "mode": "disabled",
            "detail": "Automatic LightRAG backfill is disabled.",
        }
    if not lightrag_enabled:
        return {
            **base,
            "status": "error",
            "mode": "blocked",
            "detail": "Backfill is enabled while the LightRAG engine is disabled.",
        }
    if not acknowledged:
        return {
            **base,
            "status": "error",
            "mode": "blocked",
            "detail": "Backfill requires the all-tenant acknowledgement.",
        }
    if not durable:
        return {
            **base,
            "status": "error",
            "mode": "blocked",
            "detail": "Backfill requires the durable Upstash queue.",
        }
    if maintenance_status == "running":
        state_age = _iso_age_seconds((state or {}).get("updated_at"))
        try:
            check_interval = max(60, int(os.getenv("LIGHTRAG_BACKFILL_INTERVAL_SECONDS", "300")))
        except (TypeError, ValueError):
            check_interval = 300
        if state_age is None or state_age > check_interval * 2:
            return {
                **base,
                "status": "error",
                "mode": "stale",
                "detail": "Backfill maintenance is stuck in a stale running state.",
            }
    if maintenance_status not in {"not_started", "running", "pending", "done"}:
        return {
            **base,
            "status": "error",
            "mode": "invalid_state",
            "detail": "Backfill maintenance state is invalid.",
        }
    return {
        **base,
        "status": "ok",
        "mode": "waiting" if maintenance_status == "not_started" else maintenance_status,
        "detail": (
            "Backfill maintenance has not started yet."
            if maintenance_status == "not_started"
            else "Backfill maintenance state is current."
        ),
    }


def _health_payload() -> dict:
    backend_dir = Path(__file__).parent.parent
    env_path = backend_dir / ".env"
    from dotenv import load_dotenv
    load_dotenv(env_path, override=False)

    mineru_token = os.getenv("MINERU_API_TOKEN", "")
    mineru_base_url = os.getenv("MINERU_BASE_URL", "https://mineru.net/api/v4")
    mineru_model = os.getenv("MINERU_MODEL_VERSION", "pipeline")
    langextract_status = _check_python_import("langextract", backend_dir)
    parser_mode = _normalized_parser_mode()
    active_parser = "mineru" if parser_mode == "mineru" or (parser_mode == "auto" and mineru_token) else "local"
    parser_status = "error" if parser_mode == "mineru" and not mineru_token else "ok"
    mineru_status = "ok" if mineru_token or active_parser == "local" else "error"

    graph_repo = graph_store.get_graph_repository()
    app_repo = app_store.get_app_repository()
    blob_repo = blob_store.get_blob_repository()
    queue_repo = queue_store.get_queue_repository()
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="health") as executor:
        futures = {
            "graph_database": executor.submit(graph_repo.health),
            "app_database": executor.submit(app_repo.health),
            "blob_storage": executor.submit(blob_repo.health),
            "task_queue": executor.submit(queue_repo.health),
            "lightrag": executor.submit(_lightrag_health),
        }
        dependency_health = {name: future.result() for name, future in futures.items()}

    lightrag_health = dependency_health.pop("lightrag")
    documents = app_repo.list_documents()
    raw_components: dict[str, dict] = {
        "document_parser": {
            "status": parser_status,
            "mode": parser_mode,
            "active_parser": active_parser,
            "mineru_configured": bool(mineru_token),
            "local_supported_formats": sorted(SUPPORTED_LOCAL_EXTENSIONS),
        },
        "mineru_venv": {
            "status": mineru_status,
            "mode": "cloud" if mineru_token else "disabled",
            "exists": bool(mineru_token),
            "base_url": mineru_base_url,
            "key_configured": bool(mineru_token),
        },
        "mineru_api": {
            "status": mineru_status,
            "mode": "cloud" if mineru_token else "disabled",
            "base_url": mineru_base_url,
            "key_configured": bool(mineru_token),
            "model": mineru_model,
        },
        "langextract_venv": {
            **langextract_status,
        },
        "llm_api": {
            "status": "ok" if LLM_API_KEY else "error",
            "base_url": LLM_BASE_URL,
            "key_configured": bool(LLM_API_KEY),
            "provider": LLM_PROVIDER,
            "model": LLM_MODEL,
            "index_model": LLM_INDEX_MODEL,
        },
        "llm_index_api": {
            "status": "ok" if LLM_API_KEY else "error",
            "base_url": LLM_BASE_URL,
            "key_configured": bool(LLM_API_KEY),
            "provider": LLM_PROVIDER,
            "model": LLM_INDEX_MODEL,
        },
        # Backward-compatible key for the current frontend contract.
        "deepseek_api": {
            "status": "ok" if LLM_API_KEY else "error",
            "base_url": LLM_BASE_URL,
            "key_configured": bool(LLM_API_KEY),
        },
        "storage": {
            "status": "ok",
            "kg_nodes_exists": fs.kg_nodes_path().exists(),
            "kg_edges_exists": fs.kg_edges_path().exists(),
            "uploads_dir_exists": fs.UPLOADS_DIR.exists(),
            **fs.storage_profile(),
        },
        "lightrag": lightrag_health,
        "lightrag_worker": _lightrag_worker_component(queue_repo, lightrag_health),
        "lightrag_graph_database": _lightrag_component(lightrag_health, "neo4j", "graph_database", default_detail="Neo4j Aura graph storage"),
        "lightrag_vector_database": _lightrag_component(lightrag_health, "postgres", "vector_database", default_detail="Dedicated Neon retrieval database"),
        "lightrag_reranker": _lightrag_component(lightrag_health, "reranker", default_detail="BAAI/bge-reranker-v2-m3"),
        "lightrag_backfill": _lightrag_backfill_component(
            app_repo,
            queue_repo,
            documents,
            lightrag_health,
        ),
        **dependency_health,
    }

    production_issues = _production_dependency_issues(raw_components)
    if production_issues:
        raw_components["storage"]["status"] = "degraded"
        raw_components["storage"]["warning"] = (
            "Production dependencies are not durable: " + ", ".join(production_issues)
        )

    overall = "healthy" if all(c["status"] == "ok" for c in raw_components.values()) else "degraded"
    components = {
        name: _sanitize_component(component)
        for name, component in raw_components.items()
    }

    return {
        "status": overall,
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "components": components,
        "production_ready": not production_issues and overall == "healthy",
    }


@router.get("/health")
async def health_check():
    return APIResponse.ok(_health_payload())


@router.get("/health/live")
async def live_check():
    return APIResponse.ok({
        "status": "live",
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
    })


@router.get("/health/ready")
async def ready_check():
    health = _health_payload()
    components = health["components"]
    ready = bool(health["production_ready"])
    return APIResponse.ok({
        "status": "ready" if ready else "degraded",
        "version": health["version"],
        "uptime_seconds": health["uptime_seconds"],
        "components": components,
    })


@router.get("/system/stats")
async def system_stats(
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    global _stats_cache
    now = time.monotonic()
    allowed_ids = visible_document_ids(public_demo, identity.owner_id)
    cache_key = (
        tuple(sorted(allowed_ids)) if allowed_ids is not None else None,
        identity.owner_id if allowed_ids is not None else None,
    )
    cached = _stats_cache.get(cache_key)
    if cached and now - cached[0] < _STATS_CACHE_TTL_SECONDS:
        return APIResponse.ok(dict(cached[1]))

    from services import indexing_service as idx_svc

    app_repo = app_store.get_app_repository()
    docs = list(app_repo.list_documents())
    if allowed_ids is not None:
        docs = [doc for doc in docs if doc.get("doc_id") in allowed_ids]
    from services import kg_service

    kg_stats = kg_service.get_stats(allowed_ids)
    history = app_repo.load_query_history(identity.owner_id)

    payload = {
        "total_documents": len(docs),
        "indexed_documents": sum(1 for d in docs if d.get("status") == "indexed"),
        "failed_documents": sum(1 for d in docs if d.get("status") == "failed"),
        "total_nodes": kg_stats.get("total_nodes", 0),
        "total_edges": kg_stats.get("total_edges", 0),
        "type_distribution": kg_stats.get("type_distribution", {}),
        "total_queries": len(history),
        "active_jobs": idx_svc.count_active_jobs(),
        "storage_used_mb": 0 if allowed_ids is not None else fs.storage_used_mb(),
    }
    _stats_cache[cache_key] = (now, payload)
    return APIResponse.ok(dict(payload))


@router.get("/system/formats")
async def list_formats():
    return APIResponse.ok({
        "formats": [
            {"ext": "pdf",  "description": "PDF 文档（文本型/扫描型/混合型）", "max_size_mb": 200, "max_pages": 600, "requires_ocr": False},
            {"ext": "docx", "description": "Microsoft Word（新版）", "max_size_mb": 200, "max_pages": 600, "requires_ocr": False},
            {"ext": "doc",  "description": "Microsoft Word（旧版）", "max_size_mb": 200, "max_pages": 600, "requires_ocr": False},
            {"ext": "pptx", "description": "PowerPoint（新版）", "max_size_mb": 200, "max_pages": 600, "requires_ocr": False},
            {"ext": "ppt",  "description": "PowerPoint（旧版）", "max_size_mb": 200, "max_pages": 600, "requires_ocr": False},
            {"ext": "png",  "description": "PNG 图片（单页）", "max_size_mb": 200, "max_pages": 1, "requires_ocr": True},
            {"ext": "jpg",  "description": "JPEG 图片（单页）", "max_size_mb": 200, "max_pages": 1, "requires_ocr": True},
            {"ext": "jpeg", "description": "JPEG 图片（单页）", "max_size_mb": 200, "max_pages": 1, "requires_ocr": True},
            {"ext": "html", "description": "HTML 文件", "max_size_mb": 200, "max_pages": 600, "requires_ocr": False},
            {"ext": "txt",  "description": "纯文本文件（本地离线解析）", "max_size_mb": 200, "max_pages": 1, "requires_ocr": False},
            {"ext": "md",   "description": "Markdown 文件（本地离线解析）", "max_size_mb": 200, "max_pages": 1, "requires_ocr": False},
            {"ext": "markdown", "description": "Markdown 文件（本地离线解析）", "max_size_mb": 200, "max_pages": 1, "requires_ocr": False},
        ],
        "ocr_languages": [
            {"code": "ch", "name": "中文（默认）"},
            {"code": "en", "name": "英文"},
            {"code": "japan", "name": "日文"},
            {"code": "korean", "name": "韩文"},
            {"code": "french", "name": "法文"},
            {"code": "german", "name": "德文"},
        ],
        "notes": [
            "language 参数默认值为 'ch'（非 'zh'），遵循 PaddleOCR v3 语言代码规范",
            "上传时不需要携带 Content-Type，服务端自动识别",
            "PNG/JPG/JPEG 单次最多处理 1 页",
        ],
    })


@router.get("/system/demo")
async def get_demo_data():
    # Try backend KG first, then fall back to graphrag_pipeline/output
    from services import kg_service

    exported = kg_service.export_kg()
    nodes = exported.get("nodes", [])
    edges = exported.get("edges", [])

    if not nodes:
        # Fallback: load from existing graphrag_pipeline output
        legacy_nodes_path = Path("F:/GraphRAGAgent/graphrag_pipeline/output/kg_nodes.json")
        legacy_edges_path = Path("F:/GraphRAGAgent/graphrag_pipeline/output/kg_edges.json")
        if legacy_nodes_path.exists():
            import json
            nodes = json.loads(legacy_nodes_path.read_text(encoding="utf-8"))
            edges = json.loads(legacy_edges_path.read_text(encoding="utf-8")) if legacy_edges_path.exists() else []
        else:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=400,
                content=APIResponse.err(3002, "No demo data available. Index a document first.").model_dump(),
            )

    type_counts: dict[str, int] = {}
    for n in nodes:
        t = n.get("type", "UNKNOWN")
        type_counts[t] = type_counts.get(t, 0) + 1

    import networkx as nx
    G = nx.Graph()
    for n in nodes:
        G.add_node(n["id"])
    for e in edges:
        G.add_edge(e["source"], e["target"])

    return APIResponse.ok({
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "type_counts": type_counts,
            "density": round(nx.density(G), 4) if G.number_of_nodes() > 1 else 0.0,
        },
    })
