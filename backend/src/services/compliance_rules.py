"""算法 C｜发送前合规复核（纯 Python，确定性，不依赖 ORM）。

口径（README §10 与「合规复核」页同步展示）：

1. 授权归并：按（会员 × 渠道）归并，事件时间最晚的事件决定当前状态。
   - 重复事件（event_id 相同）：幂等忽略，不改变状态。
   - 迟到事件（event_time 不晚于当前最新生效时间）：忽略 —— 迟到或重复事件不能恢复已退订。
   - 只有事件时间更晚的「恢复授权」才能把状态改回可发。
2. 渠道判定：投诉走全渠道（all）屏蔽；短信退订只屏蔽短信；APP 关闭只屏蔽 APP。
   发送渠道为 all 时，任一渠道被屏蔽即不可发。
3. 复核：未发布名单剔除不可发会员（只标记不删除），再按分群配额从合规候选中补位，
   补足预算内人数；候选不足时先同群补、再按分群优先级跨群补，仍不足记 shortage。
   补位选取是确定性的（候选顺序由调用方按综合分排好），重试不会重复补位。
4. 发布：把当前工作区固化为快照（可发名单 + 剔除原因），计数与明细同源。
"""
from __future__ import annotations

from src.constants.enums import (
    CONSENT_CHANNEL_ORDER,
    ConsentAction,
    ConsentChannel,
    ConsentStatus,
    EventDisposition,
    ExcludeReason,
    MemberSource,
    ReviewStatus,
)

# 动作 -> 授权状态
ACTION_TO_STATUS: dict[str, str] = {
    ConsentAction.UNSUBSCRIBED.value: ConsentStatus.BLOCKED.value,
    ConsentAction.OPTED_OUT.value: ConsentStatus.BLOCKED.value,
    ConsentAction.COMPLAINED.value: ConsentStatus.BLOCKED.value,
    ConsentAction.SUBSCRIBED.value: ConsentStatus.SENDABLE.value,
}

# 剔除主因优先级：投诉 > 多渠道 > 短信退订 > APP 关闭
_REASON_BY_ACTION: dict[str, str] = {
    ConsentAction.COMPLAINED.value: ExcludeReason.COMPLAINED.value,
    ConsentAction.UNSUBSCRIBED.value: ExcludeReason.SMS_UNSUBSCRIBED.value,
    ConsentAction.OPTED_OUT.value: ExcludeReason.APP_OPTED_OUT.value,
}


def merge_decision(
    *,
    current_event_id: str | None,
    current_event_time,
    event_id: str,
    action: str,
    event_time,
) -> tuple[str, str | None]:
    """单条事件归并到（会员 × 渠道）最新状态。

    返回 (disposition, new_status)：
    - duplicate：event_id 与当前生效事件一致（重复事件），忽略；
    - late_ignored：event_time 不晚于当前生效时间（迟到事件），忽略 —— 不能恢复已退订；
    - applied：生效，new_status 为归并后的授权状态。
    """
    if action not in ACTION_TO_STATUS:
        raise ValueError(f"unknown consent action: {action}")
    if current_event_id is not None and event_id == current_event_id:
        return EventDisposition.LATE_IGNORED.value, None
    if current_event_time is not None and event_time <= current_event_time:
        return EventDisposition.LATE_IGNORED.value, None
    return EventDisposition.APPLIED.value, ACTION_TO_STATUS[action]


def evaluate_member(consents: dict[str, tuple[str, str]], send_channel: str) -> dict:
    """按发送渠道判定单个会员是否可发。

    consents: {channel: (status, action)}，缺失渠道视为可发。
    返回 {sendable, blocked_sources, reason}：
    - blocked_sources：命中的屏蔽渠道（sms/app/all 的子集，all = 投诉全渠道屏蔽）；
    - reason：剔除主因（可发时为 None）。
    """
    blocked_sources: list[str] = []
    blocking_actions: list[str] = []
    for channel in CONSENT_CHANNEL_ORDER:
        status, action = consents.get(channel, (ConsentStatus.SENDABLE.value, None))
        if status == ConsentStatus.BLOCKED.value:
            blocked_sources.append(channel)
            if action:
                blocking_actions.append(action)

    if send_channel == ConsentChannel.SMS.value:
        relevant = {ConsentChannel.SMS.value, ConsentChannel.ALL.value}
    elif send_channel == ConsentChannel.APP.value:
        relevant = {ConsentChannel.APP.value, ConsentChannel.ALL.value}
    else:
        relevant = set(CONSENT_CHANNEL_ORDER)
    sendable = not any(source in relevant for source in blocked_sources)

    reason = None
    if not sendable:
        if ConsentAction.COMPLAINED.value in blocking_actions:
            reason = ExcludeReason.COMPLAINED.value
        elif len(set(blocking_actions)) > 1 or not blocking_actions:
            reason = ExcludeReason.MULTI_BLOCKED.value
        else:
            reason = _REASON_BY_ACTION.get(blocking_actions[0], ExcludeReason.MULTI_BLOCKED.value)
    return {"sendable": sendable, "blocked_sources": blocked_sources, "reason": reason}


