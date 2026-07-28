"""Backward-compatible facade for RBAC dependencies and services."""

from .deps import get_current_user, oauth2_scheme, require, require_global
from .services.rbac_service import (
    PERMISSIONS,
    ROLE_PERMISSIONS,
    global_permission_effects,
    has_global_permission,
    has_permission,
    user_permissions,
)
from .services.rbac_service import (
    permission_effects as _user_permission_effects,
)

__all__ = [
    "PERMISSIONS",
    "ROLE_PERMISSIONS",
    "_user_permission_effects",
    "get_current_user",
    "global_permission_effects",
    "has_global_permission",
    "has_permission",
    "oauth2_scheme",
    "require",
    "require_global",
    "user_permissions",
]
