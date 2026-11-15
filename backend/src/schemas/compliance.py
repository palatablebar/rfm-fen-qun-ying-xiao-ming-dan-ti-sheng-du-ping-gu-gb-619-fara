from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ComplianceReviewIn(BaseModel):
    """合规复核：send_channel 决定按哪个渠道口径判定可发；reimport 从分群重导会员。"""

    send_channel: Literal["sms", "app", "all"] = "all"
    reimport: bool = False


class CompliancePublishIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)
