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
// Dashboard view（参考 Qwen2API_Go overview-tab 重写：2 行 4 列 KPI + 主图表 + 侧边卡片 + 底部 4 卡）
// =========================================================================

async function refreshDashboard() {
  const d = await api('dashboard');

  const all = d.all_time;
  const r5 = d.recent_5m;
  const successRateColor = r5.success_rate >= 95 ? 'success'
                         : r5.success_rate >= 80 ? 'warning' : 'error';
  const rpm = d.rpm || 0;
  const avgRpm = d.avg_rpm || 0;
  const peakRpm = d.peak_rpm || 0;
  const requests30m = d.requests_30m || 0;
  const tokenTotals = d.token_totals || { prompt: 0, completion: 0, total: 0 };
  const token30m = d.token_30m || { prompt: 0, completion: 0, total: 0 };
  const accountsActive = d.accounts_active || 0;
  const accountsTotal = d.accounts_total || 0;
  const protoBreakdown = d.proto_breakdown || {};

  // === KPI Row 1 ===
  const kpiRow1 = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">总请求数</div>
        <div class="kpi-value">${(all.total || 0).toLocaleString()}</div>
        <div class="kpi-sub">成功 ${all.success} · 4xx ${all.client_errors} · 5xx ${all.server_errors}</div>
      </div>
      <div class="kpi-card ${successRateColor}">
        <div class="kpi-label">5分钟成功率</div>
        <div class="kpi-value">${(r5.success_rate || 0).toFixed(1)}%</div>
        <div class="kpi-sub">${r5.success}/${r5.total} 请求</div>
      </div>
      <div class="kpi-card success">
        <div class="kpi-label">活跃账号</div>
        <div class="kpi-value">${accountsActive}<span style="font-size:14px;color:var(--text-muted);"> / ${accountsTotal}</span></div>
        <div class="kpi-sub">已使用过的账号</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">运行时长</div>
        <div class="kpi-value" style="font-size:18px;">${fmtDuration(d.uptime_seconds)}</div>
        <div class="kpi-sub">自 ${fmtTime(d.now - d.uptime_seconds)}</div>
      </div>
    </div>
  `;

  // === KPI Row 2 ===
  const kpiRow2 = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">当前 RPM</div>
        <div class="kpi-value">${rpm}</div>
        <div class="kpi-sub">30m 平均 ${avgRpm} rpm · 峰值 ${peakRpm}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">P95 延迟</div>
        <div class="kpi-value">${r5.p95_ms}ms</div>
        <div class="kpi-sub">P50 ${r5.p50_ms}ms · P99 ${r5.p99_ms}ms</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">Prompt Tokens</div>
        <div class="kpi-value">${fmtCompactNum(tokenTotals.prompt)}</div>
        <div class="kpi-sub">30m ${fmtCompactNum(token30m.prompt)}</div>
      </div>
      <div class="kpi-card" style="border-left:3px solid var(--purple);">
        <div class="kpi-label">Completion Tokens</div>
        <div class="kpi-value">${fmtCompactNum(tokenTotals.completion)}</div>
        <div class="kpi-sub">总计 ${fmtCompactNum(tokenTotals.total)}</div>
      </div>
    </div>
  `;

  // === 主图表 + 侧边卡片 ===
  const mainArea = `
    <div class="dashboard-grid-3">
      <div class="dashboard-col-2">
        ${renderRequestTrendCard(d.hourly)}
        ${renderTokenThroughputCard(d.hourly)}
      </div>
      <div class="dashboard-col-1">
        ${renderProtoBreakdownCard(protoBreakdown)}
        ${renderAccountHealthCard(accountsActive, accountsTotal, all)}
      </div>
    </div>
  `;

  // === 底部 4 卡 ===
  const bottomCards = `
    <div class="kpi-grid">
      ${renderTrafficSplitCard(protoBreakdown, all.total)}
      ${renderServiceParamsCard(d)}
      ${renderModelSupplyCard(d.top_models)}
      ${renderOpsMetricsCard(d, requests30m, peakRpm)}
    </div>
  `;

  document.getElementById('view-dashboard').innerHTML = kpiRow1 + kpiRow2 + mainArea + bottomCards;
}

// 紧凑数字格式：1234 → 1.2K，1234567 → 1.2M
function fmtCompactNum(n) {
  n = Number(n || 0);
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(1) + 'K';
  return (n / 1000000).toFixed(1) + 'M';
}

