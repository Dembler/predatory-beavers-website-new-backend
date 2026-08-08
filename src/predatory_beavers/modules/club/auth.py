from typing import Protocol


class AdminAuthorizer(Protocol):
    """Auth boundary implemented by the composition root, outside this module."""

    async def require_editor_session(self, session_token: str | None) -> None:
        """Raise UnauthorizedError/ForbiddenError unless the session may view admin data."""
        ...

    async def require_editor(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> None:
        """Raise UnauthorizedError/ForbiddenError unless the request may edit club data."""
        ...

    async def require_admin_session(self, session_token: str | None) -> None:
        """Raise unless the session belongs to an administrator."""
        ...

    async def require_admin(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> None:
        """Raise unless an administrator supplied a valid CSRF token."""
        ...
