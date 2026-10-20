// The dashboard's core: talking to the server (session + CSRF), the header, tabs, and the
// Overview (Today strip, hot leads, stats, Run now, recent activity).
// Built with textContent / DOM nodes only, never markup, so lead data can't run as code.
// tables.js adds the Leads and Emails tabs; settings.js, ai.js, followups.js and insights.js
// add their own parts and use api(), sessionReady and el() from here.

'use strict';

const REFRESH_MS = 5000;
const CONFIRM_SEND = 'This emails real people now (within your daily limit). Continue?';
const TASK_CONFIRM = { email: CONFIRM_SEND, pipeline: CONFIRM_SEND };

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([name, value]) => {
    if (name === 'text') node.textContent = value;
    else if (name === 'class') node.className = value;
    else if (name.startsWith('on')) node.addEventListener(name.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(name, value === true ? '' : value);
  });
  (children || []).forEach(child => child && node.appendChild(child));
  return node;
}

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

function shortTime(iso) {
  return String(iso || '').slice(0, 19).replace('T', ' ');
}

// ── server ─────────────────────────────────────────────────────────────────
// With a dashboard password, the server wants this session's CSRF token on every POST.
// It's fetched from /api/session (the session cookie itself is HttpOnly, never readable here).
let csrfToken = '';
let redirectingToLogin = false;

async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = Object.assign({}, options.headers || {});
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const res = await fetch(path, Object.assign({}, options, { method, headers, credentials: 'same-origin' }));
  if (res.status === 401) {
    // Session expired (12 h) or the dashboard restarted: sign in again.
    if (!redirectingToLogin) { redirectingToLogin = true; window.location.href = '/login'; }
    throw new Error('Sign-in required');
  }
  return res;
}

async function postJson(path, body) {
  await sessionReady;
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}

async function loadSession() {
  try {
    const res = await api('/api/session');
    const s = await res.json();
    csrfToken = s.csrf_token || '';
    document.getElementById('logout-link').hidden = !(s.auth_required && s.logged_in);
  } catch (err) {
    console.error('Failed to load session:', err);
  }
}
const sessionReady = loadSession();

// ── header ─────────────────────────────────────────────────────────────────
// "Update available" pill, fed by data/update_status.json (written by `sdr update` / the weekly check).
async function loadUpdateStatus() {
  try {
    const u = await (await api('/api/update-status')).json();
    const pill = document.getElementById('update-pill');
    if (!u.available || !u.latest) { pill.hidden = true; return; }
    const latest = String(u.latest).replace(/^v/, '');
    setText('update-pill-version', 'v' + latest);
    pill.title = 'Version ' + latest + ' is ready (you have ' + (u.current || '?') +
      '). Click to copy the command, then run it in your terminal: sdr update';
    pill.hidden = false;
  } catch (err) {
    console.error('Failed to load update status:', err);
  }
}

async function copyUpdateCommand() {
  const label = document.getElementById('update-pill-text');
  const feedback = document.getElementById('update-pill-feedback');
  try {
    await navigator.clipboard.writeText('sdr update');
    feedback.textContent = 'Copied: sdr update';
  } catch (err) {
    feedback.textContent = 'Run: sdr update';  // clipboard blocked: say it instead
  }
  label.hidden = true;
  feedback.hidden = false;
  setTimeout(() => { feedback.hidden = true; label.hidden = false; }, 2500);
}

function showInitial(icon, name) {
  icon.replaceChildren(el('span', { text: (name || '?')[0] }));
}

