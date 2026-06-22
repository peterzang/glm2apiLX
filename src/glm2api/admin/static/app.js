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
  let resp;
  try {
    resp = await fetch(`${API_BASE}/${name}`, {
      method: opts.method || 'GET',
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch (networkErr) {
    // 网络错误：服务可能挂了，显示横幅提示
    showNetworkError('网络连接失败：服务可能未运行');
    throw networkErr;
  }
  if (resp.status === 401) {
    clearToken();
    showLogin();
    throw new Error('unauthorized');
  }
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    // 优先用后端返回的 message（更详细），其次用 error code，最后 fallback 到 HTTP status
    const msg = (data && data.message) || (data && data.error) || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  // 成功 → 隐藏网络错误横幅
  hideNetworkError();
  return data;
}

// =========================================================================
// Network error banner (网络错误提示条)
// =========================================================================
function showNetworkError(msg) {
  let banner = document.getElementById('network-error-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'network-error-banner';
    banner.className = 'network-error-banner';
    document.body.appendChild(banner);
  }
  banner.innerHTML = `<span>⚠</span><span>${escapeHtml(msg)}</span>`;
  banner.style.display = 'flex';
}
function hideNetworkError() {
  const banner = document.getElementById('network-error-banner');
  if (banner) banner.style.display = 'none';
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
// Neural Network Background Animation (神经网络背景动画)
// 纯 Canvas 实现，~30 节点浮动 + 距离阈值连线 + 节点呼吸
// =========================================================================
const NeuralBg = (() => {
  let canvas, ctx, nodes = [], animationId = null, mouseX = -1000, mouseY = -1000;

  function init() {
    canvas = document.getElementById('neural-bg');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    resize();
    window.addEventListener('resize', resize);
    document.addEventListener('mousemove', e => {
      mouseX = e.clientX;
      mouseY = e.clientY;
    });
    document.addEventListener('mouseleave', () => {
      mouseX = -1000;
      mouseY = -1000;
    });
    createNodes();
    start();
  }

  function resize() {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    canvas.style.width = window.innerWidth + 'px';
    canvas.style.height = window.innerHeight + 'px';
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
  }

  function createNodes() {
    nodes = [];
    const count = 32;
    const w = window.innerWidth, h = window.innerHeight;
    for (let i = 0; i < count; i++) {
      nodes.push({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.3,
        vy: (Math.random() - 0.5) * 0.3,
        r: 1.5 + Math.random() * 1.5,
        pulse: Math.random() * Math.PI * 2,
      });
    }
  }

  function getNodeColor() {
    return getComputedStyle(document.documentElement).getPropertyValue('--neural-node').trim() || 'rgba(139, 92, 246, 0.45)';
  }
  function getLineColor() {
    return getComputedStyle(document.documentElement).getPropertyValue('--neural-line').trim() || 'rgba(99, 102, 241, 0.18)';
  }

  function animate() {
    if (!ctx || !canvas) return;
    const w = window.innerWidth, h = window.innerHeight;
    ctx.clearRect(0, 0, w, h);

    const lineColor = getLineColor();
    const nodeColor = getNodeColor();

    // 画连线
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 180) {
          const alpha = (1 - dist / 180) * 0.6;
          ctx.strokeStyle = applyAlpha(lineColor, alpha);
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          ctx.moveTo(nodes[i].x, nodes[i].y);
          ctx.lineTo(nodes[j].x, nodes[j].y);
          ctx.stroke();
        }
      }
      // 鼠标连线（更亮）
      const mdx = nodes[i].x - mouseX;
      const mdy = nodes[i].y - mouseY;
      const mdist = Math.sqrt(mdx * mdx + mdy * mdy);
      if (mdist < 220) {
        const alpha = (1 - mdist / 220) * 0.9;
        ctx.strokeStyle = applyAlpha(lineColor, alpha);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(nodes[i].x, nodes[i].y);
        ctx.lineTo(mouseX, mouseY);
        ctx.stroke();
      }
    }

    // 画节点 + 移动 + 呼吸
    for (const node of nodes) {
      node.x += node.vx;
      node.y += node.vy;
      if (node.x < 0 || node.x > w) node.vx *= -1;
      if (node.y < 0 || node.y > h) node.vy *= -1;
      node.pulse += 0.02;
      const r = Math.max(0.5, node.r + Math.sin(node.pulse) * 0.8);
      ctx.fillStyle = nodeColor;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      ctx.fill();
      // 光晕
      ctx.fillStyle = applyAlpha(nodeColor, 0.15);
      ctx.beginPath();
      ctx.arc(node.x, node.y, r * 3, 0, Math.PI * 2);
      ctx.fill();
    }

    animationId = requestAnimationFrame(animate);
  }

  function applyAlpha(rgbaStr, alpha) {
    // 形如 "rgba(99, 102, 241, 0.18)" → 替换最后一个数字
    const m = rgbaStr.match(/rgba?\(([^)]+)\)/);
    if (!m) return rgbaStr;
    const parts = m[1].split(',').map(s => s.trim());
    if (parts.length >= 3) {
      return `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${alpha})`;
    }
    return rgbaStr;
  }

  function start() {
    if (animationId) return;
    animate();
  }
  function stop() {
    if (animationId) cancelAnimationFrame(animationId);
    animationId = null;
  }

  return { init, start, stop };
})();

// =========================================================================
// Theme Toggle (亮色/暗色主题切换)
// =========================================================================
const Theme = (() => {
  const STORAGE_KEY = 'glm2api_admin_theme';
  function init() {
    const saved = localStorage.getItem(STORAGE_KEY) || 'dark';
    applyTheme(saved);
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', toggle);
  }
  function toggle() {
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    localStorage.setItem(STORAGE_KEY, next);
    showToast(`已切换到${next === 'dark' ? '暗色' : '亮色'}主题`, 'info');
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const icon = document.getElementById('theme-icon');
    if (icon) icon.textContent = theme === 'dark' ? '🌙' : '☀️';
  }
  return { init, toggle };
})();

// =========================================================================
// Mobile sidebar toggle
// =========================================================================
function initMobileMenu() {
  const btn = document.getElementById('mobile-menu-btn');
  const sidebar = document.getElementById('sidebar');
  if (!btn || !sidebar) return;
  btn.addEventListener('click', () => sidebar.classList.toggle('open'));
  // 点击导航项后关闭侧边栏
  document.querySelectorAll('.nav-item').forEach(a => {
    a.addEventListener('click', () => {
      if (window.innerWidth <= 768) sidebar.classList.remove('open');
    });
  });
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
    startAutoRefresh();
  } catch (err) {
    errEl.textContent = '登录失败：' + err.message;
    errEl.hidden = false;
  }
}

