"""D 组：QA 问答（4 个端点）"""
import asyncio
import json
from functools import partial

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse

from models.schemas import APIResponse, BatchQueryRequest, QueryRequest
from public_access import PUBLIC_DEMO_HEADER, public_document_ids
from services import qa_service as svc
from identity import RequestIdentity, VISITOR_ID_HEADER, get_request_identity
from operations import report_exception

router = APIRouter(prefix="/query", tags=["QA"])

STREAM_ANSWER_CHUNK_SIZE = 8
STREAM_ANSWER_DELAY_SECONDS = 0.06
STATELESS_BATCH_HEADER = "X-GraphRAG-Stateless-Batch"


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _query_error_payload(exc: Exception, session_id: str | None = None) -> dict:
    if isinstance(exc, ValueError) and "KG_EMPTY" in str(exc):
        return {"code": 3002, "message": "Knowledge graph is empty. Index documents first."}
    if isinstance(exc, ValueError) and "SESSION_NOT_FOUND" in str(exc):
        return {"code": 2002, "message": f"Session '{session_id}' not found"}
    return {"code": 4001, "message": svc.PUBLIC_QA_ERROR}


@router.post("")
async def run_query(
    body: QueryRequest,
    identity: RequestIdentity = Depends(get_request_identity),
    stateless_batch: str | None = Header(default=None, alias=STATELESS_BATCH_HEADER),
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            partial(
                svc.run_query,
                body.question,
                [m.model_dump() for m in body.history],
                identity.owner_id,
                body.session_id,
                persist_session=stateless_batch != "1",
                allowed_document_ids=public_document_ids(public_demo),
                actor_id=identity.actor_id if identity.authenticated else None,
                tenant_id=identity.tenant_id,
            ),
        )
        return APIResponse.ok(result)
    except ValueError as e:
        if "KG_EMPTY" in str(e):
            return JSONResponse(
                status_code=400,
                content=APIResponse.err(3002, "Knowledge graph is empty. Index documents first.").model_dump(),
            )
        if "SESSION_NOT_FOUND" in str(e):
            return JSONResponse(
                status_code=404,
                content=APIResponse.err(2002, f"Session '{body.session_id}' not found").model_dump(),
            )
        report_exception("qa_request_failed", e, identity=identity, context={"route": "/query"})
        return JSONResponse(
            status_code=500,
            content=APIResponse.err(4001, svc.PUBLIC_QA_ERROR).model_dump(),
        )
    except Exception as exc:
        report_exception("qa_request_failed", exc, identity=identity, context={"route": "/query"})
        return JSONResponse(
            status_code=500,
            content=APIResponse.err(4001, svc.PUBLIC_QA_ERROR).model_dump(),
        )


@router.post("/stream")
async def stream_query(
    body: QueryRequest,
    identity: RequestIdentity = Depends(get_request_identity),
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
):
    async def event_generator():
        yield _sse_event("status", {"message": "正在分析问题..."})
        await asyncio.sleep(0.01)
        yield _sse_event("status", {"message": "正在检索知识图谱..."})

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                partial(
                    svc.run_query,
                    body.question,
                    [m.model_dump() for m in body.history],
                    identity.owner_id,
                    body.session_id,
                    allowed_document_ids=public_document_ids(public_demo),
                    actor_id=identity.actor_id if identity.authenticated else None,
                    tenant_id=identity.tenant_id,
                ),
            )
        except Exception as exc:
            if not (isinstance(exc, ValueError) and any(code in str(exc) for code in ("KG_EMPTY", "SESSION_NOT_FOUND"))):
                report_exception("qa_stream_failed", exc, identity=identity, context={"route": "/query/stream"})
            yield _sse_event("error", _query_error_payload(exc, body.session_id))
            return

        yield _sse_event("status", {"message": "已完成检索，正在组织回答..."})
        for item in svc.result_to_stream_events(result, chunk_size=STREAM_ANSWER_CHUNK_SIZE):
            yield _sse_event(item["event"], item["data"])
            if item["event"] == "answer_delta":
                await asyncio.sleep(STREAM_ANSWER_DELAY_SECONDS)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/batch", status_code=202)
async def start_batch(body: BatchQueryRequest, identity: RequestIdentity = Depends(get_request_identity)):
    if len(body.questions) > 20:
        return JSONResponse(
            status_code=400,
            content=APIResponse.err(1001, "Maximum 20 questions per batch").model_dump(),
        )
    result = svc.start_batch(
        body.questions,
        identity.owner_id,
        actor_id=identity.actor_id if identity.authenticated else None,
        tenant_id=identity.tenant_id,
    )
    return APIResponse.ok(result)


@router.get("/batch")
async def list_batches(page: int = 1, page_size: int = 20, identity: RequestIdentity = Depends(get_request_identity)):
    result = svc.list_batches(identity.owner_id, page, page_size)
    return APIResponse.ok(result)


@router.get("/batch/{batch_id}")
async def get_batch_result(batch_id: str, identity: RequestIdentity = Depends(get_request_identity)):
    result = svc.get_batch_result(batch_id, identity.owner_id)
    if not result:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Batch '{batch_id}' not found").model_dump(),
        )
    return APIResponse.ok(result)


@router.delete("/batch/{batch_id}")
async def cancel_batch(batch_id: str, identity: RequestIdentity = Depends(get_request_identity)):
    result = svc.cancel_batch(batch_id, identity.owner_id)
    if not result:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Batch '{batch_id}' not found").model_dump(),
        )
    return APIResponse.ok(result)


@router.post("/sessions")
async def create_session(identity: RequestIdentity = Depends(get_request_identity)):
    session = svc.create_session(
        identity.owner_id,
        actor_id=identity.actor_id if identity.authenticated else None,
    )
    return APIResponse.ok(svc.get_session(session["id"], identity.owner_id))


@router.get("/sessions")
async def get_sessions(page: int = 1, page_size: int = 20, identity: RequestIdentity = Depends(get_request_identity)):
    result = svc.get_sessions(identity.owner_id, page, page_size)
    return APIResponse.ok(result)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, identity: RequestIdentity = Depends(get_request_identity)):
    result = svc.get_session(session_id, identity.owner_id)
    if not result:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Session '{session_id}' not found").model_dump(),
        )
    return APIResponse.ok(result)


@router.get("/history")
async def get_query_history(page: int = 1, page_size: int = 20, identity: RequestIdentity = Depends(get_request_identity)):
    page_size = min(page_size, 50)
    result = svc.get_history(identity.owner_id, page, page_size)
    return APIResponse.ok(result)
