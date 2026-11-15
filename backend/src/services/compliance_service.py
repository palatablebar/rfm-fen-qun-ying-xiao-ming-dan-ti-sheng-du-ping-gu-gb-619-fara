"""发送前合规复核编排：复核（剔除 + 补位）、发布（快照换版）、核对查询。

复核与发布都在 controller 的单事务里提交：
- 复核失败 → 回滚，工作区保持上一轮状态；
- 发布失败 → 回滚，is_current 的旧版本不动（失败保留旧版）。
"""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.constants.enums import (
    AUDIENCE_STATUS_LABELS,
    CONSENT_CHANNEL_LABELS,
    EXCLUDE_REASON_LABELS,
    MEMBER_SOURCE_LABELS,
    REVIEW_STATUS_LABELS,
    SEGMENT_LABELS,
    MemberSource,
    ReviewStatus,
)
from src.models.audience import Audience
from src.models.compliance import AudienceMember, AudiencePublish, AudienceReview
from src.models.member import Member
from src.services import compliance_rules, consent_service
from src.services.member_service import mask_phone
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc


def _segment_targets(audience: Audience) -> dict[str, int]:
    """算法 B 配额：{segment_code: 目标人数}（按分配优先级顺序）。"""
    targets: dict[str, int] = {}
    for row in audience.allocation or []:
        if row.get("status") != "allocated" or not row.get("allocated"):
            continue
        targets[row["segment_code"]] = int(row["allocated"])
    return targets


def _target_count(audience: Audience, targets: dict[str, int]) -> int:
    return int(audience.member_count or 0) or sum(targets.values())


def _expand_base(db: Session, audience: Audience, targets: dict[str, int], round_no: int) -> int:
    """从分群（重）导入会员：已存在的保留不动，只插入新会员（不丢记录）。"""
    existing = {
        row[0]
        for row in db.execute(
            select(AudienceMember.member_id).where(AudienceMember.audience_id == audience.id)
        ).all()
    }
    added = 0
    for segment_code, allocated in targets.items():
        members = db.scalars(
            select(Member)
            .where(Member.segment_code == segment_code)
            .order_by(Member.composite_score.desc().nullslast(), Member.id)
            .limit(allocated)
        ).all()
        for member in members:
            if member.id in existing:
                continue
            db.add(
                AudienceMember(
                    audience_id=audience.id,
                    member_id=member.id,
                    segment_code=segment_code,
                    source=MemberSource.BASE.value,
                    status=ReviewStatus.SENDABLE.value,
                    round_no=round_no,
                )
            )
            existing.add(member.id)
            added += 1
    db.flush()
    return added


def _load_members(db: Session, audience_id: int) -> list[AudienceMember]:
    return db.scalars(
        select(AudienceMember).where(AudienceMember.audience_id == audience_id).order_by(AudienceMember.id)
    ).all()


def review_audience(
    db: Session,
    *,
    audience: Audience,
    send_channel: str,
    reimport: bool,
    operator_email: str,
) -> dict:
    """合规复核：剔除不可发会员 + 补足预算内人数（幂等，重试不重复补位）。"""
    if audience.status == "pushed":
        raise AppError("CONFLICT", 409, {"reason": "名单已推送券平台，不能再复核"})
    targets = _segment_targets(audience)
    if not targets:
        raise AppError("CONFLICT", 409, {"reason": "名单没有可分群配额，请重新生成名单"})

    last_round = db.scalar(
        select(func.max(AudienceReview.round_no)).where(AudienceReview.audience_id == audience.id)
    ) or 0
    round_no = last_round + 1

    has_members = db.scalar(
        select(func.count()).select_from(AudienceMember).where(AudienceMember.audience_id == audience.id)
    ) or 0
    imported = 0
    if reimport or not has_members:
        imported = _expand_base(db, audience, targets, round_no)

    rows = _load_members(db, audience.id)
    consent_map = consent_service.load_consent_map(db)
    evaluations = {
        row.member_id: compliance_rules.evaluate_member(consent_map.get(row.member_id, {}), send_channel)
        for row in rows
    }

    in_list_ids = {row.member_id for row in rows}
    target_count = _target_count(audience, targets)
    candidates: dict[str, list[int]] = {}
    for segment_code in targets:
        pool = db.scalars(
            select(Member)
            .where(Member.segment_code == segment_code, Member.id.notin_(in_list_ids))
            .order_by(Member.composite_score.desc().nullslast(), Member.id)
            .limit(max(target_count, 1))
        ).all()
        candidates[segment_code] = [
            member.id
            for member in pool
            if compliance_rules.evaluate_member(consent_map.get(member.id, {}), send_channel)["sendable"]
        ]

    plan = compliance_rules.plan_review(
        members=[{"member_id": row.member_id, "segment_code": row.segment_code, "source": row.source} for row in rows],
        evaluations=evaluations,
        segment_targets=targets,
        target_count=target_count,
        candidates=candidates,
    )

    for row in rows:
        decision = plan["decisions"][row.member_id]
        row.status = decision["status"]
        row.exclude_reason = decision["exclude_reason"]
        row.blocked_channels = decision["blocked_sources"]
        row.round_no = round_no
    for pick in plan["backfill"]:
        db.add(
            AudienceMember(
                audience_id=audience.id,
                member_id=pick["member_id"],
                segment_code=pick["segment_code"],
                source=MemberSource.BACKFILL.value,
                status=ReviewStatus.SENDABLE.value,
                round_no=round_no,
            )
        )
    db.flush()

    db.add(
        AudienceReview(
            audience_id=audience.id,
            round_no=round_no,
            send_channel=send_channel,
            target_count=target_count,
            sendable_count=plan["sendable_after"],
            excluded_count=plan["excluded_count"],
            backfill_count=plan["backfill_total"],
            shortage=plan["shortage"],
            reimport=bool(reimport or not has_members),
            operator_email=operator_email,
        )
    )
    db.flush()
    result = get_compliance(db, audience)
    result["imported"] = imported
    return result


