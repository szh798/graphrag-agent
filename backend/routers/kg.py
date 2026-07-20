"""C 组：双引擎知识图谱。"""
from typing import Literal

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from models.schemas import APIResponse
from identity import RequestIdentity, get_request_identity
from public_access import PUBLIC_DEMO_HEADER, document_is_visible, visible_document_ids
from services import kg_service as svc

router = APIRouter(prefix="/kg", tags=["Knowledge Graph"])


def _engine_error(exc: Exception) -> JSONResponse:
    unavailable = "LIGHTRAG" in str(exc).upper() or exc.__class__.__name__.startswith("LightRAG")
    return JSONResponse(
        status_code=503 if unavailable else 500,
        content=APIResponse.err(
            5003 if unavailable else 4001,
            "LightRAG is unavailable. Switch to the classic engine explicitly." if unavailable else "Knowledge graph request failed.",
        ).model_dump(),
    )


@router.get("/nodes")
async def list_nodes(
    type: str | None = None,
    doc_id: str | None = None,
    confidence: str | None = None,
    source_page: int | None = None,
    layout: bool = False,
    engine: Literal["legacy", "lightrag"] = "legacy",
    page: int = 1,
    page_size: int = 50,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    page_size = min(page_size, 1000)
    allowed_ids = visible_document_ids(public_demo, identity.owner_id)
    if doc_id and not document_is_visible(doc_id, allowed_ids):
        return APIResponse.ok({"total": 0, "page": page, "page_size": page_size, "items": []})
    try:
        result = await svc.get_nodes_for_engine(
            engine,
            tenant_id=identity.tenant_id,
            page=page,
            page_size=page_size,
            node_type=type,
            doc_id=doc_id,
            confidence=confidence,
            source_page=source_page,
            allowed_doc_ids=allowed_ids,
            layout=layout,
        )
    except Exception as exc:
        return _engine_error(exc)
    if result["total"] == 0 and not any([type, doc_id, confidence]):
        return JSONResponse(
            status_code=400,
            content=APIResponse.err(3002, "Knowledge graph is empty. Index documents first.").model_dump(),
        )
    return APIResponse.ok(result)


@router.get("/edges")
async def list_edges(
    doc_id: str | None = None,
    relation: str | None = None,
    layout: bool = False,
    min_weight: float | None = None,
    source_page: int | None = None,
    engine: Literal["legacy", "lightrag"] = "legacy",
    page: int = 1,
    page_size: int = 100,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    page_size = min(page_size, 5000)
    allowed_ids = visible_document_ids(public_demo, identity.owner_id)
    if doc_id and not document_is_visible(doc_id, allowed_ids):
        return APIResponse.ok({"total": 0, "page": page, "page_size": page_size, "items": []})
    try:
        result = await svc.get_edges_for_engine(
            engine,
            tenant_id=identity.tenant_id,
            page=page,
            page_size=page_size,
            doc_id=doc_id,
            relation=relation,
            min_weight=min_weight,
            source_page=source_page,
            allowed_doc_ids=allowed_ids,
            layout=layout,
        )
    except Exception as exc:
        return _engine_error(exc)
    return APIResponse.ok(result)


@router.get("/nodes/{node_id}")
async def get_node_detail(
    node_id: str,
    engine: Literal["legacy", "lightrag"] = "legacy",
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    allowed_ids = visible_document_ids(public_demo, identity.owner_id)
    try:
        node = await svc.get_node_detail_for_engine(
            engine, tenant_id=identity.tenant_id, node_id=node_id, allowed_doc_ids=allowed_ids,
        )
    except Exception as exc:
        return _engine_error(exc)
    if not node or not document_is_visible(str(node.get("source_doc") or ""), allowed_ids):
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(3001, f"Node '{node_id}' not found").model_dump(),
        )
    return APIResponse.ok(node)


@router.get("/nodes/{node_id}/neighbors")
async def get_node_neighbors(
    node_id: str,
    hops: int = 1,
    engine: Literal["legacy", "lightrag"] = "legacy",
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    allowed_ids = visible_document_ids(public_demo, identity.owner_id)
    try:
        result = await svc.get_neighbors_for_engine(
            engine, tenant_id=identity.tenant_id, node_id=node_id, hops=hops, allowed_doc_ids=allowed_ids,
        )
    except Exception as exc:
        return _engine_error(exc)
    if result is None:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(3001, f"Node '{node_id}' not found").model_dump(),
        )
    center = result.get("center") or {}
    if not document_is_visible(str(center.get("source_doc") or ""), allowed_ids):
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(3001, f"Node '{node_id}' not found").model_dump(),
        )
    if engine == "legacy" and allowed_ids is not None:
        allowed_node_ids = {item.get("id") for item in svc.export_kg(allowed_doc_ids=allowed_ids).get("nodes", [])}
        result["neighbors_by_hop"] = {
            distance: [item for item in items if item.get("id") in allowed_node_ids]
            for distance, items in result.get("neighbors_by_hop", {}).items()
        }
        result["total_neighbors"] = sum(len(items) for items in result["neighbors_by_hop"].values())
    return APIResponse.ok(result)


@router.get("/stats")
async def get_kg_stats(
    engine: Literal["legacy", "lightrag"] = "legacy",
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    try:
        stats = await svc.get_stats_for_engine(
            engine,
            tenant_id=identity.tenant_id,
            allowed_doc_ids=visible_document_ids(public_demo, identity.owner_id),
        )
    except Exception as exc:
        return _engine_error(exc)
    return APIResponse.ok(stats)


@router.get("/export")
async def export_kg(
    format: str = "json",
    doc_id: str | None = None,
    engine: Literal["legacy", "lightrag"] = "legacy",
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    allowed_ids = visible_document_ids(public_demo, identity.owner_id)
    if doc_id and not document_is_visible(doc_id, allowed_ids):
        return APIResponse.ok({"format": format, "doc_id": doc_id, "engine": engine, "total_nodes": 0, "total_edges": 0, "nodes": [], "edges": []})
    try:
        result = await svc.export_kg_for_engine(
            engine,
            tenant_id=identity.tenant_id,
            doc_id=doc_id,
            allowed_doc_ids=allowed_ids,
            complete=True,
        )
    except Exception as exc:
        return _engine_error(exc)
    return APIResponse.ok(result)
