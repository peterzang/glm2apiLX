/* GLM2API Admin Panel - frontend logic (vanilla JS, no deps) */
'use strict';

const TOKEN_KEY = 'glm2api_admin_token';
const API_BASE = '/admin/api';

// =========================================================================
// Utilities
// =========================================================================

function getToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); }

function showToast(msg, kind = 'info') {
  const el = document.getElementById('toast');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add('hidden'), 3500);
}

async function api(name, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers['X-Admin-Token'] = token;
  const resp = await fetch(`${API_BASE}/${name}`, {
    method: opts.method || 'GET',
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (resp.status === 401) {
    clearToken();
    showLogin();
    throw new Error('unauthorized');
  }
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    const msg = (data && data.error) || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', { hour12: false });
}
function fmtTimeShort(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}
function fmtDuration(sec) {
  if (sec == null) return '-';
  if (sec < 60) return `${Math.floor(sec)}秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)}分${Math.floor(sec % 60)}秒`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}小时${Math.floor((sec % 3600) / 60)}分`;
  return `${Math.floor(sec / 86400)}天${Math.floor((sec % 86400) / 3600)}小时`;
}
function fmtBytes(n) {
  if (n == null) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(1)} ${units[i]}`;
}
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function shortHash(s, n = 8) {
  if (!s) return '-';
  return s.length <= n ? s : s.slice(0, n);
}

// =========================================================================
// Login
// =========================================================================

function showLogin() {
  document.getElementById('login-screen').classList.remove('hidden');
  document.getElementById('app').classList.add('hidden');
}

function showApp() {
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  refresh();
}

async function handleLoginSubmit(e) {
  e.preventDefault();
  const pw = document.getElementById('login-password').value;
  const errEl = document.getElementById('login-error');
  errEl.hidden = true;
  try {
    const data = await api('login', { method: 'POST', body: { password: pw } });
    setToken(data.token);
    showApp();
  } catch (err) {
    errEl.textContent = '登录失败：' + err.message;
    errEl.hidden = false;
  }
}

async function handleLogout() {
  try { await api('logout', { method: 'POST' }); } catch (_) {}
  clearToken();
  showLogin();
}

// =========================================================================
// Navigation
// =========================================================================

let currentView = 'dashboard';
const VIEW_TITLES = {
  dashboard: '仪表盘',
  accounts: '账号管理',
  models: '模型',
  probe: '端点测试',
  logs: '请求日志',
  rotates: '轮换事件',
  config: '配置查看',
  system: '系统监控',
};

function switchView(name) {
  currentView = name;
  document.querySelectorAll('.nav-item').forEach(a => {
    a.classList.toggle('active', a.dataset.view === name);
  });
  document.querySelectorAll('.view').forEach(s => {
    s.classList.toggle('active', s.id === `view-${name}`);
  });
  document.getElementById('topbar-title').textContent = VIEW_TITLES[name] || name;
  refresh();
}

// =========================================================================
// Refresh loop
// =========================================================================

let refreshTimer = null;
function startAutoRefresh() {
  stopAutoRefresh();
  if (document.getElementById('auto-refresh').checked) {
    refreshTimer = setInterval(() => refresh(), 5000);
  }
}
function stopAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
}

async function refresh() {
  if (!getToken()) return;
  try {
    switch (currentView) {
      case 'dashboard': await refreshDashboard(); break;
      case 'accounts': await refreshAccounts(); break;
      case 'models': await refreshModels(); break;
      case 'probe': await refreshProbe(); break;
      case 'logs': await refreshLogs(); break;
      case 'rotates': await refreshRotates(); break;
      case 'config': await refreshConfig(); break;
      case 'system': await refreshSystem(); break;
    }
  } catch (err) {
    if (err.message !== 'unauthorized') {
      console.error('refresh failed', err);
    }
  }
}

// =========================================================================
// Dashboard view
// =========================================================================

