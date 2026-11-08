from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, text, true
from sqlalchemy.orm import Mapped, mapped_column

from predatory_beavers.db.base import (
    Base,
    OptimisticVersionMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ContentSection(StrEnum):
    HISTORY = "history"
    RULES = "rules"
    CONTACT_CHANNELS = "contact_channels"
    CAPTAINS = "captains"
    PLAYER_ROLES = "player_roles"
    VIEWING_TIPS = "viewing_tips"


class ContentBlock(
    UUIDPrimaryKeyMixin, TimestampMixin, OptimisticVersionMixin, SoftDeleteMixin, Base
):
    __tablename__ = "content_blocks"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="content_sort_order_non_negative"),
        CheckConstraint(
            "section IN ('history', 'rules', 'contact_channels', 'captains', "
            "'player_roles', 'viewing_tips')",
            name="content_section_allowed",
        ),
        Index("ix_content_blocks_public_order", "section", "active", "is_deleted", "sort_order"),
    )

    section: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(200), nullable=True)
    link_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    link_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