// === SVG 图表：48h 请求趋势 ===
function renderRequestTrendCard(hourly) {
  const data = (hourly || []).slice(-48);
  const maxTotal = Math.max(1, ...data.map(h => h.total));
  const W = 600, H = 180, P = 28;
  const innerW = W - P * 2, innerH = H - P * 2;
  // 构造区域路径
  if (data.length === 0) {
    return `
      <div class="panel">
        <div class="panel-header"><div class="panel-title">请求趋势（48 小时）</div><div class="panel-meta">绿=成功 · 红=5xx</div></div>
        <div class="panel-body"><div class="empty-state">暂无数据</div></div>
      </div>
    `;
  }
  const points = data.map((h, i) => {
    const x = P + (i / Math.max(1, data.length - 1)) * innerW;
    const y = P + innerH - (h.total / maxTotal) * innerH;
    return { x, y, h };
  });
  const linePath = points.map((p, i) => (i === 0 ? `M ${p.x} ${p.y}` : `L ${p.x} ${p.y}`)).join(' ');
  const areaPath = linePath + ` L ${points[points.length-1].x} ${P + innerH} L ${points[0].x} ${P + innerH} Z`;
  // Y 轴刻度
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map(t => ({
    y: P + innerH - t * innerH,
    val: Math.round(t * maxTotal),
  }));
  // X 轴标签（每 6 小时一个）
  const xLabels = points.filter((_, i) => i % 6 === 0).map(p => ({
    x: p.x,
    label: fmtTimeShort(p.h.hour).slice(0, 5),
  }));
  return `
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">请求趋势（48 小时）</div>
        <div class="panel-meta">峰值 ${maxTotal} · 绿=成功 · 红=5xx</div>
      </div>
      <div class="panel-body">
        <svg viewBox="0 0 ${W} ${H}" class="dashboard-svg" preserveAspectRatio="none">
          <defs>
            <linearGradient id="reqGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stop-color="#3b82f6" stop-opacity="0.35"/>
              <stop offset="95%" stop-color="#3b82f6" stop-opacity="0"/>
            </linearGradient>
          </defs>
          ${yTicks.map(t => `
            <line x1="${P}" y1="${t.y}" x2="${W - P}" y2="${t.y}" stroke="var(--border)" stroke-dasharray="3 3" stroke-width="0.5"/>
            <text x="${P - 6}" y="${t.y + 3}" text-anchor="end" font-size="10" fill="var(--text-muted)">${t.val}</text>
          `).join('')}
          <path d="${areaPath}" fill="url(#reqGrad)"/>
          <path d="${linePath}" fill="none" stroke="#3b82f6" stroke-width="2"/>
          ${points.map(p => `
            <circle cx="${p.x}" cy="${p.y}" r="2" fill="#3b82f6">
              <title>${fmtTimeShort(p.h.hour)} · 总 ${p.h.total} · 成功 ${p.h.success} · 错误 ${p.h.error}</title>
            </circle>
          `).join('')}
          ${xLabels.map(l => `<text x="${l.x}" y="${H - 8}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${l.label}</text>`).join('')}
        </svg>
      </div>
    </div>
  `;
}

// === SVG 图表：每小时 Token 吞吐（堆叠柱状图）===
function renderTokenThroughputCard(hourly) {
  const data = (hourly || []).slice(-48);
  const maxTotal = Math.max(1, ...data.map(h => h.total));
  const W = 600, H = 180, P = 28;
  const innerW = W - P * 2, innerH = H - P * 2;
  if (data.length === 0) {
    return `
      <div class="panel">
        <div class="panel-header"><div class="panel-title">每小时请求量（柱状）</div><div class="panel-meta">绿=成功 · 红=5xx</div></div>
        <div class="panel-body"><div class="empty-state">暂无数据</div></div>
      </div>
    `;
  }
  const barW = innerW / data.length * 0.7;
  const gap = innerW / data.length * 0.3;
  const bars = data.map((h, i) => {
    const x = P + (i + 0.15) * (innerW / data.length);
    const totalH = (h.total / maxTotal) * innerH;
    const errH = h.error > 0 ? (h.error / maxTotal) * innerH : 0;
    const okH = totalH - errH;
    return `
      <g>
        <rect x="${x}" y="${P + innerH - okH}" width="${barW}" height="${okH}" fill="#10b981" rx="1">
          <title>${fmtTimeShort(h.hour)} · 总 ${h.total} · 成功 ${h.success}</title>
        </rect>
        ${errH > 0 ? `<rect x="${x}" y="${P + innerH - totalH}" width="${barW}" height="${errH}" fill="#ef4444" rx="1">
          <title>${fmtTimeShort(h.hour)} · 错误 ${h.error}</title>
        </rect>` : ''}
      </g>
    `;
  }).join('');
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map(t => ({
    y: P + innerH - t * innerH,
    val: Math.round(t * maxTotal),
  }));
  const xLabels = data.filter((_, i) => i % 6 === 0).map((h, i) => {
    const idx = data.indexOf(h);
    const x = P + (idx + 0.5) * (innerW / data.length);
    return `<text x="${x}" y="${H - 8}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${fmtTimeShort(h.hour).slice(0, 5)}</text>`;
  }).join('');
  return `
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">每小时请求量（柱状）</div>
        <div class="panel-meta">绿=成功 · 红=5xx</div>
      </div>
      <div class="panel-body">
        <svg viewBox="0 0 ${W} ${H}" class="dashboard-svg" preserveAspectRatio="none">
          ${yTicks.map(t => `
            <line x1="${P}" y1="${t.y}" x2="${W - P}" y2="${t.y}" stroke="var(--border)" stroke-dasharray="3 3" stroke-width="0.5"/>
            <text x="${P - 6}" y="${t.y + 3}" text-anchor="end" font-size="10" fill="var(--text-muted)">${t.val}</text>
          `).join('')}
          ${bars}
          ${xLabels}
        </svg>
      </div>
    </div>
  `;
}

