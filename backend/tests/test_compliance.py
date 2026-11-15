"""算法 C（发送前合规复核）单测：归并 / 渠道判定 / 剔除补位 / 发布快照。"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.services import compliance_rules

T0 = datetime(2026, 11, 13, 1, 0)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def _merge_seq(events):
    """按到达顺序应用归并规则，返回 (最终状态, 各事件处置)。"""
    current_id, current_time, status = None, None, "sendable"
    dispositions = []
    for event_id, action, event_time in events:
        disposition, new_status = compliance_rules.merge_decision(
            current_event_id=current_id,
            current_event_time=current_time,
            event_id=event_id,
            action=action,
            event_time=event_time,
        )
        dispositions.append(disposition)
        if disposition == "applied":
            current_id, current_time, status = event_id, event_time, new_status
    return status, dispositions


def test_unsubscribe_blocks_and_late_subscribe_cannot_restore():
    # 退订生效后，事件时间更早的「恢复授权」是迟到事件，不能恢复已退订
    status, dispositions = _merge_seq(
        [
            ("E1", "unsubscribed", T1),
            ("E2", "subscribed", T0),  # 迟到：event_time 早于退订
        ]
    )
    assert status == "blocked"
    assert dispositions == ["applied", "late_ignored"]


def test_duplicate_event_cannot_restore_unsubscribed():
    # 重复事件（同 event_id 再次到达）幂等忽略
    status, dispositions = _merge_seq(
        [
            ("E1", "unsubscribed", T1),
            ("E1", "unsubscribed", T1),
        ]
    )
    assert status == "blocked"
    assert dispositions == ["applied", "late_ignored"]


def test_newer_subscribe_restores_sendable():
    # 事件时间更晚的恢复授权可以恢复
    status, dispositions = _merge_seq(
        [
            ("E1", "unsubscribed", T0),
            ("E2", "subscribed", T2),
        ]
    )
    assert status == "sendable"
    assert dispositions == ["applied", "applied"]


def test_out_of_order_batches_keep_latest_event_time():
    # 分批乱序到达：晚到的批次携带更早的事件时间，仍按事件时间归并
    status, _ = _merge_seq(
        [
            ("E2", "subscribed", T2),   # 先到的批次里是较晚的恢复
            ("E1", "unsubscribed", T0), # 后到的批次里是较早的退订 → 迟到忽略
        ]
    )
    assert status == "sendable"


def test_evaluate_member_by_send_channel():
    consents = {"sms": ("blocked", "unsubscribed")}
    # 短信退订：发短信与全渠道不可发，只发 APP 可发
    assert compliance_rules.evaluate_member(consents, "sms")["sendable"] is False
    assert compliance_rules.evaluate_member(consents, "all")["sendable"] is False
    assert compliance_rules.evaluate_member(consents, "app")["sendable"] is True
    # 投诉（all）三个渠道都屏蔽，主因是投诉
    complaint = {"all": ("blocked", "complained")}
    for channel in ("sms", "app", "all"):
        result = compliance_rules.evaluate_member(complaint, channel)
        assert result["sendable"] is False
        assert result["reason"] == "complained"
    # 多渠道同时屏蔽 → multi_blocked
    multi = {"sms": ("blocked", "unsubscribed"), "app": ("blocked", "opted_out")}
    assert compliance_rules.evaluate_member(multi, "all")["reason"] == "multi_blocked"
    # 无记录 → 可发
    assert compliance_rules.evaluate_member({}, "all")["sendable"] is True


def _members(spec):
    return [{"member_id": mid, "segment_code": seg, "source": "base"} for mid, seg in spec]


def test_plan_review_excludes_and_backfills_to_target():
    members = _members([(1, "champion"), (2, "champion"), (3, "champion"), (4, "loyal")])
    evaluations = {
        1: {"sendable": True, "blocked_sources": [], "reason": None},
        2: {"sendable": False, "blocked_sources": ["sms"], "reason": "sms_unsubscribed"},
        3: {"sendable": True, "blocked_sources": [], "reason": None},
        4: {"sendable": False, "blocked_sources": ["all"], "reason": "complained"},
    }
    plan = compliance_rules.plan_review(
        members=members,
        evaluations=evaluations,
        segment_targets={"champion": 3, "loyal": 1},
        target_count=4,
        candidates={"champion": [11, 12], "loyal": [21]},
    )
    assert plan["decisions"][2]["status"] == "excluded"
    assert plan["decisions"][2]["exclude_reason"] == "sms_unsubscribed"
    assert plan["decisions"][4]["exclude_reason"] == "complained"
    # 缺 2 人：champion 补 1、loyal 补 1，补足预算内人数
    assert {pick["member_id"] for pick in plan["backfill"]} == {11, 21}
    assert plan["sendable_after"] == 4
    assert plan["shortage"] == 0


def test_plan_review_retry_does_not_backfill_twice():
    members = _members([(1, "champion"), (2, "champion")])
    evaluations = {
        1: {"sendable": True, "blocked_sources": [], "reason": None},
        2: {"sendable": False, "blocked_sources": ["sms"], "reason": "sms_unsubscribed"},
    }
    first = compliance_rules.plan_review(
        members=members,
        evaluations=evaluations,
        segment_targets={"champion": 2},
        target_count=2,
        candidates={"champion": [11]},
    )
    assert [pick["member_id"] for pick in first["backfill"]] == [11]
    # 重试：补位会员已在册且可发 → 不再重复补位
    members_after = members + [{"member_id": 11, "segment_code": "champion", "source": "backfill"}]
    evaluations[11] = {"sendable": True, "blocked_sources": [], "reason": None}
    second = compliance_rules.plan_review(
        members=members_after,
        evaluations=evaluations,
        segment_targets={"champion": 2},
        target_count=2,
        candidates={"champion": []},
    )
    assert second["backfill"] == []
    assert second["sendable_after"] == 2


def test_plan_review_spillover_and_shortage():
    members = _members([(1, "champion"), (2, "champion"), (3, "loyal")])
    evaluations = {
        1: {"sendable": False, "blocked_sources": ["sms"], "reason": "sms_unsubscribed"},
        2: {"sendable": False, "blocked_sources": ["sms"], "reason": "sms_unsubscribed"},
        3: {"sendable": True, "blocked_sources": [], "reason": None},
    }
    # champion 群候选不足时跨群补位；全都不足则记 shortage
    plan = compliance_rules.plan_review(
        members=members,
        evaluations=evaluations,
        segment_targets={"champion": 2, "loyal": 1},
        target_count=3,
        candidates={"champion": [], "loyal": [21]},
    )
    assert [pick["member_id"] for pick in plan["backfill"]] == [21]
    assert plan["sendable_after"] == 2
    assert plan["shortage"] == 1


def test_channel_summary_and_snapshot_counts():
    rows = [
        {"blocked_sources": []},
        {"blocked_sources": ["sms"]},
        {"blocked_sources": ["all"]},
    ]
    summary = compliance_rules.channel_summary(rows)
    assert summary["sms"] == {"sendable": 1, "excluded": 2}
    assert summary["app"] == {"sendable": 2, "excluded": 1}
    assert summary["all"] == {"sendable": 1, "excluded": 2}

    members = [
        {"member_id": 1, "member_no": "MY001", "segment_code": "champion", "source": "base"},
        {"member_id": 2, "member_no": "MY002", "segment_code": "champion", "source": "backfill"},
        {"member_id": 3, "member_no": "MY003", "segment_code": "loyal", "source": "base"},
    ]
    decisions = {
        1: {"status": "sendable", "exclude_reason": None, "blocked_sources": []},
        2: {"status": "sendable", "exclude_reason": None, "blocked_sources": []},
        3: {"status": "excluded", "exclude_reason": "complained", "blocked_sources": ["all"]},
    }
    snapshot = compliance_rules.build_snapshot(
        members=members, decisions=decisions, send_channel="all", version_no=1
    )
    assert snapshot["counts"] == {"sendable": 2, "excluded": 1, "backfill": 1}
    assert [item["member_no"] for item in snapshot["sendable"]] == ["MY001", "MY002"]
    assert snapshot["excluded"][0]["exclude_reason"] == "complained"
    assert snapshot["excluded"][0]["blocked_channels"] == ["all"]
