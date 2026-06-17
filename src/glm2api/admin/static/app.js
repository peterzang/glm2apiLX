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