// === SVG 饼图：协议分类 ===
function renderProtoBreakdownCard(protoBreakdown) {
  const entries = Object.entries(protoBreakdown || {}).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  if (total === 0) {
    return `
      <div class="panel">
        <div class="panel-header"><div class="panel-title">协议分布</div><div class="panel-meta">按端点类别</div></div>
        <div class="panel-body"><div class="empty-state">暂无数据</div></div>
      </div>
    `;
  }
  const labels = { chat: 'Chat 对话', models: 'Models 元信息', images: 'Images 图像', embeddings: 'Embeddings', moderations: 'Moderations', other: '其他' };
  const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4', '#64748b'];
  const cx = 90, cy = 90, r = 70, innerR = 38;
  let cumAngle = -Math.PI / 2;
  const slices = entries.map(([k, v], i) => {
    const angle = (v / total) * Math.PI * 2;
    const x1 = cx + r * Math.cos(cumAngle);
    const y1 = cy + r * Math.sin(cumAngle);
    const x2 = cx + r * Math.cos(cumAngle + angle);
    const y2 = cy + r * Math.sin(cumAngle + angle);
    const xi1 = cx + innerR * Math.cos(cumAngle);
    const yi1 = cy + innerR * Math.sin(cumAngle);
    const xi2 = cx + innerR * Math.cos(cumAngle + angle);
    const yi2 = cy + innerR * Math.sin(cumAngle + angle);
    const largeArc = angle > Math.PI ? 1 : 0;
    const path = `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} L ${xi2} ${yi2} A ${innerR} ${innerR} 0 ${largeArc} 0 ${xi1} ${yi1} Z`;
    cumAngle += angle;
    const pct = (v / total * 100).toFixed(0);
    return { path, color: colors[i % colors.length], label: labels[k] || k, value: v, pct };
  });
  return `
    <div class="panel">
      <div class="panel-header"><div class="panel-title">协议分布</div><div class="panel-meta">按端点类别</div></div>
      <div class="panel-body" style="display:flex;flex-direction:column;gap:12px;align-items:center;">
        <svg viewBox="0 0 180 180" class="dashboard-pie">
          ${slices.map(s => `<path d="${s.path}" fill="${s.color}"><title>${s.label}: ${s.value} (${s.pct}%)</title></path>`).join('')}
          <text x="90" y="86" text-anchor="middle" font-size="14" font-weight="600" fill="var(--text)">${total}</text>
          <text x="90" y="100" text-anchor="middle" font-size="9" fill="var(--text-muted)">总计</text>
        </svg>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;width:100%;font-size:12px;">
          ${slices.map(s => `
            <div style="display:flex;align-items:center;gap:6px;">
              <span style="width:10px;height:10px;border-radius:2px;background:${s.color};flex-shrink:0;"></span>
              <span class="text-muted">${escapeHtml(s.label)}</span>
              <strong style="margin-left:auto;">${s.value} · ${s.pct}%</strong>
            </div>
          `).join('')}
        </div>
      </div>
    </div>
  `;
}

// === 账号池健康度卡片（带进度条）===
function renderAccountHealthCard(active, total, all) {
  const valid = all.success || 0;
  const errors = (all.client_errors || 0) + (all.server_errors || 0);
  const errRate = all.total > 0 ? (errors / all.total * 100).toFixed(1) : 0;
  const successRate = all.success_rate || 0;
  return `
    <div class="panel">
      <div class="panel-header"><div class="panel-title">服务健康度</div><div class="panel-meta">实时指标</div></div>
      <div class="panel-body">
        ${renderMetricRow('活跃账号', active, total)}
        ${renderMetricRow('成功率', Math.round(successRate), 100, '%')}
        ${renderMetricRow('错误率', Math.round(errRate * 1) / 1, 100, '%', true)}
        ${renderMetricRow('请求成功', valid, all.total || 0)}
      </div>
    </div>
  `;
}

function renderMetricRow(label, value, total, suffix = '', invertColor = false) {
  const ratio = total > 0 ? Math.min(100, (value / total) * 100) : 0;
  const barColor = invertColor
    ? (ratio >= 10 ? 'var(--error)' : ratio >= 5 ? 'var(--warning)' : 'var(--success)')
    : (ratio >= 80 ? 'var(--success)' : ratio >= 50 ? 'var(--warning)' : 'var(--error)');
  return `
    <div class="metric-row">
      <div class="metric-head">
        <span>${escapeHtml(label)}</span>
        <strong>${value}${suffix} / ${total}${suffix}</strong>
      </div>
      <div class="metric-progress">
        <div class="metric-progress-fill" style="width:${ratio}%;background:${barColor};"></div>
      </div>
    </div>
  `;
}

