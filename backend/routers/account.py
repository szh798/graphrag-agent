"""Authenticated account, tenant, usage, export/delete, and operations APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from identity import RequestIdentity, require_admin, require_authenticated_identity
from models.schemas import APIResponse
from observability import get_request_id
from operations import operational_readiness
from storage import blob_repository as blob_store
from storage import graph_repository as graph_store
from storage.account_repository import get_account_repository


router = APIRouter(tags=["Account & Operations"])


class OpsEventInput(BaseModel):
    severity: str = Field(default="error", pattern="^(warning|error)$")
    source: str = Field(default="edge", max_length=40)
    event_type: str = Field(max_length=80)
    message: str = Field(max_length=500)
    request_id: str | None = Field(default=None, max_length=128)
    tenant_id: str | None = Field(default=None, max_length=160)
    actor_id: str | None = Field(default=None, max_length=160)
    context: dict = Field(default_factory=dict)


def _sync(identity: RequestIdentity) -> None:
    try:
        get_account_repository().sync_identity(identity)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Account data service is unavailable") from exc


@router.get("/account/me")
async def account_me(identity: RequestIdentity = Depends(require_authenticated_identity)):
    _sync(identity)
    return APIResponse.ok(identity.public_dict())


@router.post("/account/claim-visitor-data")
async def claim_visitor_data(identity: RequestIdentity = Depends(require_authenticated_identity)):
    repo = get_account_repository()
    repo.sync_identity(identity)
    result = repo.claim_visitor_data(identity)
    if any(result["claimed"].values()):
        repo.record_audit(identity, "account.visitor_data_claimed", get_request_id(), result["claimed"])
    return APIResponse.ok(result)


@router.get("/account/usage")
async def account_usage(
    days: int = Query(default=30, ge=1, le=366),
    tenant_total: bool = Query(default=False),
    identity: RequestIdentity = Depends(require_authenticated_identity),
):
    _sync(identity)
    if tenant_total:
        require_admin(identity)
    user_id = None if tenant_total else identity.actor_id
    data = get_account_repository().usage_summary(identity.tenant_id, user_id=user_id, days=days)
    data["scope"] = "tenant" if tenant_total else "user"
    return APIResponse.ok(data)


@router.get("/account/export")
async def export_account_data(identity: RequestIdentity = Depends(require_authenticated_identity)):
    _sync(identity)
    repo = get_account_repository()
    tenant_export = not identity.organization_id or identity.is_admin
    exported = repo.export_tenant(identity) if tenant_export else repo.export_user(identity)
    scope = "tenant" if tenant_export else "user"
    repo.record_audit(identity, "account.data_exported", get_request_id(), {"scope": scope})
    return APIResponse.ok(exported)


@router.delete("/account/data")
async def delete_personal_data(
    confirmation: str = Query(min_length=1, max_length=200),
    identity: RequestIdentity = Depends(require_authenticated_identity),
):
    if confirmation != identity.actor_id:
        raise HTTPException(status_code=400, detail="Type the exact account user id to confirm deletion")
    repo = get_account_repository()
    repo.sync_identity(identity)
    personal_tenant = identity.tenant_id == f"user:{identity.actor_id}"
    if personal_tenant:
        exported = repo.export_tenant(identity)
        _remove_document_artifacts(exported.get("documents", []))
        tenant_result = repo.delete_tenant_data(identity.tenant_id)
        result = repo.delete_personal_data(identity)
        result["deleted"].update(tenant_result["deleted"])
        result["graphs_removed_for_documents"] = len(exported.get("documents", []))
    else:
        result = repo.delete_personal_data(identity)
    return APIResponse.ok(result)


@router.delete("/account/tenant-data")
async def delete_tenant_data(
    confirmation: str = Query(min_length=1, max_length=200),
    identity: RequestIdentity = Depends(require_authenticated_identity),
):
    require_admin(identity)
    if confirmation != identity.tenant_id:
        raise HTTPException(status_code=400, detail="Type the exact tenant id to confirm deletion")
    repo = get_account_repository()
    repo.sync_identity(identity)
    exported = repo.export_tenant(identity)
    documents = exported.get("documents", [])

    _remove_document_artifacts(documents)

    result = repo.delete_tenant_data(identity.tenant_id)
    result["graphs_removed_for_documents"] = len(documents)
    return APIResponse.ok(result)


def _remove_document_artifacts(documents: list[dict]) -> None:
    graph_repo = graph_store.get_graph_repository()
    blob_repo = blob_store.get_blob_repository()
    for document in documents:
        doc_id = document.get("doc_id")
        if doc_id:
            graph_repo.remove_document(str(doc_id))
        blob_ref = document.get("blob") or document.get("blob_ref") or document.get("storage")
        if blob_ref:
            blob_repo.delete(blob_ref)


@router.post("/ops/events")
async def ingest_operational_event(
    body: OpsEventInput,
    public_demo: str | None = Header(default=None, alias="X-GraphRAG-Public-Demo"),
):
    if public_demo != "1":
        raise HTTPException(status_code=403, detail="Trusted edge event required")
    event_id = get_account_repository().record_ops_event(
        severity=body.severity,
        source=body.source,
        event_type=body.event_type,
        request_id=body.request_id or get_request_id(),
        tenant_id=body.tenant_id,
        actor_id=body.actor_id,
        message=body.message,
        payload=body.context,
    )
    return APIResponse.ok({"event_id": event_id})


@router.get("/ops/summary")
async def operations_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    identity: RequestIdentity = Depends(require_authenticated_identity),
):
    require_admin(identity)
    _sync(identity)
    summary = get_account_repository().ops_summary(hours)
    summary["readiness"] = operational_readiness()
    return APIResponse.ok(summary)
