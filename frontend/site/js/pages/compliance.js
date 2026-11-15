import { api } from '../api.js';
import { ENUMS, can } from '../config.js';
import {
  badge,
  emptyState,
  errorBox,
  errorMessage,
  esc,
  field,
  fmtNum,
  fmtTime,
  statStrip,
  table,
  toast,
} from '../ui.js';

const state = {
  audienceId: '',
  sendChannel: 'all',
  memberFilter: '',
  importResult: null,
};

function channelLabels(codes) {
  return (codes || []).map((code) => ENUMS.consentChannel[code] || code).join('、') || '—';
}

function summarySection(summary) {
  const blocked = Object.fromEntries((summary.channels || []).map((row) => [row.channel, row.blocked]));
  return `
    <section class="panel">
      <div class="panel-head">
        <h2>授权事件归并</h2>
        <span class="meta">短信退订 / APP 关闭营销通知 / 投诉名单，每天分批到达 · 按会员 × 渠道 × 事件时间归并</span>
      </div>
      ${statStrip([
        { label: '短信退订屏蔽', value: fmtNum(blocked.sms || 0), hint: 'channel = sms' },
        { label: 'APP 关闭屏蔽', value: fmtNum(blocked.app || 0), hint: 'channel = app' },
        { label: '投诉全渠道屏蔽', value: fmtNum(blocked.all || 0), hint: 'channel = all' },
        {
          label: '累计事件',
          value: fmtNum(summary.events_total || 0),
          hint: `迟到忽略 ${fmtNum(summary.late_ignored_total || 0)} 条 · 迟到/重复不恢复已退订`,
        },
      ])}
      <div class="panel-body" style="display:grid;gap:12px">
        <div class="note">
          最近批次：${(summary.batches || [])
            .map((item) => `${esc(item.batch_no)}（${fmtNum(item.events)} 条）`)
            .join(' · ') || '还没有批次'}
        </div>
        <div id="import-slot"></div>
      </div>
    </section>`;
}

function importForm(ctx) {
  if (!can(ctx.user.role, 'consent:write')) {
    return '<div class="note">当前角色只读：授权事件导入仅管理员可操作（后端强制 403）。</div>';
  }
  return `
    <div class="panel" style="box-shadow:none">
      <div class="panel-head" style="padding:10px 14px"><h2 style="font-size:1rem">导入授权事件批次</h2>
        <span class="meta">重复 event_id 幂等忽略</span></div>
      <div class="panel-body" style="display:grid;gap:10px">
        ${field({ label: '批次号', name: 'batch_no', value: `B${new Date().toISOString().slice(0, 10).replaceAll('-', '')}-1`, attrs: 'data-testid="batch-no"' })}
        <div class="field"><label for="events_raw">事件（每行一条：event_id,会员号,渠道,动作,事件时间）</label>
          <textarea id="events_raw" rows="4" style="width:100%;font-family:var(--font-mono);font-size:0.82rem" placeholder="SMS20261115-0001,MY00001,sms,unsubscribed,2026-11-15 09:30
APP20261115-0002,MY00002,app,opted_out,2026-11-15 09:31
CP20261115-0003,MY00003,all,complained,2026-11-15 10:05"></textarea>
        </div>
        <div class="rule-chipline">
          <button class="btn accent" id="import-events" data-testid="import-events">导入批次</button>
          <span class="note">渠道：sms / app / all；动作：unsubscribed / opted_out / complained / subscribed</span>
        </div>
        ${state.importResult ? `<div class="note" data-testid="import-result">${esc(state.importResult)}</div>` : ''}
      </div>
    </div>`;
}

function eventsTable(events) {
  return table({
    rows: events.items,
    columns: [
      { key: 'batch_no', label: '批次', className: 'mono' },
      { key: 'event_id', label: '事件ID', className: 'mono' },
      { key: 'member_no', label: '会员号', className: 'mono' },
      { key: 'channel_label', label: '渠道' },
      { key: 'action_label', label: '动作' },
      { key: 'event_time', label: '事件时间', render: (row) => fmtTime(row.event_time) },
      {
        key: 'disposition',
        label: '归并处置',
        render: (row) => badge(row.disposition === 'applied' ? 'ok' : 'warn', row.disposition_label),
      },
    ],
    empty: { title: '还没有授权事件', hint: '等每日批次到达，或由管理员导入一批。' },
  });
}