async function handleLogout() {
  try { await api('logout', { method: 'POST' }); } catch (_) {}
  stopAutoRefresh();
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
  apikeys: 'API 管理',
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
  // v43: 配色叙事 — 切换页面时设置 data-page 属性
  document.body.setAttribute('data-page', name);
  // v49: 宠物只在仪表盘显示
  const pet = document.getElementById('pet-mascot');
  if (pet) {
    pet.style.display = (name === 'dashboard') ? 'block' : 'none';
  }
  refresh();
}

// =========================================================================
// v43: NeuralPulse 签名组件 — glm2api 专属神经网络脉冲动画
// =========================================================================

class NeuralPulse {
  /**
   * glm2api 专属签名组件：3x3 节点网格 + 动态连线 + 脉冲传播。
   * 活跃时节点呼吸 + 脉冲沿线传播；空闲时静态低亮度。
   */
  constructor(canvas, options = {}) {
    this.canvas = typeof canvas === 'string' ? document.querySelector(canvas) : canvas;
    if (!this.canvas || !this.canvas.getContext) return;
    this.ctx = this.canvas.getContext('2d');
    this.active = options.active ?? false;
    this.color = options.color || '#6366f1';
    this.size = options.size || 48;
    this.canvas.width = this.size;
    this.canvas.height = this.size;
    this.nodes = [];
    this.pulses = [];
    this._initNodes();
    this._animate = this._animate.bind(this);
    this._animate();
  }

  _initNodes() {
    const s = this.size;
    const step = s / 4;
    for (let i = 0; i < 3; i++) {
      for (let j = 0; j < 3; j++) {
        this.nodes.push({
          x: step + j * step,
          y: step + i * step,
          glow: 0,
          row: i,
          col: j,
        });
      }
    }
  }

  trigger() {
    // 请求时调用：从节点 0 发脉冲到节点 4（中心）
    if (this.pulses.length < 3) {
      const from = Math.floor(Math.random() * 9);
      const to = 4; // 中心节点
      if (from !== to) {
        this.pulses.push({ from, to, progress: 0, speed: 0.02 + Math.random() * 0.02 });
      }
    }
  }

  _animate() {
    const ctx = this.ctx;
    if (!ctx) return;
    const s = this.size;
    ctx.clearRect(0, 0, s, s);
    const time = Date.now() / 1000;
    // 连线
    ctx.strokeStyle = this.color + '20';
    ctx.lineWidth = 0.5;
    for (let i = 0; i < this.nodes.length; i++) {
      for (let j = i + 1; j < this.nodes.length; j++) {
        const a = this.nodes[i], b = this.nodes[j];
        // 只连相邻节点
        if (Math.abs(a.row - b.row) > 1 || Math.abs(a.col - b.col) > 1) continue;
        if (Math.abs(a.row - b.row) + Math.abs(a.col - b.col) > 1) continue;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
    // 脉冲传播
    this.pulses = this.pulses.filter(p => {
      p.progress += p.speed;
      if (p.progress >= 1) return false;
      const from = this.nodes[p.from];
      const to = this.nodes[p.to];
      const px = from.x + (to.x - from.x) * p.progress;
      const py = from.y + (to.y - from.y) * p.progress;
      ctx.fillStyle = this.color;
      ctx.beginPath();
      ctx.arc(px, py, 1.5, 0, Math.PI * 2);
      ctx.fill();
      return true;
    });
    // 节点
    this.nodes.forEach((n, i) => {
      const breathe = this.active ? (0.5 + 0.5 * Math.sin(time * 2 + i * 0.5)) : 0.2;
      const r = 1.5 + breathe * 0.8;
      const alpha = this.active ? (0.3 + breathe * 0.5) : 0.15;
      ctx.fillStyle = this.color + Math.round(alpha * 255).toString(16).padStart(2, '0');
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();
      if (this.active && breathe > 0.7) {
        ctx.fillStyle = this.color + '15';
        ctx.beginPath();
        ctx.arc(n.x, n.y, r * 2.5, 0, Math.PI * 2);
        ctx.fill();
      }
    });
    requestAnimationFrame(this._animate);
  }
}

// =========================================================================
// Refresh loop
// =========================================================================

let refreshTimer = null;
// 自动刷新间隔：5 秒
// 用户需求：仪表盘所有数据应该是实时的——停留页面就能看到数据更新，
// 不需要切到其他标签页再切回来。
// 5 秒足够实时（人眼能感知的"刷新"节奏），又不会过载服务器。
// 注：之前误改成 1 天是错的，因为用户当时反馈的"Models 一直在涨"
//   根因是 /health 探活被误归类，已在 v24 修复，与刷新间隔无关。
const AUTO_REFRESH_INTERVAL_MS = 5000;
function startAutoRefresh() {
  stopAutoRefresh();
  if (document.getElementById('auto-refresh').checked) {
    refreshTimer = setInterval(() => refresh(), AUTO_REFRESH_INTERVAL_MS);
  }
}
function stopAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
}

async function refresh(force = false) {
  if (!getToken()) return;
  // v32 修复：自动刷新破坏表单 bug
  // 用户反馈：添加账号时填 token、创建 API key、端点测试选模式时，
  // 5 秒自动刷新会把整个页面 innerHTML 替换掉，导致表单消失/输入丢失。
  // 修复：自动刷新（非强制）时，如果检测到用户正在与表单交互，跳过本次刷新。
  // 强制刷新（用户点"刷新"按钮）不受此限制。
  if (!force && _isUserInteractingWithForm()) {
    return;  // 跳过本次自动刷新，等下一个周期
  }
  // 强制刷新时重置 dashboard 哈希，绕过增量更新跳过逻辑
  if (force) _lastDashboardHash = '';
  try {
    switch (currentView) {
      case 'dashboard': await refreshDashboard(); break;
      case 'accounts': await refreshAccounts(); break;
      case 'models': await refreshModels(); break;
      case 'probe': await refreshProbe(); break;
      case 'logs': await refreshLogs(); break;
      case 'rotates': await refreshRotates(); break;
      case 'config': await refreshConfig(); break;
      case 'apikeys': await refreshApiKeys(); break;
      case 'system': await refreshSystem(); break;
    }
  } catch (err) {
    if (err.message !== 'unauthorized') {
      console.error('refresh failed', err);
    }
  }
}

/**
 * 检测用户是否正在与当前页面的表单交互。
 *
 * 检测条件（任一满足即返回 true，跳过自动刷新）：
 * 1. 当前页面有 input/textarea/select 获得焦点（正在输入）
 * 2. 当前页面有可见的表单/对话框打开（如添加账号表单、创建 API key 对话框）
 * 3. 当前页面有打开的 <details> 元素（探针高级选项等）
 *
 * 这样用户在填表单时不会被 5 秒自动刷新打断。
 * Dashboard 页面没有表单，不受影响，仍会正常实时刷新。
 */
function _isUserInteractingWithForm() {
  const activeEl = document.activeElement;
  if (!activeEl) return false;
  const tag = activeEl.tagName.toLowerCase();
  // 条件 1：input/textarea/select 获得焦点
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    return true;
  }
  // 条件 2：contenteditable 元素获得焦点
  if (activeEl.isContentEditable) {
    return true;
  }
  // 条件 3：检测当前 view 是否有打开的表单/对话框
  // 添加账号表单：#account-add-form 且 display 不为 none
  const addAccountForm = document.getElementById('account-add-form');
  if (addAccountForm && addAccountForm.style.display !== 'none') {
    return true;
  }
  // API key 创建表单：#apikey-create-form 且 display 不为 none
  const apikeyForm = document.getElementById('apikey-create-form');
  if (apikeyForm && apikeyForm.style.display !== 'none') {
    return true;
  }
  // API key 创建对话框：检查是否有 .modal 或 .dialog 可见
  const openModal = document.querySelector('.modal:not(.hidden), .dialog:not(.hidden)');
  if (openModal) {
    return true;
  }
  return false;
}

