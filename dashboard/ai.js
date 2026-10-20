// AI on the dashboard: the "Needs you" card (AI drafts waiting for your OK) and the
// "Test connection" button in Settings > AI. Everything is built with textContent / value,
// never as markup, so a draft's text can never run as code.

(function () {
  'use strict';

  const KIND_LABELS = { opener: 'First line of their first email', answer: 'Answer to their question' };
  const REFRESH_MS = 60000;

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

  // ── Needs you ────────────────────────────────────────────────────────────

  async function decide(draft, action, area, status, buttons) {
    const edited = area.value.trim() !== draft.body.trim();
    const body = { id: draft.id, action: action === 'approve' && edited ? 'edit' : action };
    if (body.action === 'edit') body.body = area.value;
    buttons.forEach(b => { b.disabled = true; });
    status.className = 'set-status';
    status.textContent = 'Saving...';
    let data;
    try {
      await sessionReady;
      const res = await api('/api/ai/drafts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      data = await res.json();
    } catch (err) {
      data = { success: false, message: 'That could not be saved. Reload the page and try again.' };
    }
    if (!data.success) {
      buttons.forEach(b => { b.disabled = false; });
      status.className = 'set-status set-error';
      status.textContent = data.message || 'That could not be saved.';
      return;
    }
    status.className = 'set-status set-ok';
    status.textContent = action === 'skip' ? 'Skipped: the standard wording is used.'
      : 'Approved: it goes out with the next run.';
    setTimeout(loadDrafts, 1500);
  }

  function draftRow(draft) {
    const who = (draft.first_name || draft.lead_name || '') + ' at ' + (draft.company || '') + ' <' + (draft.email || '') + '>';
    const area = el('textarea', { class: 'set-input ai-draft-text', rows: draft.kind === 'answer' ? 4 : 2,
                                  'aria-label': 'Draft text (you can edit it)' });
    area.value = draft.body;
    const status = el('div', { class: 'set-status', role: 'status' });
    const approve = el('button', { class: 'btn', type: 'button', text: 'Approve' });
    const skip = el('button', { class: 'btn btn-secondary', type: 'button', text: 'Skip' });
    const buttons = [approve, skip];
    approve.addEventListener('click', () => decide(draft, 'approve', area, status, buttons));
    skip.addEventListener('click', () => decide(draft, 'skip', area, status, buttons));
    return el('div', { class: 'ai-draft' + (draft.stale ? ' ai-draft-stale' : '') }, [
      el('div', { class: 'ai-draft-head' }, [
        el('span', { class: 'ai-draft-kind', text: KIND_LABELS[draft.kind] || draft.kind }),
        el('span', { class: 'set-help', text: ' · ' + who }),
        draft.stale ? el('span', { class: 'ai-draft-badge', text: 'Waiting over 2 hours' }) : null,
      ]),
      area,
      el('div', { class: 'set-actions' }, [approve, skip, status]),
    ]);
  }

  async function loadDrafts() {
    const root = document.getElementById('ai-needs-you');
    if (!root) return;
    let data;
    try {
      await sessionReady;
      const res = await api('/api/ai/drafts');
      data = await res.json();
    } catch (err) {
      return;
    }
    root.textContent = '';
    const items = data.drafts || [];
    root.hidden = items.length === 0;
    if (!items.length) return;
    root.appendChild(el('div', { class: 'section-title', text: 'Needs you (' + items.length + ')' }));
    root.appendChild(el('p', { class: 'set-help', text: 'Your AI wrote these. Edit anything you like, then Approve '
      + '(it goes out with the next run) or Skip (the standard wording is used). ' + ((data.ai || {}).notice || '') }));
    items.forEach(item => root.appendChild(draftRow(item)));
  }

  // ── Settings > AI: Test connection ───────────────────────────────────────

  function showResult(out, data) {
    out.textContent = '';
    out.className = 'set-status ' + (data.ok ? 'set-ok' : 'set-error');
    out.appendChild(el('div', { text: data.message || '' }));
    if (data.notice) out.appendChild(el('div', { class: 'set-help', text: data.notice }));
    if (data.models && data.models.length) {
      out.appendChild(el('div', { class: 'set-help', text: 'Models: ' + data.models.slice(0, 12).join(', ') }));
    }
  }

  async function testConnection(button, out) {
    button.disabled = true;
    out.className = 'set-status';
    out.textContent = 'Testing your saved AI settings...';
    let data;
    try {
      await sessionReady;
      const res = await api('/api/ai/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      data = await res.json();
    } catch (err) {
      data = { ok: false, message: 'The test could not run. Reload the page and try again.' };
    }
    button.disabled = false;
    showResult(out, data);
  }

  function addTestButton() {
    const section = document.getElementById('settings-ai');
    if (!section || section.querySelector('.ai-test')) return;
    const actions = section.querySelector('.set-actions');
    if (!actions) return;
    const out = el('div', { class: 'set-status', role: 'status' });
    const button = el('button', { class: 'btn btn-secondary ai-test', type: 'button', text: 'Test connection' });
    button.addEventListener('click', () => testConnection(button, out));
    actions.parentNode.insertBefore(el('div', { class: 'set-actions' }, [
      button, el('span', { class: 'set-help', text: 'Tests the saved settings: press Save first.' }), out,
    ]), actions.nextSibling);
  }

  document.addEventListener('DOMContentLoaded', () => {
    loadDrafts();
    setInterval(loadDrafts, REFRESH_MS);
    const settings = document.getElementById('settings-root');
    if (settings) new MutationObserver(addTestButton).observe(settings, { childList: true, subtree: true });
  });

  window.loadAIDrafts = loadDrafts;
})();