function channelCheckTable(compliance) {
  const rows = ['sms', 'app', 'all'].map((channel) => ({
    channel,
    ...compliance.channels[channel],
  }));
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>渠道</th><th>可发</th><th>剔除</th></tr></thead>
    <tbody>${rows
      .map(
        (row) => `<tr data-channel="${esc(row.channel)}">
          <td><b>${esc(row.channel_label)}</b></td>
          <td class="num">${fmtNum(row.sendable)}</td>
          <td class="num">${fmtNum(row.excluded)}</td>
        </tr>`,
      )
      .join('')}</tbody>
  </table></div>`;
}

function versionsTable(versions) {
  return table({
    rows: versions,
    columns: [
      { key: 'version_no', label: '版本', render: (row) => `<span class="mono">v${row.version_no}</span>` },
      {
        key: 'is_current',
        label: '状态',
        render: (row) => (row.is_current ? badge('ok', '当前生效') : badge('', '历史版本')),
      },
      { key: 'send_channel_label', label: '发送渠道' },
      { key: 'sendable_count', label: '可发', className: 'num', render: (row) => fmtNum(row.sendable_count) },
      { key: 'excluded_count', label: '剔除', className: 'num', render: (row) => fmtNum(row.excluded_count) },
      { key: 'backfill_count', label: '补位', className: 'num', render: (row) => fmtNum(row.backfill_count) },
      { key: 'operator_email', label: '发布人', className: 'mono' },
      { key: 'created_at', label: '发布时间', render: (row) => fmtTime(row.created_at) },
    ],
    empty: { title: '还没有发布版本', hint: '复核通过后点「发布可发名单」固定快照。' },
  });
}

function reviewsTable(reviews) {
  return table({
    rows: reviews,
    columns: [
      { key: 'round_no', label: '轮次', render: (row) => `<span class="mono">#${row.round_no}</span>` },
      { key: 'send_channel_label', label: '渠道口径' },
      { key: 'sendable_count', label: '可发', className: 'num', render: (row) => fmtNum(row.sendable_count) },
      { key: 'excluded_count', label: '剔除', className: 'num', render: (row) => fmtNum(row.excluded_count) },
      { key: 'backfill_count', label: '新增补位', className: 'num', render: (row) => fmtNum(row.backfill_count) },
      { key: 'shortage', label: '缺口', className: 'num', render: (row) => fmtNum(row.shortage) },
      { key: 'reimport', label: '重导', render: (row) => (row.reimport ? badge('info', '是') : '—') },
      { key: 'operator_email', label: '操作人', className: 'mono' },
      { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
    ],
    empty: { title: '还没有复核记录', hint: '点「执行复核」开始第一轮。' },
  });
}

function membersTable(members) {
  return table({
    rows: members.items,
    columns: [
      { key: 'member_no', label: '会员号', className: 'mono' },
      { key: 'name', label: '姓名' },
      { key: 'phone', label: '手机号', className: 'mono' },
      { key: 'segment_label', label: '分群' },
      {
        key: 'source',
        label: '来源',
        render: (row) => badge(row.source === 'backfill' ? 'info' : '', row.source_label),
      },
      {
        key: 'status',
        label: '复核结论',
        render: (row) => badge(row.status === 'sendable' ? 'ok' : 'danger', row.status_label),
      },
      { key: 'exclude_reason', label: '剔除原因', render: (row) => esc(row.exclude_reason_label || '—') },
      { key: 'blocked_channels', label: '屏蔽渠道', render: (row) => esc(channelLabels(row.blocked_channels)) },
      { key: 'round_no', label: '轮次', className: 'num' },
    ],
    empty: { title: '工作区还没有会员', hint: '执行一次复核（会自动从分群导入名单会员）。' },
  });
}

async function loadCompliance() {
  if (!state.audienceId) return null;
  const [compliance, members] = await Promise.all([
    api.get(`/api/audiences/${state.audienceId}/compliance`),
    api.get(`/api/audiences/${state.audienceId}/compliance/members`, memberQuery()),
  ]);
  return { compliance, members };
}

function memberQuery() {
  const query = { limit: 50 };
  if (state.memberFilter === 'sendable' || state.memberFilter === 'excluded') query.status = state.memberFilter;
  if (state.memberFilter === 'backfill') query.source = 'backfill';
  return query;
}