// =========================================================================
// Dashboard view（参考 Qwen2API_Go overview-tab 重写：2 行 4 列 KPI + 主图表 + 侧边卡片 + 底部 4 卡）
// =========================================================================

// 缓存上一次的 dashboard 数据，用于增量更新与脉冲动画
let _lastDashboardSnapshot = null;
let _lastDashboardHash = '';

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

  // === 增量更新策略 ===
  // 1) 计算当前快照 hash
  const snapshot = {
    total: all.total, success: all.success, ce: all.client_errors, se: all.server_errors,
    apiTotal: all.api_total, modelsTotal: all.models_total, otherTotal: all.other_total,
    sr: r5.success_rate, sr_s: r5.success, sr_t: r5.total,
    r5Api: r5.api_total, r5Models: r5.models_total, r5Other: r5.other_total, r5Health: r5.health_total,
    r5ApiSR: r5.api_success_rate,
    rpm, avgRpm, peakRpm, requests30m,
    tp: tokenTotals.prompt, tc: tokenTotals.completion, tt: tokenTotals.total,
    t30p: token30m.prompt, t30c: token30m.completion,
    aa: accountsActive, at: accountsTotal, uptime: d.uptime_seconds,
    p50: r5.p50_ms, p95: r5.p95_ms, p99: r5.p99_ms,
    pb: protoBreakdown, top_models: d.top_models,
    repetition: d.repetition, model_latencies: d.model_latencies,
    hourly_len: (d.hourly || []).length,
    hourly_last_total: (d.hourly || []).slice(-1)[0]?.total || 0,
  };
  const newHash = JSON.stringify(snapshot);
  const isFirstRender = _lastDashboardHash === '';
  const dataChanged = newHash !== _lastDashboardHash;
  _lastDashboardHash = newHash;
  _lastDashboardSnapshot = d;

  // 2) 如果数据没变，跳过重渲染（避免闪烁 + 减少 CPU 占用）
  if (!isFirstRender && !dataChanged) return;

  // === 变量定义（v44 修复：Bento Grid 改造时遗漏的变量定义）===
  const apiTotal = all.api_total ?? 0;
  const apiSuccess = all.api_success ?? 0;
  const apiClientErr = all.api_client_errors ?? 0;
  const apiServerErr = all.api_server_errors ?? 0;
  const modelsTotal = all.models_total ?? 0;
  const modelsSuccess = all.models_success ?? 0;
  const otherTotal = all.other_total ?? 0;
  const r5Api = r5.api_total ?? 0;
  const r5Models = r5.models_total ?? 0;
  const r5Other = r5.other_total ?? 0;
  const r5Health = r5.health_total ?? 0;

  // === v47: 全新仪表盘 — 圆形核心仪表 + 流体数据栏 ===
  const apiSuccessRate = r5.api_success_rate ?? 0;
  const sr = r5.success_rate || 0;
  const srColor = sr >= 95 ? 'var(--success)' : sr >= 80 ? 'var(--warning)' : 'var(--error)';
  const apiSRColor = apiSuccessRate >= 95 ? 'var(--success)' : apiSuccessRate >= 80 ? 'var(--warning)' : 'var(--error)';
  // 圆环 SVG 参数
  const gaugeR = 72, gaugeC = 2 * Math.PI * gaugeR;
  const gaugeOffset = gaugeC * (1 - sr / 100);

  const dashHero = `
    <div class="dash-hero">
      <!-- 左侧统计 -->
      <div class="dash-hero-left">
        <div class="dash-stat">
          <div class="dash-stat-label">API 请求</div>
          <div class="dash-stat-value" style="color:var(--primary-glow);">${apiTotal.toLocaleString()}</div>
          <div class="dash-stat-sub">成功 ${apiSuccess} · 4xx ${apiClientErr} · 5xx ${apiServerErr}</div>
        </div>
        <div class="dash-stat">
          <div class="dash-stat-label">当前 RPM</div>
          <div class="dash-stat-value">${rpm}</div>
          <div class="dash-stat-sub">30m 均值 ${avgRpm} · 峰值 ${peakRpm}</div>
        </div>
      </div>
      <!-- 中心圆环仪表 + NeuralPulse -->
      <div class="dash-gauge">
        <svg viewBox="0 0 180 180">
          <circle cx="90" cy="90" r="${gaugeR}" fill="none" stroke="var(--glass-border)" stroke-width="6"/>
          <circle cx="90" cy="90" r="${gaugeR}" fill="none" stroke="${srColor}" stroke-width="6"
            stroke-dasharray="${gaugeC}" stroke-dashoffset="${gaugeOffset}" stroke-linecap="round"
            style="transition:stroke-dashoffset 0.8s ease;"/>
        </svg>
        <div class="dash-gauge-center">
          <div class="dash-gauge-value" data-metric="success-rate" style="color:${srColor};">${sr.toFixed(1)}%</div>
          <div class="dash-gauge-label">5 分钟成功率</div>
        </div>
        <div class="dash-gauge-pulse">
          <canvas class="neural-pulse-canvas" id="bento-neural-pulse" width="120" height="120" style="width:48px;height:48px;"></canvas>
        </div>
      </div>
      <!-- 右侧统计 -->
      <div class="dash-hero-right">
        <div class="dash-stat">
          <div class="dash-stat-label">Models 请求</div>
          <div class="dash-stat-value" style="color:var(--purple);">${modelsTotal.toLocaleString()}</div>
          <div class="dash-stat-sub">成功 ${modelsSuccess} · 近 5 分钟 ${r5Models}</div>
        </div>
        <div class="dash-stat">
          <div class="dash-stat-label">P95 延迟</div>
          <div class="dash-stat-value" style="color:${r5.p95_ms > 5000 ? 'var(--error)' : r5.p95_ms > 2000 ? 'var(--warning)' : 'var(--success)'};">${r5.p95_ms}ms</div>
          <div class="dash-stat-sub">P50 ${r5.p50_ms}ms · P99 ${r5.p99_ms}ms</div>
        </div>
      </div>
    </div>
  `;

  // 流体数据栏（无方框，用分隔线）
  const maxRpm = Math.max(peakRpm, 1);
  const maxTokens = Math.max(tokenTotals.total, 1);
  const maxAccounts = Math.max(accountsTotal, 1);
  const maxUptime = Math.max(d.uptime_seconds, 1);
  const dashFlow = `
    <div class="dash-flow">
      <div class="dash-flow-item">
        <div class="dash-flow-label">API 成功率</div>
        <div class="dash-flow-value" style="color:${apiSRColor};">${apiSuccessRate.toFixed(1)}%</div>
        <div class="dash-flow-sub">近 5 分钟 ${r5Api} 次调用</div>
        <div class="dash-flow-bar"><div class="dash-flow-bar-fill" style="width:${apiSuccessRate}%;background:${apiSRColor};"></div></div>
      </div>
      <div class="dash-flow-item">
        <div class="dash-flow-label">活跃账号</div>
        <div class="dash-flow-value" style="color:var(--success);">${accountsActive}<span style="font-size:13px;opacity:0.5;"> / ${accountsTotal}</span></div>
        <div class="dash-flow-sub">已使用的游客/用户账号</div>
        <div class="dash-flow-bar"><div class="dash-flow-bar-fill" style="width:${(accountsActive/maxAccounts)*100}%;background:var(--success);"></div></div>
      </div>
      <div class="dash-flow-item">
        <div class="dash-flow-label">Token 累计</div>
        <div class="dash-flow-value" style="color:var(--purple);">${fmtCompactNum(tokenTotals.total)}</div>
        <div class="dash-flow-sub">P ${fmtCompactNum(tokenTotals.prompt)} · C ${fmtCompactNum(tokenTotals.completion)} · 30m ${fmtCompactNum(token30m.total)}</div>
        <div class="dash-flow-bar"><div class="dash-flow-bar-fill" style="width:${Math.min(100,(token30m.total/maxTokens)*100)}%;background:var(--purple);"></div></div>
      </div>
      <div class="dash-flow-item">
        <div class="dash-flow-label">运行时长</div>
        <div class="dash-flow-value" style="color:var(--warning);">${fmtDuration(d.uptime_seconds)}</div>
        <div class="dash-flow-sub">自 ${fmtTime(d.now - d.uptime_seconds)} · 累计 ${all.total} 请求</div>
        <div class="dash-flow-bar"><div class="dash-flow-bar-fill" style="width:${Math.min(100,(d.uptime_seconds/86400)*100)}%;background:var(--warning);"></div></div>
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
    <div class="kpi-grid" style="margin-top:20px;">
      ${renderRepetitionCard(d.repetition)}
      ${renderModelLatencyCard(d.model_latencies)}
    </div>
  `;

  document.getElementById('view-dashboard').innerHTML = dashHero + dashFlow + mainArea + bottomCards;

  // v47: 初始化 NeuralPulse 组件（圆环底部，48px 精致版）
  const pulseCanvas = document.getElementById('bento-neural-pulse');
  if (pulseCanvas) {
    const np = new NeuralPulse(pulseCanvas, {
      active: rpm > 0 || r5Api > 0,
      color: getComputedStyle(document.documentElement).getPropertyValue('--page-accent-glow').trim() || '#818cf8',
      size: 120,  /* canvas 内部分辨率 120，CSS 缩放到 48px */
    });
    // 当有 API 请求时触发脉冲
    if (r5Api > 0) {
      np.trigger();
      setInterval(() => np.trigger(), 3000 + Math.random() * 2000);
    }
  }

  // 用户反馈：数据刷新时不要出现"方框亮一下"的脉冲动画，
  // 数字直接更新即可，不要任何视觉特效。

  // v49: 更新电子宠物情绪
  updatePetMood({
    successRate: sr,
    apiSuccessRate: apiSuccessRate,
    rpm: rpm,
    p95: r5.p95_ms,
    hasApiTraffic: r5Api > 0,
    accountsActive: accountsActive,
  });
}

