"""授权事件接入与归并：分批导入（幂等）+ 最新状态查询。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CONSENT_ACTION_LABELS,
    CONSENT_CHANNEL_LABELS,
    CONSENT_CHANNEL_ORDER,
    CONSENT_STATUS_LABELS,
    EVENT_DISPOSITION_LABELS,
    ConsentStatus,
    EventDisposition,
)
from src.models.consent import ConsentEvent, MemberConsent
from src.models.member import Member
from src.services import compliance_rules
from src.utils.timeutil import iso_utc, utcnow


def import_events(db: Session, *, batch_no: str, events: list[dict], operator_email: str) -> dict:
    """分批导入授权事件。

    幂等：event_id 唯一，重复事件跳过（不计入流水）；同一批次内重复也只取第一条。
    归并：按事件时间升序应用，迟到事件不改变（会员 × 渠道）的最新状态。
    """
    member_nos = {item["member_no"] for item in events}
    id_by_no = dict(
        db.execute(select(Member.member_no, Member.id).where(Member.member_no.in_(member_nos))).all()
    ) if member_nos else {}

    existing_ids = set()
    if events:
        existing_ids = {
            row[0]
            for row in db.execute(
                select(ConsentEvent.event_id).where(ConsentEvent.event_id.in_({item["event_id"] for item in events}))
            ).all()
        }

    counters = {"total": len(events), "applied": 0, "late_ignored": 0, "duplicates": 0, "unknown_members": 0}
    seen_in_batch: set[str] = set()
    pending: list[tuple[dict, int]] = []  # (event, member_id)
    for item in events:
        event_id = item["event_id"]
        if event_id in seen_in_batch or event_id in existing_ids:
            counters["duplicates"] += 1
            continue
        seen_in_batch.add(event_id)
        member_id = id_by_no.get(item["member_no"])
        if member_id is None:
            counters["unknown_members"] += 1
            continue
        pending.append((item, member_id))

    # 同批内也按事件时间归并：先到的旧事件不能覆盖同批更晚的新状态
    pending.sort(key=lambda pair: (pair[0]["event_time"], pair[0]["event_id"]))

    consent_rows = {}
    if pending:
        keys = {(member_id, item["channel"]) for item, member_id in pending}
        member_ids = {key[0] for key in keys}
        channels = {key[1] for key in keys}
        rows = db.scalars(
            select(MemberConsent).where(MemberConsent.member_id.in_(member_ids), MemberConsent.channel.in_(channels))
        ).all()
        consent_rows = {(row.member_id, row.channel): row for row in rows}

    received_at = utcnow()
    for item, member_id in pending:
        key = (member_id, item["channel"])
        current = consent_rows.get(key)
        disposition, new_status = compliance_rules.merge_decision(
            current_event_id=current.last_event_id if current else None,
            current_event_time=current.last_event_time if current else None,
            event_id=item["event_id"],
            action=item["action"],
            event_time=item["event_time"],
        )
        db.add(
            ConsentEvent(
                event_id=item["event_id"],
                batch_no=batch_no,
                member_id=member_id,
                channel=item["channel"],
                action=item["action"],
                event_time=item["event_time"],
                received_at=received_at,
                disposition=disposition,
                source=item.get("source") or "batch",
                note=item.get("note"),
            )
        )
        counters["applied" if disposition == EventDisposition.APPLIED.value else "late_ignored"] += 1
        if disposition != EventDisposition.APPLIED.value:
            continue
        if current is None:
            current = MemberConsent(
                member_id=member_id,
                channel=item["channel"],
                status=new_status,
                last_action=item["action"],
                last_event_id=item["event_id"],
                last_event_time=item["event_time"],
            )
            db.add(current)
            consent_rows[key] = current
        else:
            current.status = new_status
            current.last_action = item["action"]
            current.last_event_id = item["event_id"]
            current.last_event_time = item["event_time"]
    db.flush()
    counters["batch_no"] = batch_no
    counters["operator"] = operator_email
    return counters


def load_consent_map(db: Session, member_ids: set[int] | None = None) -> dict[int, dict[str, tuple[str, str]]]:
    """{member_id: {channel: (status, action)}}，供复核评估使用。"""
    stmt = select(MemberConsent)
    if member_ids is not None:
        if not member_ids:
            return {}
        stmt = stmt.where(MemberConsent.member_id.in_(member_ids))
    result: dict[int, dict[str, tuple[str, str]]] = {}
    for row in db.scalars(stmt).all():
        result.setdefault(row.member_id, {})[row.channel] = (row.status, row.last_action)
    return result


def summary(db: Session) -> dict:
    """分渠道归并状态总览 + 到达批次统计。"""
    rows = db.execute(
        select(MemberConsent.channel, MemberConsent.status, func.count()).group_by(
            MemberConsent.channel, MemberConsent.status
        )
    ).all()
    channels = {
        channel: {"blocked": 0, "sendable": 0}
        for channel in CONSENT_CHANNEL_ORDER
    }
    for channel, status, count in rows:
        bucket = channels.setdefault(channel, {"blocked": 0, "sendable": 0})
        bucket["blocked" if status == ConsentStatus.BLOCKED.value else "sendable"] += count
    event_total = db.scalar(select(func.count()).select_from(ConsentEvent)) or 0
    late_total = db.scalar(
        select(func.count()).select_from(ConsentEvent).where(
            ConsentEvent.disposition == EventDisposition.LATE_IGNORED.value
        )
    ) or 0
    batches = db.execute(
        select(ConsentEvent.batch_no, func.count(), func.max(ConsentEvent.received_at))
        .group_by(ConsentEvent.batch_no)
        .order_by(func.max(ConsentEvent.received_at).desc())
        .limit(10)
    ).all()
    return {
        "channels": [
            {
                "channel": channel,
                "channel_label": CONSENT_CHANNEL_LABELS.get(channel, channel),
                "blocked": channels.get(channel, {}).get("blocked", 0),
                "sendable": channels.get(channel, {}).get("sendable", 0),
            }
            for channel in CONSENT_CHANNEL_ORDER
        ],
        "events_total": event_total,
        "late_ignored_total": late_total,
        "batches": [
            {"batch_no": batch_no, "events": count, "received_at": iso_utc(received_at)}
            for batch_no, count, received_at in batches
        ],
    }


def list_events(
    db: Session,
    *,
    channel: str | None = None,
    action: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(ConsentEvent)
    count_stmt = select(func.count()).select_from(ConsentEvent)
    if channel:
        stmt = stmt.where(ConsentEvent.channel == channel)
        count_stmt = count_stmt.where(ConsentEvent.channel == channel)
    if action:
        stmt = stmt.where(ConsentEvent.action == action)
        count_stmt = count_stmt.where(ConsentEvent.action == action)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(ConsentEvent.id.desc()).limit(limit).offset(offset)).all()
    member_nos = {}
    if rows:
        ids = {row.member_id for row in rows}
        member_nos = dict(db.execute(select(Member.id, Member.member_no).where(Member.id.in_(ids))).all())
    return {
        "items": [
            {
                "id": row.id,
                "event_id": row.event_id,
                "batch_no": row.batch_no,
                "member_no": member_nos.get(row.member_id),
                "channel": row.channel,
                "channel_label": CONSENT_CHANNEL_LABELS.get(row.channel, row.channel),
                "action": row.action,
                "action_label": CONSENT_ACTION_LABELS.get(row.action, row.action),
                "event_time": iso_utc(row.event_time),
                "received_at": iso_utc(row.received_at),
                "disposition": row.disposition,
                "disposition_label": EVENT_DISPOSITION_LABELS.get(row.disposition, row.disposition),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def list_consents(
    db: Session,
    *,
    channel: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(MemberConsent)
    count_stmt = select(func.count()).select_from(MemberConsent)
    if channel:
        stmt = stmt.where(MemberConsent.channel == channel)
        count_stmt = count_stmt.where(MemberConsent.channel == channel)
    if status:
        stmt = stmt.where(MemberConsent.status == status)
        count_stmt = count_stmt.where(MemberConsent.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(MemberConsent.last_event_time.desc(), MemberConsent.id).limit(limit).offset(offset)
    ).all()
    members = {}
    if rows:
        ids = {row.member_id for row in rows}
        members = {
            item.id: item for item in db.scalars(select(Member).where(Member.id.in_(ids))).all()
        }
    return {
        "items": [
            {
                "id": row.id,
                "member_no": members[row.member_id].member_no if row.member_id in members else None,
                "member_name": members[row.member_id].name if row.member_id in members else None,
                "channel": row.channel,
                "channel_label": CONSENT_CHANNEL_LABELS.get(row.channel, row.channel),
                "status": row.status,
                "status_label": CONSENT_STATUS_LABELS.get(row.status, row.status),
                "last_action": row.last_action,
                "last_action_label": CONSENT_ACTION_LABELS.get(row.last_action, row.last_action),
                "last_event_id": row.last_event_id,
                "last_event_time": iso_utc(row.last_event_time),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
