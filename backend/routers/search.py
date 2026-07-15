"""E 组：搜索（3 个端点）"""
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from models.schemas import APIResponse
from public_access import PUBLIC_DEMO_HEADER, public_document_ids
from services import search_service as svc

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("/entities")
async def search_entities(
    q: str,
    type: str | None = None,
    limit: int = 15,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    limit = min(limit, 100)
    result = svc.search_entities(q, type, limit, public_document_ids(public_demo))
    return APIResponse.ok(result)


@router.get("/path")
async def search_path(request: Request, max_hops: int = 3):
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
    result = svc.search_path(
        from_id,
        to_id,
        max_hops,
        public_document_ids(request.headers.get(PUBLIC_DEMO_HEADER)),
    )
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
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    result = svc.search_graph(q, include_neighbors, public_document_ids(public_demo))
    return APIResponse.ok(result)
