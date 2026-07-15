"""C 组：知识图谱（6 个端点）"""
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from models.schemas import APIResponse
from public_access import PUBLIC_DEMO_HEADER, document_is_visible, public_document_ids
from services import kg_service as svc

router = APIRouter(prefix="/kg", tags=["Knowledge Graph"])


@router.get("/nodes")
async def list_nodes(
    type: str | None = None,
    doc_id: str | None = None,
    confidence: str | None = None,
    page: int = 1,
    page_size: int = 50,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    page_size = min(page_size, 1000)
    allowed_ids = public_document_ids(public_demo)
    if doc_id and not document_is_visible(doc_id, allowed_ids):
        return APIResponse.ok({"total": 0, "page": page, "page_size": page_size, "items": []})
    if allowed_ids is not None and len(allowed_ids) == 1:
        doc_id = next(iter(allowed_ids))
    result = svc.get_nodes(page, page_size, type, doc_id, confidence)
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
    page: int = 1,
    page_size: int = 100,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    page_size = min(page_size, 5000)
    allowed_ids = public_document_ids(public_demo)
    if doc_id and not document_is_visible(doc_id, allowed_ids):
        return APIResponse.ok({"total": 0, "page": page, "page_size": page_size, "items": []})
    if allowed_ids is not None and len(allowed_ids) == 1:
        doc_id = next(iter(allowed_ids))
    result = svc.get_edges(page, page_size, doc_id, relation)
    return APIResponse.ok(result)


@router.get("/nodes/{node_id}")
async def get_node_detail(
    node_id: str,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    node = svc.get_node_detail(node_id)
    if not node or not document_is_visible(str(node.get("source_doc") or ""), public_document_ids(public_demo)):
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(3001, f"Node '{node_id}' not found").model_dump(),
        )
    return APIResponse.ok(node)


@router.get("/nodes/{node_id}/neighbors")
async def get_node_neighbors(
    node_id: str,
    hops: int = 1,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    allowed_ids = public_document_ids(public_demo)
    node = svc.get_node_detail(node_id)
    if not node or not document_is_visible(str(node.get("source_doc") or ""), allowed_ids):
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(3001, f"Node '{node_id}' not found").model_dump(),
        )
    result = svc.get_neighbors(node_id, hops)
    if result is None:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(3001, f"Node '{node_id}' not found").model_dump(),
        )
    if allowed_ids is not None:
        allowed_node_ids = {item.get("id") for item in svc.export_kg(allowed_doc_ids=allowed_ids).get("nodes", [])}
        result["neighbors_by_hop"] = {
            distance: [item for item in items if item.get("id") in allowed_node_ids]
            for distance, items in result.get("neighbors_by_hop", {}).items()
        }
        result["total_neighbors"] = sum(len(items) for items in result["neighbors_by_hop"].values())
    return APIResponse.ok(result)


@router.get("/stats")
async def get_kg_stats(public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER)):
    stats = svc.get_stats(public_document_ids(public_demo))
    return APIResponse.ok(stats)


@router.get("/export")
async def export_kg(
    format: str = "json",
    doc_id: str | None = None,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    result = svc.export_kg(doc_id, public_document_ids(public_demo))
    return APIResponse.ok(result)