// =========================================================================
// v49: 萌系电子宠物 — 根据仪表盘数据变化情绪
// =========================================================================

let _petMood = 'idle';
const _petMessages = {
  happy:    ['API 全部成功！我好开心~', '一切正常运行中~', '成功率满分！耶~'],
  sad:      ['呜...有请求失败了...', '5xx 错误让我难过...', '成功率下降了，我会盯着的...'],
  surprised:['哇！流量突然好大！', 'RM 好高！忙不过来了！', '延迟突然变高了！'],
  sleeping: ['没有请求，我先睡一会儿~', '安静的时候最适合打盹~', '等有人来调用我再醒~'],
  thinking: ['让我想想这个请求...', '正在思考中...', '嗯...延迟有点高...'],
  idle:     ['我在守护你的 API~', '你好呀~', '随时待命！'],
};

function updatePetMood(data) {
  const pet = document.getElementById('pet-mascot');
  if (!pet) return;
  // v49: 宠物只在仪表盘显示，其他页面隐藏
  if (currentView !== 'dashboard') {
    pet.style.display = 'none';
    return;
  }
  pet.style.display = 'block';

  // 根据数据决定情绪
  let mood = 'idle';
  let msg = '';

  if (data.successRate < 80 || data.apiSuccessRate < 80) {
    mood = 'sad';
    msg = _petMessages.sad[Math.floor(Math.random() * _petMessages.sad.length)];
  } else if (data.rpm > 20 || data.p95 > 5000) {
    mood = 'surprised';
    msg = _petMessages.surprised[Math.floor(Math.random() * _petMessages.surprised.length)];
  } else if (data.p95 > 2000) {
    mood = 'thinking';
    msg = _petMessages.thinking[Math.floor(Math.random() * _petMessages.thinking.length)];
  } else if (data.successRate >= 95 && data.hasApiTraffic) {
    mood = 'happy';
    msg = _petMessages.happy[Math.floor(Math.random() * _petMessages.happy.length)];
  } else if (!data.hasApiTraffic && data.rpm === 0) {
    mood = 'sleeping';
    msg = _petMessages.sleeping[Math.floor(Math.random() * _petMessages.sleeping.length)];
  } else {
    mood = 'idle';
    msg = _petMessages.idle[Math.floor(Math.random() * _petMessages.idle.length)];
  }

  // 如果情绪没变，不重复更新
  if (mood === _petMood) return;
  _petMood = mood;

  // 移除所有情绪 class
  pet.classList.remove('happy', 'sad', 'surprised', 'sleeping', 'thinking', 'idle');
  pet.classList.add(mood);

  // 更新嘴巴形状
  const mouth = document.getElementById('pet-mouth');
  if (mouth) {
    if (mood === 'happy') {
      mouth.setAttribute('d', 'M27 32 Q32 38 37 32');  // 大笑
    } else if (mood === 'sad') {
      mouth.setAttribute('d', 'M27 35 Q32 31 37 35');  // 倒过来的嘴
    } else if (mood === 'surprised') {
      mouth.setAttribute('d', 'M32 34 m-2 0 a2 2 0 1 0 4 0 a2 2 0 1 0 -4 0');  // O 型嘴
    } else if (mood === 'sleeping') {
      mouth.setAttribute('d', 'M29 34 L35 34');  // 一条线
    } else {
      mouth.setAttribute('d', 'M28 33 Q32 36 36 33');  // 默认微笑
    }
  }

  // 更新气泡文字
  const bubble = document.getElementById('pet-bubble');
  if (bubble) {
    bubble.textContent = msg;
  }
}