// === 底部 4 卡 ===
function renderTrafficSplitCard(protoBreakdown, totalReqs) {
  const pb = protoBreakdown || {};
  return `
    <div class="kpi-card">
      <div class="kpi-label" style="margin-bottom:12px;">流量拆分</div>
      <div style="display:flex;flex-direction:column;gap:8px;font-size:13px;">
        <div class="flex-between"><span class="text-muted">Chat 对话</span><strong>${pb.chat || 0}</strong></div>
        <div class="flex-between"><span class="text-muted">Models 元信息</span><strong>${pb.models || 0}</strong></div>
        <div class="flex-between"><span class="text-muted">Images 图像</span><strong>${pb.images || 0}</strong></div>
        <div class="flex-between"><span class="text-muted">Embeddings</span><strong>${pb.embeddings || 0}</strong></div>
        <div class="flex-between"><span class="text-muted">其他</span><strong>${pb.other || 0}</strong></div>
      </div>
    </div>
  `;
}

function renderServiceParamsCard(d) {
  // 简化版服务参数卡（从 d 拿不到 server 配置，用静态信息）
  return `
    <div class="kpi-card">
      <div class="kpi-label" style="margin-bottom:12px;">运行参数</div>
      <div style="display:flex;flex-direction:column;gap:8px;font-size:13px;">
        <div class="flex-between"><span class="text-muted">运行时长</span><strong class="mono">${fmtDuration(d.uptime_seconds)}</strong></div>
        <div class="flex-between"><span class="text-muted">30m 请求数</span><strong>${d.requests_30m || 0}</strong></div>
        <div class="flex-between"><span class="text-muted">当前 RPM</span><strong class="mono">${d.rpm || 0}</strong></div>
        <div class="flex-between"><span class="text-muted">平均 RPM</span><strong class="mono">${d.avg_rpm || 0}</strong></div>
        <div class="flex-between"><span class="text-muted">峰值 RPM</span><strong class="mono">${d.peak_rpm || 0}</strong></div>
      </div>
    </div>
  `;
}

function renderModelSupplyCard(topModels) {
  const models = topModels || [];
  return `
    <div class="kpi-card">
      <div class="kpi-label" style="margin-bottom:12px;">模型使用 Top ${models.length}</div>
      <div style="display:flex;flex-direction:column;gap:8px;font-size:13px;">
        ${models.length === 0 ? '<div class="text-muted">暂无数据</div>' : models.map(m => `
          <div class="flex-between">
            <span class="mono text-muted" style="font-size:11px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(m.model)}">${escapeHtml(m.model)}</span>
            <strong>${m.count}</strong>
          </div>
        `).join('')}
      </div>
    </div>
  `;
}

