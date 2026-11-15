from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src.constants.limits import MAX_IMPORT_ROWS
from src.utils.timeutil import to_utc_naive


class ConsentEventIn(BaseModel):
    """单条授权事件：event_time 支持 ISO 8601；不带时区按业务时区（UTC+8）解释。"""

    event_id: str = Field(min_length=1, max_length=64)
    member_no: str = Field(min_length=1, max_length=24)
    channel: Literal["sms", "app", "all"]
    action: Literal["unsubscribed", "opted_out", "complained", "subscribed"]
    event_time: datetime
    source: str | None = Field(default=None, max_length=24)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("event_time", mode="after")
    @classmethod
    def _normalize_event_time(cls, value: datetime) -> datetime:
        return to_utc_naive(value)


class ConsentBatchIn(BaseModel):
    """分批到达的授权事件（短信退订 / APP 关闭营销通知 / 投诉名单）。"""

    batch_no: str = Field(min_length=1, max_length=40)
    events: list[ConsentEventIn] = Field(min_length=1, max_length=MAX_IMPORT_ROWS)
