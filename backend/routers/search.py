"""E 组：搜索（3 个端点）"""
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from models.schemas import APIResponse
from identity import RequestIdentity, get_request_identity
from public_access import PUBLIC_DEMO_HEADER, visible_document_ids
from services import search_service as svc

router = APIRouter(prefix="/search", tags=["Search"])


def _engine_error(exc: Exception) -> JSONResponse:
    unavailable = "LIGHTRAG" in str(exc).upper() or exc.__class__.__name__.startswith("LightRAG")
    return JSONResponse(
        status_code=503 if unavailable else 500,
        content=APIResponse.err(
            5003 if unavailable else 4001,
            "LightRAG is unavailable. Switch to the classic engine explicitly." if unavailable else "Search request failed.",
        ).model_dump(),
    )


@router.get("/entities")
async def search_entities(
    q: str,
    type: str | None = None,
    limit: int = 15,
    engine: Literal["legacy", "lightrag"] = "legacy",
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    limit = min(limit, 100)
    try:
        result = await svc.search_entities_for_engine(
            engine,
            tenant_id=identity.tenant_id,
            q=q,
            entity_type=type,
            limit=limit,
            allowed_doc_ids=visible_document_ids(public_demo, identity.owner_id),
        )
    except Exception as exc:
        return _engine_error(exc)
    return APIResponse.ok(result)


@router.get("/path")
async def search_path(
    request: Request,
    max_hops: int = 3,
    engine: Literal["legacy", "lightrag"] = "legacy",
    identity: RequestIdentity = Depends(get_request_identity),
):
    # 'from' is a Python keyword, read from raw query params
    params = dict(request.query_params)
    from_id = params.get("from")
    to_id = params.get("to")

    if not from_id or not to_id:
        return JSONResponse(
            status_code=400,
            content=APIResponse.err(1001, "Parameters 'from' and 'to' are required").model_dump(),
        )
    max_hops = max(1, min(max_hops, 5))
    try:
        result = await svc.search_path_for_engine(
            engine,
            tenant_id=identity.tenant_id,
            from_id=from_id,
            to_id=to_id,
            max_hops=max_hops,
            allowed_doc_ids=visible_document_ids(request.headers.get(PUBLIC_DEMO_HEADER), identity.owner_id),
        )
    except Exception as exc:
        return _engine_error(exc)
    if result is None:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(3001, "One or both nodes not found").model_dump(),
        )
    return APIResponse.ok(result)


@router.get("/graph")
async def search_graph(
    q: str,
    include_neighbors: bool = False,
    engine: Literal["legacy", "lightrag"] = "legacy",
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    try:
        result = await svc.search_graph_for_engine(
            engine,
            tenant_id=identity.tenant_id,
            q=q,
            include_neighbors=include_neighbors,
            allowed_doc_ids=visible_document_ids(public_demo, identity.owner_id),
        )
    except Exception as exc:
        return _engine_error(exc)
    return APIResponse.ok(result)