def channel_ok(blocked_sources: list[str], channel: str) -> bool:
    """某个渠道维度上是否可发（用于分渠道核对统计）。"""
    if channel == ConsentChannel.SMS.value:
        return ConsentChannel.SMS.value not in blocked_sources and ConsentChannel.ALL.value not in blocked_sources
    if channel == ConsentChannel.APP.value:
        return ConsentChannel.APP.value not in blocked_sources and ConsentChannel.ALL.value not in blocked_sources
    return not blocked_sources


def plan_review(
    *,
    members: list[dict],
    evaluations: dict[int, dict],
    segment_targets: dict[str, int],
    target_count: int,
    candidates: dict[str, list[int]],
) -> dict:
    """复核计划：判定在册会员 + 计算补位选取（纯函数，不重试副作用）。

    members: 当前名单会员 [{member_id, segment_code, source}]（含历史补位）。
    evaluations: {member_id: evaluate_member 结果}（按发送渠道评估）。
    segment_targets: {segment_code: 目标人数}（来自算法 B 的 allocated）。
    target_count: 预算内总人数（名单生成时的人数口径）。
    candidates: {segment_code: [member_id, ...]} 各群可补位候选，
                调用方保证：合规、不在册、按优先级（综合分降序）排好。

    返回 {decisions, backfill, shortage, sendable_after, excluded_count, backfill_total}。
    decisions 只描述结论；落库由 service 层完成（剔除标记、补位插入）。
    """
    decisions: dict[int, dict] = {}
    sendable_by_segment: dict[str, int] = {code: 0 for code in segment_targets}
    sendable_total = 0
    excluded_count = 0
    for row in members:
        member_id = int(row["member_id"])
        evaluation = evaluations.get(member_id) or {"sendable": True, "blocked_sources": [], "reason": None}
        if evaluation["sendable"]:
            status = ReviewStatus.SENDABLE.value
            sendable_total += 1
            segment = row.get("segment_code") or ""
            if segment in sendable_by_segment:
                sendable_by_segment[segment] += 1
        else:
            status = ReviewStatus.EXCLUDED.value
            excluded_count += 1
        decisions[member_id] = {
            "status": status,
            "exclude_reason": evaluation.get("reason") if status == ReviewStatus.EXCLUDED.value else None,
            "blocked_sources": list(evaluation.get("blocked_sources") or []),
        }

    # 同群补位：各群缺额 = 目标 - 该群可发
    picked: list[dict] = []
    picked_ids: set[int] = set()
    leftover: dict[str, list[int]] = {}
    for segment, target in segment_targets.items():
        quota = max(0, min(int(target), target_count) - sendable_by_segment.get(segment, 0))
        pool = [mid for mid in candidates.get(segment, []) if mid not in picked_ids]
        take = pool[:quota]
        leftover[segment] = pool[quota:]
        for member_id in take:
            picked.append({"member_id": member_id, "segment_code": segment, "source": MemberSource.BACKFILL.value})
            picked_ids.add(member_id)

    # 跨群补位：同群候选不足时，按分群顺序用余量补足预算内总人数
    shortage = target_count - (sendable_total + len(picked))
    if shortage > 0:
        for segment in segment_targets:
            if shortage <= 0:
                break
            for member_id in leftover.get(segment, []):
                if shortage <= 0:
                    break
                if member_id in picked_ids:
                    continue
                picked.append({"member_id": member_id, "segment_code": segment, "source": MemberSource.BACKFILL.value})
                picked_ids.add(member_id)
                shortage -= 1
    shortage = max(0, shortage)

    return {
        "decisions": decisions,
        "backfill": picked,
        "shortage": shortage,
        "sendable_after": sendable_total + len(picked),
        "excluded_count": excluded_count,
        "backfill_total": len(picked),
    }


def channel_summary(rows: list[dict]) -> dict[str, dict]:
    """分渠道核对：短信 / APP / 全渠道各自的可发与剔除人数。

    rows: [{blocked_sources: [...]}]（在册会员的屏蔽渠道命中情况）。
    """
    summary: dict[str, dict] = {}
    for channel in CONSENT_CHANNEL_ORDER:
        sendable = sum(1 for row in rows if channel_ok(list(row.get("blocked_sources") or []), channel))
        summary[channel] = {"sendable": sendable, "excluded": len(rows) - sendable}
    return summary


def build_snapshot(
    *,
    members: list[dict],
    decisions: dict[int, dict],
    send_channel: str,
    version_no: int,
) -> dict:
    """发布快照：固定可发名单与剔除原因（落库后不再变化）。"""
    sendable: list[dict] = []
    excluded: list[dict] = []
    backfill_count = 0
    for row in members:
        member_id = int(row["member_id"])
        decision = decisions.get(member_id) or {}
        entry = {
            "member_no": row.get("member_no"),
            "segment_code": row.get("segment_code"),
            "source": row.get("source", MemberSource.BASE.value),
        }
        if decision.get("status") == ReviewStatus.EXCLUDED.value:
            entry["exclude_reason"] = decision.get("exclude_reason")
            entry["blocked_channels"] = list(decision.get("blocked_sources") or [])
            excluded.append(entry)
        else:
            sendable.append(entry)
            if entry["source"] == MemberSource.BACKFILL.value:
                backfill_count += 1
    return {
        "version_no": version_no,
        "send_channel": send_channel,
        "sendable": sendable,
        "excluded": excluded,
        "counts": {
            "sendable": len(sendable),
            "excluded": len(excluded),
            "backfill": backfill_count,
        },
    }