async function refreshDashboard() {
  const d = await api('dashboard');

  const all = d.all_time;
  const r5 = d.recent_5m;
  const successRateColor = r5.success_rate >= 95 ? 'success'
                         : r5.success_rate >= 80 ? 'warning' : 'error';

  const html = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">总请求数</div>
        <div class="kpi-value">${all.total.toLocaleString()}</div>
        <div class="kpi-sub">成功 ${all.success} · 4xx ${all.client_errors} · 5xx ${all.server_errors}</div>
      </div>
      <div class="kpi-card ${successRateColor}">
        <div class="kpi-label">5分钟成功率</div>
        <div class="kpi-value">${r5.success_rate.toFixed(1)}%</div>
        <div class="kpi-sub">${r5.success}/${r5.total} 请求</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">P95 延迟</div>
        <div class="kpi-value">${r5.p95_ms}ms</div>
        <div class="kpi-sub">P50 ${r5.p50_ms}ms · P99 ${r5.p99_ms}ms</div>
      </div>
      <div class="kpi-card success">
        <div class="kpi-label">全局成功率</div>
        <div class="kpi-value">${all.success_rate.toFixed(1)}%</div>
        <div class="kpi-sub">历史累计</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">运行时长</div>
        <div class="kpi-value" style="font-size:18px;">${fmtDuration(d.uptime_seconds)}</div>
        <div class="kpi-sub">自 ${fmtTime(d.now - d.uptime_seconds)}</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">近 48 小时请求量</div>
        <div class="panel-meta">绿=成功 · 红=5xx错误</div>
      </div>
      <div class="panel-body">
        ${renderHourlyBar(d.hourly)}
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
      <div class="panel">
        <div class="panel-header"><div class="panel-title">模型使用分布 (Top 8)</div></div>
        <div class="panel-body">${renderModelList(d.top_models)}</div>
      </div>
      <div class="panel">
        <div class="panel-header"><div class="panel-title">协议分布</div></div>
        <div class="panel-body">${renderProtocolList(d.protocols)}</div>
      </div>
    </div>
  `;
  document.getElementById('view-dashboard').innerHTML = html;
}

function renderHourlyBar(hourly) {
  if (!hourly || hourly.length === 0) {
    return `<div class="empty-state">暂无数据</div>`;
  }
  const maxTotal = Math.max(1, ...hourly.map(h => h.total));
  const bars = hourly.map(h => {
    const totalH = (h.total / maxTotal) * 100;
    const errH = h.error > 0 ? (h.error / maxTotal) * 100 : 0;
    const okH = totalH - errH;
    const time = fmtTimeShort(h.hour + 60 * 30); // mid-of-hour
    return `
      <div class="bar-col">
        <div class="bar-tooltip">${time} · 总${h.total} · 成功${h.success} · 错误${h.error}</div>
        <div class="bar-stack error" style="height:${errH}%;"></div>
        <div class="bar-stack" style="height:${okH}%;"></div>
        <div class="bar-label">${time.slice(0, 5)}</div>
      </div>
    `;
  }).join('');
  return `<div class="bar-chart">${bars}</div>`;
}

function renderModelList(models) {
  if (!models || models.length === 0) return `<div class="empty-state">暂无数据</div>`;
  const max = Math.max(1, ...models.map(m => m.count));
  return models.map(m => {
    const pct = (m.count / max) * 100;
    return `
      <div style="margin-bottom:10px;">
        <div class="flex-between mb-1">
          <span class="mono" style="font-size:12px;">${escapeHtml(m.model)}</span>
          <span class="text-muted" style="font-size:12px;">${m.count}</span>
        </div>
        <div style="height:6px;background:var(--bg-elevated);border-radius:3px;overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:linear-gradient(90deg,var(--primary),var(--purple));"></div>
        </div>
      </div>
    `;
  }).join('');
}

function renderProtocolList(protocols) {
  const entries = Object.entries(protocols || {}).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return `<div class="empty-state">暂无数据</div>`;
  const total = entries.reduce((s, [, c]) => s + c, 0);
  const colors = {
    'openai-chat': 'badge-success',
    'openai-responses': 'badge-info',
    'anthropic': 'badge-purple',
    'openai-legacy': 'badge-muted',
    'openai-embeddings': 'badge-info',
    'openai-moderations': 'badge-muted',
    'openai-images': 'badge-warning',
    'meta': 'badge-muted',
    'other': 'badge-muted',
  };
  return entries.map(([proto, count]) => {
    const pct = ((count / total) * 100).toFixed(1);
    const cls = colors[proto] || 'badge-muted';
    return `
      <div class="flex-between" style="padding:6px 0;border-bottom:1px solid var(--border);">
        <span><span class="badge ${cls}">${escapeHtml(proto)}</span></span>
        <span class="mono">${count} · ${pct}%</span>
      </div>
    `;
  }).join('');
}

// =========================================================================
// Accounts view
// =========================================================================

async function refreshAccounts() {
  const data = await api('accounts');
  const accs = data.accounts || [];
  let html = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">账号总数</div>
        <div class="kpi-value">${accs.length}</div>
        <div class="kpi-sub">最大并发 ${data.max_concurrency}</div>
      </div>
      <div class="kpi-card ${data.queue_ahead > 0 ? 'warning' : 'success'}">
        <div class="kpi-label">队列等待</div>
        <div class="kpi-value">${data.queue_ahead}</div>
        <div class="kpi-sub">超时 ${data.queue_wait_timeout}s</div>
      </div>
      <div class="kpi-card success">
        <div class="kpi-label">游客账号</div>
        <div class="kpi-value">${accs.filter(a => a.is_guest).length}</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">游客账号 (非游客)</div>
        <div class="kpi-value">${accs.filter(a => !a.is_guest).length}</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">账号详情</div>
        <div class="panel-meta">点击"轮换"按钮可手动重置 device_id</div>
      </div>
      <div class="panel-body">
        <div class="table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>类型</th>
                <th>device_id</th>
                <th>请求数</th>
                <th>累计</th>
                <th>预取</th>
                <th>Token</th>
                <th>最后使用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              ${accs.map(a => renderAccountRow(a)).join('') || `<tr><td colspan="9" class="empty-state">无账号</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
  document.getElementById('view-accounts').innerHTML = html;
  // Bind rotate buttons
  document.querySelectorAll('[data-rotate-idx]').forEach(btn => {
    btn.addEventListener('click', () => handleRotate(parseInt(btn.dataset.rotateIdx, 10)));
  });
}

