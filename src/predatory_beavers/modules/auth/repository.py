from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from predatory_beavers.modules.auth.models import Session, User


class AuthRepository:
    def __init__(self, db_session: AsyncSession) -> None:
        self._db_session = db_session

    async def get_user_by_login(self, login: str) -> User | None:
        normalized_login = login.strip().lower()
        statement = select(User).where(
            or_(
                func.lower(User.username) == normalized_login,
                func.lower(User.email) == normalized_login,
            )
        )
        return cast(User | None, await self._db_session.scalar(statement))

    async def create_session(self, auth_session: Session) -> Session:
        self._db_session.add(auth_session)
        try:
            await self._db_session.commit()
        except Exception:
            await self._db_session.rollback()
            raise
        await self._db_session.refresh(auth_session)
        return auth_session

    async def prune_user_sessions(
        self,
        user_id: UUID,
        now: datetime,
        *,
        keep_active: int,
    ) -> None:
        statement = (
            select(Session)
            .where(
                Session.user_id == user_id,
                Session.revoked_at.is_(None),
                Session.expires_at > now,
            )
            .order_by(Session.created_at.desc(), Session.id.desc())
            .with_for_update()
        )
        active_sessions = list(await self._db_session.scalars(statement))
        for stale_session in active_sessions[keep_active:]:
            stale_session.revoked_at = now

    async def get_active_session(self, token_hash: str, now: datetime) -> Session | None:
        statement = (
            select(Session)
            .options(joinedload(Session.user))
            .join(Session.user)
            .where(
                Session.token_hash == token_hash,
                Session.revoked_at.is_(None),
                Session.expires_at > now,
                User.is_active.is_(True),
            )
        )
        return cast(Session | None, await self._db_session.scalar(statement))

    async def revoke_session(self, auth_session: Session, revoked_at: datetime) -> None:
        auth_session.revoked_at = revoked_at
        try:
            await self._db_session.commit()
        except Exception:
            await self._db_session.rollback()
            raise
