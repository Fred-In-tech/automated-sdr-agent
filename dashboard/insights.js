// Overview: will your emails land (Deliverability card) and what they produce (Results funnel).
// Built with textContent only. "Check again" re-reads your domain's DNS; nothing is changed or sent.

(function () {
  'use strict';

  const ICONS = { ok: '✓', warning: '!', problem: '✗', info: 'i' };
  const STATUS_TEXT = { ok: 'All good', warning: 'Works, could be better', problem: 'Needs fixing', unknown: 'Not checked yet' };

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

  function copyButton(text) {
    const b = el('button', { class: 'btn btn-secondary dl-copy', type: 'button', text: 'Copy record' });
    b.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; } catch (e) { b.textContent = 'Select and copy'; }
      setTimeout(() => { b.textContent = 'Copy record'; }, 1500);
    });
    return b;
  }

  function checkRow(c) {
    const showFix = c.status !== 'ok' && (c.fix || c.record);
    return el('div', { class: 'dl-check dl-' + c.status }, [
      el('span', { class: 'dl-icon', text: ICONS[c.status] || '•', 'aria-hidden': 'true' }),
      el('div', { class: 'dl-body' }, [
        el('div', {}, [el('strong', { text: c.label + ': ' }), el('span', { text: c.detail })]),
        showFix && c.fix ? el('div', { class: 'set-help', text: c.fix }) : null,
        showFix && c.record ? el('div', { class: 'dl-record' }, [el('code', { text: c.record }), copyButton(c.record)]) : null,
      ]),
    ]);
  }

  // Problems stay open; when it all works (or only "could be better"), the details fold away
  // so the Overview isn't one long DNS lesson.
  function checksBlock(checks, status) {
    if (!checks.length) return null;
    const open = status === 'problem';
    const counts = checks.filter(c => c.status !== 'ok' && c.status !== 'info').length;
    return el('details', { class: 'dl-details', open: open }, [
      el('summary', { class: 'set-help', text: counts ? counts + (counts === 1 ? ' thing' : ' things') + ' to look at'
        : 'Show the ' + checks.length + ' checks' }),
    ].concat(checks.map(checkRow)));
  }

  function sampleLine(sample) {
    if (!sample || !(sample.spf || sample.dkim || sample.dmarc)) {
      return el('p', { class: 'set-help', text: 'Tip: run "sdr test-email" to see how a real email of yours is judged ' +
        '(SPF, DKIM, DMARC) by the receiving server.' });
    }
    const parts = ['spf', 'dkim', 'dmarc'].filter(k => sample[k]).map(k => k.toUpperCase() + ' ' + sample[k]);
    const where = sample.spam ? ' · landed in SPAM' : '';
    return el('p', { class: 'set-help', text: 'Your last sample email: ' + parts.join(', ') +
      (sample.seen_by ? ' (' + sample.seen_by + ')' : '') + where });
  }

  function deliverabilityCard(d, onCheck) {
    const status = d.status || 'unknown';
    const button = el('button', { class: 'btn btn-secondary', type: 'button', text: 'Check again' });
    const note = el('span', { class: 'set-help', role: 'status' });
    button.addEventListener('click', () => onCheck(button, note));
    const when = d.checked_at ? new Date(d.checked_at).toLocaleString() : '';
    return el('div', { class: 'section-card dl-card dl-card-' + status }, [
      el('div', { class: 'section-header' }, [
        el('div', { class: 'section-title', text: 'Deliverability' + (d.domain ? ' · ' + d.domain : '') }),
        el('span', { class: 'dl-pill dl-pill-' + status, text: STATUS_TEXT[status] || status }),
      ]),
      d.cap_note ? el('div', { class: 'dl-banner', text: d.cap_note }) : null,
      status === 'unknown' ? el('p', { class: 'set-help', text: (d.message || 'Not checked yet.') +
        ' Press Check again to look up your domain\'s SPF, DKIM and DMARC.' }) : null,
    ].concat([checksBlock(d.checks || [], status)], [
      sampleLine(d.sample),
      el('div', { class: 'set-actions' }, [button, el('span', { class: 'set-help', text: when ? 'Checked ' + when : '' }), note]),
    ]));
  }

  function bar(width) {
    const node = el('div', { class: 'fn-bar' });
    node.style.width = width + '%';      // CSSOM, allowed by the dashboard's content security policy
    return node;
  }

  function funnelCard(f) {
    const steps = (f && f.steps) || [];
    const top = steps.length ? steps[0].count : 0;
    const rows = steps.map(s => {
      const width = top ? Math.max(2, Math.round(100 * s.count / top)) : 0;
      return el('div', { class: 'fn-row' }, [
        el('div', { class: 'fn-label', text: s.label }),
        el('div', { class: 'fn-track' }, [bar(width)]),
        el('div', { class: 'fn-count', text: String(s.count) + (s.rate == null ? '' : ' · ' + s.rate + '%') }),
      ]);
    });
    return el('div', { class: 'section-card' }, [
      el('div', { class: 'section-title', text: 'Results funnel' }),
      el('p', { class: 'set-help', text: top ? 'Built on replies and outcomes, never on opens. Percentages are of the step before.'
        : 'Fills in as first emails go out and people reply.' }),
    ].concat(rows, [f && f.won_value ? el('p', { class: 'fn-won', text: 'Won value: $' +
      Number(f.won_value).toLocaleString() }) : null]));
  }

  // Why replies didn't turn into interest: the counts point at what to change (timing, targeting,
  // offer or wording), with one real reply as an example.
  function reasonsCard(reasons) {
    if (!reasons || !reasons.length) return null;
    const total = reasons.reduce((sum, r) => sum + r.count, 0);
    return el('div', { class: 'section-card' }, [
      el('div', { class: 'section-title', text: 'Why people said no' }),
      el('p', { class: 'set-help', text: 'From ' + total + (total === 1 ? ' reply' : ' replies') +
        ' that weren\'t interested (the latest reply per lead).' }),
    ].concat(reasons.map(r => el('div', { class: 'fn-row' }, [
      el('div', { class: 'fn-label', text: r.label, title: r.example ? '"' + r.example + '"' : null }),
      el('div', { class: 'fn-track' }, [bar(Math.max(2, Math.round(100 * r.count / total)))]),
      el('div', { class: 'fn-count', text: String(r.count) }),
    ]))));
  }

  function testRows(title, rows, texts, describe) {
    if (!rows || !rows.length) return [];
    return [el('div', { class: 'ab-title', text: title })].concat(rows.map(v => {
      const index = 'ABCDEFGH'.indexOf(v.variant);
      const text = String(describe(v, index >= 0 ? texts[index] : '') || '').replace(/\{\{\s*(\w+)\s*\}\}/g, '[$1]');
      return el('div', { class: 'fn-row', title: text || null }, [
        el('div', { class: 'fn-label', text: v.variant + (text ? ': ' + (text.length > 48 ? text.slice(0, 47) + '…' : text) : '') }),
        el('div', { class: 'fn-track' }, [bar(Math.max(2, Math.round(v.reply_rate || 0)))]),
        el('div', { class: 'fn-count', text: v.replied + '/' + v.contacted + ' · ' + (v.reply_rate || 0) + '%' }),
      ]);
    }));
  }

  // A/B tests side by side: reply rate per subject line, first line and signature.
  function testsCard(t) {
    if (!t) return null;
    const rows = [].concat(
      testRows('Subject line', t.subject, t.subject_texts || [], (_v, text) => text),
      testRows('First line', t.opener, t.opener_texts || [], (v, text) => v.variant === 'ai' ? 'written by your AI' : text),
      testRows('Signature', t.signature, [], () => ''));
    if (!rows.length) return null;
    return el('div', { class: 'section-card' }, [
      el('div', { class: 'section-title', text: 'What gets replies (A/B tests)' }),
      el('p', { class: 'set-help', text: 'Reply rate per version. Small numbers swing a lot: wait for about 50 ' +
        'emails per version before you pick a winner.' }),
    ].concat(rows));
  }

  async function checkAgain(button, note) {
    button.disabled = true;
    note.textContent = 'Checking your domain...';
    try {
      await sessionReady;
      await api('/api/deliverability/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    } catch (err) { /* the reload below shows what we know */ }
    button.disabled = false;
    loadInsights();
  }

  async function loadInsights() {
    const root = document.getElementById('insights-root');
    if (!root) return;
    let data;
    try {
      await sessionReady;
      const res = await api('/api/insights');
      data = await res.json();
    } catch (err) {
      return;
    }
    root.textContent = '';
    root.appendChild(el('div', { class: 'grid-2' }, [deliverabilityCard(data.deliverability || {}, checkAgain),
                                                     funnelCard(data.funnel)]));
    const learn = [reasonsCard(data.reasons), testsCard(data.tests)].filter(Boolean);
    if (learn.length) root.appendChild(el('div', { class: 'grid-2' }, learn));
  }

  document.addEventListener('DOMContentLoaded', loadInsights);
  window.loadInsights = loadInsights;
})();
