"""授权事件与归并状态：短信退订 / APP 关闭营销通知 / 投诉名单，每天分批到达。

- consent_events：原始事件流水（append-only），event_id 唯一 —— 重复事件幂等忽略。
- member_consents：按（会员 × 渠道）归并的最新授权状态，
  只被「事件时间更晚」的事件改写 —— 迟到或重复事件不能恢复已退订。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ConsentEvent(Base):
    __tablename__ = "consent_events"
    __table_args__ = (
        Index("ix_consent_events_member_channel", "member_id", "channel"),
        Index("ix_consent_events_batch", "batch_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)  # 上游事件键，去重依据
    batch_no: Mapped[str] = mapped_column(String(40))               # 到达批次（每天分批）
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(8))                 # sms / app / all
    action: Mapped[str] = mapped_column(String(16))                 # unsubscribed / opted_out / complained / subscribed
    event_time: Mapped[datetime] = mapped_column(DateTime)          # 事件实际发生时间（业务时间）
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # 到达本系统时间
    disposition: Mapped[str] = mapped_column(String(16), default="applied")  # applied / late_ignored
    source: Mapped[str] = mapped_column(String(24), default="batch")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MemberConsent(Base):
    __tablename__ = "member_consents"
    __table_args__ = (
        UniqueConstraint("member_id", "channel", name="uq_member_consents_member_channel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(8))      # sms / app / all
    status: Mapped[str] = mapped_column(String(12), default="sendable", index=True)  # sendable / blocked
    last_action: Mapped[str] = mapped_column(String(16), default="subscribed")
    last_event_id: Mapped[str] = mapped_column(String(64), default="")
    last_event_time: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