function renderOpsMetricsCard(d, requests30m, peakRpm) {
  const tokenTotals = d.token_totals || { prompt: 0, completion: 0, total: 0 };
  return `
    <div class="kpi-card">
      <div class="kpi-label" style="margin-bottom:12px;">Token 累计</div>
      <div style="display:flex;flex-direction:column;gap:8px;font-size:13px;">
        <div class="flex-between"><span class="text-muted">Prompt</span><strong class="mono">${fmtCompactNum(tokenTotals.prompt)}</strong></div>
        <div class="flex-between"><span class="text-muted">Completion</span><strong class="mono">${fmtCompactNum(tokenTotals.completion)}</strong></div>
        <div class="flex-between"><span class="text-muted">总计</span><strong class="mono">${fmtCompactNum(tokenTotals.total)}</strong></div>
        <div class="flex-between" style="margin-top:4px;padding-top:8px;border-top:1px solid var(--border);">
          <span class="text-muted">30m Token</span>
          <strong class="mono">${fmtCompactNum((d.token_30m || {}).total || 0)}</strong>
        </div>
      </div>
    </div>
  `;
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
// Models view (真实上游助手 + 本地 OpenAI 兼容模型)
// =========================================================================

let _modelsCache = null;        // 缓存上次拉取的 models 列表，避免 probe 时重复拉
let _probingAll = false;        // 防止"全部探针"按钮被重复点击

async function refreshModels() {
  const data = await api('models');
  _modelsCache = data;
  const models = data.models || [];
  const orphans = data.orphan_assistants || [];
  const cache = data.upstream_cache || {};

  // 统计
  const probed = models.filter(m => m.last_probe);
  const okCount = probed.filter(m => m.last_probe.ok).length;
  const failCount = probed.length - okCount;
  const imageCount = models.filter(m => m.is_image_model).length;
  const variantCount = models.filter(m => m.is_variant).length;
  const withUpstream = models.filter(m => m.upstream_name).length;
  const cacheAge = cache.cache_age_seconds ? Math.round(cache.cache_age_seconds / 60) : 0;
  const cacheTtl = Math.round((cache.cache_ttl_seconds || 1800) / 60);

  const html = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">模型总数</div>
        <div class="kpi-value">${data.total || 0}</div>
        <div class="kpi-sub">基础 ${data.base_count || 0} · 变体 ${variantCount} · 图像 ${imageCount}</div>
      </div>
      <div class="kpi-card success">
        <div class="kpi-label">探测可用</div>
        <div class="kpi-value">${okCount}</div>
        <div class="kpi-sub">已探测 ${probed.length} / ${models.length} · 失败 ${failCount}</div>
      </div>
      <div class="kpi-card ${withUpstream === 0 ? 'warning' : ''}">
        <div class="kpi-label">关联真实助手</div>
        <div class="kpi-value">${withUpstream}</div>
        <div class="kpi-sub">来自 chatglm.cn 实时拉取 · 孤儿助手 ${orphans.length} 个</div>
      </div>
      <div class="kpi-card ${cacheAge >= cacheTtl - 5 ? 'warning' : ''}">
        <div class="kpi-label">上游缓存</div>
        <div class="kpi-value" style="font-size:18px;">${cacheAge} 分钟</div>
        <div class="kpi-sub">TTL ${cacheTtl} 分钟 · ${cache.cached ? '已缓存' : '未缓存'}</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">统一模型列表（本地兼容模型 + 关联真实助手元数据）</div>
        <div class="panel-meta">
          <button id="models-upstream-refresh" class="btn btn-ghost btn-sm">刷新上游</button>
          <button id="models-probe-all" class="btn btn-primary btn-sm" style="margin-left:8px;">${_probingAll ? '探针中…' : '全部探针'}</button>
          <button id="models-clear-probe" class="btn btn-ghost btn-sm" style="margin-left:8px;">清除探针</button>
        </div>
      </div>
      <div class="panel-body">
        <div class="filter-bar" style="margin-bottom:12px;">
          <label>过滤:
            <input type="text" id="models-filter" placeholder="按 model id / 助手名过滤..." style="background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-light);border-radius:4px;padding:4px 8px;font-size:13px;width:240px;" />
          </label>
          <label>状态:
            <select id="models-status-filter" style="background:var(--bg-elevated);color:var(--text);border:1px solid var(--border-light);border-radius:4px;padding:4px 8px;font-size:13px;">
              <option value="all">全部</option>
              <option value="ok">✅ 可用</option>
              <option value="fail">❌ 失败</option>
              <option value="unprobed">⏳ 未探测</option>
            </select>
          </label>
          <span class="text-muted" style="font-size:11px;margin-left:auto;">main.js: ${escapeHtml(cache.main_js_url || '-')}</span>
        </div>
        <div class="table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>模型 ID</th>
                <th>基础</th>
                <th>特性</th>
                <th>类型</th>
                <th>关联上游助手</th>
                <th>最近探针</th>
                <th>延迟</th>
                <th>账号</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="models-tbody">
              ${models.map(m => renderUnifiedModelRow(m)).join('') || `<tr><td colspan="9" class="empty-state">无模型</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    ${orphans.length > 0 ? `
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">未映射助手（${orphans.length} 个，目前无法通过 OpenAI API 调用）</div>
        <div class="panel-meta">如需调用这些助手，请在 config.py 的 BUILTIN_EXPOSED_MODELS 添加对应别名</div>
      </div>
      <div class="panel-body">
        <div class="table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>头像</th>
                <th>名称</th>
                <th>assistant_id</th>
                <th>描述</th>
                <th>scope</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              ${orphans.map(a => renderOrphanAssistantRow(a)).join('')}
            </tbody>
          </table>
        </div>
      </div>
    </div>` : ''}
  `;
  document.getElementById('view-models').innerHTML = html;

  // 绑定事件
  document.getElementById('models-upstream-refresh').addEventListener('click', handleUpstreamRefresh);
  document.getElementById('models-probe-all').addEventListener('click', handleProbeAll);
  document.getElementById('models-clear-probe').addEventListener('click', handleClearProbe);
  document.getElementById('models-filter').addEventListener('input', applyModelFilter);
  document.getElementById('models-status-filter').addEventListener('change', applyModelFilter);
  document.querySelectorAll('[data-probe-model]').forEach(btn => {
    btn.addEventListener('click', () => handleProbeModel(btn.dataset.probeModel));
  });
}

function renderUnifiedModelRow(m) {
  const features = (m.features || []).map(f => `<span class="badge badge-info">${escapeHtml(f)}</span>`).join(' ');
  const typeBadge = m.is_image_model
    ? `<span class="badge badge-warning">图像</span>`
    : m.is_variant
      ? `<span class="badge badge-muted">变体</span>`
      : `<span class="badge badge-success">基础</span>`;
  // 关联上游助手（avatar + name）
  let upstreamCell;
  if (m.upstream_name) {
    const avatar = m.upstream_avatar
      ? `<img src="${escapeHtml(m.upstream_avatar)}" alt="" style="width:20px;height:20px;border-radius:50%;object-fit:cover;vertical-align:middle;margin-right:6px;" onerror="this.style.display='none';" />`
      : '';
    const enabledBadge = m.upstream_enabled
      ? ''
      : ` <span class="badge badge-muted" style="font-size:9px;">已禁用</span>`;
    upstreamCell = `${avatar}<span class="text-muted" style="font-size:12px;">${escapeHtml(m.upstream_name)}</span>${enabledBadge}`;
  } else {
    upstreamCell = '<span class="text-muted">-</span>';
  }
  // 探针徽章
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
    <tr data-model-id="${escapeHtml(m.id)}" data-probe-ok="${probe ? (probe.ok ? 'ok' : 'fail') : 'unprobed'}" data-upstream-name="${escapeHtml(m.upstream_name || '').toLowerCase()}">
      <td class="mono" style="font-size:12px;">${escapeHtml(m.id)}</td>
      <td class="mono text-muted" style="font-size:12px;">${escapeHtml(m.base)}</td>
      <td>${features || '<span class="text-muted">-</span>'}</td>
      <td>${typeBadge}</td>
      <td>${upstreamCell}</td>
      <td>${probeBadge}</td>
      <td class="mono">${latencyStr}</td>
      <td class="mono text-muted">${accountStr}</td>
      <td><button class="btn btn-ghost btn-sm" data-probe-model="${escapeHtml(m.id)}">测试</button></td>
    </tr>
  `;
}

function renderOrphanAssistantRow(a) {
  const avatarCell = a.avatar
    ? `<img src="${escapeHtml(a.avatar)}" alt="" style="width:32px;height:32px;border-radius:50%;object-fit:cover;" onerror="this.style.display='none';" />`
    : `<div style="width:32px;height:32px;border-radius:50%;background:var(--bg-elevated);display:flex;align-items:center;justify-content:center;font-size:14px;">🤖</div>`;
  const statusBadge = a.fetch_error
    ? `<span class="badge badge-error" title="${escapeHtml(a.fetch_error)}">❌ 拉取失败</span>`
    : a.enabled
      ? `<span class="badge badge-success">✅ 已启用</span>`
      : `<span class="badge badge-muted">⏸ 已禁用</span>`;
  const description = a.description
    ? escapeHtml(a.description.length > 80 ? a.description.slice(0, 80) + '...' : a.description)
    : '<span class="text-muted">-</span>';
  return `
    <tr>
      <td>${avatarCell}</td>
      <td><strong>${escapeHtml(a.name || '(无名)')}</strong></td>
      <td class="mono" style="font-size:11px;">${escapeHtml(a.assistant_id)}</td>
      <td style="font-size:12px;max-width:300px;">${description}</td>
      <td class="mono">${a.scope}</td>
      <td>${statusBadge}</td>
    </tr>
  `;
}

async function handleUpstreamRefresh() {
  const btn = document.getElementById('models-upstream-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '刷新中...'; }
  showToast('正在强制刷新上游助手列表...', 'info');
  try {
    await api('upstream_refresh', { method: 'POST', body: {} });
    showToast('✅ 上游助手列表已刷新', 'success');
    await refreshModels();
  } catch (err) {
    showToast('刷新失败: ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '刷新上游'; }
  }
}

function applyModelFilter() {
  const q = (document.getElementById('models-filter').value || '').toLowerCase().trim();
  const status = document.getElementById('models-status-filter').value;
  document.querySelectorAll('#models-tbody tr[data-model-id]').forEach(tr => {
    const id = (tr.dataset.modelId || '').toLowerCase();
    const upstreamName = (tr.dataset.upstreamName || '').toLowerCase();
    const ok = tr.dataset.probeOk;
    const matchQ = !q || id.includes(q) || upstreamName.includes(q);
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
  if (btn) { btn.disabled = false; btn.textContent = '全部探针'; }
  showToast(`批量探针完成：✅ ${okCount} 可用 / ❌ ${failCount} 失败`, failCount > 0 ? 'warning' : 'success');
  await refreshModels();
}

async function handleClearProbe() {
  // 没有专门的后端端点，直接前端重渲染（缓存还在后端，但下次 probe 会覆盖）
  if (!confirm('确认清除所有模型的探针结果？')) return;
  if (_modelsCache && _modelsCache.models) {
    _modelsCache.models = _modelsCache.models.map(m => ({ ...m, last_probe: null }));
    refreshModelsUI();
    showToast('已清除探针结果（后端缓存仍在，下次探测会覆盖）', 'info');
  }
}

// 仅刷新 UI（不重新拉 API），用于 clear probe 后立即生效
function refreshModelsUI() {
  if (!_modelsCache) return;
  const models = _modelsCache.models || [];
  const tbody = document.getElementById('models-tbody');
  if (tbody) {
    tbody.innerHTML = models.map(m => renderUnifiedModelRow(m)).join('') || `<tr><td colspan="9" class="empty-state">无模型</td></tr>`;
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
// Probe view (端点测试 - 双列布局：左 Debug 表单 + 右 API Overview)
// 参考 Qwen2API_Go debug-tab，但适配我们项目的端点结构
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
  const sorted = [...models].sort((a, b) => {
    if (a.is_variant !== b.is_variant) return a.is_variant ? 1 : -1;
    return a.id.localeCompare(b.id);
  });
  const options = sorted.map(m => {
    const tag = m.is_image_model ? '[图像] ' : (m.is_variant ? '[变体] ' : '');
    return `<option value="${escapeHtml(m.id)}">${tag}${escapeHtml(m.id)}</option>`;
  }).join('');

  document.getElementById('view-probe').innerHTML = `
    <div class="probe-layout">
      <!-- 左列：Debug 表单 + 结果 -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">调试终端</div>
          <div class="panel-meta">选择模型 + 输入消息 → 查看完整响应</div>
        </div>
        <div class="panel-body">
          <div class="probe-form">
            <!-- 4 字段网格 -->
            <div class="probe-form-grid">
              <div class="probe-form-group">
                <label class="probe-label">模型</label>
                <select id="probe-model" class="probe-input">
                  ${options || '<option value="">(无可用模型)</option>'}
                </select>
              </div>
              <div class="probe-form-group">
                <label class="probe-label">Temperature <span id="probe-temp-val" class="text-muted">0.7</span></label>
                <input type="range" id="probe-temperature" min="0" max="2" step="0.1" value="0.7" class="probe-input" />
              </div>
              <div class="probe-form-group">
                <label class="probe-label">Max Tokens</label>
                <input type="number" id="probe-max-tokens" value="512" min="1" max="8192" class="probe-input" />
              </div>
              <div class="probe-form-group">
                <label class="probe-label">Stream</label>
                <select id="probe-stream" class="probe-input">
                  <option value="false">false（同步）</option>
                  <option value="true">true（流式 - 暂未支持）</option>
                </select>
              </div>
            </div>

            <!-- System + User 双 textarea -->
            <div class="probe-form-grid-2">
              <div class="probe-form-group">
                <label class="probe-label">System Prompt</label>
                <textarea id="probe-system" class="probe-input" rows="5" placeholder="例如：你是一个简洁的助手"></textarea>
              </div>
              <div class="probe-form-group">
                <label class="probe-label">User Message</label>
                <textarea id="probe-message" class="probe-input" rows="5" placeholder="输入要发送给模型的消息...">你好，用一句话介绍你自己</textarea>
              </div>
            </div>

            <!-- 操作按钮 -->
            <div class="probe-actions">
              <button id="probe-send" class="btn btn-primary">发送请求</button>
              <button id="probe-clear" class="btn btn-ghost">清空结果</button>
              <span id="probe-status" class="text-muted" style="font-size:12px;"></span>
            </div>

            <div id="probe-error-box" class="probe-error-box" style="display:none;"></div>
          </div>

          <!-- 双列结果：模型回复 + Token Usage -->
          <div class="probe-result-grid" id="probe-result-area" style="display:none;">
            <div class="probe-form-group">
              <label class="probe-label">模型回复</label>
              <div id="probe-content-box" class="probe-content-box">发送请求后此处显示模型回复</div>
            </div>
            <div class="probe-form-group">
              <label class="probe-label">Token Usage</label>
              <div class="probe-usage-box" id="probe-usage-box">
                <div class="flex-between"><span class="text-muted">Input</span><strong id="usage-prompt">0</strong></div>
                <div class="flex-between"><span class="text-muted">Output</span><strong id="usage-completion">0</strong></div>
                <div class="flex-between"><span class="text-muted">Total</span><strong id="usage-total">0</strong></div>
                <div class="flex-between" style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border);">
                  <span class="text-muted">Model</span>
                  <strong class="mono" id="usage-model" style="font-size:11px;">-</strong>
                </div>
                <div class="flex-between">
                  <span class="text-muted">Latency</span>
                  <strong class="mono" id="usage-latency">-</strong>
                </div>
                <div class="flex-between">
                  <span class="text-muted">Account</span>
                  <strong class="mono" id="usage-account">-</strong>
                </div>
              </div>
            </div>
          </div>

          <!-- 原始 JSON -->
          <div class="probe-form-group" id="probe-raw-wrap" style="display:none;margin-top:16px;">
            <label class="probe-label">原始 JSON 响应</label>
            <pre class="probe-code" id="probe-raw-json">{ }</pre>
          </div>
        </div>
      </div>

      <!-- 右列：API Overview -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">API 端点概览</div>
          <div class="panel-meta">本项目支持的所有协议端点</div>
        </div>
        <div class="panel-body">
          <div class="probe-endpoint-list">
            ${renderEndpointItem('GET',  '/health',                       '健康检查端点')}
            ${renderEndpointItem('GET',  '/v1/models',                    'OpenAI 兼容模型列表')}
            ${renderEndpointItem('POST', '/v1/chat/completions',          'OpenAI Chat Completions（流式 + 非流式 + tools）')}
            ${renderEndpointItem('POST', '/v1/messages',                  'Anthropic Messages API（流式 + 非流式）')}
            ${renderEndpointItem('POST', '/v1/responses',                 'OpenAI Responses API（事件流）')}
            ${renderEndpointItem('POST', '/v1/completions',               'Legacy text completion')}
            ${renderEndpointItem('POST', '/v1/images/generations',        '文生图（cogView）')}
            ${renderEndpointItem('POST', '/v1/moderations',               '内容审核（启发式）')}
            ${renderEndpointItem('POST', '/v1/embeddings',                '文本 embedding（hash 投影）')}
            ${renderEndpointItem('GET',  '/admin',                        '管理面板（HTML）')}
            ${renderEndpointItem('POST', '/admin/api/login',              '管理面板登录')}
            ${renderEndpointItem('GET',  '/admin/api/dashboard',          '仪表盘聚合数据')}
            ${renderEndpointItem('GET',  '/admin/api/models',             '真实上游助手 + 本地兼容模型')}
            ${renderEndpointItem('POST', '/admin/api/probe',              '端点测试（本页使用）')}
          </div>

          <div class="probe-form-group" style="margin-top:20px;">
            <label class="probe-label">curl 示例</label>
            <pre class="probe-code" id="probe-curl-example">${generateCurlExample(sorted[0]?.id || 'glm-4-flash')}</pre>
          </div>

          <div class="probe-form-group" style="margin-top:16px;">
            <label class="probe-label">Python SDK 示例</label>
            <pre class="probe-code">from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="dummy",  # 留空 SERVER_API_KEYS 时任意填
)

resp = client.chat.completions.create(
    model="${escapeHtml(sorted[0]?.id || 'glm-4-flash')}",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)</pre>
          </div>

          <div class="probe-form-group" style="margin-top:16px;">
            <label class="probe-label">Anthropic SDK 示例</label>
            <pre class="probe-code">from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:8000/v1",
    api_key="dummy",
)

