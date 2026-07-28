"""Application-layer exceptions translated to HTTP responses at the app edge."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 400
    code = "service_error"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AuthenticationError(ServiceError):
    status_code = 401
    code = "authentication_failed"


class PermissionDeniedError(ServiceError):
    status_code = 403
    code = "forbidden"


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class GoneError(ServiceError):
    status_code = 410
    code = "gone"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


class PayloadTooLargeError(ServiceError):
    status_code = 413
    code = "payload_too_large"


class ConfigurationError(ServiceError):
    status_code = 500
    code = "configuration_error"


class RetrievalUnavailableError(ServiceError):
    """The explicitly selected retrieval provider cannot serve this request."""

    status_code = 503
    code = "retrieval_unavailable"
