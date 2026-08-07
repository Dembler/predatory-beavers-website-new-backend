from predatory_beavers.modules.auth.errors import InvalidCsrfTokenError, InvalidSessionError
from predatory_beavers.modules.auth.models import UserRole
from predatory_beavers.modules.auth.service import AuthService


class SessionAdminAuthorizer:
    """Bridges session auth to protected club-management use cases."""

    def __init__(self, auth_service: AuthService) -> None:
        self._auth_service = auth_service

    async def require_editor(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> None:
        if not session_token:
            raise InvalidSessionError
        if not csrf_token:
            raise InvalidCsrfTokenError
        await self._auth_service.authorize(
            session_token,
            csrf_token,
            {UserRole.EDITOR, UserRole.ADMIN},
        )
