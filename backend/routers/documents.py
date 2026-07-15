"""A 组：文档管理（4 个端点）"""
from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from models.schemas import APIResponse
from identity import RequestIdentity, get_request_identity
from public_access import PUBLIC_DEMO_HEADER, document_is_visible, visible_document_ids
from services import document_service as svc

router = APIRouter(prefix="/documents", tags=["Documents"])
_UPLOAD_CHUNK_SIZE = 1024 * 1024


class DirectUploadBlob(BaseModel):
    url: str
    downloadUrl: str | None = None
    pathname: str
    contentType: str | None = None
    contentDisposition: str | None = None
    etag: str | None = None


class CompleteDirectUploadRequest(BaseModel):
    filename: str
    sizeBytes: int = Field(gt=0, le=svc.MAX_FILE_SIZE_BYTES)
    contentType: str | None = None
    language: str = "ch"
    enableFormula: bool = True
    enableTable: bool = True
    blob: DirectUploadBlob
    ownerId: str = Field(min_length=1, max_length=160)
    actorId: str | None = Field(default=None, max_length=160)


def _upload_error(code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=APIResponse.err(code, message).model_dump(),
    )


async def _read_validated_upload(file: UploadFile) -> tuple[bytes | None, JSONResponse | None]:
    filename = file.filename or ""

    # Reject invalid filenames and a known-oversized multipart part before
    # copying the upload into application memory.
    ok, code, msg = svc.validate_upload(filename, 0)
    if not ok:
        await file.close()
        return None, _upload_error(code, msg)
    declared_size = getattr(file, "size", None)
    if isinstance(declared_size, int):
        ok, code, msg = svc.validate_upload(filename, declared_size)
        if not ok:
            await file.close()
            return None, _upload_error(code, msg)

    buffer = BytesIO()
    total = 0
    head = bytearray()
    while chunk := await file.read(_UPLOAD_CHUNK_SIZE):
        total += len(chunk)
        if total > svc.MAX_FILE_SIZE_BYTES:
            await file.close()
            size_mb = total / (1024 * 1024)
            return None, _upload_error(
                1003,
                f"File size {size_mb:.1f}MB exceeds {svc.MAX_FILE_SIZE_MB}MB limit",
            )
        if len(head) < 4096:
            head.extend(chunk[: 4096 - len(head)])
        buffer.write(chunk)

    ok, code, msg = svc.validate_upload(filename, total)
    if ok:
        ok, code, msg = svc.validate_upload_content(filename, file.content_type, bytes(head), total)
    if not ok:
        await file.close()
        return None, _upload_error(code, msg)
    return buffer.getvalue(), None


@router.post("/upload", status_code=200)
async def upload_document(
    file: UploadFile = File(...),
    language: str = Form("ch"),
    enable_formula: bool = Form(True),
    enable_table: bool = Form(True),
    identity: RequestIdentity = Depends(get_request_identity),
):
    content, error = await _read_validated_upload(file)
    if error is not None:
        return error
    assert content is not None
    doc = svc.save_upload(
        file.filename or "upload",
        content,
        language,
        enable_formula,
        enable_table,
        owner_id=identity.owner_id,
        actor_id=identity.actor_id if identity.authenticated else None,
    )
    return APIResponse.ok(svc.public_document(doc))


@router.post("/upload/complete", status_code=201)
async def complete_direct_upload(
    body: CompleteDirectUploadRequest,
    internal_upload: str = Header("", alias="X-GraphRAG-Internal-Upload"),
):
    if internal_upload != "1":
        return JSONResponse(
            status_code=401,
            content=APIResponse.err(4001, "Unauthorized upload completion").model_dump(),
        )
    try:
        doc = svc.register_direct_upload(
            filename=body.filename,
            size_bytes=body.sizeBytes,
            content_type=body.contentType,
            blob_ref=body.blob.model_dump(exclude_none=True),
            language=body.language,
            enable_formula=body.enableFormula,
            enable_table=body.enableTable,
            owner_id=body.ownerId,
            actor_id=body.actorId,
        )
    except ValueError as exc:
        raw = str(exc)
        code_text, _, message = raw.partition(":")
        code = int(code_text) if code_text.isdigit() else 1001
        return _upload_error(code, message or "Invalid completed upload")
    return APIResponse.ok(svc.public_document(doc))


@router.get("/{doc_id}")
async def get_document(
    doc_id: str,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    doc = svc.get_document(doc_id)
    if not doc or not document_is_visible(doc_id, visible_document_ids(public_demo, identity.owner_id)):
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{doc_id}' not found").model_dump(),
        )
    return APIResponse.ok(svc.public_document(doc))


@router.get("/{doc_id}/index-result")
async def get_document_index_result(
    doc_id: str,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    doc = svc.get_document(doc_id)
    if not doc or not document_is_visible(doc_id, visible_document_ids(public_demo, identity.owner_id)):
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{doc_id}' not found").model_dump(),
        )
    result = svc.get_document_index_result(doc_id)
    if not result:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Index result for document '{doc_id}' not found").model_dump(),
        )
    return APIResponse.ok(result)


@router.get("/{doc_id}/extractions")
async def get_document_extractions(
    doc_id: str,
    page: int = 1,
    page_size: int = 50,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    doc = svc.get_document(doc_id)
    if not doc or not document_is_visible(doc_id, visible_document_ids(public_demo, identity.owner_id)):
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{doc_id}' not found").model_dump(),
        )
    result = svc.get_document_extractions(doc_id, page, page_size)
    if not result:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2002, f"Extraction records for document '{doc_id}' not found").model_dump(),
        )
    return APIResponse.ok(result)


@router.get("")
async def list_documents(
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    format: str | None = None,
    public_demo: str | None = Header(default=None, alias=PUBLIC_DEMO_HEADER),
    identity: RequestIdentity = Depends(get_request_identity),
):
    page_size = min(page_size, 100)
    allowed_ids = visible_document_ids(public_demo, identity.owner_id)
    result = svc.list_documents(page, page_size, status, format, allowed_ids=allowed_ids)
    return APIResponse.ok(result)


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, identity: RequestIdentity = Depends(get_request_identity)):
    doc = svc.get_document(doc_id)
    if not doc:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{doc_id}' not found").model_dump(),
        )
    if str(doc.get("owner_id") or "default") != identity.owner_id:
        return JSONResponse(
            status_code=403,
            content=APIResponse.err(4003, "Only the document owner can delete it").model_dump(),
        )
    ok, removed_nodes, removed_edges = svc.delete_document(doc_id)
    return APIResponse.ok({
        "deleted": True,
        "doc_id": doc_id,
        "removed_nodes": removed_nodes,
        "removed_edges": removed_edges,
    })