function renderAccountRow(a) {
  const typeBadge = a.is_guest
    ? `<span class="badge badge-info">游客</span>`
    : `<span class="badge badge-purple">Refresh</span>`;
  const prefetchBadge = a.prefetch_in_progress
    ? `<span class="badge badge-warning">进行中</span>`
    : a.has_prefetched_token
      ? `<span class="badge badge-success">已就绪</span>`
      : `<span class="badge badge-muted">空</span>`;
  const tokenExpire = a.cached_token_expires_in < 0
    ? `<span class="text-muted">无缓存</span>`
    : a.cached_token_expires_in < 300
      ? `<span class="text-warning">${Math.floor(a.cached_token_expires_in)}s</span>`
      : `<span class="text-success">${Math.floor(a.cached_token_expires_in)}s</span>`;
  const rotatePct = a.rotate_threshold > 0
    ? Math.min(100, Math.round((a.device_request_count / a.rotate_threshold) * 100))
    : 0;
  return `
    <tr>
      <td class="mono">#${a.index}</td>
      <td>${typeBadge}</td>
      <td class="mono">${escapeHtml(a.device_id_short)}…</td>
      <td>
        <div class="mono">${a.device_request_count}/${a.rotate_threshold || '∞'}</div>
        <div style="height:3px;background:var(--bg-elevated);border-radius:2px;margin-top:2px;overflow:hidden;">
          <div style="height:100%;width:${rotatePct}%;background:${rotatePct >= 80 ? 'var(--error)' : 'var(--primary)'};"></div>
        </div>
      </td>
      <td class="mono">
        <span class="text-success">${a.success_count}</span> / <span class="text-error">${a.error_count}</span>
      </td>
      <td>${prefetchBadge}</td>
      <td>${tokenExpire}</td>
      <td class="text-muted" style="font-size:12px;">${a.last_used_ts ? fmtTimeShort(a.last_used_ts) : '-'}</td>
      <td>
        <button class="btn btn-ghost btn-sm" data-rotate-idx="${a.index}">轮换</button>
      </td>
    </tr>
  `;
}

async function handleRotate(idx) {
  if (!confirm(`确认手动轮换账号 #${idx} 的 device_id？`)) return;
  try {
    const data = await api(`accounts/${idx}/rotate`, { method: 'POST' });
    showToast(`已轮换：${data.old_device} → ${data.new_device}`, 'success');
    refreshAccounts();
  } catch (err) {
    showToast('轮换失败：' + err.message, 'error');
  }
}

// =========================================================================
// Models view (动态模型列表 + 单模型探针)
// =========================================================================

let _modelsCache = null;        // 缓存上次拉取的 models 列表，避免 probe 时重复拉
let _probingAll = false;        // 防止"全部探针"按钮被重复点击

