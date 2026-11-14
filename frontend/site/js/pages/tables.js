import { api } from '../api.js';
import { can, compatTone, ENUMS, reviewTone, versionTone } from '../config.js';
import {
  bindFilterBar,
  dataTable,
  drawer,
  errorBox,
  errorMessage,
  esc,
  field,
  filterBar,
  fmtTime,
  fmtTimeShort,
  kv,
  toast,
} from '../ui.js';

const state = { q: '', enabled: '' };

function schemaTable(columns) {
  if (!columns || !columns.length) return '<p class="muted">这张表还没有列定义。</p>';
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>列</th><th>类型</th><th>可空</th><th>默认</th></tr></thead>
    <tbody>${columns
      .map(
        (col) => `<tr>
          <td class="mono">${esc(col.name)}</td>
          <td class="mono">${esc(col.type)}</td>
          <td>${col.nullable === false ? '<span class="badge danger">NOT NULL</span>' : '<span class="badge info">NULL</span>'}</td>
          <td class="mono">${esc(col.default || '—')}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function affectedTable(version) {
  const rows = version.affected_tables || [];
  if (!rows.length) return '<p class="muted">该版本没有逐表变更。</p>';
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>表</th><th>变更字段</th><th>兼容性</th><th>摘要</th></tr></thead>
    <tbody>${rows
      .map(
        (row) => `<tr>
          <td class="mono">${esc(row.code)}</td>
          <td class="mono">${esc((row.fields || []).join('、') || '—')}</td>
          <td><span class="badge ${compatTone(row.compat)}">${esc(ENUMS.compat[row.compat] || row.compat)}</span></td>
          <td>${esc(row.summary || '—')}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function snapshotTable(snapshot) {
  if (!snapshot || !snapshot.length) return '<p class="muted">版本快照为空。</p>';
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>表</th><th>采集</th><th>水位(s)</th><th>列数</th></tr></thead>
    <tbody>${snapshot
      .map(
        (item) => `<tr>
          <td class="mono">${esc(item.code)}</td>
          <td>${item.enabled ? '<span class="badge ok">采集中</span>' : '<span class="badge">已暂停</span>'}</td>
          <td class="num">${esc(item.allowed_lateness_seconds)}</td>
          <td class="num">${esc((item.schema_columns || []).length)}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

/** 顶部订阅治理条：当前版本 / 待生效变更 / 操作入口。 */
function governancePanel(view, ctx, gov) {
  const editable = can(ctx.user.role, 'subscription:write');
  const current = gov.current;
  const pending = gov.pending;
  const currentCard = current
    ? `<div class="gov-card">
        <div class="eyebrow">当前基线版本（运行中的批次钉住这一版）</div>
        <div class="gov-main">
          <span class="badge ${versionTone(current.status)}">${esc(current.version_label)} · ${esc(current.status_label)}</span>
          <b class="mono">${esc(current.review_fingerprint ? current.review_fingerprint.slice(0, 10) : '—')}</b>
        </div>
        <div class="tiny muted">启用时间 ${fmtTimeShort(current.activated_at)} · 快照 ${current.subscription_snapshot.length} 张表</div>
      </div>`
    : '<div class="gov-card"><div class="eyebrow">当前基线版本</div><p class="muted">尚无基线，首次订阅调整时自动建立。</p></div>';

  const pendingCard = pending
    ? `<div class="gov-card alert-border">
        <div class="eyebrow">待生效版本（变更尚未影响在途批次）</div>
        <div class="gov-main">
          <span class="badge ${versionTone(pending.status)}">${esc(pending.version_label)} · ${esc(pending.status_label)}</span>
          <span class="badge ${reviewTone(pending.review_status)}">${esc(pending.review_status_label)}</span>
          <span class="badge ${compatTone(pending.compat)}">${esc(ENUMS.compat[pending.compat] || pending.compat)}</span>
          ${pending.baseline_stale ? '<span class="badge danger">基线已漂移 · 需重算</span>' : ''}
        </div>
        <div class="tiny muted">
          基于 ${esc(pending.base_version_label || '—')} · 影响 ${pending.affected_count} 张表
          ${pending.activation_error ? ` · <span class="danger-text">${esc(pending.activation_error)}</span>` : ''}
        </div>
        ${
          editable
            ? `<div class="gov-actions">
                <button class="btn sm ghost" data-recompute="${pending.id}">按当前基线重算</button>
                <button class="btn sm accent" data-activate="${pending.id}">启用版本（全量切换）</button>
                <button class="btn sm ghost" data-open-version="${pending.id}">查看版本详情</button>
              </div>`
            : '<div class="tiny muted">只读角色仅可查看待生效版本</div>'
        }
      </div>`
    : `<div class="gov-card">
        <div class="eyebrow">待生效版本</div>
        <p class="muted">没有待生效变更。修改水位参数 / 暂停订阅 / 调整列结构后会在这里生成待生效版本。</p>
      </div>`;

  return `<section class="panel" data-testid="subscription-governance">
    <div class="panel-head">
      <h2>订阅版本治理</h2>
      <div class="head-actions">
        <span class="meta">共 ${esc(gov.version_total)} 个版本</span>
        ${editable ? '<button class="btn sm ghost" id="list-versions">版本历史</button>' : ''}
      </div>
    </div>
    <div class="panel-body gov-grid">${currentCard}${pendingCard}</div>
  </section>`;
}

async function openVersion(view, ctx, versionId) {
  let version;
  try {
    version = await api.get(`/api/subscription-versions/${versionId}`);
  } catch (err) {
    toast(errorMessage(err), 'err');
    return;
  }
  const editable = can(ctx.user.role, 'subscription:write');
  const { root, close } = drawer({
    wide: true,
    title: `订阅版本 ${version.version_label}`,
    subtitle: `${version.status_label} · 基于 ${version.base_version_label || '—'} · ${version.review_status_label}`,
    body: `
      ${kv([
        ['版本状态', `<span class="badge ${versionTone(version.status)}">${esc(version.status_label)}</span>`],
        ['复核状态', `<span class="badge ${reviewTone(version.review_status)}">${esc(version.review_status_label)}</span>`],
        ['兼容性结论', `<span class="badge ${compatTone(version.compat)}">${esc(ENUMS.compat[version.compat] || version.compat)}</span>`],
        ['基线漂移', version.baseline_stale ? '<span class="badge danger">是，需按当前基线重算</span>' : '否'],
        ['复核人 / 备注', `${esc(version.reviewer || '—')} · ${esc(version.review_note || '—')}`],
        ['版本指纹', `<span class="mono small">${esc(version.review_fingerprint || '—')}</span>`],
        ['启用时间', fmtTime(version.activated_at)],
      ])}
      <h3 class="drawer-h3">逐表变更（连续变更已合并检查）</h3>
      ${affectedTable(version)}
      <h3 class="drawer-h3">版本快照（批次实际使用的订阅范围）</h3>
      ${snapshotTable(version.subscription_snapshot)}
      <h3 class="drawer-h3">变更与复核留痕</h3>
      <ul class="finding-list">${(version.change_log || [])
        .map((item) => `<li><span class="mono muted">${esc(fmtTimeShort(item.at))}</span><span>${esc(item.actor || '—')}</span><span class="mono">${esc(item.event || [].concat(item.fields || []).join(','))}</span><span>${esc(item.compat ? ENUMS.compat[item.compat] || item.compat : '')}</span></li>`)
        .join('') || '<p class="muted">无留痕</p>'}</ul>`,
    footer:
      editable && version.status === 'draft'
        ? `<form id="review-form" class="inline-form">
            ${field({
              label: '复核结论（先按当前基线重算，不沿用历史审批）',
              name: 'decision',
              value: version.review_status === 'blocked' ? 'blocked' : version.review_status === 'approved' ? 'approved' : 'pending_review',
              options: [
                { value: 'pending_review', label: '仅重算，停在待复核' },
                { value: 'approved', label: '复核通过（仅全量向前兼容可放行）' },
                { value: 'blocked', label: '拦截该版本' },
              ],
            })}
            ${field({ label: '复核备注', name: 'note', value: version.review_note || '', placeholder: '写一句放行/拦截依据' })}
            <button class="btn accent" type="submit">提交复核</button>
            <button class="btn ghost" type="button" data-recompute="${version.id}">重算门禁（重试不新增审计）</button>
            <button class="btn ghost" type="button" data-activate="${version.id}">启用版本（全量切换）</button>
          </form>`
        : '<span class="mono muted">只读角色或非待生效版本不可复核</span>',
  });

  const refresh = async () => {
    close();
    await tablesPage.render(view, ctx);
  };
  root.querySelector('#review-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      const result = await api.patch(`/api/subscription-versions/${version.id}/review`, {
        decision: form.decision.value,
        note: form.note.value.trim() || null,
      });
      toast(`复核已保存：${result.review_status_label}`);
      await refresh();
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
  root.querySelector('[data-recompute]')?.addEventListener('click', async () => {
    try {
      const result = await api.post(`/api/subscription-versions/${version.id}/reevaluate`);
      toast(`重算完成：${ENUMS.compat[result.compat] || result.compat} / ${result.review_status_label}`);
      await refresh();
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
  root.querySelector('[data-activate]')?.addEventListener('click', async () => activateVersion(view, ctx, version.id));
}

async function activateVersion(view, ctx, versionId) {
  if (!window.confirm('全量切换将同时生效采集范围、全库合并与门禁审计；失败会自动回滚至上一版。确认启用？')) return;
  try {
    const result = await api.post(`/api/subscription-versions/${versionId}/activate`, { note: null });
    toast(`版本 ${result.version_label} 已全量切换生效`);
    await tablesPage.render(view, ctx);
  } catch (err) {
    toast(errorMessage(err), 'err');
  }
}

async function openVersionHistory(view, ctx) {
  let data;
  try {
    data = await api.get('/api/subscription-versions', { limit: 50 });
  } catch (err) {
    toast(errorMessage(err), 'err');
    return;
  }
  const { root, close } = drawer({
    wide: true,
    title: '订阅版本历史',
    subtitle: '待生效 → 当前基线 → 已被取代；批次按启动时钉住的版本号可核对',
    body: dataTable({
      rows: data.items,
      testId: 'version-history',
      empty: { title: '还没有订阅版本', hint: '首次订阅调整会自动建立 v1 基线。' },
      columns: [
        { key: 'version_label', label: '版本', className: 'mono' },
        { key: 'status', label: '状态', render: (row) => `<span class="badge ${versionTone(row.status)}">${esc(row.status_label)}</span>` },
        { key: 'base_version_label', label: '基线', className: 'mono' },
        { key: 'compat', label: '兼容性', render: (row) => `<span class="badge ${compatTone(row.compat)}">${esc(ENUMS.compat[row.compat] || row.compat)}</span>` },
        { key: 'review_status', label: '复核', render: (row) => `<span class="badge ${reviewTone(row.review_status)}">${esc(row.review_status_label)}</span>` },
        { key: 'affected_count', label: '影响表', className: 'num' },
        { key: 'activated_at', label: '启用时间', render: (row) => fmtTime(row.activated_at) },
      ],
    }),
    footer: '<span class="mono muted">点击行查看版本快照与逐表变更</span>',
  });
  root.querySelectorAll('table.data tbody tr').forEach((row) => {
    row.addEventListener('click', () => {
      const item = data.items.find((entry) => String(entry.id) === row.dataset.id);
      if (item) {
        close();
        openVersion(view, ctx, item.id);
      }
    });
  });
}

function openTable(view, ctx, table) {
  const editable = can(ctx.user.role, 'table:write');
  const { root, close } = drawer({
    wide: true,
    title: table.name,
    subtitle: `${table.code} · ${table.database_name}.${table.table_name} · PK ${table.pk_column}`,
    body: `${kv([
      ['源表', `<span class="mono">${esc(table.database_name)}.${esc(table.table_name)}</span>`],
      ['当前基线', `<span class="badge ok">${esc(table.active_version_label || '—')}</span>`],
      ['主键列', `<span class="mono">${esc(table.pk_column)}</span>`],
      ['binlog 位点', `<span class="mono">${esc(table.binlog_file || '—')} : ${esc(table.binlog_position)}</span>`],
      ['允许迟到', `${esc(table.allowed_lateness_seconds)} 秒（当前基线值，修改走待生效版本）`],
      ['状态', table.enabled ? '<span class="badge ok">订阅中</span>' : '<span class="badge">已暂停</span>'],
      ['事件 / 迟到', `${esc(table.event_count)} / ${esc(table.late_count)}`],
      ['最近事件', fmtTime(table.last_event_at)],
      ['负责人', esc(table.owner)],
      ['备注', esc(table.note || '—')],
    ])}
    <h3 class="drawer-h3">列定义（${esc(table.column_count)} 列 · 当前基线）</h3>
    ${schemaTable(table.schema_columns)}
    <p class="muted tiny">订阅调整不再直接改表：保存后生成/合并「待生效版本」，运行中的批次继续用启动时版本，启用后才全量生效。</p>`,
    footer: editable
      ? `<form id="table-edit" class="inline-form">
          ${field({ label: '允许迟到（秒，写入待生效版本）', name: 'lateness', type: 'number', value: table.allowed_lateness_seconds, attrs: 'min="0" max="3600"' })}
          <button class="btn accent" type="submit">提交为待生效变更</button>
          <button class="btn ghost" type="button" data-toggle>${table.enabled ? '暂停订阅（待生效）' : '恢复订阅（待生效）'}</button>
        </form>`
      : '<span class="mono muted">只读角色仅可查看</span>',
  });
  root.querySelector('#table-edit')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api.patch(`/api/tables/${table.id}`, { allowed_lateness_seconds: Number(event.target.lateness.value) });
      toast('已写入待生效版本，启用后才会影响新批次');
      close();
      await tablesPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
  root.querySelector('[data-toggle]')?.addEventListener('click', async () => {
    try {
      await api.patch(`/api/tables/${table.id}`, { enabled: !table.enabled });
      toast(table.enabled ? '暂停订阅已进入待生效版本' : '恢复订阅已进入待生效版本');
      close();
      await tablesPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

function openCreate(view, ctx) {
  const { root, close } = drawer({
    title: '登记源表订阅',
    subtitle: '登记直接进入当前基线版本；编码重复会返回 409',
    body: `<form id="table-form" class="form-grid">
      ${field({ label: '编码（大写字母/数字/下划线）', name: 'code', placeholder: 'T_CLAIM_DETAIL' })}
      ${field({ label: '显示名', name: 'name', placeholder: '例如：理赔明细表' })}
      ${field({ label: '库名', name: 'database_name', value: 'biz_prod' })}
      ${field({ label: '表名', name: 'table_name', placeholder: 't_claim_detail' })}
      ${field({ label: '主键列', name: 'pk_column', value: 'id' })}
      ${field({ label: '允许迟到（秒）', name: 'allowed_lateness_seconds', type: 'number', value: '30', attrs: 'min="0" max="3600"' })}
      ${field({ label: 'binlog 文件', name: 'binlog_file', placeholder: 'mysql-bin.000418' })}
      <label class="field"><span>列定义（JSON 数组）</span>
        <textarea name="schema_columns" rows="6" class="mono">[{"name":"id","type":"bigint","nullable":false},{"name":"status","type":"varchar(16)","nullable":true}]</textarea>
      </label>
      <div class="form-error" id="table-error"></div>
      <button class="btn accent" type="submit">保存订阅</button>
    </form>`,
  });
  root.querySelector('#table-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    let columns;
    try {
      columns = JSON.parse(form.schema_columns.value || '[]');
    } catch {
      root.querySelector('#table-error').textContent = '列定义不是合法 JSON';
      return;
    }
    try {
      const created = await api.post('/api/tables', {
        code: form.code.value.trim(),
        name: form.name.value.trim(),
        database_name: form.database_name.value.trim() || 'biz_prod',
        table_name: form.table_name.value.trim(),
        pk_column: form.pk_column.value.trim() || 'id',
        allowed_lateness_seconds: Number(form.allowed_lateness_seconds.value || 30),
        binlog_file: form.binlog_file.value.trim() || null,
        schema_columns: columns,
      });
      toast(`已登记 ${created.code}，并入当前基线 ${created.active_version_label || ''}`);
      close();
      await tablesPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#table-error').textContent =
        err.code === 'CONFLICT' ? '编码已存在（409），换一个再提交' : errorMessage(err);
    }
  });
}

export const tablesPage = {
  async render(view, ctx) {
    let data;
    let gov;
    try {
      [data, gov] = await Promise.all([
        api.get('/api/tables', { limit: 50, q: state.q, enabled: state.enabled }),
        api.get('/api/subscription-versions/overview'),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => tablesPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      ${governancePanel(view, ctx, gov)}
      <section class="panel">
        <div class="panel-head">
          <h2>源表与订阅</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 张 · 当前基线 ${esc(gov.current ? gov.current.version_label : '—')}</span>
            ${can(ctx.user.role, 'table:write') ? '<button class="btn sm accent" id="new-table">登记源表</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${filterBar({
            id: 'table-filters',
            fields: [
              { label: '搜索', name: 'q', value: state.q, placeholder: '表名 / 编码 / 表' },
              {
                label: '订阅状态',
                name: 'enabled',
                value: state.enabled,
                options: [
                  { value: '', label: '全部' },
                  { value: 'true', label: '订阅中' },
                  { value: 'false', label: '已暂停' },
                ],
              },
            ],
          })}
        </div>
        ${dataTable({
          rows: data.items,
          testId: 'table-list',
          empty: { title: '还没有源表', hint: '管理员可以登记第一张被订阅的 MySQL 表。' },
          columns: [
            { key: 'code', label: '编码', className: 'mono' },
            { key: 'name', label: '源表', render: (row) => `<b>${esc(row.name)}</b><div class="mono muted">${esc(row.database_name)}.${esc(row.table_name)}</div>` },
            { key: 'active_version_label', label: '基线版本', className: 'mono', render: (row) => esc(row.active_version_label || '—') },
            { key: 'pk_column', label: '主键', className: 'mono' },
            { key: 'allowed_lateness_seconds', label: '允许迟到', className: 'num', render: (row) => `${row.allowed_lateness_seconds}s` },
            { key: 'event_count', label: '事件', className: 'num' },
            { key: 'late_count', label: '迟到', className: 'num', render: (row) => (row.late_count ? `<span class="warn-text mono">${row.late_count}</span>` : '0') },
            { key: 'last_event_at', label: '最近事件', render: (row) => fmtTime(row.last_event_at) },
            { key: 'enabled', label: '状态', render: (row) => (row.enabled ? '<span class="badge ok">订阅中</span>' : '<span class="badge">已暂停</span>') },
          ],
        })}
      </section>`;

    bindFilterBar(view, 'table-filters', (values) => {
      state.q = values.q.trim();
      state.enabled = values.enabled;
      tablesPage.render(view, ctx);
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const item = data.items.find((entry) => String(entry.id) === row.dataset.id);
        if (item) openTable(view, ctx, item);
      });
    });
    view.querySelector('#new-table')?.addEventListener('click', () => openCreate(view, ctx));
    view.querySelector('#list-versions')?.addEventListener('click', () => openVersionHistory(view, ctx));
    view.querySelectorAll('[data-open-version]').forEach((btn) =>
      btn.addEventListener('click', () => openVersion(view, ctx, Number(btn.dataset.openVersion))),
    );
    view.querySelectorAll('[data-recompute]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        try {
          const result = await api.post(`/api/subscription-versions/${btn.dataset.recompute}/reevaluate`);
          toast(`重算完成：${result.review_status_label}`);
          await tablesPage.render(view, ctx);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      }),
    );
    view.querySelectorAll('[data-activate]').forEach((btn) =>
      btn.addEventListener('click', () => activateVersion(view, ctx, Number(btn.dataset.activate))),
    );
  },
};

export default tablesPage;
