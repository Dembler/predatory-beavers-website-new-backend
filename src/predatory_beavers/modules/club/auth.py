from typing import Protocol


class AdminAuthorizer(Protocol):
    """Auth boundary implemented by the composition root, outside this module."""

    async def require_editor(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> None:
        """Raise UnauthorizedError/ForbiddenError unless the request may edit club data."""
        ...
