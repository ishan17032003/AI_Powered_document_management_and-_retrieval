"""XENIUS DocVault API — application entry point."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .bootstrap import prepare_runtime_directories
from .error_responses import (
    http_error_handler,
    service_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from .middleware import LoginBodyLimitMiddleware, RequestCorrelationMiddleware
from .model_warmup import warm_all_models
from .mongodb import init_mongodb
from .request_body_limits import RequestBodyLimitMiddleware
from .routers import access, admin, audit, auth, documents, duplicates, ingestions, system
from .routers import search as search_router
from .runtime import settings
from .schema_compatibility import assert_schema_compatible
from .services.exceptions import ServiceError

app = FastAPI(
    title=f"{settings.app_name} API",
    version=__version__,
    description="AI-based Digital Document Management System — local vertical slice.",
    debug=settings.debug,
)

# Middleware is added inside-out by Starlette. The body limit sits inside CORS
# so its 413 response keeps browser contract headers, while correlation remains
# outermost and adds request identifiers to every rejection.
app.add_middleware(LoginBodyLimitMiddleware)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_upload_bytes=settings.max_upload_bytes,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Correlation-ID"],
)
app.add_middleware(RequestCorrelationMiddleware)


@app.on_event("startup")
async def _startup() -> None:
    # A web worker must never create or "repair" database objects. Operators
    # migrate explicitly before any process can serve.
    assert_schema_compatible(settings.database_url)
    prepare_runtime_directories(settings)
    # Download (first boot) and load all pipeline models — BGE-M3, reranker,
    # Docling, SigLIP2 — on background threads so the first user request never
    # pays the download/cold-start cost while /ready stays green immediately.
    warm_all_models()
    # Initialise MongoDB connection and create indexes for Ask AI session store.
    # No-op when DOCVAULT_MONGODB_URL is not set (stateless mode).
    await init_mongodb()


@app.exception_handler(ServiceError)
async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return await service_error_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return await validation_error_handler(request, exc)


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    return await http_error_handler(request, exc)


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return await unhandled_error_handler(request, exc)


app.include_router(system.router)
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(search_router.router)
app.include_router(duplicates.router)
app.include_router(audit.router)
app.include_router(admin.router)
app.include_router(access.router)
app.include_router(ingestions.router)

# Direct alias for Google OAuth redirect URI (matches http://localhost:8080/api/auth/callback)
app.add_api_route(
    "/api/auth/callback",
    auth.google_drive_callback,
    methods=["GET"],
    include_in_schema=False,
    response_class=HTMLResponse,
)

