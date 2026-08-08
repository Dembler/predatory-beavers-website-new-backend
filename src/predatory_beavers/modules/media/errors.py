from fastapi import status

from predatory_beavers.api.errors import AppError


class MediaTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "media_too_large"
    detail = "Uploaded media is too large"


class UnsupportedImageError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_image"
    detail = "Only JPEG, PNG, and WebP images are supported"


class InvalidImageError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "invalid_image"
    detail = "Uploaded image is invalid"
