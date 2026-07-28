"""HTTP routes for authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_user
from ..services import auth_service
from ..utils.request_context import get_request_context

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=schemas.Token)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    try:
        token = auth_service.login(
            db,
            username=form.username,
            password=form.password,
            context=get_request_context(request),
        )
        # Login creates the durable AuthSession and audit record.  Persist
        # both before returning the token; otherwise the immediate /me call
        # rejects the token because its session row is still uncommitted.
        db.commit()
        return token
    except Exception:
        db.rollback()
        raise


@router.get("/me", response_model=schemas.UserOut)
def me(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return auth_service.current_user(db, user)