resp = client.messages.create(
    model="${escapeHtml(sorted[0]?.id || 'glm-4-flash')}",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.content[0].text)</pre>
          </div>
        </div>
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
    document.getElementById('probe-result-area').style.display = 'none';
    document.getElementById('probe-raw-wrap').style.display = 'none';
    document.getElementById('probe-error-box').style.display = 'none';
  });
  // 模型选择变化时更新 curl 示例
  document.getElementById('probe-model').addEventListener('change', e => {
    const curlEl = document.getElementById('probe-curl-example');
    if (curlEl) curlEl.textContent = generateCurlExample(e.target.value);
  });
}

function renderEndpointItem(method, path, summary) {
  const methodClass = {
    GET: 'endpoint-method-get',
    POST: 'endpoint-method-post',
    DELETE: 'endpoint-method-delete',
    PUT: 'endpoint-method-put',
  }[method] || 'endpoint-method-post';
  return `
    <div class="probe-endpoint">
      <div class="probe-endpoint-head">
        <span class="endpoint-method ${methodClass}">${method}</span>
        <code class="endpoint-path">${escapeHtml(path)}</code>
      </div>
      <p class="probe-endpoint-summary">${escapeHtml(summary)}</p>
    </div>
  `;
}

function generateCurlExample(modelId) {
  const m = modelId || 'glm-4-flash';
  return `curl -X POST http://127.0.0.1:8000/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer dummy" \\
  -d '{
    "model": "${m}",
    "stream": false,
    "temperature": 0.7,
    "max_tokens": 512,
    "messages": [
      {"role": "user", "content": "你好，用一句话介绍你自己"}
    ]
  }'`;
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
  const errorBox = document.getElementById('probe-error-box');

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
  errorBox.style.display = 'none';
  document.getElementById('probe-result-area').style.display = 'none';
  document.getElementById('probe-raw-wrap').style.display = 'none';

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
    errorBox.style.display = '';
    errorBox.textContent = '❌ ' + err.message;
    showToast('请求失败: ' + err.message, 'error');
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = '发送请求';
  }
}

