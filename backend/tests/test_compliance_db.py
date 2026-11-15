"""合规复核 DB 编排集成测试（SQLite 内存库，自带 engine，不依赖外部环境）。

覆盖：授权归并（迟到/重复不恢复已退订）→ 复核剔除与补位（重试不重复补位）→
发布固定快照（失败保留旧版）→ 分渠道核对与导出。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.models import Base
from src.models.audience import Audience
from src.models.campaign import Campaign
from src.models.compliance import AudienceMember, AudiencePublish
from src.models.member import Member
from src.services import compliance_service, consent_service, export_service
from src.utils.errors import AppError

T0 = datetime(2026, 11, 13, 1, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with factory() as session:
        yield session


def _seed_members(db: Session) -> list[Member]:
    members = []
    for index in range(1, 21):
        members.append(
            Member(
                member_no=f"MY{index:05d}",
                name=f"会员{index:02d}",
                phone=f"1380000{index:04d}",
                city="上海",
                tier="normal",
                joined_on=datetime(2025, 1, 1).date(),
                segment_code="champion" if index <= 12 else "loyal",
                composite_score=round(100 - index, 2),
                purchase_rate=0.05,
            )
        )
    db.add_all(members)
    db.flush()
    return members


def _seed_audience(db: Session) -> Audience:
    campaign = Campaign(code="C1", name="测试活动", budget=1000.0, status="active")
    db.add(campaign)
    db.flush()
    audience = Audience(
        audience_no="A2026111401",
        campaign_id=campaign.id,
        status="confirmed",
        budget=1000.0,
        member_count=6,
        allocation=[
            {"segment_code": "champion", "status": "allocated", "allocated": 4, "budget": 600.0},
            {"segment_code": "loyal", "status": "allocated", "allocated": 2, "budget": 300.0},
        ],
    )
    db.add(audience)
    db.flush()
    return audience


def _event(event_id, member_no, channel, action, hours):
    return {
        "event_id": event_id,
        "member_no": member_no,
        "channel": channel,
        "action": action,
        "event_time": T0 + timedelta(hours=hours),
    }


def test_consent_import_merge_and_idempotency(db):
    members = _seed_members(db)
    first = consent_service.import_events(
        db,
        batch_no="B1",
        events=[
            _event("E1", "MY00001", "sms", "unsubscribed", 2),
            _event("E2", "MY00002", "app", "opted_out", 2),
            _event("E3", "MY00003", "all", "complained", 2),
            _event("E4", "MY00004", "sms", "unsubscribed", 2),
            _event("E6", "MY00005", "sms", "unsubscribed", 1),
            _event("E7", "MY00005", "sms", "subscribed", 3),   # 同批更晚的恢复：生效
            _event("E8", "NOBODY", "sms", "unsubscribed", 1),  # 未知会员
        ],
        operator_email="test@rfmseg.io",
    )
    assert (first["applied"], first["late_ignored"], first["unknown_members"]) == (6, 0, 1)
    db.commit()

    # 后续批次才到达的「恢复授权」，事件时间早于已生效退订 → 迟到，不能恢复已退订
    late = consent_service.import_events(
        db,
        batch_no="B2",
        events=[_event("E5", "MY00004", "sms", "subscribed", 1)],
        operator_email="test@rfmseg.io",
    )
    assert late["late_ignored"] == 1 and late["applied"] == 0

    consent_map = consent_service.load_consent_map(db)
    by_member = {members[i].id: consent_map.get(members[i].id, {}) for i in range(5)}
    assert by_member[members[0].id]["sms"][0] == "blocked"
    assert by_member[members[1].id]["app"][0] == "blocked"
    assert by_member[members[2].id]["all"][0] == "blocked"
    assert by_member[members[3].id]["sms"][0] == "blocked"   # 迟到恢复未生效
    assert by_member[members[4].id]["sms"][0] == "sendable"  # 更晚恢复已生效

    # 重复导入同批：全部幂等跳过，状态不变
    second = consent_service.import_events(
        db,
        batch_no="B1-retry",
        events=[_event("E1", "MY00001", "sms", "unsubscribed", 2)],
        operator_email="test@rfmseg.io",
    )
    assert second["duplicates"] == 1 and second["applied"] == 0
    assert consent_service.load_consent_map(db)[members[0].id]["sms"][0] == "blocked"


def test_review_exclude_backfill_and_retry(db):
    _seed_members(db)
    audience = _seed_audience(db)
    consent_service.import_events(
        db,
        batch_no="B1",
        events=[
            _event("E1", "MY00001", "sms", "unsubscribed", 1),  # champion 在册
            _event("E2", "MY00002", "all", "complained", 1),    # champion 在册
            _event("E3", "MY00013", "app", "opted_out", 1),     # loyal 在册
        ],
        operator_email="test@rfmseg.io",
    )
    result = compliance_service.review_audience(
        db, audience=audience, send_channel="all", reimport=True, operator_email="test@rfmseg.io"
    )
    counts = result["counts"]
    # 6 人在册：3 人剔除（E1/E2/E3），补位 3 人补足预算内 6 人
    assert counts["excluded"] == 3
    assert counts["sendable"] == 6
    assert counts["backfill"] == 3
    assert counts["shortage"] == 0
    assert result["target_count"] == 6
    # 分渠道核对：短信屏蔽 1（E1）+ 投诉 1（E2）；APP 屏蔽 1（E3）+ 投诉 1（E2）
    assert result["channels"]["sms"]["excluded"] == 2
    assert result["channels"]["app"]["excluded"] == 2
    assert result["channels"]["all"]["excluded"] == 3

    # 重试不重复补位：在册记录不变，无新增补位
    again = compliance_service.review_audience(
        db, audience=audience, send_channel="all", reimport=False, operator_email="test@rfmseg.io"
    )
    total_rows = db.scalars(
        select(AudienceMember).where(AudienceMember.audience_id == audience.id)
    ).all()
    assert len(total_rows) == 9  # 6 base + 3 backfill，没有重复
    assert again["counts"]["backfill"] == 3
    assert again["round_no"] == 2  # 轮次留痕


def test_publish_freezes_snapshot_and_failure_keeps_old(db, monkeypatch):
    members = _seed_members(db)
    audience = _seed_audience(db)
    consent_service.import_events(
        db, batch_no="B1", events=[_event("E1", "MY00001", "sms", "unsubscribed", 1)],
        operator_email="test@rfmseg.io",
    )
    compliance_service.review_audience(
        db, audience=audience, send_channel="all", reimport=True, operator_email="test@rfmseg.io"
    )
    first = compliance_service.publish_audience(db, audience=audience, operator_email="test@rfmseg.io", note="v1")
    db.commit()
    assert first["current_version"]["version_no"] == 1
    v1_sendable = first["current_version"]["sendable_count"]

    # 新退订到达 → 再复核 → 发布 v2，v1 转历史
    consent_service.import_events(
        db, batch_no="B2", events=[_event("E2", "MY00003", "sms", "unsubscribed", 3)],
        operator_email="test@rfmseg.io",
    )
    compliance_service.review_audience(
        db, audience=audience, send_channel="all", reimport=False, operator_email="test@rfmseg.io"
    )
    second = compliance_service.publish_audience(db, audience=audience, operator_email="test@rfmseg.io", note="v2")
    db.commit()
    assert second["current_version"]["version_no"] == 2
    versions = {row.version_no: row.is_current for row in db.scalars(select(AudiencePublish)).all()}
    assert versions == {1: False, 2: True}

    # 发布中途失败（旧版已下架、新版未落库）→ 回滚，v2 仍是当前版本（失败保留旧版）
    real_flush = db.flush
    def _boom(*args, **kwargs):
        raise RuntimeError("flush failed")

    monkeypatch.setattr(db, "flush", _boom)
    with pytest.raises(RuntimeError):
        compliance_service.publish_audience(db, audience=audience, operator_email="test@rfmseg.io", note="v3")
    monkeypatch.setattr(db, "flush", real_flush)
    db.rollback()
    current = db.scalars(select(AudiencePublish).where(AudiencePublish.is_current.is_(True))).one()
    assert current.version_no == 2
    assert current.sendable_count == second["current_version"]["sendable_count"]
    assert v1_sendable == first["current_version"]["sendable_count"]

    # 快照固定了可发名单与剔除原因
    snapshot = current.snapshot
    assert snapshot["counts"]["sendable"] == current.sendable_count
    excluded_nos = {item["member_no"] for item in snapshot["excluded"]}
    assert {members[0].member_no, members[2].member_no} <= excluded_nos
    assert all(item.get("exclude_reason") for item in snapshot["excluded"])


def test_publish_requires_review_and_export_csv(db):
    _seed_members(db)
    audience = _seed_audience(db)
    with pytest.raises(AppError):
        compliance_service.publish_audience(db, audience=audience, operator_email="t", note=None)

    compliance_service.review_audience(
        db, audience=audience, send_channel="sms", reimport=True, operator_email="test@rfmseg.io"
    )
    content = export_service.compliance_csv(db, audience.id).decode("utf-8-sig")
    assert "复核汇总" in content and "分渠道核对" in content and "会员明细" in content
    assert "短信" in content and "APP 推送" in content and "全渠道" in content
