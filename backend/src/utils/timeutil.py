"""时间工具：UTC naive 存储 + 业务时区（UTC+8）展示。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.config.settings import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds") + "Z"


def local_now(reference: datetime | None = None) -> datetime:
    base = reference or utcnow()
    return base + timedelta(minutes=settings.app_timezone_offset_minutes)


def local_stamp(value: datetime | None = None) -> str:
    return (value or local_now()).strftime("%Y-%m-%d %H:%M")


def iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def days_ago(anchor: date, days: int) -> date:
    return anchor - timedelta(days=days)


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def to_utc_naive(value: datetime) -> datetime:
    """事件时间入库口径：带时区 → 转 UTC naive；naive → 按业务时区（UTC+8）解释后转 UTC naive。"""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value - timedelta(minutes=settings.app_timezone_offset_minutes)
