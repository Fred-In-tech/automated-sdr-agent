// The Follow-ups tab: every interested reply becomes a deal with one dated next step.
// Today list first (overdue, due today, no next step, upcoming), then a stage board once there
// are a few deals. Built with textContent / value only, never markup. Nothing here sends email:
// "Reply" opens your own mail app.

(function () {
  'use strict';

  const BADGES = { overdue: 'Overdue', today: 'Due today', none: 'No next step', upcoming: '' };
  const BOARD_MIN_DEALS = 3;
  let stages = [];

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

  const money = v => (v === null || v === undefined) ? '' :
    '$' + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const isoDate = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  function tomorrow() { const d = new Date(); d.setDate(d.getDate() + 1); return isoDate(d); }
  function prettyDate(iso) {
    if (!iso) return '';
    const d = new Date(iso + 'T12:00:00');
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  }

  async function act(payload, status) {
    if (status) { status.className = 'set-status'; status.textContent = 'Saving...'; }
    let data;
    try {
      await sessionReady;
      const res = await api('/api/followups', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      data = await res.json();
    } catch (err) {
      data = { success: false, message: 'That could not be saved. Reload the page and try again.' };
    }
    if (!data.success) {
      if (status) { status.className = 'set-status set-error'; status.textContent = data.message || 'Not saved.'; }
      return false;
    }
    loadFollowups();
    return true;
  }

  function stageSelect(current) {
    const select = el('select', { class: 'set-input fu-stage', 'aria-label': 'Stage' });
    stages.forEach(s => select.appendChild(el('option', { value: s.value, text: s.label, selected: s.value === current })));
    return select;
  }

  // ── one deal row ─────────────────────────────────────────────────────────

  function doneForm(deal, row, status) {
    const step = el('input', { class: 'set-input', type: 'text', maxlength: 200, placeholder: 'Next step, e.g. Send the proposal',
                               'aria-label': 'Next step' });
    const due = el('input', { class: 'set-input fu-date', type: 'date', value: tomorrow(), 'aria-label': 'Due date' });
    const stage = stageSelect(deal.stage);
    stage.querySelectorAll('option').forEach(o => { if (o.value === 'won' || o.value === 'lost') o.remove(); });
    const save = el('button', { class: 'btn', type: 'button', text: 'Save next step' });
    save.addEventListener('click', () => act({ id: deal.id, action: 'done', next_step: step.value, due: due.value,
                                               stage: stage.value }, status));
    const value = el('input', { class: 'set-input fu-value', type: 'text', inputmode: 'decimal', placeholder: 'Value ($)',
                                value: deal.value ?? '', 'aria-label': 'Deal value' });
    const won = el('button', { class: 'btn btn-emerald', type: 'button', text: 'Won' });
    const lost = el('button', { class: 'btn btn-secondary', type: 'button', text: 'Lost' });
    won.addEventListener('click', () => act({ id: deal.id, action: 'done', outcome: 'won', value: value.value }, status));
    lost.addEventListener('click', () => act({ id: deal.id, action: 'done', outcome: 'lost' }, status));
    const form = el('div', { class: 'fu-form' }, [
      el('div', { class: 'set-help', text: 'Done. What happens next?' }),
      el('div', { class: 'fu-form-row' }, [step, due, stage, save]),
      el('div', { class: 'fu-form-row' }, [el('span', { class: 'set-help', text: 'Or close it:' }), value, won, lost]),
    ]);
    row.appendChild(form);
    step.focus();
  }

  function dealRow(deal) {
    const who = (deal.first_name || deal.lead_name || '') + (deal.company ? ' · ' + deal.company : '');
    const status = el('div', { class: 'set-status', role: 'status' });
    const badge = BADGES[deal.bucket] ? el('span', { class: 'fu-badge fu-' + deal.bucket, text: BADGES[deal.bucket] }) : null;
    const stale = deal.stale ? el('span', { class: 'fu-badge fu-stale', text: 'Stale', title: 'No stage change in 5+ days' }) : null;
    const stage = stageSelect(deal.stage);
    stage.addEventListener('change', () => act({ id: deal.id, action: 'update', stage: stage.value }, status));
    const done = el('button', { class: 'btn', type: 'button', text: 'Done' });
    const row = el('div', { class: 'fu-row fu-row-' + deal.bucket });
    done.addEventListener('click', () => { if (!row.querySelector('.fu-form')) doneForm(deal, row, status); });
    const snooze = ['tomorrow', '3days', 'monday'].map((when, i) => {
      const b = el('button', { class: 'btn btn-secondary fu-snooze', type: 'button',
                               text: ['Tomorrow', '+3 days', 'Next Mon'][i], title: 'Snooze the next step' });
      b.addEventListener('click', () => act({ id: deal.id, action: 'snooze', when }, status));
      return b;
    });
    const reply = el('a', { class: 'btn btn-secondary', href: 'mailto:' + encodeURIComponent(deal.email || '').replace(/%40/g, '@'),
                            text: 'Reply', title: 'Opens your mail app' });
    row.appendChild(el('div', { class: 'fu-main' }, [
      el('div', { class: 'fu-who' }, [el('strong', { text: who || deal.email || '' }), badge, stale,
        deal.value ? el('span', { class: 'fu-value-tag', text: money(deal.value) }) : null]),
      el('div', { class: 'fu-next' }, [
        el('span', { text: deal.next_step || 'No next step yet: press Done to add one.' }),
        deal.due ? el('span', { class: 'set-help', text: ' · ' + prettyDate(deal.due) }) : null,
      ]),
    ]));
    row.appendChild(el('div', { class: 'fu-actions' }, [stage, done].concat(snooze, [reply, status])));
    return row;
  }

  // ── the tab ──────────────────────────────────────────────────────────────

  function stat(label, value, sub) {
    return el('div', { class: 'stat-card' }, [
      el('div', { class: 'stat-label', text: label }),
      el('div', { class: 'stat-value', text: String(value) }),
      sub ? el('div', { class: 'stat-subtext', text: sub }) : null,
    ]);
  }

  function board(deals) {
    if (deals.length < BOARD_MIN_DEALS) return null;
    const columns = stages.map(s => {
      const items = deals.filter(d => d.stage === s.value);
      const total = items.reduce((sum, d) => sum + (d.value || 0), 0);
      return el('div', { class: 'fu-col' }, [
        el('div', { class: 'fu-col-head', text: s.label + ' (' + items.length + ')' + (total ? ' · ' + money(total) : '') }),
      ].concat(items.map(d => el('div', { class: 'fu-card' }, [
        el('div', { text: d.company || d.email }),
        d.value ? el('div', { class: 'set-help', text: money(d.value) }) : null,
      ]))));
    });
    return el('div', { class: 'section-card' }, [
      el('div', { class: 'section-title', text: 'Pipeline' }),
      el('div', { class: 'fu-board' }, columns),
    ]);
  }

  async function loadFollowups() {
    const root = document.getElementById('followups-root');
    if (!root) return;
    let data;
    try {
      await sessionReady;
      const res = await api('/api/followups');
      data = await res.json();
    } catch (err) {
      root.textContent = 'Your follow-ups could not be loaded. Reload the page.';
      return;
    }
    stages = data.stages || [];
    const s = data.summary || {};
    root.textContent = '';
    root.appendChild(el('div', { class: 'grid-4' }, [
      stat('Due now', s.due_now || 0, 'Overdue or due today'),
      stat('Open deals', s.open || 0, s.pipeline_value ? money(s.pipeline_value) + ' in play' : 'From interested replies'),
      stat('Won', s.won || 0, s.won_value ? money(s.won_value) : ''),
      stat('Revenue per 100 emails', s.revenue_per_100_emails == null ? '-' : money(s.revenue_per_100_emails),
           s.first_emails ? 'From ' + s.first_emails + ' first emails' : 'Add a value when you win a deal'),
    ]));
    const list = el('div', { class: 'section-card' }, [
      el('div', { class: 'section-title', text: 'Today' }),
      el('p', { class: 'set-help', text: 'Everyone who replied with interest, with one next step each. ' +
        'Done asks what happens next, so nobody slips through the cracks.' }),
    ]);
    const today = data.today || [];
    if (!today.length) {
      list.appendChild(el('p', { class: 'fu-empty', text: 'No follow-ups yet. When a lead replies with interest or ' +
        'a question, they appear here with a next step due the next day.' }));
    }
    today.forEach(deal => list.appendChild(dealRow(deal)));
    root.appendChild(list);
    const pipeline = board(data.deals || []);
    if (pipeline) root.appendChild(pipeline);
  }

  document.addEventListener('DOMContentLoaded', loadFollowups);
  window.loadFollowups = loadFollowups;
})();
