"""发送前合规复核：名单会员工作区 + 发布版本快照。

- audience_members：会员级复核工作区。(audience_id, member_id) 唯一 —— 重试不重复补位；
  剔除只标记不删除、补位只插入不覆盖 —— 补位过程不得丢失记录。
- audience_reviews：每一轮复核的口径与计数（append-only）。
- audience_publishes：发布时固定可发名单与剔除原因（JSON 快照）。
  换版在单事务内「旧版下架 + 新版上架」，失败回滚 —— 失败保留旧版。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class AudienceMember(Base):
    __tablename__ = "audience_members"
    __table_args__ = (
        UniqueConstraint("audience_id", "member_id", name="uq_audience_members_audience_member"),
        Index("ix_audience_members_audience_status", "audience_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    audience_id: Mapped[int] = mapped_column(ForeignKey("audiences.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    segment_code: Mapped[str] = mapped_column(String(20), default="")
    source: Mapped[str] = mapped_column(String(12), default="base")      # base / backfill
    status: Mapped[str] = mapped_column(String(12), default="sendable")  # sendable / excluded
    exclude_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    blocked_channels: Mapped[list] = mapped_column(JSON, default=list)   # 命中的屏蔽渠道 ["sms","all"]
    round_no: Mapped[int] = mapped_column(Integer, default=1)            # 最近一轮复核的轮次
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AudienceReview(Base):
    """复核轮次：每一轮合规复核的口径与计数（append-only，重试/重导都留痕）。"""

    __tablename__ = "audience_reviews"
    __table_args__ = (
        UniqueConstraint("audience_id", "round_no", name="uq_audience_reviews_audience_round"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    audience_id: Mapped[int] = mapped_column(ForeignKey("audiences.id", ondelete="CASCADE"), index=True)
    round_no: Mapped[int] = mapped_column(Integer)
    send_channel: Mapped[str] = mapped_column(String(8), default="all")
    target_count: Mapped[int] = mapped_column(Integer, default=0)     # 预算内人数
    sendable_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    backfill_count: Mapped[int] = mapped_column(Integer, default=0)   # 本轮新增补位
    shortage: Mapped[int] = mapped_column(Integer, default=0)         # 合规候选不足、未能补足的人数
    reimport: Mapped[bool] = mapped_column(Boolean, default=False)    # 本轮是否从分群重导会员
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AudiencePublish(Base):
    __tablename__ = "audience_publishes"
    __table_args__ = (
        UniqueConstraint("audience_id", "version_no", name="uq_audience_publishes_audience_version"),
        Index("ix_audience_publishes_current", "audience_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    audience_id: Mapped[int] = mapped_column(ForeignKey("audiences.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    send_channel: Mapped[str] = mapped_column(String(8), default="all")  # 发布时的发送渠道口径
    sendable_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    backfill_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)           # 可发名单 + 剔除原因（固定）
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
