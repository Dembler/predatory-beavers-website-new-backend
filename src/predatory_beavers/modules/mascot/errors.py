"""Module-specific errors for the mascot prototype."""

from fastapi import status

from predatory_beavers.api.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)


class MascotDisabledError(NotFoundError):
    """Raised when the prototype is disabled or running in production."""

    code = "not_found"
    detail = "Not found"


class InvalidMascotSessionError(UnauthorizedError):
    code = "invalid_mascot_session"
    detail = "Authentication required"


class InvalidMascotCsrfError(ForbiddenError):
    code = "invalid_csrf_token"
    detail = "Invalid CSRF token"


class MascotOriginError(ForbiddenError):
    code = "invalid_origin"
    detail = "Request origin is not allowed"


class MascotSessionOwnershipError(ForbiddenError):
    code = "session_ownership"
    detail = "Session does not own this resource"


class MascotInputError(AppError):
    status_code = 422
    code = "invalid_message"
    detail = "Message is empty or too long"


class MascotRateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    detail = "Too many messages. Try again later"


class MascotPayloadMismatchError(ConflictError):
    code = "idempotency_payload_mismatch"
    detail = "This request id was already used with different data"


class MascotActiveRunError(ConflictError):
    code = "active_run_exists"
    detail = "Another run is already active for this session"

    def __init__(self, run_id: object) -> None:
        super().__init__(errors=[{"run_id": str(run_id)}])