async function refreshModels() {
  const data = await api('models');
  _modelsCache = data;
  const models = data.models || [];
  // 统计
  const probed = models.filter(m => m.last_probe);
  const okCount = probed.filter(m => m.last_probe.ok).length;
  const failCount = probed.length - okCount;
  const imageCount = models.filter(m => m.is_image_model).length;
  const variantCount = models.filter(m => m.is_variant).length;

  const html = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">模型总数</div>
        <div class="kpi-value">${data.total}</div>
        <div class="kpi-sub">基础模型 ${data.base_count} · 变体 ${variantCount}</div>
      </div>
      <div class="kpi-card success">
        <div class="kpi-label">探测可用</div>
        <div class="kpi-value">${okCount}</div>
        <div class="kpi-sub">已探测 ${probed.length} / ${data.total}</div>
      </div>
      <div class="kpi-card ${failCount > 0 ? 'error' : ''}">
        <div class="kpi-label">探测失败</div>
        <div class="kpi-value">${failCount}</div>
        <div class="kpi-sub">${failCount === 0 ? '暂无失败' : '点击状态徽章查看错误'}</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">图像模型</div>
        <div class="kpi-value">${imageCount}</div>
        <div class="kpi-sub">cogView / glm-image-1</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">模型列表</div>
        <div class="panel-meta">
          <button id="models-probe-all" class="btn btn-primary btn-sm">${_probingAll ? '探针中…' : '🧪 全部探针'}</button>
          <button id="models-clear-probe" class="btn btn-ghost btn-sm" style="margin-left:8px;">清除探针结果</button>
        </div>
      </div>
      <div class="panel-body">
        <div class="filter-bar" style="margin-bottom:12px;">
          <label>过滤:
            <input type="text" id="models-filter" placeholder="按名称过滤..." style="background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-light);border-radius:4px;padding:4px 8px;font-size:13px;width:220px;" />
          </label>
          <label>状态:
            <select id="models-status-filter" style="background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-light);border-radius:4px;padding:4px 8px;font-size:13px;">
              <option value="all">全部</option>
              <option value="ok">✅ 可用</option>
              <option value="fail">❌ 失败</option>
              <option value="unprobed">⏳ 未探测</option>
            </select>
          </label>
        </div>
        <div class="table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>模型 ID</th>
                <th>基础模型</th>
                <th>特性</th>
                <th>类型</th>
                <th>协议</th>
                <th>最近探针</th>
                <th>延迟</th>
                <th>账号</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="models-tbody">
              ${models.map(m => renderModelRow(m)).join('') || `<tr><td colspan="9" class="empty-state">无模型</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
  document.getElementById('view-models').innerHTML = html;

  // 绑定事件
  document.getElementById('models-probe-all').addEventListener('click', handleProbeAll);
  document.getElementById('models-clear-probe').addEventListener('click', handleClearProbe);
  document.getElementById('models-filter').addEventListener('input', applyModelFilter);
  document.getElementById('models-status-filter').addEventListener('change', applyModelFilter);
  document.querySelectorAll('[data-probe-model]').forEach(btn => {
    btn.addEventListener('click', () => handleProbeModel(btn.dataset.probeModel));
  });
}

function renderModelRow(m) {
  const features = (m.features || []).map(f => `<span class="badge badge-info">${escapeHtml(f)}</span>`).join(' ');
  const typeBadge = m.is_image_model
    ? `<span class="badge badge-warning">图像</span>`
    : m.is_variant
      ? `<span class="badge badge-muted">变体</span>`
      : `<span class="badge badge-success">基础</span>`;
  const profile = m.profile || {};
  const probe = m.last_probe;
  let probeBadge, latencyStr, accountStr;
  if (!probe) {
    probeBadge = `<span class="badge badge-muted">未探测</span>`;
    latencyStr = '-';
    accountStr = '-';
  } else if (probe.ok) {
    probeBadge = `<span class="badge badge-success" title="${escapeHtml(probe.content_preview || '')}">✅ 可用</span>`;
    latencyStr = `<span class="text-success">${probe.latency_ms}ms</span>`;
    accountStr = probe.account_index >= 0 ? `#${probe.account_index}` : '-';
  } else {
    probeBadge = `<span class="badge badge-error" title="${escapeHtml(probe.error || '')}">❌ 失败</span>`;
    latencyStr = `<span class="text-error">${probe.latency_ms}ms</span>`;
    accountStr = probe.account_index >= 0 ? `#${probe.account_index}` : '-';
  }
  return `
    <tr data-model-id="${escapeHtml(m.id)}" data-probe-ok="${probe ? (probe.ok ? 'ok' : 'fail') : 'unprobed'}">
      <td class="mono" style="font-size:12px;">${escapeHtml(m.id)}</td>
      <td class="mono text-muted" style="font-size:12px;">${escapeHtml(m.base)}</td>
      <td>${features || '<span class="text-muted">-</span>'}</td>
      <td>${typeBadge}</td>
      <td class="mono text-muted" style="font-size:11px;">${escapeHtml(profile.preferred_format || '-')}</td>
      <td>${probeBadge}</td>
      <td class="mono">${latencyStr}</td>
      <td class="mono text-muted">${accountStr}</td>
      <td><button class="btn btn-ghost btn-sm" data-probe-model="${escapeHtml(m.id)}">测试</button></td>
    </tr>
  `;
}

function applyModelFilter() {
  const q = (document.getElementById('models-filter').value || '').toLowerCase().trim();
  const status = document.getElementById('models-status-filter').value;
  document.querySelectorAll('#models-tbody tr[data-model-id]').forEach(tr => {
    const id = (tr.dataset.modelId || '').toLowerCase();
    const ok = tr.dataset.probeOk;
    const matchQ = !q || id.includes(q);
    const matchS = status === 'all' || ok === status;
    tr.style.display = (matchQ && matchS) ? '' : 'none';
  });
}