// Colours + logo come from [email_design] in config/profile.toml, so every install shows its own brand.
function applyBrand(p) {
  const d = p.design || {};
  const isHex = v => /^#[0-9a-fA-F]{6}$/.test(v || '');
  const root = document.documentElement.style;
  [['brand_color', '--brand'], ['heading_color', '--ink'], ['text_color', '--text'], ['background', '--bg']]
    .forEach(([key, cssVar]) => { if (isHex(d[key])) root.setProperty(cssVar, d[key]); });
  const icon = document.getElementById('brand-icon');
  if (!/^https:\/\//.test(d.logo_url || '')) { showInitial(icon, p.product_name); return; }
  const img = el('img', { src: d.logo_url, alt: (p.product_name || '') + ' logo' });
  img.addEventListener('error', () => showInitial(icon, p.product_name));
  icon.replaceChildren(img);
  let favicon = document.querySelector("link[rel='icon']");
  if (!favicon) { favicon = el('link', { rel: 'icon' }); document.head.appendChild(favicon); }
  favicon.href = d.logo_url;
}

async function loadProfile() {
  try {
    const p = await (await api('/api/profile')).json();
    if (!p.configured) return;
    applyBrand(p);
    setText('brand-name', p.product_name);
    const link = document.getElementById('brand-link');
    link.href = p.website;
    link.textContent = p.website.replace(/^https?:\/\//, '').replace(/\/$/, '') + ' ↗';
    setText('brand-tagline', (p.design && p.design.tagline ? p.design.tagline + ' · ' : '') + 'SDR Control Center');
    document.title = p.product_name + ' · SDR Control Center';
    setText('stat-leads-subtext', p.ideal_client + 's');
    const chips = [p.ideal_client].concat((p.cities || []).slice(0, 6));
    document.getElementById('target-chips-list').replaceChildren(...chips.map(c => el('span', { class: 'chip', text: c })));
  } catch (err) {
    console.error('Failed to load profile:', err);
  }
}

// ── tabs ───────────────────────────────────────────────────────────────────
function switchTab(tabId) {
  const button = document.querySelector('.tab-btn[data-tab="' + tabId + '"]');
  const panel = document.getElementById('tab-' + tabId);
  if (!button || !panel) return;
  document.querySelectorAll('.tab-btn').forEach(btn => { btn.classList.remove('active'); btn.removeAttribute('aria-current'); });
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  button.classList.add('active');
  button.setAttribute('aria-current', 'page');
  panel.classList.add('active');
  history.replaceState(null, '', tabId === 'overview' ? location.pathname : '#' + tabId);
  if (tabId === 'settings' && window.loadSettings) window.loadSettings();
  if (tabId === 'followups' && window.loadFollowups) window.loadFollowups();
}

// ── overview ───────────────────────────────────────────────────────────────
function renderToday(t) {
  setText('today-sent', String(t.sent_today || 0));
  const limit = t.daily_limit || 0;
  const bar = document.getElementById('today-meter-bar');
  bar.style.width = (limit ? Math.min(100, Math.round(100 * (t.sent_today || 0) / limit)) : 0) + '%';
  const limitNode = document.getElementById('today-limit');
  limitNode.textContent = limit ? 'of ' + limit + ' today' + (t.limit_note ? ' · lowered, see Deliverability' : '') +
    (t.send_day ? '' : ' · not a send day') : 'Finish setup to start sending';
  limitNode.className = 'today-sub' + (t.limit_note ? ' today-warn' : '');
  if (t.limit_note) limitNode.title = t.limit_note;
  setText('today-next', t.schedule_on ? (t.next_run || 'No run times') : 'Schedule off');
  setText('today-next-sub', t.schedule_on ? 'Replies are checked between runs' : 'Turn it on: sdr schedule on');
  setText('today-followups', String(t.follow_ups_due || 0));
  const badge = document.getElementById('schedule-badge');
  badge.className = 'status-badge' + (t.schedule_on ? '' : ' status-off');
  setText('schedule-badge-text', t.schedule_on ? 'Schedule on' : 'Schedule off');
}

async function fetchToday() {
  try {
    renderToday(await (await api('/api/today')).json());
  } catch (err) {
    console.error('Failed to load today:', err);
  }
}

function hotLeadRow(h) {
  return el('div', { class: 'log-item' }, [
    el('div', {}, [
      el('strong', { text: h.company || h.email || '' }),
      el('span', { text: ' <' + (h.email || '') + '> · ' + String(h.intent || '').replace(/_/g, ' ') }),
      el('div', { class: 'muted', text: String(h.excerpt || '').slice(0, 160) }),
    ]),
    el('div', { class: 'muted small', text: String(h.received_at || '').slice(0, 10) }),
  ]);
}

async function fetchStats() {
  try {
    const p = await (await api('/api/pipeline')).json();
    setText('stat-leads', String(p.leads_found || 0));
    setText('stat-contacted', String(p.contacted || 0));
    setText('stat-sequence', 'In sequence: ' + (p.in_sequence || 0) + ' · ' + (p.disqualified || 0) + ' rejected');
    setText('stat-reply-rate', (p.reply_rate || 0) + '%');
    setText('stat-replied', (p.replied || 0) + ' real replies');
    setText('stat-hot', String(p.positive || 0));
    setText('stat-positive-rate', (p.positive_rate || 0) + '% positive');
    document.getElementById('alarm-card').hidden = !p.bounce_alarm;
    setText('alarm-text', p.bounce_alarm || '');
    const hot = p.hot_leads || [];
    document.getElementById('hot-leads-card').hidden = !hot.length;
    document.getElementById('hot-leads-list').replaceChildren(...hot.map(hotLeadRow));
  } catch (err) {
    console.error('Failed to fetch pipeline:', err);
  }
}

async function fetchLogs() {
  try {
    const data = await (await api('/api/logs')).json();
    document.getElementById('log-container').replaceChildren(...(data.logs || []).map(log => el('div', { class: 'log-item' }, [
      el('div', {}, [
        el('strong', { text: '[' + (log.bot_name || '') + '] ' }),
        el('span', { text: (log.action || '') + ' - ' }),
        el('span', { class: 'muted', text: log.details || '' }),
      ]),
      el('div', { class: 'muted small', text: shortTime(log.timestamp) }),
    ])));
  } catch (err) {
    console.error('Failed to fetch logs:', err);
  }
}

async function resumeSending() {
  if (!confirm('Start sending again? Bounced addresses are already removed. Earlier bounces stop counting; the limit stays the same.')) return;
  try {
    const res = await postJson('/api/resume-sending', {});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) alert(data.message || 'Could not resume. Reload the page and try again.');
    refreshAllData();
  } catch (err) {
    console.error('Failed to resume sending:', err);
  }
}

async function triggerBot(task, button) {
  if (TASK_CONFIRM[task] && !confirm(TASK_CONFIRM[task])) return;
  if (button) button.disabled = true;
  try {
    const res = await postJson('/api/run-bot', { task });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) alert(data.message || 'Could not start that task. Reload the page and try again.');
    refreshAllData();
  } catch (err) {
    if (!redirectingToLogin) alert('That task could not start: ' + err);
  } finally {
    if (button) setTimeout(() => { button.disabled = false; }, 3000);
  }
}

