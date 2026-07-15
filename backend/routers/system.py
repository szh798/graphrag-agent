import importlib.util
import os
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Header

from models.schemas import APIResponse
from public_access import PUBLIC_DEMO_HEADER, public_document_ids
from pipeline.llm_config import LLM_API_KEY, LLM_BASE_URL, LLM_INDEX_MODEL, LLM_MODEL, LLM_PROVIDER
from services.local_parser import SUPPORTED_LOCAL_EXTENSIONS
from storage import app_repository as app_store
from storage import blob_repository as blob_store
from storage import file_store as fs
from storage import graph_repository as graph_store
from storage import queue_repository as queue_store

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
        "graph_database": {"neo4j"},
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
    return issues


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

    raw_components: dict[str, dict] = {
        "document_parser": {
            "status": parser_status,
            "mode": parser_mode,
            "active_parser": active_parser,
            "mineru_configured": bool(mineru_token),
            "local_supported_formats": sorted(SUPPORTED_LOCAL_EXTENSIONS),
        },
        "mineru_venv": {
            "status": "ok" if mineru_token else "error",
            "path": "cloud",
            "exists": bool(mineru_token),
            "base_url": mineru_base_url,
            "key_configured": bool(mineru_token),
        },
        "mineru_api": {
            "status": "ok" if mineru_token else "error",
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
        "graph_database": graph_store.get_graph_repository().health(),
        "app_database": app_store.get_app_repository().health(),
        "blob_storage": blob_store.get_blob_repository().health(),
        "task_queue": queue_store.get_queue_repository().health(),
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
        "version": "1.0.0",
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
        "version": "1.0.0",
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
    visitor_id: str | None = Header(default=None, alias="X-GraphRAG-Visitor-ID"),
):
    global _stats_cache
    now = time.monotonic()
    allowed_ids = public_document_ids(public_demo)
    cache_key = (
        tuple(sorted(allowed_ids)) if allowed_ids is not None else None,
        visitor_id if allowed_ids is not None else None,
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
    history = app_repo.load_query_history(visitor_id or app_store.LEGACY_OWNER_ID)

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
