from fastapi import status

from predatory_beavers.api.errors import AppError


class AsbDisabledError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "asb_disabled"
    detail = "ASB integration is disabled"


class AsbIdentifierNotAllowedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "asb_identifier_not_allowed"
    detail = "ASB identifiers are not allowlisted"


class AsbUpstreamError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "asb_upstream_error"
    detail = "ASB is temporarily unavailable"


class AsbInvalidResponseError(AsbUpstreamError):
    code = "asb_invalid_response"
    detail = "ASB returned invalid data"


class AsbResponseTooLargeError(AsbUpstreamError):
    code = "asb_response_too_large"
    detail = "ASB response exceeded the configured limit"