// ── refresh ────────────────────────────────────────────────────────────────
function refreshAllData() {
  fetchToday();
  fetchStats();
  if (document.getElementById('activity').open) fetchLogs();
  if (window.fetchLeads) window.fetchLeads();
  if (window.fetchEmails) window.fetchEmails();
}

function manualRefreshDashboard() {
  const btn = document.getElementById('global-refresh-btn');
  btn.classList.add('spinning');
  setTimeout(() => btn.classList.remove('spinning'), 750);
  refreshAllData();
  if (window.loadInsights) window.loadInsights();
  if (window.loadAIDrafts) window.loadAIDrafts();
}

// One click handler for the whole page: buttons say what they do with data-* attributes.
const ACTIONS = {
  'copy-update': copyUpdateCommand,
  refresh: manualRefreshDashboard,
  resume: resumeSending,
};

document.addEventListener('click', event => {
  const target = event.target.closest('[data-tab], [data-tab-link], [data-task], [data-action], [data-close]');
  if (!target) return;
  if (target.dataset.tab) switchTab(target.dataset.tab);
  else if (target.dataset.tabLink) { event.preventDefault(); switchTab(target.dataset.tabLink); }
  else if (target.dataset.task) triggerBot(target.dataset.task, target);
  else if (target.dataset.action && ACTIONS[target.dataset.action]) ACTIONS[target.dataset.action](target);
  else if (target.dataset.close && event.target === target) window.closeModal(target.dataset.close);
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') document.querySelectorAll('.modal-overlay.open').forEach(m => window.closeModal(m.id));
});

window.addEventListener('DOMContentLoaded', () => {
  // "sdr setup" opens the dashboard at #settings to finish setting up in the browser.
  const wanted = location.hash.replace('#', '').split('-')[0];
  if (wanted) switchTab(wanted);
  document.getElementById('activity').addEventListener('toggle', event => { if (event.target.open) fetchLogs(); });
  loadProfile();
  loadUpdateStatus();
  refreshAllData();
  setInterval(refreshAllData, REFRESH_MS);
});

window.ACTIONS = ACTIONS;
