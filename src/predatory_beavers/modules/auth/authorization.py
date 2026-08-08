from uuid import UUID

from predatory_beavers.modules.audit.context import AuditActor, set_audit_actor
from predatory_beavers.modules.auth.errors import InvalidCsrfTokenError, InvalidSessionError
from predatory_beavers.modules.auth.models import UserRole
from predatory_beavers.modules.auth.service import AuthService


class SessionAdminAuthorizer:
    """Bridges session auth to protected club-management use cases."""

    def __init__(self, auth_service: AuthService) -> None:
        self._auth_service = auth_service

    async def require_editor_session(self, session_token: str | None) -> None:
        if not session_token:
            raise InvalidSessionError
        user = await self._auth_service.authorize_session(
            session_token,
            {UserRole.EDITOR, UserRole.ADMIN},
        )
        self._bind_actor(user.id, user.username, user.role)

    async def require_editor(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> None:
        if not session_token:
            raise InvalidSessionError
        if not csrf_token:
            raise InvalidCsrfTokenError
        user = await self._auth_service.authorize(
            session_token,
            csrf_token,
            {UserRole.EDITOR, UserRole.ADMIN},
        )
        self._bind_actor(user.id, user.username, user.role)

    async def require_admin_session(self, session_token: str | None) -> None:
        if not session_token:
            raise InvalidSessionError
        user = await self._auth_service.authorize_session(
            session_token,
            {UserRole.ADMIN},
        )
        self._bind_actor(user.id, user.username, user.role)

    async def require_admin(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> None:
        if not session_token:
            raise InvalidSessionError
        if not csrf_token:
            raise InvalidCsrfTokenError
        user = await self._auth_service.authorize(
            session_token,
            csrf_token,
            {UserRole.ADMIN},
        )
        self._bind_actor(user.id, user.username, user.role)

    @staticmethod
    def _bind_actor(user_id: UUID, username: str, role: UserRole) -> None:
        set_audit_actor(
            AuditActor(
                id=user_id,
                username=username,
                role=role.value,
            )
        )
