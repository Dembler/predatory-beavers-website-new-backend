from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from predatory_beavers.modules.auth.models import UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: str
    role: UserRole
    first_name: str | None
    last_name: str | None


class LoginData(BaseModel):
    user: UserRead
    csrf_token: str
    expires_at: datetime


class LogoutData(BaseModel):
    logged_out: bool = True