// === 复读率监控卡片（v3 审核报告建议）===
function renderRepetitionCard(rep) {
  rep = rep || {};
  const total = rep.total_events || 0;
  const recent24h = rep.recent_24h_count || 0;
  const byModel = rep.by_model || {};
  const byPath = rep.by_path || {};
  const tone = total > 0 ? 'warning' : 'success';
  const modelRows = Object.entries(byModel).map(([m, c]) =>
    `<div class="flex-between"><span class="mono text-muted" style="font-size:11px;">${escapeHtml(m)}</span><strong>${c}</strong></div>`
  ).join('') || '<div class="text-muted">暂无</div>';
  const pathRows = Object.entries(byPath).map(([p, c]) =>
    `<div class="flex-between"><span class="text-muted">${escapeHtml(p === 'stream' ? '流式' : '非流式')}</span><strong>${c}</strong></div>`
  ).join('') || '<div class="text-muted">暂无</div>';
  return `
    <div class="kpi-card ${tone}">
      <div class="kpi-label" style="margin-bottom:12px;">复读检测</div>
      <div style="display:flex;flex-direction:column;gap:8px;font-size:13px;">
        <div class="flex-between"><span class="text-muted">总触发次数</span><strong class="mono ${total > 0 ? 'text-warning' : 'text-success'}">${total}</strong></div>
        <div class="flex-between"><span class="text-muted">最近 24h</span><strong class="mono">${recent24h}</strong></div>
        <div style="margin-top:6px;padding-top:8px;border-top:1px solid var(--border);">
          <div class="text-muted" style="font-size:11px;margin-bottom:4px;">按模型</div>
          ${modelRows}
        </div>
        <div>
          <div class="text-muted" style="font-size:11px;margin-bottom:4px;">按路径</div>
          ${pathRows}
        </div>
      </div>
    </div>
  `;
}