def publish_audience(db: Session, *, audience: Audience, operator_email: str, note: str | None) -> dict:
    """发布：固定当前工作区为一个版本。换版同事务，失败回滚保留旧版。"""
    rows = _load_members(db, audience.id)
    if not rows:
        raise AppError("CONFLICT", 409, {"reason": "还没有复核结果，请先执行合规复核"})
    latest_round = db.scalars(
        select(AudienceReview)
        .where(AudienceReview.audience_id == audience.id)
        .order_by(AudienceReview.round_no.desc())
        .limit(1)
    ).first()
    send_channel = latest_round.send_channel if latest_round else "all"

    decisions = {
        row.member_id: {
            "status": row.status,
            "exclude_reason": row.exclude_reason,
            "blocked_sources": list(row.blocked_channels or []),
        }
        for row in rows
    }
    if not any(item["status"] == ReviewStatus.SENDABLE.value for item in decisions.values()):
        raise AppError("CONFLICT", 409, {"reason": "当前复核结果没有可发会员，不能发布"})

    member_nos = dict(
        db.execute(
            select(Member.id, Member.member_no).where(Member.id.in_({row.member_id for row in rows}))
        ).all()
    )
    version_no = (
        db.scalar(select(func.max(AudiencePublish.version_no)).where(AudiencePublish.audience_id == audience.id)) or 0
    ) + 1
    snapshot = compliance_rules.build_snapshot(
        members=[
            {
                "member_id": row.member_id,
                "member_no": member_nos.get(row.member_id),
                "segment_code": row.segment_code,
                "source": row.source,
            }
            for row in rows
        ],
        decisions=decisions,
        send_channel=send_channel,
        version_no=version_no,
    )
    # 单事务换版：旧版下架 + 新版上架；此处任何失败都会回滚，旧版仍是 current
    db.execute(
        update(AudiencePublish)
        .where(AudiencePublish.audience_id == audience.id, AudiencePublish.is_current.is_(True))
        .values(is_current=False)
    )
    publish = AudiencePublish(
        audience_id=audience.id,
        version_no=version_no,
        is_current=True,
        send_channel=send_channel,
        sendable_count=snapshot["counts"]["sendable"],
        excluded_count=snapshot["counts"]["excluded"],
        backfill_count=snapshot["counts"]["backfill"],
        snapshot=snapshot,
        note=note,
        operator_email=operator_email,
    )
    db.add(publish)
    db.flush()
    return get_compliance(db, audience)


def _serialize_version(row: AudiencePublish) -> dict:
    return {
        "id": row.id,
        "version_no": row.version_no,
        "is_current": row.is_current,
        "send_channel": row.send_channel,
        "send_channel_label": CONSENT_CHANNEL_LABELS.get(row.send_channel, row.send_channel),
        "sendable_count": row.sendable_count,
        "excluded_count": row.excluded_count,
        "backfill_count": row.backfill_count,
        "note": row.note,
        "operator_email": row.operator_email,
        "created_at": iso_utc(row.created_at),
    }


