from fastapi import status

from predatory_beavers.api.errors import AppError, ForbiddenError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    code = "invalid_credentials"
    detail = "Invalid username or password"


class InvalidSessionError(UnauthorizedError):
    code = "invalid_session"
    detail = "Authentication required"


class InvalidCsrfTokenError(ForbiddenError):
    code = "invalid_csrf_token"
    detail = "Invalid CSRF token"


class LoginRateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "login_rate_limited"
    detail = "Too many login attempts. Try again later"