export const compliancePage = {
  async render(view, ctx) {
    let summary;
    let audiences;
    let events;
    try {
      [summary, audiences, events] = await Promise.all([
        api.get('/api/consents/summary'),
        api.get('/api/audiences', { limit: 50 }),
        api.get('/api/consents/events', { limit: 15 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => compliancePage.render(view, ctx));
      return;
    }
    if (!state.audienceId && audiences.items.length) state.audienceId = String(audiences.items[0].id);

    let detail = null;
    if (state.audienceId) {
      try {
        detail = await loadCompliance();
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    }
    const compliance = detail?.compliance;
    const members = detail?.members;
    const counts = compliance?.counts;
    const canReview = can(ctx.user.role, 'compliance:review');
    const canPublish = can(ctx.user.role, 'compliance:publish');
    const canExport = can(ctx.user.role, 'export:read');

    view.innerHTML = `
      ${summarySection(summary)}

      <section class="panel">
        <div class="panel-head">
          <h2>名单合规复核</h2>
          <span class="meta">未发布名单剔除不可发会员并补足预算内人数 · 重试不重复补位</span>
        </div>
        <div class="panel-body" style="display:grid;gap:12px">
          <div class="filter-bar">
            ${field({
              label: '名单',
              name: 'audience_id',
              value: state.audienceId,
              options: audiences.items.map((item) => ({
                value: item.id,
                label: `${item.audience_no} · ${item.campaign_name || ''}（${item.status_label}）`,
              })),
            })}
            ${field({
              label: '发送渠道',
              name: 'send_channel',
              value: state.sendChannel,
              options: [
                { value: 'all', label: '全渠道（短信 + APP 都要可发）' },
                { value: 'sms', label: '仅短信' },
                { value: 'app', label: '仅 APP 推送' },
              ],
            })}
            ${
              canReview
                ? `<button class="btn accent" id="review" data-testid="review">执行复核</button>
                   <button class="btn ghost" id="review-reimport" data-testid="review-reimport">从分群重导并复核</button>`
                : ''
            }
            ${
              canPublish
                ? '<button class="btn ghost" id="publish" data-testid="publish">发布可发名单</button>'
                : ''
            }
            ${
              canExport && compliance?.reviewed
                ? '<button class="btn sm ghost" id="export-compliance" data-testid="export-compliance">导出合规明细 CSV</button>'
                : ''
            }
          </div>
          ${
            compliance
              ? `<div class="note">
                  名单 ${esc(compliance.audience_no)}（${esc(compliance.audience_status_label)}）· 复核轮次 #${compliance.round_no}
                  · 渠道口径「${esc(ENUMS.consentChannel[compliance.send_channel] || compliance.send_channel)}」
                  ${compliance.current_version
                    ? `· 当前发布 v${compliance.current_version.version_no}（${esc(compliance.current_version.operator_email)}）—— 已排期活动按该版本下发，复核不影响在发版本`
                    : '· 尚未发布'}
                </div>`
              : ''
          }
        </div>
        ${
          compliance
            ? `${statStrip([
                { label: '可发人数', value: fmtNum(counts.sendable), hint: `预算内人数 ${fmtNum(compliance.target_count)}` },
                { label: '剔除人数', value: fmtNum(counts.excluded), hint: '退订 / 关闭通知 / 投诉' },
                { label: '补位人数', value: fmtNum(counts.backfill), hint: `其中可发 ${fmtNum(counts.backfill_sendable)}` },
                {
                  label: '补位缺口',
                  value: fmtNum(counts.shortage),
                  hint: counts.shortage ? '合规候选不足，未补足预算内人数' : '已补足预算内人数',
                },
              ])}
              <div class="panel-body" style="display:grid;gap:12px">
                <h3 style="margin:0;font-size:0.95rem">分渠道核对</h3>
                ${channelCheckTable(compliance)}
              </div>`
            : '<div class="panel-body"><div class="empty"><div class="mark">选择名单后开始复核</div><p>复核会剔除不可发会员并从分群补位。</p></div></div>'
        }
      </section>

      ${
        compliance?.reviewed
          ? `<section class="panel">
              <div class="panel-head">
                <h2>名单会员明细</h2>
                <span class="meta">${fmtNum(members?.total || 0)} 条在册记录 · 剔除保留记录不删除</span>
              </div>
              <div class="panel-body" style="display:grid;gap:10px">
                <div class="rule-chipline" id="member-filter">
                  ${[
                    ['', '全部'],
                    ['sendable', '可发'],
                    ['excluded', '已剔除'],
                    ['backfill', '补位'],
                  ]
                    .map(
                      ([value, label]) =>
                        `<button class="btn sm ${state.memberFilter === value ? 'accent' : 'ghost'}" data-filter="${value}">${label}</button>`,
                    )
                    .join('')}
                  <span class="note">展示前 50 条，完整明细请导出 CSV</span>
                </div>
                ${members ? membersTable(members) : ''}
              </div>
            </section>
            <section class="panel">
              <div class="panel-head"><h2>发布版本</h2><span class="meta">发布固定可发名单与剔除原因 · 失败保留旧版</span></div>
              ${versionsTable(compliance.versions)}
            </section>
            <section class="panel">
              <div class="panel-head"><h2>复核轮次</h2><span class="meta">每轮复核口径与计数留痕</span></div>
              ${reviewsTable(compliance.reviews)}
            </section>`
          : ''
      }

      <section class="panel">
        <div class="panel-head">
          <h2>事件流水</h2>
          <span class="meta">最近 ${fmtNum(events.items.length)} 条 / 共 ${fmtNum(events.total)} 条</span>
        </div>
        ${eventsTable(events)}
      </section>`;

    // ---- 事件绑定 ----
    view.querySelector('#import-slot').insertAdjacentHTML('beforeend', importForm(ctx));
    view.querySelector('#import-events')?.addEventListener('click', async () => {
      const batchNo = view.querySelector('[name=batch_no]').value.trim();
      const raw = view.querySelector('#events_raw').value.trim();
      if (!batchNo || !raw) {
        toast('请填批次号与事件内容', 'err');
        return;
      }
      const parsed = [];
      for (const [index, line] of raw.split('\n').entries()) {
        const parts = line.split(',').map((cell) => cell.trim());
        if (parts.length < 5 || parts.some((cell, i) => i < 5 && !cell)) {
          toast(`第 ${index + 1} 行格式不对：event_id,会员号,渠道,动作,事件时间`, 'err');
          return;
        }
        parsed.push({
          event_id: parts[0],
          member_no: parts[1],
          channel: parts[2],
          action: parts[3],
          event_time: parts[4].replace(' ', 'T'),
          source: parts[5] || undefined,
        });
      }
      try {
        const result = await api.post('/api/consents/events', { batch_no: batchNo, events: parsed });
        state.importResult = `批次 ${result.batch_no}：共 ${result.total} 条，生效 ${result.applied}，迟到忽略 ${result.late_ignored}，重复跳过 ${result.duplicates}，未知会员 ${result.unknown_members}`;
        toast('批次已导入并完成归并');
        await compliancePage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });

    view.querySelector('[name=audience_id]')?.addEventListener('change', (event) => {
      state.audienceId = event.target.value;
      compliancePage.render(view, ctx);
    });
    view.querySelector('[name=send_channel]')?.addEventListener('change', (event) => {
      state.sendChannel = event.target.value;
    });
    view.querySelector('#review')?.addEventListener('click', () => runReview(view, ctx, false));
    view.querySelector('#review-reimport')?.addEventListener('click', () => runReview(view, ctx, true));
    view.querySelector('#publish')?.addEventListener('click', async () => {
      if (!window.confirm('发布后当前复核结果将固定为新版本，旧版本转为历史。确认发布？')) return;
      try {
        const result = await api.post(`/api/audiences/${state.audienceId}/compliance/publish`, {});
        const version = result.current_version;
        toast(`已发布 v${version.version_no}：可发 ${version.sendable_count} 人，剔除 ${version.excluded_count} 人`);
        await compliancePage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelector('#export-compliance')?.addEventListener('click', async () => {
      try {
        await api.download(`/api/exports/compliance.csv?audience_id=${state.audienceId}`, `compliance-${state.audienceId}.csv`);
        toast('已开始下载合规明细');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelectorAll('#member-filter [data-filter]').forEach((button) => {
      button.addEventListener('click', () => {
        state.memberFilter = button.dataset.filter;
        compliancePage.render(view, ctx);
      });
    });
  },
};

async function runReview(view, ctx, reimport) {
  try {
    const result = await api.post(`/api/audiences/${state.audienceId}/compliance/review`, {
      send_channel: state.sendChannel,
      reimport,
    });
    toast(
      `复核完成（#${result.round_no}）：可发 ${result.counts.sendable}，剔除 ${result.counts.excluded}，补位 ${result.counts.backfill}`,
    );
    await compliancePage.render(view, ctx);
  } catch (err) {
    toast(errorMessage(err), 'err');
  }
}

export default compliancePage;