// === 按模型延迟统计卡片（v3 审核报告建议）===
function renderModelLatencyCard(modelLatencies) {
  modelLatencies = modelLatencies || {};
  const entries = Object.entries(modelLatencies).sort((a, b) => (a[1].avg_ms || 0) - (b[1].avg_ms || 0));
  if (entries.length === 0) {
    return `
      <div class="kpi-card">
        <div class="kpi-label" style="margin-bottom:12px;">按模型延迟</div>
        <div class="text-muted" style="font-size:13px;">暂无数据（发送请求后显示）</div>
      </div>
    `;
  }
  const rows = entries.slice(0, 8).map(([model, lat]) => {
    const tone = lat.avg_ms < 2000 ? 'text-success' : lat.avg_ms < 5000 ? 'text-warning' : 'text-error';
    return `
      <div style="margin-bottom:8px;">
        <div class="flex-between" style="margin-bottom:2px;">
          <span class="mono text-muted" style="font-size:11px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(model)}">${escapeHtml(model)}</span>
          <strong class="mono ${tone}" style="font-size:12px;">${lat.avg_ms}ms</strong>
        </div>
        <div class="text-muted" style="font-size:10px;">P50 ${lat.p50_ms}ms · P95 ${lat.p95_ms}ms · ${lat.count} 次</div>
      </div>
    `;
  }).join('');
  return `
    <div class="kpi-card">
      <div class="kpi-label" style="margin-bottom:12px;">按模型平均延迟（Top ${Math.min(entries.length, 8)}）</div>
      <div>${rows}</div>
    </div>
  `;
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
  const labels = { chat: 'Chat 对话', models: 'Models 元信息', images: 'Images 图像', embeddings: 'Embeddings', moderations: 'Moderations', health: '健康检查', other: '其他' };
  const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4', '#ef4444', '#64748b'];
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
        <div class="flex-between"><span class="text-muted">健康检查</span><strong>${pb.health || 0}</strong></div>
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
        <div class="kpi-sub">系统自动获取的临时账号</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">用户账号</div>
        <div class="kpi-value">${accs.filter(a => !a.is_guest).length}</div>
        <div class="kpi-sub">已添加并验证通过的账号</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">账号详情</div>
        <div class="panel-meta">
          <button id="account-add-btn" class="btn btn-primary btn-sm">+ 添加用户账号</button>
        </div>
      </div>
      <div class="panel-body">
        <div id="account-add-form" style="display:none;margin-bottom:16px;gap:10px;align-items:center;" class="flex">
          <input type="text" id="account-refresh-token" placeholder="输入 GLM refresh_token（从 chatglm.cn 获取）" class="probe-input" style="width:400px;" />
          <button id="account-add-confirm" class="btn btn-primary btn-sm">确认添加</button>
          <button id="account-add-cancel" class="btn btn-ghost btn-sm">取消</button>
        </div>
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

  // Bind add account button
  const addBtn = document.getElementById('account-add-btn');
  const addForm = document.getElementById('account-add-form');
  const addConfirm = document.getElementById('account-add-confirm');
  const addCancel = document.getElementById('account-add-cancel');
  const tokenInput = document.getElementById('account-refresh-token');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      addForm.style.display = 'flex';
      tokenInput.focus();
    });
  }
  if (addCancel) {
    addCancel.addEventListener('click', () => {
      addForm.style.display = 'none';
      tokenInput.value = '';
    });
  }
  if (addConfirm) {
    addConfirm.addEventListener('click', async () => {
      const refreshToken = tokenInput.value.trim();
      if (!refreshToken) { showToast('请输入 refresh_token', 'error'); return; }
      // 拒绝明显无效的输入：游客占位符 / 太短
      if (refreshToken.length < 20) {
        showToast('refresh_token 格式不正确（长度过短）', 'error');
        return;
      }
      // 进入"验证中"状态：禁用按钮 + 改文案
      const origText = addConfirm.textContent;
      addConfirm.disabled = true;
      addConfirm.textContent = '正在验证 token…';
      try {
        const result = await api('accounts/add', { method: 'POST', body: { refresh_token: refreshToken } });
        showToast(`✅ ${result.message || 'token 验证通过'}（账号 #${result.index}，总账号数 ${result.total_accounts}）`, 'success');
        addForm.style.display = 'none';
        tokenInput.value = '';
        refreshAccounts();
      } catch (err) {
        // 后端返回 token_invalid / missing_refresh_token 等，message 字段含具体原因
        showToast(`❌ 添加失败：${err.message}`, 'error');
      } finally {
        addConfirm.disabled = false;
        addConfirm.textContent = origText;
      }
    });
  }
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
  } else if (result.is_image) {
    // P2 修复：图像模型返回图片 URL，显示图片而非文字
    const resp = result.response || {};
    const images = resp.data || [];
    let html = '<div class="probe-content-block"><div class="probe-content-label">生成的图片</div>';
    if (images.length === 0) {
      html += '<span class="text-muted">(无图片返回)</span>';
    } else {
      images.forEach((img, i) => {
        const url = img.url || (img.b64_json ? 'data:image/png;base64,' + img.b64_json : '');
        if (url) {
          html += `<div style="margin:10px 0;"><img src="${escapeHtml(url)}" alt="Generated Image ${i+1}" style="max-width:100%;border-radius:8px;border:1px solid var(--border);" onerror="this.style.display='none';this.nextElementSibling.style.display='block';" /><div style="display:none;color:var(--text-muted);font-size:12px;">图片加载失败，URL: ${escapeHtml(url.substring(0, 100))}...</div></div>`;
        }
      });
    }
    html += '</div>';
    contentBox.innerHTML = html;
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

// 当前选中的日志分类（默认 'all'）
let _logsCurrentCategory = 'all';

// 分类匹配规则：根据 protocol 字段判断日志属于哪个分类
// v31 精简：删掉 Chat/Anthropic/Responses 三个 tab（API 请求 tab 已显示协议列）
const LOGS_CATEGORY_MATCHERS = {
  all:        () => true,  // 全部
  api:        (l) => ['openai-chat','anthropic','openai-responses','openai-legacy','openai-images','openai-embeddings','openai-moderations'].includes(l.protocol),
  images:     (l) => l.protocol === 'openai-images',
  embeddings: (l) => l.protocol === 'openai-embeddings',
  models:     (l) => l.protocol === 'meta',  // /v1/models
  health:     (l) => l.protocol === 'health', // /health 探活
  errors:     (l) => l.status >= 400,  // 仅错误
};

async function refreshLogs() {
  const limit = document.getElementById('logs-limit').value;
  // 拉取更多日志用于前端过滤（避免过滤后条数太少）
  // errors 分类走后端 only_errors，其他分类前端过滤
  const isErrorsCategory = _logsCurrentCategory === 'errors';
  const fetchLimit = isErrorsCategory ? limit : Math.max(parseInt(limit), 500);
  const data = await api(`logs?limit=${fetchLimit}&errors=${isErrorsCategory ? 1 : 0}`);
  let logs = data.logs || [];
  // 前端按分类过滤
  const matcher = LOGS_CATEGORY_MATCHERS[_logsCurrentCategory] || LOGS_CATEGORY_MATCHERS.all;
  logs = logs.filter(matcher);
  // 截断到用户选择的条数
  logs = logs.slice(0, parseInt(limit));
  const wrapper = document.getElementById('logs-table-wrapper');
  const countInfo = document.getElementById('logs-count-info');
  if (countInfo) {
    countInfo.textContent = `共 ${logs.length} 条`;
  }
  if (logs.length === 0) {
    wrapper.innerHTML = `<div class="empty-state">该分类暂无日志记录</div>`;
    return;
  }
  const rows = logs.map(l => {
    const statusClass = l.status >= 500 ? 'text-error'
                      : l.status >= 400 ? 'text-warning'
                      : 'text-success';
    const protoBadge = l.protocol.startsWith('anthropic') ? 'badge-purple'
                     : l.protocol.includes('responses') ? 'badge-info'
                     : l.protocol === 'health' ? 'badge-muted'
                     : l.protocol === 'meta' ? 'badge-warning'
                     : l.protocol.includes('images') ? 'badge-info'
                     : 'badge-success';
    // v31 增强：显示客户端 IP（从哪里请求的）+ 更详细的信息
    // v43: 错误行整行高亮
    const rowBg = l.status >= 500 ? 'background:rgba(239,68,68,0.06);'
                : l.status >= 400 ? 'background:rgba(245,158,11,0.04);'
                : '';
    return `
      <tr style="${rowBg}">
        <td class="mono text-muted" style="font-size:11px;white-space:nowrap;">${fmtTime(l.ts)}</td>
        <td><span class="badge ${protoBadge}">${escapeHtml(l.protocol)}</span></td>
        <td class="mono">${escapeHtml(l.method)}</td>
        <td class="mono" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(l.path)}">${escapeHtml(l.path)}</td>
        <td class="mono">${escapeHtml(l.model || '-')}</td>
        <td class="mono text-muted" style="font-size:11px;" title="${escapeHtml(l.api_key || '')}">${escapeHtml(l.api_key || '-')}</td>
        <td class="mono text-muted" style="font-size:11px;" title="客户端 IP">${escapeHtml(l.client_ip || '-')}</td>
        <td class="mono"><span class="${statusClass}">${l.status}</span></td>
        <td class="mono">${l.duration_ms}ms</td>
        <td class="mono text-muted">${l.stream ? 'stream' : 'sync'}</td>
        <td class="mono text-muted">${l.account_index >= 0 ? '#' + l.account_index : '-'}</td>
        <td class="mono text-muted" style="font-size:11px;max-width:160px;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(l.error || '')}">${escapeHtml(l.error || '') || '-'}</td>
        <td class="mono text-muted" style="font-size:11px;">${escapeHtml(shortHash(l.request_id, 12))}</td>
      </tr>
    `;
  }).join('');
  wrapper.innerHTML = `
    <div class="table-wrapper">
      <table class="data-table">
        <thead>
          <tr>
            <th>时间</th><th>协议</th><th>方法</th><th>路径</th><th>模型</th><th>API Key</th>
            <th>客户端 IP</th><th>状态</th><th>延迟</th><th>模式</th><th>账号</th><th>错误</th><th>请求ID</th>
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
  let data;
  try {
    data = await api('rotates');
  } catch (err) {
    document.getElementById('rotates-table-wrapper').innerHTML = renderErrorState(
      '加载轮换事件失败',
      err.message,
      '🔄',
      'refreshRotates()'
    );
    return;
  }
  const events = data.events || [];
  const wrapper = document.getElementById('rotates-table-wrapper');
  if (events.length === 0) {
    wrapper.innerHTML = `
      <div style="text-align:center;padding:60px 20px;">
        <canvas class="neural-pulse-canvas neural-pulse-large" id="rotates-empty-pulse" width="80" height="80" style="margin:0 auto 20px;display:block;"></canvas>
        <div style="font-size:18px;font-weight:600;margin-bottom:8px;">暂无轮换事件</div>
        <div class="text-muted" style="font-size:13px;max-width:400px;margin:0 auto;">系统稳定运行中。当账号请求次数达到阈值或手动触发轮换时，事件会出现在这里。</div>
      </div>
    `;
    // v43: 空状态 NeuralPulse 动画
    const emptyPulse = document.getElementById('rotates-empty-pulse');
    if (emptyPulse) {
      new NeuralPulse(emptyPulse, { active: true, color: '#ec4899', size: 80 });
    }
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

// 通用空状态渲染（带图标 + 标题 + 描述）
function renderEmptyState(title, desc, icon = '📭') {
  return `
    <div class="empty-state">
      <span class="empty-state-icon">${icon}</span>
      <div class="empty-state-title">${escapeHtml(title)}</div>
      <div class="empty-state-desc">${escapeHtml(desc)}</div>
    </div>
  `;
}

// 通用错误状态渲染
function renderErrorState(title, desc, icon = '⚠️', retryFn = '') {
  const retryBtn = retryFn
    ? `<button class="btn btn-ghost btn-sm" style="margin-top:10px;" onclick="${escapeHtml(retryFn)}">重试</button>`
    : '';
  return `
    <div class="error-state">
      <span class="error-state-icon">${icon}</span>
      <div class="empty-state-title" style="color:var(--error);">${escapeHtml(title)}</div>
      <div class="empty-state-desc">${escapeHtml(desc)}</div>
      ${retryBtn}
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
// API Keys view (API 管理)
// =========================================================================

async function refreshApiKeys() {
  const data = await api('apikeys');
  const keys = data.keys || [];
  const total = data.total || 0;
  const envCount = data.env_keys_count || 0;
  const customCount = data.custom_keys_count || 0;

  // 汇总统计
  const totalRequests = keys.reduce((s, k) => s + (k.total_requests || 0), 0);
  const totalTokens = keys.reduce((s, k) => s + (k.total_tokens || 0), 0);
  const totalErrors = keys.reduce((s, k) => s + (k.total_errors || 0), 0);

  let html = `
    <div class="kpi-grid">
      <div class="kpi-card info">
        <div class="kpi-label">API Key 总数</div>
        <div class="kpi-value">${total}</div>
        <div class="kpi-sub">环境变量 ${envCount} · 自定义 ${customCount}</div>
      </div>
      <div class="kpi-card success">
        <div class="kpi-label">总请求数</div>
        <div class="kpi-value">${totalRequests.toLocaleString()}</div>
        <div class="kpi-sub">所有 Key 累计</div>
      </div>
      <div class="kpi-card warning">
        <div class="kpi-label">Token 用量</div>
        <div class="kpi-value">${fmtCompactNum(totalTokens)}</div>
        <div class="kpi-sub">所有 Key 累计</div>
      </div>
      <div class="kpi-card ${totalErrors > 0 ? 'error' : 'success'}">
        <div class="kpi-label">错误请求</div>
        <div class="kpi-value">${totalErrors}</div>
        <div class="kpi-sub">所有 Key 累计</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">API Key 列表</div>
        <div class="panel-meta">
          <button id="apikey-create-btn" class="btn btn-primary btn-sm">+ 创建新 Key</button>
        </div>
      </div>
      <div class="panel-body">
        <div id="apikey-create-form" style="display:none;margin-bottom:16px;gap:10px;align-items:center;" class="flex">
          <input type="text" id="apikey-name-input" placeholder="Key 名称（如 production / test）" class="probe-input" style="width:240px;" />
          <button id="apikey-create-confirm" class="btn btn-primary btn-sm">确认创建</button>
          <button id="apikey-create-cancel" class="btn btn-ghost btn-sm">取消</button>
        </div>
        <div class="table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>API Key</th>
                <th>来源</th>
                <th>状态</th>
                <th>请求数</th>
                <th>成功 / 错误</th>
                <th>Token 用量</th>
                <th>最后使用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              ${keys.length === 0 ? '<tr><td colspan="11" class="empty-state">暂无 API Key</td></tr>' : keys.map(k => `
                <tr>
                  <td><strong>${escapeHtml(k.name)}</strong></td>
                  <td class="mono" style="font-size:11px;">
                    ${k.full_key ? `
                      <span class="apikey-full">${escapeHtml(k.full_key)}</span>
                      <button class="btn btn-ghost btn-sm apikey-copy-btn" data-copy-key="${escapeHtml(k.full_key)}" title="复制 API Key" style="padding:2px 6px;font-size:11px;margin-left:4px;">📋</button>
                    ` : escapeHtml(k.key)}
                  </td>
                  <td>${k.is_env ? '<span class="badge badge-info">环境变量</span>' : '<span class="badge badge-purple">自定义</span>'}</td>
                  <td>${k.enabled ? '<span class="badge badge-success">启用</span>' : '<span class="badge badge-muted">禁用</span>'}</td>
                  <td class="mono">${k.total_requests || 0}</td>
                  <td class="mono"><span class="text-success">${k.total_success || 0}</span> / <span class="text-error">${k.total_errors || 0}</span></td>
                  <td class="mono">
                    <div style="display:flex;align-items:center;gap:6px;">
                      <div style="flex:1;min-width:60px;height:4px;background:var(--bg-elevated);border-radius:2px;overflow:hidden;">
                        <div style="height:100%;width:${Math.min(100, ((k.prompt_tokens || 0) / Math.max(1, k.total_tokens || 1)) * 100)}%;background:var(--primary);border-radius:2px;"></div>
                      </div>
                      <span style="font-size:11px;white-space:nowrap;">${fmtCompactNum(k.total_tokens || 0)}</span>
                    </div>
                  </td>
                  <td class="text-muted" style="font-size:11px;">${k.last_used_ts ? fmtTimeShort(k.last_used_ts) : '-'}</td>
                  <td>
                    ${k.is_env ? '<span class="text-muted" style="font-size:11px;">不可删除</span>' : `
                      <button class="btn btn-ghost btn-sm" data-apikey-toggle="${escapeHtml(k.key)}" data-enabled="${!k.enabled}">${k.enabled ? '禁用' : '启用'}</button>
                      <button class="btn btn-danger btn-sm" data-apikey-delete="${escapeHtml(k.key)}">删除</button>
                    `}
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    ${customCount > 0 ? `
    <div class="panel" style="margin-top:20px;">
      <div class="panel-header">
        <div class="panel-title">使用说明</div>
      </div>
      <div class="panel-body" style="font-size:13px;color:var(--text-muted);line-height:1.8;">
        <p><strong>环境变量 Key：</strong>从 Render 环境变量 <code>SERVER_API_KEYS</code> 读取，Render 重启后不会丢失。<br/>
        在 Render Dashboard → Environment 中添加 <code>SERVER_API_KEYS=key1,key2</code>（逗号分隔多个 key）。</p>
        <p><strong>自定义 Key：</strong>通过管理面板创建，服务重启后会丢失（仅存于内存）。如需持久化请添加到环境变量。</p>
        <p><strong>使用方式：</strong>在 API 请求头中添加 <code>Authorization: Bearer your-key</code> 或 <code>x-api-key: your-key</code>。</p>
      </div>
    </div>
    ` : ''}
  `;

  document.getElementById('view-apikeys').innerHTML = html;

  // 绑定创建按钮
  const createBtn = document.getElementById('apikey-create-btn');
  const createForm = document.getElementById('apikey-create-form');
  const createConfirm = document.getElementById('apikey-create-confirm');
  const createCancel = document.getElementById('apikey-create-cancel');
  const nameInput = document.getElementById('apikey-name-input');

  if (createBtn) {
    createBtn.addEventListener('click', () => {
      createForm.style.display = 'flex';
      nameInput.focus();
    });
  }
  if (createCancel) {
    createCancel.addEventListener('click', () => {
      createForm.style.display = 'none';
      nameInput.value = '';
    });
  }
  if (createConfirm) {
    createConfirm.addEventListener('click', async () => {
      const name = nameInput.value.trim() || '未命名';
      try {
        const result = await api('apikeys/create', { method: 'POST', body: { name } });
        // 自动复制到剪贴板 + 显示完整 key
        const fullKey = result.key || '';
        try {
          var ta = document.createElement('textarea');
          ta.value = fullKey;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        } catch(e) {}
        var el = document.getElementById('toast');
        el.className = 'toast success';
        el.innerHTML = '<div style="font-weight:600;margin-bottom:4px;">API Key 已创建并复制到剪贴板</div><div style="font-family:monospace;font-size:12px;word-break:break-all;">' + escapeHtml(fullKey) + '</div><div style="margin-top:4px;font-size:11px;color:var(--text-muted);">也可在下方列表中点击复制按钮</div>';
        el.classList.remove('hidden');
        clearTimeout(showToast._t);
        showToast._t = setTimeout(function(){ el.classList.add('hidden'); }, 8000);
        createForm.style.display = 'none';
        nameInput.value = '';
        refreshApiKeys();
      } catch (err) {
        showToast('创建失败: ' + err.message, 'error');
      }
    });
  }

  // 绑定复制按钮
  document.querySelectorAll('.apikey-copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.copyKey;
      // 创建临时 textarea 复制
      const textarea = document.createElement('textarea');
      textarea.value = key;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand('copy');
        showToast('✅ 已复制到剪贴板', 'success');
      } catch (e) {
        showToast('复制失败', 'error');
      }
      document.body.removeChild(textarea);
    });
  });

  // 绑定删除/禁用按钮
  document.querySelectorAll('[data-apikey-delete]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const key = btn.dataset.apikeyDelete;
      if (!confirm('确认删除此 API Key？删除后使用此 Key 的请求将被拒绝。')) return;
      try {
        await api('apikeys/delete', { method: 'POST', body: { key } });
        showToast('✅ 已删除', 'success');
        refreshApiKeys();
      } catch (err) {
        showToast('删除失败: ' + err.message, 'error');
      }
    });
  });
  document.querySelectorAll('[data-apikey-toggle]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const key = btn.dataset.apikeyToggle;
      const enabled = btn.dataset.enabled === 'true';
      try {
        await api('apikeys/toggle', { method: 'POST', body: { key, enabled } });
        showToast(`✅ 已${enabled ? '启用' : '禁用'}`, 'success');
        refreshApiKeys();
      } catch (err) {
        showToast('操作失败: ' + err.message, 'error');
      }
    });
  });
}

// =========================================================================
// System view
// =========================================================================

async function refreshSystem() {
  let sys;
  try {
    sys = await api('system');
  } catch (err) {
    document.getElementById('view-system').innerHTML = renderErrorState(
      '加载系统监控失败',
      err.message + '。该功能依赖服务器端资源监控能力，可能服务未提供 system 端点。',
      '💻',
      'refreshSystem()'
    );
    return;
  }
  // 防御：缺字段时显示友好提示
  if (!sys || !sys.process || !sys.memory) {
    document.getElementById('view-system').innerHTML = renderEmptyState(
      '系统监控暂不可用',
      '服务器返回的数据不完整。这通常发生在容器化或受限运行环境中。',
      '💻'
    );
    return;
  }
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
  // 初始化神经网络背景
  NeuralBg.init();
  // 初始化主题切换
  Theme.init();
  // 初始化移动端菜单
  initMobileMenu();

  document.getElementById('login-form').addEventListener('submit', handleLoginSubmit);
  document.getElementById('logout-btn').addEventListener('click', handleLogout);
  document.getElementById('auto-refresh').addEventListener('change', startAutoRefresh);
  document.getElementById('manual-refresh').addEventListener('click', () => refresh(true));
  // 日志分类导航栏：点击 tab 切换分类
  document.querySelectorAll('#logs-category-tabs .category-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      // 移除所有 tab 的 active
      document.querySelectorAll('#logs-category-tabs .category-tab').forEach(t => t.classList.remove('active'));
      // 给当前点击的 tab 加 active
      tab.classList.add('active');
      // 更新当前分类并刷新
      _logsCurrentCategory = tab.dataset.category;
      refreshLogs();
    });
  });
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
