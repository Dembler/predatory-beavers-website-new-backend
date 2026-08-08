import asyncio
import hashlib
import hmac
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic

from pwdlib import PasswordHash

from predatory_beavers.api.errors import ForbiddenError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.auth.errors import (
    InvalidCredentialsError,
    InvalidCsrfTokenError,
    InvalidSessionError,
    LoginRateLimitError,
)
from predatory_beavers.modules.auth.models import Session, User, UserRole
from predatory_beavers.modules.auth.repository import AuthRepository
from predatory_beavers.settings import Settings


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PasswordSecurity:
    """Application-owned Argon2 password operations, including a timing-safe dummy check."""

    def __init__(self) -> None:
        self._password_hash = PasswordHash.recommended()
        self._dummy_password_hash = self._password_hash.hash(secrets.token_urlsafe(32))

    def hash_password(self, password: str) -> str:
        return self._password_hash.hash(password)

    def verify_password(self, password: str, password_hash: str | None) -> bool:
        candidate_hash = password_hash or self._dummy_password_hash
        try:
            return self._password_hash.verify(password, candidate_hash)
        except (TypeError, ValueError):
            return False


class LoginGuard:
    """Bounds login work in one API process; production still needs an edge limiter."""

    def __init__(self, settings: Settings) -> None:
        self._max_attempts = settings.auth_login_max_attempts
        self._window_seconds = settings.auth_login_window_seconds
        self._attempts: dict[str, deque[float]] = {}
        self._max_tracked_keys = 10_000
        self._lock = asyncio.Lock()
        self._password_slots = asyncio.Semaphore(settings.auth_login_max_concurrency)

    async def verify(
        self,
        *,
        login: str,
        client_key: str | None,
        password: str,
        password_hash: str | None,
        password_security: PasswordSecurity,
    ) -> bool:
        await self._consume_attempt(login, client_key)
        async with self._password_slots:
            return await asyncio.to_thread(
                password_security.verify_password,
                password,
                password_hash,
            )

    async def _consume_attempt(self, login: str, client_key: str | None) -> None:
        keys = [f"login:{hash_token(login.strip().lower())}"]
        if client_key:
            keys.append(f"client:{hash_token(client_key)}")
        now = monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            for key in keys:
                attempts = self._attempts.get(key)
                if attempts is None:
                    if len(self._attempts) >= self._max_tracked_keys:
                        self._attempts.pop(next(iter(self._attempts)))
                    attempts = deque()
                    self._attempts[key] = attempts
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                if len(attempts) >= self._max_attempts:
                    raise LoginRateLimitError
            for key in keys:
                self._attempts[key].append(now)


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: User
    session_token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CsrfResult:
    csrf_token: str
    expires_at: datetime


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        password_security: PasswordSecurity,
        settings: Settings,
        unit_of_work: UnitOfWork,
        login_guard: LoginGuard | None = None,
    ) -> None:
        self._repository = repository
        self._password_security = password_security
        self._settings = settings
        self._unit_of_work = unit_of_work
        self._login_guard = login_guard or LoginGuard(settings)

    async def login(
        self,
        username: str,
        password: str,
        *,
        client_key: str | None = None,
    ) -> LoginResult:
        user = await self._repository.get_user_by_login(username)
        password_matches = await self._login_guard.verify(
            login=username,
            client_key=client_key,
            password=password,
            password_hash=user.password_hash if user is not None else None,
            password_security=self._password_security,
        )
        if user is None or not user.is_active or not password_matches:
            raise InvalidCredentialsError

        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._settings.session_ttl_seconds)
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        auth_session = Session(
            user_id=user.id,
            token_hash=hash_token(session_token),
            csrf_token_hash=hash_token(csrf_token),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
        try:
            await self._repository.prune_user_sessions(
                user.id,
                now,
                keep_active=self._settings.auth_max_active_sessions - 1,
            )
            await self._repository.create_session(auth_session)
            await self._unit_of_work.commit()
        except BaseException:
            await self._unit_of_work.rollback()
            raise
        return LoginResult(
            user=user,
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    async def logout(self, session_token: str, csrf_token: str) -> None:
        now = datetime.now(UTC)
        auth_session = await self._repository.get_active_session(hash_token(session_token), now)
        if auth_session is None:
            raise InvalidSessionError
        if not hmac.compare_digest(auth_session.csrf_token_hash, hash_token(csrf_token)):
            raise InvalidCsrfTokenError
        try:
            await self._repository.revoke_session(auth_session, now)
            await self._unit_of_work.commit()
        except BaseException:
            await self._unit_of_work.rollback()
            raise

    async def issue_csrf_token(self, session_token: str) -> CsrfResult:
        auth_session = await self._active_session(session_token)
        if auth_session is None:
            raise InvalidSessionError

        now = datetime.now(UTC)
        csrf_token = secrets.token_urlsafe(32)
        try:
            await self._repository.rotate_csrf_token(
                auth_session,
                csrf_token_hash=hash_token(csrf_token),
                seen_at=now,
            )
            await self._unit_of_work.commit()
        except BaseException:
            await self._unit_of_work.rollback()
            raise
        return CsrfResult(csrf_token=csrf_token, expires_at=auth_session.expires_at)

    async def me(self, session_token: str) -> User:
        auth_session = await self._active_session(session_token)
        if auth_session is None:
            raise InvalidSessionError
        return auth_session.user

    async def authorize(
        self,
        session_token: str,
        csrf_token: str,
        allowed_roles: set[UserRole],
    ) -> User:
        auth_session = await self._active_session(session_token)
        if auth_session is None:
            raise InvalidSessionError
        if not hmac.compare_digest(auth_session.csrf_token_hash, hash_token(csrf_token)):
            raise InvalidCsrfTokenError
        if auth_session.user.role not in allowed_roles:
            raise ForbiddenError("Insufficient permissions")
        return auth_session.user

    async def authorize_session(
        self,
        session_token: str,
        allowed_roles: set[UserRole],
    ) -> User:
        auth_session = await self._active_session(session_token)
        if auth_session is None:
            raise InvalidSessionError
        if auth_session.user.role not in allowed_roles:
            raise ForbiddenError("Insufficient permissions")
        return auth_session.user

    async def _active_session(self, session_token: str) -> Session | None:
        return await self._repository.get_active_session(
            hash_token(session_token), datetime.now(UTC)
        )