def get_compliance(db: Session, audience: Audience) -> dict:
    """工作区核对：可发 / 剔除 / 补位人数，分短信、APP 与全渠道。"""
    rows = _load_members(db, audience.id)
    sendable = sum(1 for row in rows if row.status == ReviewStatus.SENDABLE.value)
    excluded = len(rows) - sendable
    backfill_rows = [row for row in rows if row.source == MemberSource.BACKFILL.value]
    backfill_sendable = sum(1 for row in backfill_rows if row.status == ReviewStatus.SENDABLE.value)
    channels = compliance_rules.channel_summary([{"blocked_sources": list(row.blocked_channels or [])} for row in rows])

    reviews = db.scalars(
        select(AudienceReview).where(AudienceReview.audience_id == audience.id).order_by(AudienceReview.round_no.desc())
    ).all()
    versions = db.scalars(
        select(AudiencePublish).where(AudiencePublish.audience_id == audience.id).order_by(AudiencePublish.version_no.desc())
    ).all()
    current = next((row for row in versions if row.is_current), None)
    latest_review = reviews[0] if reviews else None
    targets = _segment_targets(audience)

    return {
        "audience_id": audience.id,
        "audience_no": audience.audience_no,
        "audience_status": audience.status,
        "audience_status_label": AUDIENCE_STATUS_LABELS.get(audience.status, audience.status),
        "send_channel": latest_review.send_channel if latest_review else "all",
        "reviewed": latest_review is not None,
        "round_no": latest_review.round_no if latest_review else 0,
        "target_count": _target_count(audience, targets),
        "counts": {
            "total": len(rows),
            "sendable": sendable,
            "excluded": excluded,
            "backfill": len(backfill_rows),
            "backfill_sendable": backfill_sendable,
            "shortage": latest_review.shortage if latest_review else 0,
        },
        "channels": {
            channel: {
                "channel_label": CONSENT_CHANNEL_LABELS.get(channel, channel),
                **channels.get(channel, {"sendable": 0, "excluded": 0}),
            }
            for channel in ("sms", "app", "all")
        },
        "reviews": [
            {
                "round_no": item.round_no,
                "send_channel": item.send_channel,
                "send_channel_label": CONSENT_CHANNEL_LABELS.get(item.send_channel, item.send_channel),
                "target_count": item.target_count,
                "sendable_count": item.sendable_count,
                "excluded_count": item.excluded_count,
                "backfill_count": item.backfill_count,
                "shortage": item.shortage,
                "reimport": item.reimport,
                "operator_email": item.operator_email,
                "created_at": iso_utc(item.created_at),
            }
            for item in reviews[:10]
        ],
        "versions": [_serialize_version(row) for row in versions],
        "current_version": _serialize_version(current) if current else None,
    }


def list_members(
    db: Session,
    audience: Audience,
    *,
    status: str | None = None,
    source: str | None = None,
    limit: int = 20,
    offset: int = 0,
    mask: bool = False,
) -> dict:
    stmt = select(AudienceMember).where(AudienceMember.audience_id == audience.id)
    count_stmt = select(func.count()).select_from(AudienceMember).where(AudienceMember.audience_id == audience.id)
    if status:
        stmt = stmt.where(AudienceMember.status == status)
        count_stmt = count_stmt.where(AudienceMember.status == status)
    if source:
        stmt = stmt.where(AudienceMember.source == source)
        count_stmt = count_stmt.where(AudienceMember.source == source)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(AudienceMember.id).limit(limit).offset(offset)).all()
    members = {}
    if rows:
        members = {
            item.id: item
            for item in db.scalars(select(Member).where(Member.id.in_({row.member_id for row in rows}))).all()
        }
    items = []
    for row in rows:
        member = members.get(row.member_id)
        phone = member.phone if member else ""
        items.append(
            {
                "id": row.id,
                "member_no": member.member_no if member else None,
                "name": member.name if member else None,
                "phone": mask_phone(phone) if mask else phone,
                "phone_masked": mask,
                "segment_code": row.segment_code,
                "segment_label": SEGMENT_LABELS.get(row.segment_code, row.segment_code),
                "source": row.source,
                "source_label": MEMBER_SOURCE_LABELS.get(row.source, row.source),
                "status": row.status,
                "status_label": REVIEW_STATUS_LABELS.get(row.status, row.status),
                "exclude_reason": row.exclude_reason,
                "exclude_reason_label": EXCLUDE_REASON_LABELS.get(row.exclude_reason or "", row.exclude_reason),
                "blocked_channels": list(row.blocked_channels or []),
                "round_no": row.round_no,
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
