"""
GraphRAG Studio — FastAPI Backend
Entry point: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import os
import sys
from pathlib import Path

# Ensure backend/ is in sys.path for absolute imports
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv(Path(__file__).parent / ".env", override=False)

from routers import account, documents, indexing, kg, query, search, system
from models.schemas import APIResponse
from observability import RequestContextMiddleware
from operations import report_exception
from security import ProxyAuthMiddleware


_LOCAL_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)


def _is_production() -> bool:
    return any(
        os.getenv(name, "").strip().lower() in {"production", "prod"}
        for name in ("VERCEL_ENV", "ENVIRONMENT")
    )


def _cors_settings() -> tuple[list[str], bool]:
    raw = os.getenv("ALLOWED_ORIGINS", "")
    origins = list(dict.fromkeys(origin.strip() for origin in raw.split(",") if origin.strip()))
    if not origins:
        origins = list(_LOCAL_ORIGINS)
    if "*" in origins:
        # Browsers forbid wildcard origins with credentialed requests. Treat a
        # configured wildcard as an explicit credential-free policy.
        return ["*"], False
    return origins, True


_PRODUCTION = _is_production()
_ALLOWED_ORIGINS, _ALLOW_CREDENTIALS = _cors_settings()

app = FastAPI(
    title="GraphRAG Studio API",
    description="Multimodal RAG Q&A system backend — MinerU + LangExtract + Agentic-RAG",
    version="1.0.0",
    docs_url=None if _PRODUCTION else "/docs",
    redoc_url=None if _PRODUCTION else "/redoc",
    openapi_url=None if _PRODUCTION else "/openapi.json",
)

# Add proxy auth before CORS so CORS remains the outer middleware and approved
# browser origins receive consistent headers on rejected API requests.
app.add_middleware(ProxyAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    identity = getattr(request.state, "identity", None)
    report_exception(
        "unhandled_exception",
        exc,
        identity=identity,
        context={"method": request.method, "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content=APIResponse.err(5000, "Internal server error").model_dump(),
    )

# All routers under /api/v1. Each router carries its own sub-prefix.
# documents.router  prefix="/documents" → /api/v1/documents
# indexing.router   prefix="/index"     → /api/v1/index
# kg.router         prefix="/kg"        → /api/v1/kg
# query.router      prefix="/query"     → /api/v1/query
# search.router     prefix="/search"    → /api/v1/search
# system.router     no prefix           → /api/v1/health, /api/v1/system/...
PREFIX = "/api/v1"
app.include_router(documents.router, prefix=PREFIX)
app.include_router(indexing.router,  prefix=PREFIX)
app.include_router(kg.router,        prefix=PREFIX)
app.include_router(query.router,     prefix=PREFIX)
app.include_router(search.router,    prefix=PREFIX)
app.include_router(system.router,    prefix=PREFIX)
app.include_router(account.router,   prefix=PREFIX)


@app.get("/")
async def root():
    data = {"msg": "GraphRAG Studio API v1.0.0", "health": "/api/v1/health/live"}
    if not _PRODUCTION:
        data["docs"] = "/docs"
    return data


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
