"""B 组：Indexing Pipeline（4 个端点）"""
import asyncio

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from models.schemas import APIResponse, RetryIndexRequest, StartIndexRequest
from identity import RequestIdentity, get_request_identity
from public_access import PUBLIC_DEMO_HEADER, document_is_visible, visible_document_ids
from services import document_service as doc_svc
from services import indexing_service as idx_svc

router = APIRouter(prefix="/index", tags=["Indexing"])
compat_router = APIRouter(prefix="/indexing", tags=["Indexing"])


@router.post("/run-next")
async def run_next_index_job(
    internal_index: str = Header("", alias="X-GraphRAG-Internal-Index"),
):
    if internal_index != "1":
        return JSONResponse(status_code=401, content=APIResponse.err(4001, "Unauthorized").model_dump())
    result = await asyncio.to_thread(idx_svc.process_next_index_job, 1)
    if not result:
        return APIResponse.ok({"processed": False})
    public_result = dict(result)
    public_result.pop("owner_id", None)
    public_result.pop("actor_id", None)
    return APIResponse.ok({"processed": True, "job": public_result})


@router.post("/start", status_code=202)
async def start_indexing(body: StartIndexRequest, identity: RequestIdentity = Depends(get_request_identity)):
    doc = doc_svc.get_document(body.doc_id)
    if not doc:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{body.doc_id}' not found").model_dump(),
        )
    if str(doc.get("owner_id") or "default") != identity.owner_id:
        return JSONResponse(
            status_code=403,
            content=APIResponse.err(4003, "Only the document owner can start indexing").model_dump(),
        )
    # A normal start always fans out to every enabled engine.  ``engine`` is
    # accepted only for compatibility with an early preview client and is
    # intentionally ignored; single-engine work is restricted to /retry.
    meta = idx_svc.start_indexing(body.doc_id)
    return APIResponse.ok({
        "job_id": meta["job_id"],
        "doc_id": meta["doc_id"],
        "status": meta["status"],
        "stage": meta["stage"],
        "created_at": meta["created_at"],
        "engines": meta.get("engines", {}),
    })


@router.post("/{doc_id}/retry", status_code=202)
async def retry_failed_engine(
    doc_id: str,
    body: RetryIndexRequest,
    identity: RequestIdentity = Depends(get_request_identity),
):
    doc = doc_svc.get_document(doc_id)
    if not doc:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{doc_id}' not found").model_dump(),
        )
    if str(doc.get("owner_id") or "default") != identity.owner_id:
        return JSONResponse(
            status_code=403,
            content=APIResponse.err(4003, "Only the document owner can retry indexing").model_dump(),
        )
    if body.engine == "lightrag":
        from services import lightrag_service

        if not lightrag_service.enabled():
            return JSONResponse(
                status_code=503,
                content=APIResponse.err(5003, "LightRAG is disabled or not configured").model_dump(),
            )
    meta = idx_svc.start_indexing(doc_id, {body.engine})
    return APIResponse.ok({
        "job_id": meta["job_id"],
        "doc_id": doc_id,
        "status": meta["status"],
        "stage": meta["stage"],
        "created_at": meta["created_at"],
        "engines": meta.get("engines", {}),
    })


@compat_router.post("/{doc_id}/retry", status_code=202)
async def retry_failed_engine_compat(
    doc_id: str,
    body: RetryIndexRequest,
    identity: RequestIdentity = Depends(get_request_identity),
):
    """Stable operations alias used by backfill automation."""
    return await retry_failed_engine(doc_id, body, identity)


@router.get("/status/{job_id}")
async def get_job_status(
    job_id: str,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    meta = idx_svc.get_job_status(job_id)
    if not meta:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Job '{job_id}' not found").model_dump(),
        )
    if not document_is_visible(str(meta.get("doc_id") or ""), visible_document_ids(public_demo, identity.owner_id)):
        return JSONResponse(status_code=404, content=APIResponse.err(2002, f"Job '{job_id}' not found").model_dump())
    public_meta = dict(meta)
    public_meta.pop("owner_id", None)
    public_meta.pop("actor_id", None)
    return APIResponse.ok(public_meta)


@router.get("/result/{job_id}")
async def get_job_result(
    job_id: str,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    meta = idx_svc.get_job_status(job_id)
    if not meta or not document_is_visible(str(meta.get("doc_id") or ""), visible_document_ids(public_demo, identity.owner_id)):
        return JSONResponse(status_code=404, content=APIResponse.err(2002, f"Job '{job_id}' not found").model_dump())
    result = idx_svc.get_job_result(job_id)
    if not result:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Job '{job_id}' not found").model_dump(),
        )
    if result.get("status") not in ("done", "partial") and "stats" not in result:
        return JSONResponse(
            status_code=400,
            content=APIResponse.err(2003, f"Job '{job_id}' is still running (status={result.get('status')})").model_dump(),
        )
    return APIResponse.ok(result)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, identity: RequestIdentity = Depends(get_request_identity)):
    meta = idx_svc.get_job_status(job_id)
    if not meta:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Job '{job_id}' not found").model_dump(),
        )
    if str(meta.get("owner_id") or "default") != identity.owner_id:
        return JSONResponse(status_code=403, content=APIResponse.err(4003, "Only the job owner can cancel it").model_dump())
    ok, prev_status = idx_svc.cancel_job(job_id)
    return APIResponse.ok({
        "cancelled": True,
        "job_id": job_id,
        "previous_status": prev_status,
    })
