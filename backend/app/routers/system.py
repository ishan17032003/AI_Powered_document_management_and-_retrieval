"""Operational endpoints with separate liveness, readiness, and diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from .. import models
from ..deps import get_current_user
from ..services import system_service

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/live")
def live() -> dict:
    return system_service.live_payload()


@router.get(
    "/health",
    deprecated=True,
)
def health() -> dict:
    return system_service.legacy_health_payload()


@router.get(
    "/ready",
    responses={503: {"description": "A required dependency is unavailable"}},
)
def ready(response: Response) -> dict:
    payload, is_ready = system_service.ready_payload()
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload


@router.get("/status")
def detailed_status(
    _user: models.User = Depends(get_current_user),
) -> dict:
    return system_service.status_payload()
