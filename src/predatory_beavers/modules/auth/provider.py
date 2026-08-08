from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.auth.authorization import SessionAdminAuthorizer
from predatory_beavers.modules.auth.repository import AuthRepository
from predatory_beavers.modules.auth.service import AuthService, LoginGuard, PasswordSecurity
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.settings import Settings


class AuthProvider(Provider):
    @provide(scope=Scope.APP)
    def password_security(self) -> PasswordSecurity:
        return PasswordSecurity()

    @provide(scope=Scope.APP)
    def login_guard(self, settings: Settings) -> LoginGuard:
        return LoginGuard(settings)

    @provide(scope=Scope.REQUEST)
    def repository(self, db_session: AsyncSession) -> AuthRepository:
        return AuthRepository(db_session)

    @provide(scope=Scope.REQUEST)
    def service(
        self,
        repository: AuthRepository,
        password_security: PasswordSecurity,
        settings: Settings,
        unit_of_work: UnitOfWork,
        login_guard: LoginGuard,
    ) -> AuthService:
        return AuthService(repository, password_security, settings, unit_of_work, login_guard)

    @provide(scope=Scope.REQUEST)
    def admin_authorizer(
        self,
        service: AuthService,
    ) -> AdminAuthorizer:
        return SessionAdminAuthorizer(service)
