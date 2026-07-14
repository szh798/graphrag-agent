"""A 组：文档管理（4 个端点）"""
from io import BytesIO

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from models.schemas import APIResponse
from services import document_service as svc

router = APIRouter(prefix="/documents", tags=["Documents"])
_UPLOAD_CHUNK_SIZE = 1024 * 1024


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
):
    content, error = await _read_validated_upload(file)
    if error is not None:
        return error
    assert content is not None
    doc = svc.save_upload(file.filename or "upload", content, language, enable_formula, enable_table)
    return APIResponse.ok(svc.public_document(doc))


@router.get("/{doc_id}")
async def get_document(doc_id: str):
    doc = svc.get_document(doc_id)
    if not doc:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{doc_id}' not found").model_dump(),
        )
    return APIResponse.ok(svc.public_document(doc))


@router.get("/{doc_id}/index-result")
async def get_document_index_result(doc_id: str):
    doc = svc.get_document(doc_id)
    if not doc:
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
async def get_document_extractions(doc_id: str, page: int = 1, page_size: int = 50):
    doc = svc.get_document(doc_id)
    if not doc:
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
):
    page_size = min(page_size, 100)
    result = svc.list_documents(page, page_size, status, format)
    return APIResponse.ok(result)


@router.delete("/{doc_id}")
async def delete_document(doc_id: str):
    doc = svc.get_document(doc_id)
    if not doc:
        return JSONResponse(
            status_code=404,
            content=APIResponse.err(2001, f"Document '{doc_id}' not found").model_dump(),
        )
    ok, removed_nodes, removed_edges = svc.delete_document(doc_id)
    return APIResponse.ok({
        "deleted": True,
        "doc_id": doc_id,
        "removed_nodes": removed_nodes,
        "removed_edges": removed_edges,
    })