function renderProbeResult(result, payload, elapsed) {
  const resultArea = document.getElementById('probe-result-area');
  const rawWrap = document.getElementById('probe-raw-wrap');
  resultArea.style.display = '';
  rawWrap.style.display = '';

  // 填充 Token Usage
  const usage = (result.response && result.response.usage) || {};
  document.getElementById('usage-prompt').textContent = usage.prompt_tokens || 0;
  document.getElementById('usage-completion').textContent = usage.completion_tokens || 0;
  document.getElementById('usage-total').textContent = usage.total_tokens || 0;
  document.getElementById('usage-model').textContent = (result.response && result.response.model) || payload.model || '-';
  document.getElementById('usage-latency').textContent = `${result.latency_ms}ms / ${elapsed}ms (端到端)`;
  document.getElementById('usage-account').textContent = result.account_index >= 0 ? `#${result.account_index}` : '-';

  // 填充模型回复
  const contentBox = document.getElementById('probe-content-box');
  if (!result.ok) {
    contentBox.innerHTML = `<span class="text-error">❌ ${escapeHtml(result.error || '未知错误')}</span>`;
  } else {
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
    let html = '';
    if (content.trim()) {
      html += `<div class="probe-content-block"><div class="probe-content-label">响应内容</div><div class="probe-content-text">${escapeHtml(content.trim())}</div></div>`;
    }
    if (reasoning.trim()) {
      html += `<div class="probe-content-block"><div class="probe-content-label">思维链 (reasoning_content)</div><pre class="probe-content-pre">${escapeHtml(reasoning.trim())}</pre></div>`;
    }
    if (toolCalls.trim()) {
      html += `<div class="probe-content-block"><div class="probe-content-label">工具调用</div><pre class="probe-content-pre">${escapeHtml(toolCalls.trim())}</pre></div>`;
    }
    if (!html) html = '<span class="text-muted">(空响应)</span>';
    contentBox.innerHTML = html;
  }

  // 填充原始 JSON
  document.getElementById('probe-raw-json').textContent = JSON.stringify(result.response, null, 2);
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
