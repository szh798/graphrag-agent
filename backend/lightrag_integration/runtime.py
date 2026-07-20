"""Private Railway FastAPI runtime for the LightRAG worker/API process.

Run with ``uvicorn lightrag_integration.runtime:app``.  The minimal ``/live``
probe is anonymous for Railway container liveness; readiness and every business
endpoint are HMAC-authenticated. Requests contain only opaque workspace keys;
raw tenant identifiers are intentionally absent from all request models.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .adapters import LightRAGAdapter, LocalLightRAGAdapter
from .errors import (
    LightRAGAuthenticationError,
    LightRAGConfigurationError,
    LightRAGError,
    LightRAGProtocolError,
    LightRAGUnavailableError,
    LightRAGValidationError,
)
from .security import NonceReplayCache, validate_workspace, verify_request
from .types import LightRAGMode, TARGET_LIGHTRAG_VERSION


logger = logging.getLogger("graphrag.lightrag.runtime")
_replay_cache = NonceReplayCache()


async def _authenticated(request: Request) -> None:
    body = await request.body()
    try:
        await asyncio.to_thread(
            verify_request,
            request.method,
            request.url.path,
            body,
            request.headers,
            replay_cache=_replay_cache,
        )
    except LightRAGAuthenticationError as exc:
        raise HTTPException(status_code=401, detail={"code": exc.code}) from exc
    except LightRAGConfigurationError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code}) from exc


class WorkspaceRequest(BaseModel):
    workspace: str

    @field_validator("workspace")
    @classmethod
    def opaque_workspace(cls, value: str) -> str:
        return validate_workspace(value)


class PagePayload(BaseModel):
    page: int = Field(ge=1)
    content: str = Field(min_length=1)


class IndexRequest(WorkspaceRequest):
    doc_id: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=1024)
    pages: list[PagePayload] = Field(min_length=1)


class DeleteRequest(WorkspaceRequest):
    doc_id: str = Field(min_length=1, max_length=512)
    page_count: int | None = Field(default=None, ge=1, le=100000)
    page_ids: list[str] = Field(default_factory=list, max_length=100000)


class QueryRequest(WorkspaceRequest):
    question: str = Field(min_length=1)
    retrieval_mode: LightRAGMode = LightRAGMode.MIX
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    allowed_doc_ids: list[str] | None = None
    include_references: bool = True


class ScopedQueryRequest(BaseModel):
    workspaces: list[str] = Field(min_length=2, max_length=9)
    question: str = Field(min_length=1)
    retrieval_mode: LightRAGMode = LightRAGMode.MIX
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    allowed_doc_ids: list[str] | None = None
    include_references: bool = True

    @field_validator("workspaces")
    @classmethod
    def opaque_unique_workspaces(cls, values: list[str]) -> list[str]:
        normalized = [validate_workspace(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("workspaces must be unique")
        return normalized


class GraphRequest(WorkspaceRequest):
    doc_id: str | None = None
    allowed_doc_ids: list[str] | None = None
    max_nodes: int = Field(default=200, ge=1, le=10000)
    max_edges: int = Field(default=2000, ge=0, le=100000)


class SearchRequest(WorkspaceRequest):
    query: str = Field(min_length=1, max_length=2000)
    entity_type: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=15, ge=1, le=100)
    allowed_doc_ids: list[str] | None = None


async def _invoke(awaitable):
    try:
        return await awaitable
    except LightRAGValidationError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code}) from exc
    except (LightRAGConfigurationError, LightRAGUnavailableError) as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code}) from exc
    except LightRAGProtocolError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code}) from exc
    except LightRAGError as exc:
        raise HTTPException(status_code=500, detail={"code": exc.code}) from exc
    except Exception as exc:
        logger.exception("Unhandled LightRAG runtime failure")
        raise HTTPException(status_code=500, detail={"code": "lightrag_runtime_error"}) from exc


def _sse_event(event: str, data: Mapping[str, Any] | dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(dict(data), ensure_ascii=False)}\n\n"


def create_internal_app(adapter: LightRAGAdapter | None = None) -> FastAPI:
    runtime = FastAPI(
        title="GraphRAG Studio LightRAG Internal Runtime",
        version=TARGET_LIGHTRAG_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    runtime.state.lightrag_adapter = adapter or LocalLightRAGAdapter()
    auth = [Depends(_authenticated)]

    @runtime.get("/live")
    async def live() -> dict[str, str]:
        # Deliberately contains no dependency, queue, workspace, endpoint, or
        # credential metadata. Railway needs only process liveness here.
        return {"status": "live", "version": TARGET_LIGHTRAG_VERSION}

    @runtime.get("/internal/v1/health", dependencies=auth)
    async def health() -> dict[str, Any]:
        return await _invoke(runtime.state.lightrag_adapter.health())

    @runtime.post("/internal/v1/index", dependencies=auth)
    async def index(request: IndexRequest) -> dict[str, Any]:
        return await _invoke(runtime.state.lightrag_adapter.index_pages(
            workspace=request.workspace,
            doc_id=request.doc_id,
            filename=request.filename,
            pages=[item.model_dump() for item in request.pages],
        ))

    @runtime.post("/internal/v1/documents/delete", dependencies=auth)
    async def delete(request: DeleteRequest) -> dict[str, Any]:
        return await _invoke(runtime.state.lightrag_adapter.delete_document(
            workspace=request.workspace,
            doc_id=request.doc_id,
            page_count=request.page_count,
            page_ids=request.page_ids,
        ))

    @runtime.post("/internal/v1/query", dependencies=auth)
    async def query(request: QueryRequest) -> dict[str, Any]:
        return await _invoke(runtime.state.lightrag_adapter.run_query(
            workspace=request.workspace,
            question=request.question,
            mode=request.retrieval_mode,
            history=request.history,
            allowed_doc_ids=set(request.allowed_doc_ids) if request.allowed_doc_ids is not None else None,
            include_references=request.include_references,
        ))

    @runtime.post("/internal/v1/query/stream", dependencies=auth)
    async def query_stream(request: QueryRequest) -> StreamingResponse:
        async def events():
            try:
                async for event in runtime.state.lightrag_adapter.stream_query(
                    workspace=request.workspace,
                    question=request.question,
                    mode=request.retrieval_mode,
                    history=request.history[-8:],
                    allowed_doc_ids=(
                        set(request.allowed_doc_ids)
                        if request.allowed_doc_ids is not None
                        else None
                    ),
                    include_references=request.include_references,
                ):
                    event_name = str(event.get("event") or "message")
                    data = event.get("data")
                    if not isinstance(data, Mapping):
                        raise LightRAGProtocolError(
                            "LightRAG stream event data must be an object"
                        )
                    yield _sse_event(event_name, data)
            except LightRAGValidationError as exc:
                yield _sse_event("error", {"code": exc.code, "status": 400})
            except (LightRAGConfigurationError, LightRAGUnavailableError) as exc:
                yield _sse_event("error", {"code": exc.code, "status": 503})
            except LightRAGProtocolError as exc:
                yield _sse_event("error", {"code": exc.code, "status": 502})
            except LightRAGError as exc:
                yield _sse_event("error", {"code": exc.code, "status": 500})
            except Exception:
                logger.exception("Unhandled LightRAG streaming failure")
                yield _sse_event(
                    "error", {"code": "lightrag_runtime_error", "status": 500}
                )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @runtime.post("/internal/v1/query/scopes/stream", dependencies=auth)
    async def scoped_query_stream(request: ScopedQueryRequest) -> StreamingResponse:
        async def events():
            try:
                async for event in runtime.state.lightrag_adapter.stream_query_scopes(
                    workspaces=request.workspaces,
                    question=request.question,
                    mode=request.retrieval_mode,
                    history=request.history[-8:],
                    allowed_doc_ids=(
                        set(request.allowed_doc_ids)
                        if request.allowed_doc_ids is not None
                        else None
                    ),
                    include_references=request.include_references,
                ):
                    event_name = str(event.get("event") or "message")
                    data = event.get("data")
                    if not isinstance(data, Mapping):
                        raise LightRAGProtocolError(
                            "LightRAG scoped stream event data must be an object"
                        )
                    yield _sse_event(event_name, data)
            except LightRAGValidationError as exc:
                yield _sse_event("error", {"code": exc.code, "status": 400})
            except (LightRAGConfigurationError, LightRAGUnavailableError) as exc:
                yield _sse_event("error", {"code": exc.code, "status": 503})
            except LightRAGProtocolError as exc:
                yield _sse_event("error", {"code": exc.code, "status": 502})
            except LightRAGError as exc:
                yield _sse_event("error", {"code": exc.code, "status": 500})
            except Exception:
                logger.exception("Unhandled LightRAG scoped streaming failure")
                yield _sse_event(
                    "error", {"code": "lightrag_runtime_error", "status": 500}
                )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @runtime.post("/internal/v1/graph/export", dependencies=auth)
    async def graph(request: GraphRequest) -> dict[str, Any]:
        return await _invoke(runtime.state.lightrag_adapter.export_graph(
            workspace=request.workspace,
            doc_id=request.doc_id,
            allowed_doc_ids=set(request.allowed_doc_ids) if request.allowed_doc_ids is not None else None,
            max_nodes=request.max_nodes,
            max_edges=request.max_edges,
        ))

    @runtime.post("/internal/v1/entities/search", dependencies=auth)
    async def search(request: SearchRequest) -> dict[str, Any]:
        return await _invoke(runtime.state.lightrag_adapter.search_entities(
            workspace=request.workspace,
            query=request.query,
            entity_type=request.entity_type,
            limit=request.limit,
            allowed_doc_ids=set(request.allowed_doc_ids) if request.allowed_doc_ids is not None else None,
        ))

    return runtime


app = create_internal_app()


__all__ = ["app", "create_internal_app"]
