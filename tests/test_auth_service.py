from datetime import UTC, datetime
from uuid import uuid4

import pytest

from predatory_beavers.modules.auth.errors import InvalidCredentialsError, LoginRateLimitError
from predatory_beavers.modules.auth.models import Session, User, UserRole
from predatory_beavers.modules.auth.service import AuthService, PasswordSecurity, hash_token
from predatory_beavers.settings import Settings


class FakeAuthRepository:
    def __init__(self, users: list[User]) -> None:
        self.users = users
        self.sessions: list[Session] = []

    async def get_user_by_login(self, login: str) -> User | None:
        normalized = login.strip().lower()
        return next(
            (
                user
                for user in self.users
                if user.username.lower() == normalized or user.email.lower() == normalized
            ),
            None,
        )

    async def create_session(self, auth_session: Session) -> Session:
        auth_session.user = next(user for user in self.users if user.id == auth_session.user_id)
        self.sessions.append(auth_session)
        return auth_session

    async def prune_user_sessions(
        self,
        user_id: object,
        now: datetime,
        *,
        keep_active: int,
    ) -> None:
        active_sessions = sorted(
            (session for session in self.sessions if session.user_id == user_id),
            key=lambda session: session.created_at,
            reverse=True,
        )
        for stale_session in active_sessions[keep_active:]:
            stale_session.revoked_at = now

    async def get_active_session(self, token_hash: str, now: datetime) -> Session | None:
        return next(
            (
                auth_session
                for auth_session in self.sessions
                if auth_session.token_hash == token_hash
                and auth_session.revoked_at is None
                and auth_session.expires_at > now
                and auth_session.user.is_active
            ),
            None,
        )

    async def revoke_session(self, auth_session: Session, revoked_at: datetime) -> None:
        auth_session.revoked_at = revoked_at

    async def rotate_csrf_token(
        self,
        auth_session: Session,
        *,
        csrf_token_hash: str,
        seen_at: datetime,
    ) -> None:
        auth_session.csrf_token_hash = csrf_token_hash
        auth_session.last_seen_at = seen_at


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture(scope="module")
def password_security() -> PasswordSecurity:
    return PasswordSecurity()


def build_user(password_security: PasswordSecurity) -> User:
    return User(
        id=uuid4(),
        username="coach",
        email="coach@example.com",
        password_hash=password_security.hash_password("correct-password"),
        role=UserRole.ADMIN,
        first_name="Alex",
        last_name="Coach",
        is_active=True,
    )


@pytest.mark.asyncio
async def test_login_success_stores_only_hashed_session_token(
    password_security: PasswordSecurity,
) -> None:
    user = build_user(password_security)
    repository = FakeAuthRepository([user])
    service = AuthService(
        repository,  # type: ignore[arg-type]
        password_security,
        Settings(env="test"),
        FakeUnitOfWork(),
    )

    result = await service.login("COACH", "correct-password")

    assert result.user is user
    assert len(repository.sessions) == 1
    stored_session = repository.sessions[0]
    assert stored_session.token_hash == hash_token(result.session_token)
    assert stored_session.token_hash != result.session_token
    assert not hasattr(stored_session, "token")
    assert stored_session.csrf_token_hash == hash_token(result.csrf_token)


@pytest.mark.asyncio
async def test_login_failure_is_identical_for_unknown_user_and_wrong_password(
    password_security: PasswordSecurity,
) -> None:
    user = build_user(password_security)
    repository = FakeAuthRepository([user])
    service = AuthService(
        repository,  # type: ignore[arg-type]
        password_security,
        Settings(env="test"),
        FakeUnitOfWork(),
    )

    errors = []
    for username, password in (
        ("missing", "correct-password"),
        ("coach", "wrong-password"),
    ):
        with pytest.raises(InvalidCredentialsError) as caught:
            await service.login(username, password)
        errors.append((caught.value.code, caught.value.detail, caught.value.status_code))

    assert errors[0] == errors[1]
    assert repository.sessions == []


@pytest.mark.asyncio
async def test_logout_revokes_session(password_security: PasswordSecurity) -> None:
    user = build_user(password_security)
    repository = FakeAuthRepository([user])
    service = AuthService(
        repository,  # type: ignore[arg-type]
        password_security,
        Settings(env="test"),
        FakeUnitOfWork(),
    )
    login = await service.login("coach@example.com", "correct-password")

    await service.logout(login.session_token, login.csrf_token)

    assert repository.sessions[0].revoked_at is not None
    assert repository.sessions[0].revoked_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_login_rate_limit_and_active_session_cap(
    password_security: PasswordSecurity,
) -> None:
    user = build_user(password_security)
    repository = FakeAuthRepository([user])
    settings = Settings(
        env="test",
        auth_login_max_attempts=3,
        auth_max_active_sessions=2,
    )
    service = AuthService(
        repository,  # type: ignore[arg-type]
        password_security,
        settings,
        FakeUnitOfWork(),
    )

    await service.login("coach", "correct-password", client_key="127.0.0.1")
    await service.login("coach", "correct-password", client_key="127.0.0.1")
    await service.login("coach", "correct-password", client_key="127.0.0.1")

    active_sessions = [session for session in repository.sessions if session.revoked_at is None]
    assert len(active_sessions) == 2
    with pytest.raises(LoginRateLimitError):
        await service.login("coach", "correct-password", client_key="127.0.0.1")


@pytest.mark.asyncio
async def test_csrf_token_can_be_rotated_after_session_restore(
    password_security: PasswordSecurity,
) -> None:
    user = build_user(password_security)
    repository = FakeAuthRepository([user])
    unit_of_work = FakeUnitOfWork()
    service = AuthService(
        repository,  # type: ignore[arg-type]
        password_security,
        Settings(env="test"),
        unit_of_work,
    )
    login = await service.login("coach", "correct-password")

    restored = await service.issue_csrf_token(login.session_token)

    assert restored.csrf_token != login.csrf_token
    assert repository.sessions[0].csrf_token_hash == hash_token(restored.csrf_token)
    assert restored.expires_at == login.expires_at
    assert unit_of_work.commits == 2