async function handleProbeModel(modelId) {
  // 即时把按钮变成 loading 状态
  const btn = document.querySelector(`[data-probe-model="${cssEscape(modelId)}"]`);
  if (btn) { btn.disabled = true; btn.textContent = '探测中…'; }
  showToast(`正在探测 ${modelId}...`, 'info');
  try {
    const result = await api('probe_model', { method: 'POST', body: { model: modelId, prompt: 'hi' } });
    if (result.ok) {
      showToast(`✅ ${modelId} 可用 (${result.latency_ms}ms)`, 'success');
    } else {
      showToast(`❌ ${modelId} 失败: ${result.error || ''}`, 'error');
    }
    // 重新拉取列表以更新徽章（也更新缓存）
    await refreshModels();
  } catch (err) {
    showToast('探测失败: ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '测试'; }
  }
}

async function handleProbeAll() {
  if (_probingAll) return;
  if (!_modelsCache || !_modelsCache.models) return;
  _probingAll = true;
  const btn = document.getElementById('models-probe-all');
  if (btn) { btn.disabled = true; btn.textContent = '探针中…'; }
  const models = _modelsCache.models;
  let okCount = 0, failCount = 0;
  showToast(`开始批量探针 ${models.length} 个模型...`, 'info');
  for (let i = 0; i < models.length; i++) {
    const m = models[i];
    if (btn) btn.textContent = `探针中… (${i + 1}/${models.length})`;
    try {
      const result = await api('probe_model', { method: 'POST', body: { model: m.id, prompt: 'hi' } });
      if (result.ok) okCount++; else failCount++;
    } catch (err) {
      failCount++;
    }
  }
  _probingAll = false;
  if (btn) { btn.disabled = false; btn.textContent = '🧪 全部探针'; }
  showToast(`批量探针完成：✅ ${okCount} 可用 / ❌ ${failCount} 失败`, failCount > 0 ? 'warning' : 'success');
  await refreshModels();
}

async function handleClearProbe() {
  // 没有专门的后端端点，直接前端重渲染（缓存还在后端，但下次 probe 会覆盖）
  // 简化：通过重新拉取 models 端点，因为 last_probe 还是会有；这里实际上需要后端清理
  // 暂用前端隐藏：清空所有 last_probe 显示
  if (!confirm('确认清除所有模型的探针结果？')) return;
  // 前端临时清空：直接重渲染但不显示探针状态
  if (_modelsCache) {
    _modelsCache.models = _modelsCache.models.map(m => ({ ...m, last_probe: null }));
    refreshModelsUI();
    showToast('已清除探针结果（后端缓存仍在，下次探测会覆盖）', 'info');
  }
}

// 仅刷新 UI（不重新拉 API），用于 clear probe 后立即生效
function refreshModelsUI() {
  if (!_modelsCache) return;
  const data = _modelsCache;
  const models = data.models || [];
  const tbody = document.getElementById('models-tbody');
  if (tbody) {
    tbody.innerHTML = models.map(m => renderModelRow(m)).join('') || `<tr><td colspan="9" class="empty-state">无模型</td></tr>`;
    document.querySelectorAll('[data-probe-model]').forEach(btn => {
      btn.addEventListener('click', () => handleProbeModel(btn.dataset.probeModel));
    });
  }
}

// CSS escape for attribute selector (浏览器原生支持)
function cssEscape(s) {
  if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(s);
  return String(s).replace(/["\\]/g, '\\$&');
}

// =========================================================================
// Probe view (端点测试 - 自由发送 chat completions 请求)
// =========================================================================

async function refreshProbe() {
  // 如果是首次进入，拉一次模型列表填充下拉
  let modelsData = _modelsCache;
  if (!modelsData) {
    try {
      modelsData = await api('models');
      _modelsCache = modelsData;
    } catch (err) {
      // 静默失败，下面会显示错误
    }
  }
  const models = (modelsData && modelsData.models) || [];
  // 把基础模型排在前面
  const sorted = [...models].sort((a, b) => {
    if (a.is_variant !== b.is_variant) return a.is_variant ? 1 : -1;
    return a.id.localeCompare(b.id);
  });
  const options = sorted.map(m => {
    const tag = m.is_image_model ? '[图像] ' : (m.is_variant ? '[变体] ' : '');
    return `<option value="${escapeHtml(m.id)}">${tag}${escapeHtml(m.id)}</option>`;
  }).join('');

  document.getElementById('view-probe').innerHTML = `
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">端点测试</div>
        <div class="panel-meta">选择模型 + 输入消息 → 查看完整响应</div>
      </div>
      <div class="panel-body">
        <div class="probe-form">
          <div class="probe-row">
            <label class="probe-label">模型</label>
            <select id="probe-model" class="probe-input" style="flex:1;">
              ${options || '<option value="">(无可用模型)</option>'}
            </select>
          </div>
          <div class="probe-row" style="gap:12px;">
            <div style="flex:1;">
              <label class="probe-label">Temperature <span id="probe-temp-val" class="text-muted">0.7</span></label>
              <input type="range" id="probe-temperature" min="0" max="2" step="0.1" value="0.7" class="probe-input" />
            </div>
            <div style="width:140px;">
              <label class="probe-label">Max Tokens</label>
              <input type="number" id="probe-max-tokens" value="512" min="1" max="8192" class="probe-input" />
            </div>
          </div>
          <div class="probe-row">
            <label class="probe-label">System Prompt (可选)</label>
            <textarea id="probe-system" class="probe-input" rows="2" placeholder="例如：你是一个简洁的助手"></textarea>
          </div>
          <div class="probe-row">
            <label class="probe-label">用户消息</label>
            <textarea id="probe-message" class="probe-input" rows="4" placeholder="输入要发送给模型的消息...">你好，用一句话介绍你自己</textarea>
          </div>
          <div class="probe-row" style="flex-direction:row;align-items:center;gap:12px;">
            <button id="probe-send" class="btn btn-primary">🚀 发送请求</button>
            <button id="probe-clear" class="btn btn-ghost">清空</button>
            <span id="probe-status" class="text-muted" style="font-size:12px;"></span>
          </div>
        </div>
      </div>
    </div>

    <div class="panel" id="probe-result-panel" style="display:none;">
      <div class="panel-header">
        <div class="panel-title">响应结果</div>
        <div class="panel-meta" id="probe-result-meta"></div>
      </div>
      <div class="panel-body">
        <div id="probe-result-tabs" class="probe-tabs">
          <button class="probe-tab active" data-tab="content">响应内容</button>
          <button class="probe-tab" data-tab="raw">原始 JSON</button>
          <button class="probe-tab" data-tab="meta">元信息</button>
        </div>
        <div id="probe-tab-content" class="probe-tab-pane"></div>
      </div>
    </div>
  `;

  // 绑定事件
  document.getElementById('probe-temperature').addEventListener('input', e => {
    document.getElementById('probe-temp-val').textContent = e.target.value;
  });
  document.getElementById('probe-send').addEventListener('click', handleProbeSend);
  document.getElementById('probe-clear').addEventListener('click', () => {
    document.getElementById('probe-system').value = '';
    document.getElementById('probe-message').value = '';
    document.getElementById('probe-result-panel').style.display = 'none';
  });
  document.querySelectorAll('.probe-tab').forEach(tab => {
    tab.addEventListener('click', () => switchProbeTab(tab.dataset.tab));
  });
}

let _lastProbeResult = null;

async function handleProbeSend() {
  const model = document.getElementById('probe-model').value;
  const systemPrompt = document.getElementById('probe-system').value.trim();
  const userMessage = document.getElementById('probe-message').value.trim();
  const temperature = parseFloat(document.getElementById('probe-temperature').value);
  const maxTokens = parseInt(document.getElementById('probe-max-tokens').value, 10) || 512;
  const statusEl = document.getElementById('probe-status');
  const sendBtn = document.getElementById('probe-send');

  if (!model) { showToast('请选择模型', 'error'); return; }
  if (!userMessage) { showToast('请输入用户消息', 'error'); return; }

  const messages = [];
  if (systemPrompt) messages.push({ role: 'system', content: systemPrompt });
  messages.push({ role: 'user', content: userMessage });

  const payload = {
    model,
    messages,
    temperature,
    max_tokens: maxTokens,
    stream: false,
  };

  sendBtn.disabled = true;
  sendBtn.textContent = '请求中…';
  statusEl.innerHTML = '<span class="spinner"></span> 等待响应...';
  document.getElementById('probe-result-panel').style.display = 'none';

  const t0 = performance.now();
  try {
    const result = await api('probe', { method: 'POST', body: payload });
    const elapsed = Math.round(performance.now() - t0);
    _lastProbeResult = { result, payload, elapsed };
    renderProbeResult(result, payload, elapsed);
    statusEl.textContent = '';
    if (result.ok) {
      showToast(`✅ 请求成功 (${result.latency_ms}ms)`, 'success');
    } else {
      showToast(`❌ 请求失败: ${result.error || ''}`, 'error');
    }
  } catch (err) {
    statusEl.innerHTML = `<span class="text-error">❌ ${escapeHtml(err.message)}</span>`;
    showToast('请求失败: ' + err.message, 'error');
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = '🚀 发送请求';
  }
}

function renderProbeResult(result, payload, elapsed) {
  const panel = document.getElementById('probe-result-panel');
  panel.style.display = '';
  const metaEl = document.getElementById('probe-result-meta');
  metaEl.innerHTML = `
    ${result.ok ? '<span class="badge badge-success">OK</span>' : '<span class="badge badge-error">FAIL</span>'}
    <span class="text-muted">·</span>
    <span class="mono">HTTP ${result.status}</span>
    <span class="text-muted">·</span>
    <span class="mono">${result.latency_ms}ms</span>
    ${result.account_index >= 0 ? `<span class="text-muted">·</span><span class="mono">账号 #${result.account_index}</span>` : ''}
  `;
  switchProbeTab('content');
}

function switchProbeTab(tab) {
  document.querySelectorAll('.probe-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  const pane = document.getElementById('probe-tab-content');
  if (!_lastProbeResult) { pane.innerHTML = '<div class="empty-state">无数据</div>'; return; }
  const { result, payload, elapsed } = _lastProbeResult;
  if (tab === 'content') {
    if (!result.ok) {
      pane.innerHTML = `<div class="probe-error">❌ ${escapeHtml(result.error || '未知错误')}</div>`;
      return;
    }
    const resp = result.response || {};
    const choices = resp.choices || [];
    let content = '';
    let reasoning = '';
    let toolCalls = '';
    choices.forEach(ch => {
      const msg = ch.message || {};
      if (msg.content) content += msg.content + '\n';
      if (msg.reasoning_content) reasoning += msg.reasoning_content + '\n';
      if (msg.tool_calls) toolCalls += JSON.stringify(msg.tool_calls, null, 2) + '\n';
    });
    const usage = resp.usage || {};
    pane.innerHTML = `
      <div class="probe-content-block">
        <div class="probe-content-label">响应内容</div>
        <div class="probe-content-text">${escapeHtml(content.trim()) || '<span class="text-muted">(空)</span>'}</div>
      </div>
      ${reasoning ? `
        <div class="probe-content-block">
          <div class="probe-content-label">思维链 (reasoning_content)</div>
          <pre class="probe-content-pre">${escapeHtml(reasoning.trim())}</pre>
        </div>` : ''}
      ${toolCalls ? `
        <div class="probe-content-block">
          <div class="probe-content-label">工具调用</div>
          <pre class="probe-content-pre">${escapeHtml(toolCalls.trim())}</pre>
        </div>` : ''}
      <div class="probe-content-block">
        <div class="probe-content-label">Usage</div>
        <div class="mono" style="font-size:12px;color:var(--text-muted);">
          prompt=${usage.prompt_tokens || 0} · completion=${usage.completion_tokens || 0} · total=${usage.total_tokens || 0}
        </div>
      </div>
    `;
  } else if (tab === 'raw') {
    pane.innerHTML = `<pre class="probe-json">${escapeHtml(JSON.stringify(result.response, null, 2))}</pre>`;
  } else { // meta
    pane.innerHTML = `
      <div class="config-list">
        <div class="config-row"><div class="config-key">请求模型</div><div class="config-value">${escapeHtml(payload.model)}</div></div>
        <div class="config-row"><div class="config-key">HTTP 状态</div><div class="config-value">${result.status}</div></div>
        <div class="config-row"><div class="config-key">上游延迟</div><div class="config-value">${result.latency_ms} ms</div></div>
        <div class="config-row"><div class="config-key">总耗时 (含网络)</div><div class="config-value">${elapsed} ms</div></div>
        <div class="config-row"><div class="config-key">使用账号</div><div class="config-value">${result.account_index >= 0 ? '#' + result.account_index : '-'}</div></div>
        <div class="config-row"><div class="config-key">会话 ID</div><div class="config-value mono">${escapeHtml(result.conversation_id || '-')}</div></div>
        <div class="config-row"><div class="config-key">是否成功</div><div class="config-value ${result.ok ? 'bool-true' : 'bool-false'}">${result.ok}</div></div>
        ${result.error ? `<div class="config-row"><div class="config-key">错误</div><div class="config-value" style="color:var(--error);">${escapeHtml(result.error)}</div></div>` : ''}
      </div>
    `;
  }
}

// =========================================================================
// Logs view
// =========================================================================

async function refreshLogs() {
  const onlyErrors = document.getElementById('logs-only-errors').checked ? 1 : 0;
  const limit = document.getElementById('logs-limit').value;
  const data = await api(`logs?limit=${limit}&errors=${onlyErrors}`);
  const logs = data.logs || [];
  const wrapper = document.getElementById('logs-table-wrapper');
  if (logs.length === 0) {
    wrapper.innerHTML = `<div class="empty-state">暂无日志记录</div>`;
    return;
  }
  const rows = logs.map(l => {
    const statusClass = l.status >= 500 ? 'text-error'
                      : l.status >= 400 ? 'text-warning'
                      : 'text-success';
    return `
      <tr>
        <td class="mono text-muted" style="font-size:11px;">${fmtTime(l.ts)}</td>
        <td class="mono">${escapeHtml(l.method)}</td>
        <td class="mono" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(l.path)}">${escapeHtml(l.path)}</td>
        <td><span class="badge ${l.protocol.startsWith('anthropic') ? 'badge-purple' : l.protocol.includes('responses') ? 'badge-info' : 'badge-success'}">${escapeHtml(l.protocol)}</span></td>
        <td class="mono text-muted">${escapeHtml(l.model || '-')}</td>
        <td class="mono"><span class="${statusClass}">${l.status}</span></td>
        <td class="mono">${l.duration_ms}ms</td>
        <td class="mono text-muted">${l.stream ? 'stream' : 'sync'}</td>
        <td class="mono text-muted">${l.account_index >= 0 ? '#' + l.account_index : '-'}</td>
        <td class="mono text-muted" style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(l.error || '')}">${escapeHtml(l.error || '') || '-'}</td>
        <td class="mono text-muted" style="font-size:11px;">${escapeHtml(shortHash(l.request_id, 12))}</td>
      </tr>
    `;
  }).join('');
  wrapper.innerHTML = `
    <div class="table-wrapper">
      <table class="data-table">
        <thead>
          <tr>
            <th>时间</th><th>方法</th><th>路径</th><th>协议</th><th>模型</th>
            <th>状态</th><th>延迟</th><th>模式</th><th>账号</th><th>错误</th><th>请求ID</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

// =========================================================================
// Rotate events view
// =========================================================================

async function refreshRotates() {
  const data = await api('rotates');
  const events = data.events || [];
  const wrapper = document.getElementById('rotates-table-wrapper');
  if (events.length === 0) {
    wrapper.innerHTML = `<div class="empty-state">暂无轮换事件</div>`;
    return;
  }
  const rows = events.map(e => {
    const reasonClass = e.reason === 'manual' ? 'badge-info' :
                        e.reason === 'rate_limited' ? 'badge-error' :
                        e.reason === 'proactive' ? 'badge-success' : 'badge-muted';
    return `
      <tr>
        <td class="mono text-muted" style="font-size:11px;">${fmtTime(e.ts)}</td>
        <td class="mono">#${e.account_index}</td>
        <td class="mono">${escapeHtml(e.old_device)}…</td>
        <td class="mono">→</td>
        <td class="mono">${escapeHtml(e.new_device)}…</td>
        <td><span class="badge ${reasonClass}">${escapeHtml(e.reason)}</span></td>
      </tr>
    `;
  }).join('');
  wrapper.innerHTML = `
    <div class="table-wrapper">
      <table class="data-table">
        <thead>
          <tr><th>时间</th><th>账号</th><th>旧 device</th><th></th><th>新 device</th><th>原因</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

// =========================================================================
// Config view
// =========================================================================

async function refreshConfig() {
  const cfg = await api('config');
  const SECRET_KEYS = new Set(['glm_refresh_token', 'server_api_keys_first']);
  const BOOL_KEYS = new Set([
    'glm_use_guest_refresh_token', 'glm_delete_conversation', 'debug_dump_all'
  ]);
  const rows = Object.entries(cfg).map(([k, v]) => {
    let valueClass = '';
    let valueStr = '';
    if (Array.isArray(v)) {
      valueStr = v.length ? v.join(', ') : '(empty)';
    } else if (typeof v === 'boolean') {
      valueClass = v ? 'bool-true' : 'bool-false';
      valueStr = String(v);
    } else if (SECRET_KEYS.has(k)) {
      valueClass = 'secret';
      valueStr = String(v);
    } else {
      valueStr = String(v);
    }
    return `
      <div class="config-row">
        <div class="config-key">${escapeHtml(k)}</div>
        <div class="config-value ${valueClass}">${escapeHtml(valueStr)}</div>
      </div>
    `;
  }).join('');
  document.getElementById('view-config').innerHTML = `
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">运行时配置 (脱敏)</div>
        <div class="panel-meta">敏感字段已部分遮蔽</div>
      </div>
      <div class="panel-body">
        <div class="config-list">${rows}</div>
      </div>
    </div>
  `;
}

// =========================================================================
// System view
// =========================================================================

async function refreshSystem() {
  const sys = await api('system');
  const memPctOfRss = 0; // unknown; just show rss
  document.getElementById('view-system').innerHTML = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">进程 PID</div>
        <div class="kpi-value">${sys.process.pid}</div>
        <div class="kpi-sub">PPID ${sys.process.ppid}</div>
      </div>
      <div class="kpi-card success">
        <div class="kpi-label">内存 RSS</div>
        <div class="kpi-value" style="font-size:20px;">${sys.memory.rss_human}</div>
        <div class="kpi-sub">${sys.memory.rss_bytes.toLocaleString()} bytes</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">线程数</div>
        <div class="kpi-value">${sys.threads}</div>
      </div>
      <div class="kpi-card ${sys.uptime_seconds > 86400 ? 'success' : ''}">
        <div class="kpi-label">运行时长</div>
        <div class="kpi-value" style="font-size:18px;">${fmtDuration(sys.uptime_seconds)}</div>
        <div class="kpi-sub">自 ${fmtTime(sys.now - sys.uptime_seconds)}</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header"><div class="panel-title">进程信息</div></div>
      <div class="panel-body">
        <div class="config-list">
          <div class="config-row"><div class="config-key">Python</div><div class="config-value">${escapeHtml(sys.process.python)}</div></div>
          <div class="config-row"><div class="config-key">Platform</div><div class="config-value">${escapeHtml(sys.process.platform)}</div></div>
          <div class="config-row"><div class="config-key">Machine</div><div class="config-value">${escapeHtml(sys.process.machine)}</div></div>
          <div class="config-row"><div class="config-key">Hostname</div><div class="config-value">${escapeHtml(sys.process.node)}</div></div>
          <div class="config-row"><div class="config-key">Started</div><div class="config-value">${fmtTime(sys.now - sys.uptime_seconds)}</div></div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header"><div class="panel-title">磁盘 & 日志</div></div>
      <div class="panel-body">
        <div class="kpi-grid">
          <div class="kpi-card">
            <div class="kpi-label">日志占用</div>
            <div class="kpi-value" style="font-size:20px;">${sys.disk.log_size_human}</div>
            <div class="kpi-sub">${sys.disk.log_file_count} 个文件</div>
          </div>
          <div class="kpi-card success">
            <div class="kpi-label">磁盘剩余</div>
            <div class="kpi-value" style="font-size:20px;">${sys.disk.free_human}</div>
            <div class="kpi-sub">总 ${sys.disk.total_human} · 已用 ${sys.disk.used_human}</div>
          </div>
        </div>
        <div class="config-list">
          <div class="config-row"><div class="config-key">项目目录</div><div class="config-value">${escapeHtml(sys.disk.project_root)}</div></div>
          <div class="config-row"><div class="config-key">日志目录</div><div class="config-value">${escapeHtml(sys.disk.log_dir)}</div></div>
        </div>
      </div>
    </div>
  `;
}

// =========================================================================
// Boot
// =========================================================================

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('login-form').addEventListener('submit', handleLoginSubmit);
  document.getElementById('logout-btn').addEventListener('click', handleLogout);
  document.getElementById('auto-refresh').addEventListener('change', startAutoRefresh);
  document.getElementById('manual-refresh').addEventListener('click', refresh);
  document.getElementById('logs-only-errors').addEventListener('change', refreshLogs);
  document.getElementById('logs-limit').addEventListener('change', refreshLogs);

  document.querySelectorAll('.nav-item').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      switchView(a.dataset.view);
    });
  });

  // Try auto-login with stored token
  if (getToken()) {
    api('dashboard').then(() => {
      showApp();
      startAutoRefresh();
    }).catch(() => {
      clearToken();
      showLogin();
    });
  } else {
    showLogin();
  }
});
