"""Local and signed-remote adapters for LightRAG 1.5.4."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from .errors import (
    LightRAGConfigurationError,
    LightRAGError,
    LightRAGProtocolError,
    LightRAGUnavailableError,
    LightRAGValidationError,
)
from .normalization import normalize_entity_search, normalize_graph, normalize_query_result
from .providers import build_provider_bindings, load_provider_settings
from .security import NonceReplayCache, sign_request, validate_workspace
from .types import (
    LightRAGMode,
    TARGET_LIGHTRAG_VERSION,
    coerce_pages,
    page_document_id,
    parse_source_path,
    source_path,
)


class LightRAGAdapter(Protocol):
    async def health(self) -> dict[str, Any]: ...

    async def index_pages(
        self, *, workspace: str, doc_id: str, filename: str, pages: Sequence[Any]
    ) -> dict[str, Any]: ...

    async def delete_document(
        self,
        *,
        workspace: str,
        doc_id: str,
        page_count: int | None = None,
        page_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]: ...

    async def run_query(
        self,
        *,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> dict[str, Any]: ...

    def stream_query(
        self,
        *,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> AsyncIterator[dict[str, Any]]: ...

    def stream_query_scopes(
        self,
        *,
        workspaces: Sequence[str],
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def export_graph(
        self,
        *,
        workspace: str,
        doc_id: str | None,
        allowed_doc_ids: set[str] | None,
        max_nodes: int,
        max_edges: int,
    ) -> dict[str, Any]: ...

    async def search_entities(
        self,
        *,
        workspace: str,
        query: str,
        entity_type: str | None,
        limit: int,
        allowed_doc_ids: set[str] | None,
    ) -> dict[str, Any]: ...


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def installed_lightrag_version() -> str | None:
    try:
        return importlib.metadata.version("lightrag-hku")
    except importlib.metadata.PackageNotFoundError:
        return None


def lightrag_package_available() -> bool:
    return importlib.util.find_spec("lightrag") is not None


async def _maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable) or asyncio.isfuture(value):
        return await value
    return value


InstanceFactory = Callable[[str], Any]
DependencyProbe = Callable[[], Awaitable[Mapping[str, Any]] | Mapping[str, Any]]


class LocalLightRAGAdapter:
    """Lazy embedded adapter used by the private Railway runtime.

    No import of ``lightrag`` occurs until the first operation. Tests inject a
    factory and therefore do not need the optional package or external stores.
    """

    def __init__(
        self,
        *,
        instance_factory: InstanceFactory | None = None,
        dependency_probe: DependencyProbe | None = None,
    ) -> None:
        self._factory = instance_factory or self._default_factory
        self._enforce_distribution = instance_factory is None
        self._dependency_probe = dependency_probe or self._probe_dependencies
        self._instances: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # TokenTracker is attached to the cached Core instance.  Serialize all
        # operations that can invoke its model bindings so per-request usage is
        # exact and a concurrent index/query cannot reset another request.
        self._usage_locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        self._active_workspaces: dict[str, int] = {}
        self._cache_guard = asyncio.Lock()
        self._probe_guard = asyncio.Lock()
        self._probe_cached_at = 0.0
        self._probe_cache: dict[str, Any] | None = None

    @staticmethod
    def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    @staticmethod
    def _bounded_float(
        name: str, default: float, minimum: float, maximum: float
    ) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    def _cache_limit(self) -> int:
        return self._bounded_int("LIGHTRAG_WORKSPACE_CACHE_MAX", 64, 1, 512)

    def _cache_ttl_seconds(self) -> int:
        return self._bounded_int(
            "LIGHTRAG_WORKSPACE_CACHE_TTL_SECONDS", 1800, 60, 86400
        )

    def _check_distribution(self) -> None:
        if not self._enforce_distribution:
            return
        version = installed_lightrag_version()
        if not version or not lightrag_package_available():
            raise LightRAGUnavailableError(
                f"lightrag-hku=={TARGET_LIGHTRAG_VERSION} is not installed"
            )
        if _truthy("LIGHTRAG_STRICT_VERSION", True) and version != TARGET_LIGHTRAG_VERSION:
            raise LightRAGConfigurationError(
                f"LightRAG version mismatch; expected {TARGET_LIGHTRAG_VERSION}"
            )

    async def _default_factory(self, workspace: str) -> Any:
        self._check_distribution()
        from lightrag import LightRAG  # type: ignore[import-not-found]

        bindings = build_provider_bindings()
        from lightrag.utils import TokenTracker  # type: ignore[import-not-found]

        token_tracker = TokenTracker()
        role_llm_configs = deepcopy(bindings.role_llm_configs)
        for role_config in role_llm_configs.values():
            if isinstance(role_config, dict):
                role_config["kwargs"] = {
                    **dict(role_config.get("kwargs") or {}),
                    "token_tracker": token_tracker,
                }
        root = Path(os.getenv("LIGHTRAG_WORKING_DIR", "/tmp/graphrag-lightrag"))
        working_dir = root / workspace
        working_dir.mkdir(parents=True, exist_ok=True)
        rag = LightRAG(
            working_dir=str(working_dir),
            workspace=workspace,
            kv_storage=os.getenv("LIGHTRAG_KV_STORAGE", "PGKVStorage"),
            vector_storage=os.getenv("LIGHTRAG_VECTOR_STORAGE", "PGVectorStorage"),
            graph_storage=os.getenv("LIGHTRAG_GRAPH_STORAGE", "Neo4JStorage"),
            doc_status_storage=os.getenv("LIGHTRAG_DOC_STATUS_STORAGE", "PGDocStatusStorage"),
            llm_model_name=bindings.settings.extract_model,
            llm_model_func=bindings.llm_model_func,
            llm_model_kwargs={"token_tracker": token_tracker},
            role_llm_configs=role_llm_configs,
            embedding_func=bindings.embedding_func,
            rerank_model_func=bindings.rerank_model_func,
            entity_extraction_use_json=_truthy("ENTITY_EXTRACTION_USE_JSON", True),
            # Parsing is performed once by the existing MinerU/local parser.
            # LightRAG receives stable page text and must not launch a second
            # multimodal parsing pipeline.
            vlm_process_enable=False,
            addon_params={
                "language": os.getenv(
                    "LIGHTRAG_SUMMARY_LANGUAGE",
                    os.getenv("SUMMARY_LANGUAGE", "Chinese"),
                ),
                "entity_types_guidance": (
                    "- TECHNOLOGY: 技术、工具、平台或方法\n"
                    "- CONCEPT: 概念、理论、流程或能力\n"
                    "- PERSON: 人物或角色\n"
                    "- ORGANIZATION: 组织、公司、学校或团队\n"
                    "- LOCATION: 地理位置"
                ),
            },
        )
        await rag.initialize_storages()
        # Studio-only metadata is intentionally kept on the in-memory object;
        # it is never written to the LightRAG stores or returned by health.
        rag._studio_token_tracker = token_tracker
        rag._studio_query_model = bindings.settings.query_model
        return rag

    async def _instance(self, workspace: str) -> Any:
        workspace = validate_workspace(workspace)
        async with self._cache_guard:
            cached = self._instances.get(workspace)
            if cached is not None:
                self._last_used[workspace] = time.monotonic()
            lock = self._locks.setdefault(workspace, asyncio.Lock())
        if cached is not None:
            await self._evict_instances(exclude={workspace})
            return cached
        async with lock:
            async with self._cache_guard:
                cached = self._instances.get(workspace)
            if cached is None:
                created = await _maybe_await(self._factory(workspace))
                async with self._cache_guard:
                    # The per-workspace creation lock makes this the normal
                    # branch.  The guard also keeps test/manual cache injection
                    # from leaking a just-created duplicate.
                    cached = self._instances.setdefault(workspace, created)
                    self._last_used[workspace] = time.monotonic()
                if cached is not created:
                    await self._finalize_instance(created)
            else:
                async with self._cache_guard:
                    self._last_used[workspace] = time.monotonic()
        await self._evict_instances(exclude={workspace})
        return cached

    @staticmethod
    async def _finalize_instance(rag: Any) -> None:
        finalize = getattr(rag, "finalize_storages", None)
        if callable(finalize):
            try:
                await _maybe_await(finalize())
            except Exception:
                # Eviction is best-effort cleanup.  The instance is already
                # detached from the cache and must never block user traffic.
                return

    async def _evict_instances(self, *, exclude: set[str] | None = None) -> None:
        excluded = set(exclude or ())
        now = time.monotonic()
        stale_before = now - self._cache_ttl_seconds()
        evicted: list[Any] = []
        async with self._cache_guard:
            candidates = [
                workspace
                for workspace in self._instances
                if workspace not in excluded
                and self._active_workspaces.get(workspace, 0) == 0
                and not (
                    self._locks.get(workspace)
                    and self._locks[workspace].locked()
                )
                and not (
                    self._usage_locks.get(workspace)
                    and self._usage_locks[workspace].locked()
                )
            ]
            candidates.sort(key=lambda item: self._last_used.get(item, 0.0))
            selected = {
                workspace
                for workspace in candidates
                if self._last_used.get(workspace, 0.0) <= stale_before
            }
            remaining = len(self._instances) - len(selected)
            for workspace in candidates:
                if remaining <= self._cache_limit():
                    break
                if workspace in selected:
                    continue
                selected.add(workspace)
                remaining -= 1
            for workspace in selected:
                rag = self._instances.pop(workspace, None)
                if rag is not None:
                    evicted.append(rag)
                self._last_used.pop(workspace, None)
                self._active_workspaces.pop(workspace, None)
                self._locks.pop(workspace, None)
                self._usage_locks.pop(workspace, None)
        for rag in evicted:
            await self._finalize_instance(rag)

    @asynccontextmanager
    async def _instance_lease(self, workspace: str) -> AsyncIterator[Any]:
        workspace = validate_workspace(workspace)
        async with self._cache_guard:
            self._active_workspaces[workspace] = (
                self._active_workspaces.get(workspace, 0) + 1
            )
            self._last_used[workspace] = time.monotonic()
        try:
            yield await self._instance(workspace)
        finally:
            async with self._cache_guard:
                active = max(0, self._active_workspaces.get(workspace, 1) - 1)
                if active:
                    self._active_workspaces[workspace] = active
                else:
                    self._active_workspaces.pop(workspace, None)
                if workspace in self._instances:
                    self._last_used[workspace] = time.monotonic()
            await self._evict_instances()

    @staticmethod
    def _has_any(*names: str) -> bool:
        return any(bool(os.getenv(name, "").strip()) for name in names)

    @classmethod
    def _provider_components(cls) -> tuple[dict[str, Any], dict[str, Any]]:
        llm_configured = (
            cls._has_any("LIGHTRAG_LLM_API_KEY", "LLM_BINDING_API_KEY", "LLM_API_KEY")
            and cls._has_any("LIGHTRAG_LLM_BASE_URL", "LLM_BINDING_HOST", "LLM_BASE_URL")
            and cls._has_any(
                "LIGHTRAG_INDEX_MODEL",
                "EXTRACT_LLM_MODEL",
                "LLM_INDEX_MODEL",
                "LLM_MODEL",
            )
            and cls._has_any(
                "LIGHTRAG_EMBEDDING_MODEL",
                "EMBEDDING_MODEL",
                "LLM_EMBEDDING_MODEL",
            )
            and cls._has_any(
                "LIGHTRAG_EMBEDDING_DIM",
                "EMBEDDING_DIM",
                "LLM_EMBEDDING_DIMENSIONS",
            )
        )
        reranker_configured = (
            cls._has_any("LIGHTRAG_RERANK_API_KEY", "RERANK_BINDING_API_KEY")
            and cls._has_any("LIGHTRAG_RERANK_BASE_URL", "RERANK_BINDING_HOST")
        )
        llm = {
            "status": "ok" if llm_configured else "error",
            "configured": llm_configured,
            "binding": "openai-compatible",
        }
        reranker = {
            "status": "ok" if reranker_configured else "error",
            "configured": reranker_configured,
            "model": os.getenv(
                "LIGHTRAG_RERANK_MODEL",
                os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
            ),
        }
        return llm, reranker

    @staticmethod
    def _scrub_metrics(value: Any) -> Any:
        """Remove auth-bearing values before exposing runtime queue metrics."""

        markers = ("secret", "password", "credential", "api_key", "auth", "token")
        safe_string_keys = {
            "status",
            "backend",
            "binding",
            "model",
            "error_type",
            "version",
            "target_version",
        }
        if isinstance(value, Mapping):
            scrubbed: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                lowered = name.lower()
                if any(marker in lowered for marker in markers):
                    continue
                if isinstance(item, str):
                    if lowered in safe_string_keys:
                        scrubbed[name] = item[:128]
                    continue
                scrubbed[name] = LocalLightRAGAdapter._scrub_metrics(item)
            return scrubbed
        if isinstance(value, (list, tuple)):
            return [
                LocalLightRAGAdapter._scrub_metrics(item)
                for item in value
                if not isinstance(item, str)
            ]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, str):
            return None
        return str(type(value).__name__)

    @staticmethod
    def _queue_depth(value: Any) -> int:
        depth_keys = {"queued", "queue_size", "pending", "waiting", "live_queued"}
        if isinstance(value, Mapping):
            total = 0
            for key, item in value.items():
                if str(key).lower() in depth_keys and isinstance(item, (int, float)):
                    total += max(0, int(item))
                elif isinstance(item, (Mapping, list, tuple)):
                    total += LocalLightRAGAdapter._queue_depth(item)
            return total
        if isinstance(value, (list, tuple)):
            return sum(LocalLightRAGAdapter._queue_depth(item) for item in value)
        return 0

    async def _runtime_metrics(self) -> tuple[dict[str, Any], int]:
        metrics: dict[str, Any] = {
            "cached_workspaces": len(self._instances),
            "llm_queues": [],
            "embedding_queues": [],
            "rerank_queues": [],
        }
        queue_depth = 0
        getters = {
            "llm_queues": "get_llm_queue_status",
            "embedding_queues": "get_embedding_queue_status",
            "rerank_queues": "get_rerank_queue_status",
        }
        async with self._cache_guard:
            instances = list(self._instances.items())
            for workspace, _ in instances:
                self._active_workspaces[workspace] = (
                    self._active_workspaces.get(workspace, 0) + 1
                )
        try:
            for _, rag in instances:
                for metric_name, getter_name in getters.items():
                    getter = getattr(rag, getter_name, None)
                    if not callable(getter):
                        continue
                    try:
                        snapshot = self._scrub_metrics(
                            await _maybe_await(getter())
                        )
                    except Exception:
                        snapshot = {"available": False, "status": "error"}
                    metrics[metric_name].append(snapshot)
                    queue_depth += self._queue_depth(snapshot)
        finally:
            async with self._cache_guard:
                for workspace, _ in instances:
                    active = max(
                        0, self._active_workspaces.get(workspace, 1) - 1
                    )
                    if active:
                        self._active_workspaces[workspace] = active
                    else:
                        self._active_workspaces.pop(workspace, None)
            await self._evict_instances()
        return metrics, queue_depth

    def _probe_timeout(self) -> float:
        return self._bounded_float(
            "LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS", 5.0, 1.0, 15.0
        )

    @staticmethod
    def _postgres_probe_sync() -> dict[str, Any]:
        import psycopg

        timeout = max(
            1,
            int(
                LocalLightRAGAdapter._bounded_float(
                    "LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS", 5.0, 1.0, 15.0
                )
                / 2
            ),
        )
        connection = psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.getenv("POSTGRES_DATABASE") or os.environ["POSTGRES_DB"],
            sslmode=os.getenv("POSTGRES_SSL_MODE", "require"),
            connect_timeout=timeout,
            options=f"-c statement_timeout={timeout * 1000}",
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
                )
                row = cursor.fetchone()
                pgvector = bool(row and row[0])
                # A read-only credential is not sufficient for indexing.  A
                # temporary table plus rollback proves write capability while
                # leaving no persistent tenant or schema data behind.
                cursor.execute(
                    "CREATE TEMP TABLE lightrag_readiness_probe (ready integer) ON COMMIT DROP"
                )
                cursor.execute(
                    "INSERT INTO lightrag_readiness_probe (ready) VALUES (1)"
                )
                cursor.execute("SELECT ready FROM lightrag_readiness_probe")
                cursor.fetchone()
                connection.rollback()
            if not pgvector:
                raise RuntimeError("pgvector extension is unavailable")
            return {"reachable": True, "pgvector": True, "writable": True}
        finally:
            connection.close()

    @staticmethod
    def _neo4j_probe_sync() -> dict[str, Any]:
        from neo4j import GraphDatabase

        timeout = max(
            1.0,
            LocalLightRAGAdapter._bounded_float(
                "LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS", 5.0, 1.0, 15.0
            )
            / 3,
        )
        driver = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(
                os.getenv("NEO4J_USERNAME") or os.environ["NEO4J_USER"],
                os.environ["NEO4J_PASSWORD"],
            ),
            connection_timeout=timeout,
            connection_acquisition_timeout=timeout,
            liveness_check_timeout=timeout,
            max_transaction_retry_time=timeout,
        )
        try:
            driver.verify_connectivity()
            with driver.session(
                database=os.getenv("NEO4J_DATABASE", "neo4j")
            ) as session:
                transaction = session.begin_transaction(timeout=timeout)
                try:
                    transaction.run(
                        "CREATE (probe:`_LightRAGReadinessProbe` {ready: true}) "
                        "RETURN probe.ready AS ready"
                    ).consume()
                finally:
                    transaction.rollback()
            return {"reachable": True, "writable": True}
        finally:
            driver.close()

    @staticmethod
    def _openai_endpoint(base_url: str, suffix: str) -> str:
        base = str(base_url or "").strip().rstrip("/")
        normalized_suffix = "/" + suffix.strip("/")
        return base if urlparse(base).path.endswith(normalized_suffix) else f"{base}{normalized_suffix}"

    @staticmethod
    def _rerank_endpoint(base_url: str) -> str:
        base = str(base_url or "").strip().rstrip("/")
        path = urlparse(base).path.lower().rstrip("/")
        return base if path.endswith(("/rerank", "/rerankings")) else f"{base}/rerank"

    @staticmethod
    def _llm_probe_sync() -> dict[str, Any]:
        import requests

        settings = load_provider_settings()
        models = list(dict.fromkeys((settings.query_model, settings.extract_model)))
        request_timeout = max(
            1.0,
            LocalLightRAGAdapter._bounded_float(
                "LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS", 5.0, 1.0, 15.0
            )
            / (len(models) + 0.5),
        )
        for model in models:
            response = requests.post(
                LocalLightRAGAdapter._openai_endpoint(
                    settings.llm_base_url, "chat/completions"
                ),
                headers={
                    "Authorization": f"Bearer {settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "health"}],
                    "temperature": 0,
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping) or not payload.get("choices"):
                raise RuntimeError("LLM readiness response is invalid")
        return {
            "reachable": True,
            "model": settings.query_model,
            "verified_models": len(models),
        }

    @staticmethod
    def _embedding_probe_sync() -> dict[str, Any]:
        import requests

        settings = load_provider_settings()
        response = requests.post(
            LocalLightRAGAdapter._openai_endpoint(
                settings.embedding_base_url, "embeddings"
            ),
            headers={
                "Authorization": f"Bearer {settings.embedding_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.embedding_model,
                "input": ["health"],
                "dimensions": settings.embedding_dim,
            },
            timeout=max(
                1.0,
                LocalLightRAGAdapter._bounded_float(
                    "LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS", 5.0, 1.0, 15.0
                )
                * 0.8,
            ),
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        embedding = (
            data[0].get("embedding")
            if isinstance(data, list) and data and isinstance(data[0], Mapping)
            else None
        )
        if not isinstance(embedding, list) or len(embedding) != settings.embedding_dim:
            raise RuntimeError("embedding readiness response has the wrong dimension")
        return {
            "reachable": True,
            "model": settings.embedding_model,
            "dimensions": len(embedding),
        }

    @staticmethod
    def _reranker_probe_sync() -> dict[str, Any]:
        import requests

        settings = load_provider_settings()
        response = requests.post(
            LocalLightRAGAdapter._rerank_endpoint(settings.rerank_base_url),
            headers={
                "Authorization": f"Bearer {settings.rerank_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.rerank_model,
                "query": "health",
                "documents": ["health"],
                "top_n": 1,
                "return_documents": False,
            },
            timeout=max(
                1.0,
                LocalLightRAGAdapter._bounded_float(
                    "LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS", 5.0, 1.0, 15.0
                )
                * 0.8,
            ),
        )
        response.raise_for_status()
        return {"reachable": True, "model": settings.rerank_model}

    @staticmethod
    def _queue_profile() -> dict[str, Any]:
        raw = os.getenv("GRAPHRAG_QUEUE_BACKEND", "local_thread").strip().lower()
        local_aliases = {
            "local_thread",
            "thread",
            "threads",
            "local",
            "memory",
            "in_memory",
        }
        remote_aliases = {"upstash", "redis", "upstash_redis"}
        if raw in local_aliases:
            backend = "local_thread"
        elif raw in remote_aliases:
            backend = "upstash"
        else:
            backend = raw or "unknown"
        production_name = any(
            os.getenv(name, "").strip().lower() in {"prod", "production"}
            for name in ("ENVIRONMENT", "APP_ENV", "RAILWAY_ENVIRONMENT_NAME")
        )
        on_railway = bool(os.getenv("RAILWAY_PROJECT_ID", "").strip())
        durable_required = (
            production_name
            or on_railway
            or _truthy("LIGHTRAG_REQUIRE_DURABLE_QUEUE", False)
        )
        return {
            "backend": backend,
            "known": raw in local_aliases or raw in remote_aliases,
            "remote": backend == "upstash",
            "durable_required": durable_required,
        }

    @staticmethod
    def _queue_probe_sync() -> dict[str, Any]:
        from storage.queue_repository import get_queue_repository

        profile = LocalLightRAGAdapter._queue_profile()
        if not profile["known"]:
            raise LightRAGConfigurationError("unsupported index queue backend")
        result = get_queue_repository().health()
        if not isinstance(result, Mapping) or result.get("status") != "ok":
            raise RuntimeError("index queue is unavailable")
        durable = bool(result.get("durable"))
        if profile["durable_required"] and not durable:
            raise LightRAGConfigurationError(
                "a durable index queue is required in production"
            )
        return {
            "reachable": True,
            "backend": str(result.get("backend") or "unknown"),
            "durable": durable,
            "durable_required": bool(profile["durable_required"]),
        }

    async def _timed_probe(
        self, name: str, callback: Callable[[], Mapping[str, Any]]
    ) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        try:
            detail = await asyncio.wait_for(
                asyncio.to_thread(callback), timeout=self._probe_timeout()
            )
            safe = self._scrub_metrics(detail)
            result = dict(safe) if isinstance(safe, Mapping) else {}
            result.update(
                {
                    "status": "ok",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
        except Exception as exc:
            result = {
                "status": "error",
                "reachable": False,
                "error_type": type(exc).__name__,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        return name, result

    async def _probe_dependencies(self) -> Mapping[str, Any]:
        probes = (
            ("postgres", self._postgres_probe_sync),
            ("neo4j", self._neo4j_probe_sync),
            ("llm", self._llm_probe_sync),
            ("embedding", self._embedding_probe_sync),
            ("reranker", self._reranker_probe_sync),
            ("queue", self._queue_probe_sync),
        )
        results = await asyncio.gather(
            *(self._timed_probe(name, callback) for name, callback in probes)
        )
        return dict(results)

    async def _dependency_status(self) -> dict[str, Any]:
        ttl = self._bounded_float(
            "LIGHTRAG_HEALTH_PROBE_TTL_SECONDS", 300.0, 5.0, 900.0
        )
        now = time.monotonic()
        if self._probe_cache is not None and now - self._probe_cached_at < ttl:
            return deepcopy(self._probe_cache)
        async with self._probe_guard:
            now = time.monotonic()
            if self._probe_cache is not None and now - self._probe_cached_at < ttl:
                return deepcopy(self._probe_cache)
            try:
                raw = await _maybe_await(self._dependency_probe())
                if not isinstance(raw, Mapping):
                    raise TypeError("dependency probe must return a mapping")
                safe = self._scrub_metrics(raw)
                self._probe_cache = dict(safe) if isinstance(safe, Mapping) else {}
            except Exception as exc:
                self._probe_cache = {
                    name: {
                        "status": "error",
                        "reachable": False,
                        "error_type": type(exc).__name__,
                    }
                    for name in (
                        "postgres",
                        "neo4j",
                        "llm",
                        "embedding",
                        "reranker",
                        "queue",
                    )
                }
            self._probe_cached_at = time.monotonic()
            return deepcopy(self._probe_cache)

    async def health(self) -> dict[str, Any]:
        version = installed_lightrag_version()
        package_available = lightrag_package_available()
        version_ok = version == TARGET_LIGHTRAG_VERSION
        package_ready = package_available and (
            version_ok or not _truthy("LIGHTRAG_STRICT_VERSION", True)
        )
        postgres_configured = (
            all(
                os.getenv(name, "").strip()
                for name in ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_PASSWORD")
            )
            and self._has_any("POSTGRES_DATABASE", "POSTGRES_DB")
            and os.getenv("LIGHTRAG_KV_STORAGE", "PGKVStorage") == "PGKVStorage"
            and os.getenv("LIGHTRAG_VECTOR_STORAGE", "PGVectorStorage")
            == "PGVectorStorage"
            and os.getenv("LIGHTRAG_DOC_STATUS_STORAGE", "PGDocStatusStorage")
            == "PGDocStatusStorage"
        )
        neo4j_configured = (
            self._has_any("NEO4J_URI")
            and self._has_any("NEO4J_USERNAME", "NEO4J_USER")
            and self._has_any("NEO4J_PASSWORD")
            and os.getenv("LIGHTRAG_GRAPH_STORAGE", "Neo4JStorage") == "Neo4JStorage"
        )
        llm_component, reranker_component = self._provider_components()
        embedding_configured = (
            self._has_any(
                "LIGHTRAG_EMBEDDING_API_KEY",
                "EMBEDDING_BINDING_API_KEY",
                "LIGHTRAG_LLM_API_KEY",
                "LLM_BINDING_API_KEY",
                "LLM_API_KEY",
            )
            and self._has_any(
                "LIGHTRAG_EMBEDDING_BASE_URL",
                "EMBEDDING_BINDING_HOST",
                "LIGHTRAG_LLM_BASE_URL",
                "LLM_BINDING_HOST",
                "LLM_BASE_URL",
            )
            and self._has_any(
                "LIGHTRAG_EMBEDDING_MODEL",
                "EMBEDDING_MODEL",
                "LLM_EMBEDDING_MODEL",
            )
            and self._has_any(
                "LIGHTRAG_EMBEDDING_DIM",
                "EMBEDDING_DIM",
                "LLM_EMBEDDING_DIMENSIONS",
            )
        )
        queue_profile = self._queue_profile()
        queue_has_credentials = (
            self._has_any("UPSTASH_REDIS_REST_URL", "KV_REST_API_URL")
            and self._has_any("UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN")
        )
        queue_configured = bool(queue_profile["known"]) and (
            (bool(queue_profile["remote"]) and queue_has_credentials)
            or (
                not queue_profile["remote"]
                and not queue_profile["durable_required"]
            )
        )
        nonce_profile = NonceReplayCache.distributed_profile()
        security_component = {
            "status": "ok" if (
                not nonce_profile["required"] or nonce_profile["configured"]
            ) else "error",
            "configured": nonce_profile["configured"],
            "distributed_nonce_required": nonce_profile["required"],
        }
        provider_configured = False
        try:
            load_provider_settings()
            provider_configured = True
        except LightRAGConfigurationError:
            provider_configured = False
        neo4j = {
            "status": "error",
            "configured": neo4j_configured,
            "backend": os.getenv("LIGHTRAG_GRAPH_STORAGE", "Neo4JStorage"),
        }
        postgres = {
            "status": "error",
            "configured": postgres_configured,
            "backend": "PGKVStorage+PGVectorStorage+PGDocStatusStorage",
            "pgvector": False,
        }
        queue = {
            "status": "error",
            "configured": queue_configured,
            "backend": queue_profile["backend"],
            "durable": bool(queue_profile["remote"]),
            "durable_required": bool(queue_profile["durable_required"]),
        }
        embedding_component = {
            "status": "error",
            "configured": embedding_configured,
            "binding": "openai-compatible",
        }
        probes = await self._dependency_status()

        def merge_probe(
            component: dict[str, Any], name: str, *, configured: bool
        ) -> dict[str, Any]:
            detail = probes.get(name)
            if isinstance(detail, Mapping):
                component.update(dict(detail))
            component["configured"] = configured
            if not configured or component.get("status") != "ok":
                component["status"] = "error"
            return component

        postgres = merge_probe(postgres, "postgres", configured=postgres_configured)
        neo4j = merge_probe(neo4j, "neo4j", configured=neo4j_configured)
        llm_component = merge_probe(
            llm_component, "llm", configured=bool(llm_component["configured"])
        )
        embedding_component = merge_probe(
            embedding_component,
            "embedding",
            configured=embedding_configured,
        )
        reranker_component = merge_probe(
            reranker_component,
            "reranker",
            configured=bool(reranker_component["configured"]),
        )
        queue = merge_probe(queue, "queue", configured=queue_configured)
        worker_ready = (
            package_ready
            and provider_configured
            and all(
                component["status"] == "ok"
                for component in (
                    postgres,
                    neo4j,
                    llm_component,
                    embedding_component,
                    reranker_component,
                    queue,
                )
            )
        )
        worker = {
            "status": "ok" if worker_ready else "error",
            "configured": (
                package_ready
                and provider_configured
                and postgres_configured
                and neo4j_configured
                and queue_configured
            ),
            "version": version,
            "target_version": TARGET_LIGHTRAG_VERSION,
        }
        metrics, queue_depth = await self._runtime_metrics()
        components = {
            "worker": worker,
            "api": dict(worker),
            "llm": llm_component,
            "embedding": embedding_component,
            "neo4j": neo4j,
            "graph_database": dict(neo4j),
            "postgres": postgres,
            "vector_database": dict(postgres),
            "reranker": reranker_component,
            "queue": queue,
            "security": security_component,
        }
        status = "ready" if all(
            components[name]["status"] == "ok"
            for name in (
                "worker",
                "neo4j",
                "postgres",
                "llm",
                "embedding",
                "reranker",
                "queue",
                "security",
            )
        ) else "error"
        return {
            "status": status,
            "transport": "local",
            "target_version": TARGET_LIGHTRAG_VERSION,
            "installed_version": version,
            "package_available": package_available,
            "version_ok": version_ok,
            "cached_workspaces": len(self._instances),
            "workspace_cache_max": self._cache_limit(),
            "workspace_cache_ttl_seconds": self._cache_ttl_seconds(),
            "components": components,
            "queue_depth": queue_depth,
            "metrics": metrics,
        }

    async def index_pages(
        self, *, workspace: str, doc_id: str, filename: str, pages: Sequence[Any]
    ) -> dict[str, Any]:
        workspace = validate_workspace(workspace)
        normalized = coerce_pages(pages)
        page_ids = [page_document_id(workspace, doc_id, page.page) for page in normalized]
        paths = [source_path(doc_id, filename, page.page) for page in normalized]
        async with self._instance_lease(workspace) as rag:
            async with self._usage_locks.setdefault(workspace, asyncio.Lock()):
                await rag.ainsert(
                    [page.content for page in normalized],
                    ids=page_ids,
                    file_paths=paths,
                )
        return {
            "engine": "lightrag",
            "status": "done",
            "doc_id": doc_id,
            "indexed_pages": len(normalized),
            "stats": {"pages": len(normalized)},
            "page_ids": page_ids,
            "pages": [page.page for page in normalized],
        }

    async def delete_document(
        self,
        *,
        workspace: str,
        doc_id: str,
        page_count: int | None = None,
        page_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        workspace = validate_workspace(workspace)
        ids = [str(item).strip() for item in (page_ids or []) if str(item).strip()]
        if not ids and page_count:
            ids = [page_document_id(workspace, doc_id, page) for page in range(1, int(page_count) + 1)]
        if not ids:
            raise LightRAGValidationError(
                "page_ids or page_count is required to delete a page-indexed LightRAG document"
            )
        deleted: list[str] = []
        failed: list[str] = []
        async with self._instance_lease(workspace) as rag:
            async with self._usage_locks.setdefault(workspace, asyncio.Lock()):
                for page_id in dict.fromkeys(ids):
                    result = await rag.adelete_by_doc_id(page_id)
                    status = str(
                        getattr(result, "status", "success") or "success"
                    ).lower()
                    if status in {"success", "deleted", "ok"}:
                        deleted.append(page_id)
                    else:
                        failed.append(page_id)
        return {
            "engine": "lightrag",
            "doc_id": doc_id,
            "deleted": not failed,
            "deleted_page_ids": deleted,
            "failed_page_ids": failed,
        }

    async def run_query(
        self,
        *,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> dict[str, Any]:
        completed: dict[str, Any] | None = None
        async for event in self.stream_query(
            workspace=workspace,
            question=question,
            mode=mode,
            history=history,
            allowed_doc_ids=allowed_doc_ids,
            include_references=include_references,
        ):
            if event.get("event") == "done" and isinstance(event.get("data"), dict):
                completed = event["data"]
        if completed is None:
            raise LightRAGProtocolError("LightRAG stream ended without final metadata")
        return completed

    @staticmethod
    def _normalized_history(
        history: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        # Keep the product's existing history boundary even when the internal
        # endpoint is called independently from the public gateway.
        for item in list(history)[-8:]:
            role = str(item.get("role") or "").lower()
            role = (
                "user"
                if role in {"human", "user"}
                else "assistant"
                if role in {"ai", "assistant"}
                else role
            )
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                normalized.append({"role": role, "content": content})
        return normalized

    @staticmethod
    def _tracker_usage(rag: Any) -> dict[str, Any]:
        tracker = getattr(rag, "_studio_token_tracker", None)
        getter = getattr(tracker, "get_usage", None)
        if not callable(getter):
            return {}
        raw = getter()
        usage = dict(raw) if isinstance(raw, Mapping) else {}
        # Zero is an actual tracker result (not an estimate), notably on cache
        # hits.  Do not replace it with guessed token counts.
        usage["estimated"] = False
        return usage

    @staticmethod
    def _reset_tracker(rag: Any) -> None:
        reset = getattr(getattr(rag, "_studio_token_tracker", None), "reset", None)
        if callable(reset):
            reset()

    @staticmethod
    def _strictly_filter_query_data(
        raw: Any,
        allowed_doc_ids: set[str] | None,
    ) -> dict[str, Any]:
        """Keep only page chunks from explicitly allowed documents.

        LightRAG 1.5.4 has no document-id predicate in ``QueryParam``.  Entity
        and relation descriptions may aggregate sources from several documents,
        so post-filtering citations alone is unsafe.  We therefore use graph
        retrieval to select page chunks, discard every out-of-scope chunk, and
        synthesize solely from the retained page text.
        """

        root = dict(raw) if isinstance(raw, Mapping) else {}
        raw_data = root.get("data")
        data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
        chunks: list[dict[str, Any]] = []
        reference_ids: set[str] = set()
        for raw_chunk in data.get("chunks", []):
            if not isinstance(raw_chunk, Mapping):
                continue
            chunk = dict(raw_chunk)
            source = parse_source_path(
                chunk.get("file_path") or chunk.get("source_path")
            )
            doc_id = str(chunk.get("doc_id") or source.get("doc_id") or "")
            if allowed_doc_ids is not None and (
                not doc_id or doc_id not in allowed_doc_ids
            ):
                continue
            chunk["doc_id"] = doc_id
            chunks.append(chunk)
            ref_id = str(chunk.get("reference_id") or "")
            if ref_id:
                reference_ids.add(ref_id)

        references: list[dict[str, Any]] = []
        for raw_reference in data.get("references", []):
            if not isinstance(raw_reference, Mapping):
                continue
            reference = dict(raw_reference)
            source = parse_source_path(
                reference.get("file_path") or reference.get("source_path")
            )
            doc_id = str(reference.get("doc_id") or source.get("doc_id") or "")
            ref_id = str(reference.get("reference_id") or "")
            doc_allowed = (
                allowed_doc_ids is None or doc_id in allowed_doc_ids
            )
            if doc_allowed and (not reference_ids or ref_id in reference_ids):
                reference["doc_id"] = doc_id
                references.append(reference)

        filtered = {
            **root,
            "status": "success" if chunks else "failure",
            "message": (
                "Query executed successfully"
                if chunks
                else "No relevant chunks in the selected documents"
            ),
            "data": {
                # Aggregated descriptions are deliberately excluded from the
                # synthesis payload; page chunks remain the sole evidence.
                "entities": [],
                "relationships": [],
                "chunks": chunks,
                "references": references,
            },
        }
        return filtered

    @classmethod
    def _merge_query_contexts(
        cls,
        contexts: Sequence[Mapping[str, Any]],
        *,
        include_references: bool,
    ) -> dict[str, Any]:
        """Round-robin, deduplicate and bound isolated retrieval results.

        The limits deliberately match a normal LightRAG retrieval-sized
        context rather than multiplying it by the number of workspaces.  This
        prevents a public corpus from crowding out private evidence (or vice
        versa) and keeps the existing model context/output contract intact.
        """

        max_chunks = cls._bounded_int(
            "LIGHTRAG_MERGED_CONTEXT_MAX_CHUNKS", 20, 1, 100
        )
        max_chars = cls._bounded_int(
            "LIGHTRAG_MERGED_CONTEXT_MAX_CHARS", 60000, 1000, 500000
        )
        queues: list[list[dict[str, Any]]] = []
        references_by_scope: list[dict[str, dict[str, Any]]] = []
        for context in contexts:
            raw_data = context.get("data")
            data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
            queues.append(
                [dict(chunk) for chunk in data.get("chunks", []) if isinstance(chunk, Mapping)]
            )
            references_by_scope.append({
                str(reference.get("reference_id") or ""): dict(reference)
                for reference in data.get("references", [])
                if isinstance(reference, Mapping)
            })

        selected_chunks: list[dict[str, Any]] = []
        selected_references: list[dict[str, Any]] = []
        seen_chunks: set[tuple[str, int, str, str]] = set()
        consumed_chars = 0
        positions = [0] * len(queues)
        while len(selected_chunks) < max_chunks and consumed_chars < max_chars:
            progressed = False
            for scope_index, queue in enumerate(queues):
                while positions[scope_index] < len(queue):
                    chunk = dict(queue[positions[scope_index]])
                    positions[scope_index] += 1
                    source = parse_source_path(
                        chunk.get("file_path") or chunk.get("source_path")
                    )
                    doc_id = str(chunk.get("doc_id") or source.get("doc_id") or "")
                    try:
                        page = int(chunk.get("page") or source.get("page") or 0)
                    except (TypeError, ValueError):
                        page = 0
                    content = str(chunk.get("content") or "").strip()
                    chunk_id = str(
                        chunk.get("chunk_id")
                        or chunk.get("source_id")
                        or chunk.get("reference_id")
                        or ""
                    )
                    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
                    key = (doc_id, page, chunk_id, digest)
                    if not content or key in seen_chunks:
                        continue

                    remaining = max_chars - consumed_chars
                    if remaining <= 0:
                        break
                    if len(content) > remaining:
                        content = content[:remaining]
                    old_reference_id = str(chunk.get("reference_id") or "")
                    new_reference_id = str(len(selected_chunks) + 1)
                    chunk.update({
                        "content": content,
                        "doc_id": doc_id,
                        "page": page,
                        "reference_id": new_reference_id,
                    })
                    selected_chunks.append(chunk)
                    seen_chunks.add(key)
                    consumed_chars += len(content)

                    if include_references:
                        reference = dict(
                            references_by_scope[scope_index].get(
                                old_reference_id, {}
                            )
                        )
                        reference.update({
                            "reference_id": new_reference_id,
                            "doc_id": doc_id,
                            "page": page,
                            "chunk_id": chunk_id or new_reference_id,
                            "file_path": str(
                                chunk.get("file_path")
                                or chunk.get("source_path")
                                or reference.get("file_path")
                                or reference.get("source_path")
                                or ""
                            ),
                            "content": content,
                        })
                        selected_references.append(reference)
                    progressed = True
                    break
                if len(selected_chunks) >= max_chunks or consumed_chars >= max_chars:
                    break
            if not progressed:
                break

        return {
            "status": "success" if selected_chunks else "failure",
            "message": (
                "Query context merged successfully"
                if selected_chunks
                else "No relevant chunks in the selected documents"
            ),
            "data": {
                "entities": [],
                "relationships": [],
                "chunks": selected_chunks,
                "references": selected_references,
            },
        }

    @staticmethod
    def _merge_usage_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        has_estimated = False
        estimated = False
        for record in records:
            if "estimated" in record:
                has_estimated = True
                estimated = estimated or bool(record.get("estimated"))
            for key, value in record.items():
                if (
                    key != "estimated"
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    merged[key] = merged.get(key, 0) + value
        if has_estimated:
            merged["estimated"] = estimated
        return merged

    @staticmethod
    def _filtered_system_prompt(raw: Mapping[str, Any]) -> str:
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        chunks = data.get("chunks", []) if isinstance(data, Mapping) else []
        evidence: list[str] = []
        for position, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, Mapping):
                continue
            reference_id = str(chunk.get("reference_id") or position)
            source = parse_source_path(
                chunk.get("file_path") or chunk.get("source_path")
            )
            content = str(chunk.get("content") or "").strip()
            if content:
                evidence.append(
                    f"[{reference_id}] {source.get('filename') or 'document'} "
                    f"page {source.get('page') or 0}\n{content}"
                )
        context = "\n\n".join(evidence)
        return (
            "你是 GraphRAG Studio 的问答助手。只能依据下方已授权文档片段回答；"
            "不得使用未提供的文档内容或臆测事实。信息不足时明确说明。引用事实时使用"
            "对应的 [编号]。\n\n--- 已授权证据 ---\n" + context
        )

    @staticmethod
    async def _response_chunks(raw: Mapping[str, Any]) -> AsyncIterator[str]:
        llm_response = raw.get("llm_response")
        llm = dict(llm_response) if isinstance(llm_response, Mapping) else {}
        iterator = llm.get("response_iterator")
        if llm.get("is_streaming") and iterator is not None:
            async for chunk in iterator:
                text = str(chunk or "")
                if text:
                    yield text
            return
        content = str(llm.get("content") or raw.get("answer") or raw.get("response") or "")
        if content:
            yield content

    async def stream_query(
        self,
        *,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        workspace = validate_workspace(workspace)
        question = str(question or "").strip()
        if not question:
            raise LightRAGValidationError("question is required")
        async with self._instance_lease(workspace) as rag:
            async for event in self._stream_query_with_instance(
                rag=rag,
                workspace=workspace,
                question=question,
                mode=mode,
                history=history,
                allowed_doc_ids=allowed_doc_ids,
                include_references=include_references,
            ):
                yield event

    async def _retrieve_query_scope(
        self,
        *,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from lightrag import QueryParam  # type: ignore[import-not-found]

        if allowed_doc_ids is not None and not allowed_doc_ids:
            return (
                self._strictly_filter_query_data({}, allowed_doc_ids),
                {},
            )
        async with self._instance_lease(workspace) as rag:
            async with self._usage_locks.setdefault(workspace, asyncio.Lock()):
                retrieve = getattr(rag, "aquery_data", None)
                if not callable(retrieve):
                    raise LightRAGProtocolError(
                        "LightRAG Core does not expose document-safe retrieval"
                    )
                self._reset_tracker(rag)
                param = QueryParam(
                    mode=mode.value,
                    conversation_history=self._normalized_history(history),
                    include_references=include_references,
                    stream=False,
                    enable_rerank=True,
                )
                retrieved = await retrieve(question, param=param)
                filtered = self._strictly_filter_query_data(
                    retrieved, allowed_doc_ids
                )
                self._enrich_reference_content(filtered)
                return filtered, self._tracker_usage(rag)

    async def stream_query_scopes(
        self,
        *,
        workspaces: Sequence[str],
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        """Retrieve isolated scopes, then synthesize one answer.

        Each Core instance performs retrieval only inside its immutable
        workspace.  No generated answer is accepted from those retrievals.
        Their document-filtered chunks are merged before a single bypass-mode
        query-model invocation on the primary instance.
        """

        normalized_workspaces = list(
            dict.fromkeys(validate_workspace(item) for item in workspaces)
        )
        if not normalized_workspaces:
            raise LightRAGValidationError("at least one workspace is required")
        if len(normalized_workspaces) > 9:
            raise LightRAGValidationError(
                "at most nine knowledge spaces are allowed"
            )
        question = str(question or "").strip()
        if not question:
            raise LightRAGValidationError("question is required")
        if len(normalized_workspaces) == 1:
            async for event in self.stream_query(
                workspace=normalized_workspaces[0],
                question=question,
                mode=mode,
                history=history,
                allowed_doc_ids=allowed_doc_ids,
                include_references=include_references,
            ):
                yield event
            return

        started = time.perf_counter()
        retrieved = await asyncio.gather(*(
            self._retrieve_query_scope(
                workspace=workspace,
                question=question,
                mode=mode,
                history=history,
                allowed_doc_ids=allowed_doc_ids,
                include_references=include_references,
            )
            for workspace in normalized_workspaces
        ))
        contexts = [item[0] for item in retrieved]
        usage_records = [item[1] for item in retrieved if item[1]]
        merged = self._merge_query_contexts(
            contexts,
            include_references=include_references,
        )

        primary_workspace = normalized_workspaces[0]
        raw: dict[str, Any] = {
            **merged,
            "llm_response": {
                "content": "",
                "response_iterator": None,
                "is_streaming": False,
            },
        }
        model = ""
        if merged["data"]["chunks"]:
            from lightrag import QueryParam  # type: ignore[import-not-found]

            async with self._instance_lease(primary_workspace) as rag:
                async with self._usage_locks.setdefault(
                    primary_workspace, asyncio.Lock()
                ):
                    generate = getattr(rag, "aquery_llm", None)
                    if not callable(generate):
                        raise LightRAGProtocolError(
                            "LightRAG Core does not expose context-only synthesis"
                        )
                    self._reset_tracker(rag)
                    bypass = QueryParam(
                        mode="bypass",
                        conversation_history=self._normalized_history(history),
                        include_references=False,
                        stream=True,
                        enable_rerank=False,
                    )
                    generated = await generate(
                        question,
                        param=bypass,
                        system_prompt=self._filtered_system_prompt(merged),
                    )
                    if isinstance(generated, Mapping):
                        generated_data = dict(generated)
                        llm_response = generated_data.get("llm_response")
                        raw["llm_response"] = (
                            dict(llm_response)
                            if isinstance(llm_response, Mapping)
                            else {
                                "content": generated_data.get("response", ""),
                                "response_iterator": None,
                                "is_streaming": False,
                            }
                        )
                    else:
                        raw["llm_response"] = {
                            "content": generated if isinstance(generated, str) else None,
                            "response_iterator": (
                                generated if not isinstance(generated, str) else None
                            ),
                            "is_streaming": not isinstance(generated, str),
                        }

                    answer_parts: list[str] = []
                    async for chunk in self._response_chunks(raw):
                        answer_parts.append(chunk)
                        yield {"event": "answer_delta", "data": {"text": chunk}}
                    raw["llm_response"] = {
                        "content": "".join(answer_parts),
                        "response_iterator": None,
                        "is_streaming": False,
                    }
                    generation_usage = self._tracker_usage(rag)
                    if generation_usage:
                        usage_records.append(generation_usage)
                    model = str(getattr(rag, "_studio_query_model", "") or "")

        raw["metadata"] = {
            "usage": self._merge_usage_records(usage_records),
            "model": model,
        }
        result = normalize_query_result(
            raw,
            workspace=primary_workspace,
            mode=mode,
            allowed_doc_ids=allowed_doc_ids,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        result["workspace_scope_count"] = len(normalized_workspaces)
        yield {"event": "done", "data": result}

    async def _stream_query_with_instance(
        self,
        *,
        rag: Any,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        from lightrag import QueryParam  # type: ignore[import-not-found]

        normalized_history = self._normalized_history(history)
        param = QueryParam(
            mode=mode.value,
            conversation_history=normalized_history,
            include_references=include_references,
            stream=True,
            enable_rerank=True,
        )
        started = time.perf_counter()
        usage_lock = self._usage_locks.setdefault(workspace, asyncio.Lock())
        async with usage_lock:
            self._reset_tracker(rag)
            if allowed_doc_ids is not None:
                if not allowed_doc_ids:
                    raw: dict[str, Any] = {
                        "status": "failure",
                        "data": {"entities": [], "relationships": [], "chunks": [], "references": []},
                        "llm_response": {"content": "", "response_iterator": None, "is_streaming": False},
                    }
                else:
                    retrieve = getattr(rag, "aquery_data", None)
                    if not callable(retrieve):
                        raise LightRAGProtocolError(
                            "LightRAG Core does not expose document-safe retrieval"
                        )
                    retrieved = await retrieve(question, param=param)
                    filtered = self._strictly_filter_query_data(
                        retrieved, allowed_doc_ids
                    )
                    if filtered["data"]["chunks"]:
                        bypass = QueryParam(
                            mode="bypass",
                            conversation_history=normalized_history,
                            include_references=False,
                            stream=True,
                            enable_rerank=False,
                        )
                        generated = await rag.aquery_llm(
                            question,
                            param=bypass,
                            system_prompt=self._filtered_system_prompt(filtered),
                        )
                        generated = (
                            dict(generated)
                            if isinstance(generated, Mapping)
                            else {}
                        )
                        raw = {
                            **filtered,
                            "llm_response": generated.get(
                                "llm_response",
                                {
                                    "content": generated.get("response", ""),
                                    "response_iterator": None,
                                    "is_streaming": False,
                                },
                            ),
                        }
                    else:
                        raw = {
                            **filtered,
                            "llm_response": {
                                "content": "",
                                "response_iterator": None,
                                "is_streaming": False,
                            },
                        }
            elif callable(getattr(rag, "aquery_llm", None)):
                raw = await rag.aquery_llm(question, param=param)
            else:
                response = await rag.aquery(question, param=param)
                raw = {
                    "data": {},
                    "llm_response": {
                        "content": response if isinstance(response, str) else None,
                        "response_iterator": response if not isinstance(response, str) else None,
                        "is_streaming": not isinstance(response, str),
                    },
                }

            self._enrich_reference_content(raw)
            answer_parts: list[str] = []
            async for chunk in self._response_chunks(raw):
                answer_parts.append(chunk)
                yield {"event": "answer_delta", "data": {"text": chunk}}

            answer = "".join(answer_parts)
            final_raw = dict(raw) if isinstance(raw, Mapping) else {}
            final_raw["llm_response"] = {
                "content": answer,
                "response_iterator": None,
                "is_streaming": False,
            }
            metadata = (
                dict(final_raw.get("metadata"))
                if isinstance(final_raw.get("metadata"), Mapping)
                else {}
            )
            usage = self._tracker_usage(rag)
            if usage:
                metadata["usage"] = usage
            model = str(getattr(rag, "_studio_query_model", "") or "")
            if model:
                metadata["model"] = model
            final_raw["metadata"] = metadata
            result = normalize_query_result(
                final_raw,
                workspace=workspace,
                mode=mode,
                allowed_doc_ids=allowed_doc_ids,
                elapsed_seconds=round(time.perf_counter() - started, 3),
            )
            yield {"event": "done", "data": result}

    @staticmethod
    def _enrich_reference_content(raw: Any) -> None:
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            return
        data = raw["data"]
        references = data.get("references")
        chunks = data.get("chunks")
        if not isinstance(references, list) or not isinstance(chunks, list):
            return
        content_by_ref: dict[str, list[str]] = {}
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            ref_id = str(chunk.get("reference_id") or "")
            content = str(chunk.get("content") or "")
            if ref_id and content:
                content_by_ref.setdefault(ref_id, []).append(content)
        for reference in references:
            if isinstance(reference, dict):
                content = content_by_ref.get(str(reference.get("reference_id") or ""))
                if content:
                    reference["content"] = content

    async def _raw_graph(
        self, rag: Any, max_nodes: int, max_edges: int
    ) -> dict[str, Any]:
        graph_store = getattr(rag, "chunk_entity_relation_graph", None)
        labels: list[str] = []
        popular = getattr(graph_store, "get_popular_labels", None)
        if callable(popular):
            labels.extend(list(await _maybe_await(popular(min(max_nodes, 200)))))
        if callable(getattr(rag, "get_graph_labels", None)):
            labels.extend(list(await rag.get_graph_labels()))
        labels = list(dict.fromkeys(str(label) for label in labels if str(label)))
        nodes: list[Any] = []
        edges: list[Any] = []
        seen_nodes: set[str] = set()
        seen_labels: set[str] = set()
        seen_edges: set[str] = set()
        # Each call remains inside exactly one immutable workspace instance.
        for label in labels:
            if len(nodes) >= max_nodes:
                break
            if label in seen_labels:
                continue
            raw = await rag.get_knowledge_graph(
                node_label=str(label),
                max_depth=3,
                max_nodes=max(1, max_nodes - len(nodes)),
            )
            data = raw.model_dump() if callable(getattr(raw, "model_dump", None)) else raw
            if not isinstance(data, dict):
                continue
            for node in data.get("nodes", []):
                item = node if isinstance(node, dict) else getattr(node, "__dict__", {})
                key = str(item.get("id") or item.get("label") or item)
                if key not in seen_nodes:
                    seen_nodes.add(key)
                    nodes.append(node)
                node_label = str(item.get("label") or item.get("id") or "")
                if node_label:
                    seen_labels.add(node_label)
            for edge in data.get("edges", data.get("relationships", [])):
                if len(edges) >= max_edges:
                    break
                item = edge if isinstance(edge, dict) else getattr(edge, "__dict__", {})
                key = str(item.get("id") or (item.get("source"), item.get("target"), item.get("properties")))
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(edge)
        return {"nodes": nodes, "edges": edges}

    async def export_graph(
        self,
        *,
        workspace: str,
        doc_id: str | None,
        allowed_doc_ids: set[str] | None,
        max_nodes: int,
        max_edges: int,
    ) -> dict[str, Any]:
        workspace = validate_workspace(workspace)
        async with self._instance_lease(workspace) as rag:
            async with self._usage_locks.setdefault(workspace, asyncio.Lock()):
                raw = await self._raw_graph(rag, max_nodes, max_edges)
                return normalize_graph(
                    raw,
                    workspace=workspace,
                    doc_id=doc_id,
                    allowed_doc_ids=allowed_doc_ids,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )

    async def search_entities(
        self,
        *,
        workspace: str,
        query: str,
        entity_type: str | None,
        limit: int,
        allowed_doc_ids: set[str] | None,
    ) -> dict[str, Any]:
        workspace = validate_workspace(workspace)
        query = str(query or "").strip()
        if not query:
            raise LightRAGValidationError("query is required")
        async with self._instance_lease(workspace) as rag:
            async with self._usage_locks.setdefault(workspace, asyncio.Lock()):
                return await self._search_entities_with_instance(
                    rag=rag,
                    workspace=workspace,
                    query=query,
                    entity_type=entity_type,
                    limit=limit,
                    allowed_doc_ids=allowed_doc_ids,
                )

    async def _search_entities_with_instance(
        self,
        *,
        rag: Any,
        workspace: str,
        query: str,
        entity_type: str | None,
        limit: int,
        allowed_doc_ids: set[str] | None,
    ) -> dict[str, Any]:
        store = getattr(rag, "chunk_entity_relation_graph", None)
        search = getattr(store, "search_labels", None)
        if not callable(search):
            raise LightRAGProtocolError(
                "LightRAG graph storage does not support entity search"
            )
        labels = list(
            await _maybe_await(
                search(query, min(max(limit * 3, limit), 100))
            )
        )
        nodes: list[Any] = []
        edges: list[Any] = []
        for label in labels[:limit]:
            raw = await rag.get_knowledge_graph(
                node_label=str(label),
                max_depth=1,
                max_nodes=max(limit, 10),
            )
            data = (
                raw.model_dump()
                if callable(getattr(raw, "model_dump", None))
                else raw
            )
            if isinstance(data, dict):
                nodes.extend(data.get("nodes", []))
                edges.extend(
                    data.get("edges", data.get("relationships", []))
                )
        return normalize_entity_search(
            {"nodes": nodes, "edges": edges} if nodes else labels,
            workspace=workspace,
            query=query,
            entity_type=entity_type,
            limit=limit,
            allowed_doc_ids=allowed_doc_ids,
        )


class RemoteLightRAGAdapter:
    """Signed client for the private Railway LightRAG runtime."""

    def __init__(self, *, base_url: str | None = None, secret: str | None = None) -> None:
        self.base_url = str(base_url if base_url is not None else os.getenv("LIGHTRAG_BASE_URL", "")).strip().rstrip("/")
        self.secret = secret if secret is not None else os.getenv("LIGHTRAG_HMAC_SECRET", "")
        parsed = urlparse(self.base_url)
        if not self.base_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LightRAGConfigurationError("LIGHTRAG_BASE_URL must be an absolute HTTP(S) URL")
        local_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        if parsed.scheme != "https" and parsed.hostname not in local_hosts and not _truthy("LIGHTRAG_ALLOW_INSECURE_HTTP"):
            raise LightRAGConfigurationError("LIGHTRAG_BASE_URL must use HTTPS outside local development")

    def _timeout(self) -> float:
        try:
            return max(1.0, min(float(os.getenv("LIGHTRAG_TIMEOUT_SECONDS", "300")), 1800.0))
        except ValueError:
            return 300.0

    async def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else b""
        )
        url = f"{self.base_url}{path}"
        canonical_path = urlparse(url).path
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **sign_request(method, canonical_path, body, secret=self.secret),
        }

        def send() -> dict[str, Any]:
            request = urllib.request.Request(url, data=body if payload is not None else None, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout()) as response:
                    raw_body = response.read()
            except urllib.error.HTTPError as exc:
                raise LightRAGUnavailableError(f"LightRAG service returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LightRAGUnavailableError("LightRAG service is unavailable") from exc
            try:
                decoded = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LightRAGProtocolError("LightRAG service returned an invalid response") from exc
            if not isinstance(decoded, dict):
                raise LightRAGProtocolError("LightRAG service response must be an object")
            return decoded

        return await asyncio.to_thread(send)

    async def _stream_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Consume the signed internal SSE response without buffering it."""

        try:
            import httpx
        except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover
            raise LightRAGConfigurationError(
                "httpx is required for LightRAG streaming"
            ) from exc

        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        url = f"{self.base_url}{path}"
        canonical_path = urlparse(url).path
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **sign_request(method, canonical_path, body, secret=self.secret),
        }
        timeout = httpx.Timeout(self._timeout())
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    method, url, content=body, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        raise LightRAGUnavailableError(
                            f"LightRAG service returned HTTP {response.status_code}"
                        )
                    event_name = "message"
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if not line:
                            if not data_lines:
                                event_name = "message"
                                continue
                            try:
                                data = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError as exc:
                                raise LightRAGProtocolError(
                                    "LightRAG stream returned invalid JSON"
                                ) from exc
                            if not isinstance(data, dict):
                                raise LightRAGProtocolError(
                                    "LightRAG stream event must be an object"
                                )
                            if event_name == "error":
                                code = str(data.get("code") or "lightrag_unavailable")
                                raise LightRAGUnavailableError(
                                    f"LightRAG stream failed ({code})"
                                )
                            yield {"event": event_name, "data": data}
                            event_name = "message"
                            data_lines = []
                            continue
                        if line.startswith(":"):
                            continue
                        field, separator, value = line.partition(":")
                        if not separator:
                            continue
                        value = value[1:] if value.startswith(" ") else value
                        if field == "event":
                            event_name = value or "message"
                        elif field == "data":
                            data_lines.append(value)
                    if data_lines:
                        try:
                            data = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError as exc:
                            raise LightRAGProtocolError(
                                "LightRAG stream returned invalid JSON"
                            ) from exc
                        if event_name == "error":
                            raise LightRAGUnavailableError(
                                "LightRAG stream failed"
                            )
                        if not isinstance(data, dict):
                            raise LightRAGProtocolError(
                                "LightRAG stream event must be an object"
                            )
                        yield {"event": event_name, "data": data}
        except LightRAGError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            raise LightRAGUnavailableError(
                "LightRAG service is unavailable"
            ) from exc

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/internal/v1/health")

    async def index_pages(self, *, workspace: str, doc_id: str, filename: str, pages: Sequence[Any]) -> dict[str, Any]:
        normalized = coerce_pages(pages)
        return await self._request("POST", "/internal/v1/index", {
            "workspace": validate_workspace(workspace),
            "doc_id": doc_id,
            "filename": filename,
            "pages": [{"page": item.page, "content": item.content} for item in normalized],
        })

    async def delete_document(
        self,
        *,
        workspace: str,
        doc_id: str,
        page_count: int | None = None,
        page_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return await self._request("POST", "/internal/v1/documents/delete", {
            "workspace": validate_workspace(workspace),
            "doc_id": doc_id,
            "page_count": page_count,
            "page_ids": list(page_ids or []),
        })

    async def run_query(
        self,
        *,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> dict[str, Any]:
        return await self._request("POST", "/internal/v1/query", {
            "workspace": validate_workspace(workspace),
            "question": question,
            "retrieval_mode": mode.value,
            "history": [dict(item) for item in history],
            "allowed_doc_ids": sorted(allowed_doc_ids) if allowed_doc_ids is not None else None,
            "include_references": include_references,
        })

    async def stream_query(
        self,
        *,
        workspace: str,
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self._stream_request(
            "POST",
            "/internal/v1/query/stream",
            {
                "workspace": validate_workspace(workspace),
                "question": question,
                "retrieval_mode": mode.value,
                "history": [dict(item) for item in list(history)[-8:]],
                "allowed_doc_ids": (
                    sorted(allowed_doc_ids)
                    if allowed_doc_ids is not None
                    else None
                ),
                "include_references": include_references,
            },
        ):
            yield event

    async def stream_query_scopes(
        self,
        *,
        workspaces: Sequence[str],
        question: str,
        mode: LightRAGMode,
        history: Sequence[Mapping[str, Any]],
        allowed_doc_ids: set[str] | None,
        include_references: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        normalized_workspaces = list(
            dict.fromkeys(validate_workspace(item) for item in workspaces)
        )
        async for event in self._stream_request(
            "POST",
            "/internal/v1/query/scopes/stream",
            {
                "workspaces": normalized_workspaces,
                "question": question,
                "retrieval_mode": mode.value,
                "history": [dict(item) for item in list(history)[-8:]],
                "allowed_doc_ids": (
                    sorted(allowed_doc_ids)
                    if allowed_doc_ids is not None
                    else None
                ),
                "include_references": include_references,
            },
        ):
            yield event

    async def export_graph(
        self,
        *,
        workspace: str,
        doc_id: str | None,
        allowed_doc_ids: set[str] | None,
        max_nodes: int,
        max_edges: int,
    ) -> dict[str, Any]:
        return await self._request("POST", "/internal/v1/graph/export", {
            "workspace": validate_workspace(workspace),
            "doc_id": doc_id,
            "allowed_doc_ids": sorted(allowed_doc_ids) if allowed_doc_ids is not None else None,
            "max_nodes": max_nodes,
            "max_edges": max_edges,
        })

    async def search_entities(
        self,
        *,
        workspace: str,
        query: str,
        entity_type: str | None,
        limit: int,
        allowed_doc_ids: set[str] | None,
    ) -> dict[str, Any]:
        return await self._request("POST", "/internal/v1/entities/search", {
            "workspace": validate_workspace(workspace),
            "query": query,
            "entity_type": entity_type,
            "limit": limit,
            "allowed_doc_ids": sorted(allowed_doc_ids) if allowed_doc_ids is not None else None,
        })
