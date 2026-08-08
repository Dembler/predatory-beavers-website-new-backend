from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuditActor:
    id: UUID | None
    username: str
    role: str | None


audit_actor_context: ContextVar[AuditActor | None] = ContextVar(
    "audit_actor",
    default=None,
)


def bind_audit_actor(actor: AuditActor | None) -> Token[AuditActor | None]:
    return audit_actor_context.set(actor)


def set_audit_actor(actor: AuditActor) -> None:
    audit_actor_context.set(actor)


def get_audit_actor() -> AuditActor | None:
    return audit_actor_context.get()


def reset_audit_actor(token: Token[AuditActor | None]) -> None:
    audit_actor_context.reset(token)
