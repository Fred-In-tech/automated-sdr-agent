// The Leads and Emails tabs, the sent-email viewer and the lead import.
// Uses api(), postJson(), el() and ACTIONS from app.js. DOM nodes only, never markup.

(function () {
  'use strict';

  const STATUS_LABELS = {
    new: 'Waiting for first email', review: 'Waiting for your OK', contacted: 'In sequence',
    interested: 'Interested', not_now: 'Not now', nurtured: 'Checked in', no_response: 'Finished, no reply',
    replied: 'Replied', not_interested: 'Said no', unsubscribed: 'Opted out', bounced: 'Bounced',
    disqualified: 'Poor fit', invalid_email: 'Bad address',
  };
  const STATUS_BADGES = {
    interested: 'badge-warm', review: 'badge-warm', contacted: 'badge-blue', new: 'badge-blue',
    bounced: 'badge-danger', unsubscribed: 'badge-muted', not_interested: 'badge-muted',
    disqualified: 'badge-muted', invalid_email: 'badge-muted', no_response: 'badge-muted',
  };
  let leads = [];
  let emailLogs = [];

  function statusBadge(status) {
    return el('span', { class: 'badge ' + (STATUS_BADGES[status] || 'badge-emerald'),
                        text: STATUS_LABELS[status] || status || '-' });
  }

  function emptyRow(columns, text) {
    return el('tr', { class: 'empty-row' }, [el('td', { colspan: String(columns), text })]);
  }

  // ── leads ──────────────────────────────────────────────────────────────

  function leadRow(l) {
    const source = (l.enriched_info && l.enriched_info.lead_source) || '-';
    const why = l.ai_fit || (l.fit_reasons_list || []).join(' · ');
    return el('tr', {}, [
      el('td', { text: '#' + l.id }),
      el('td', {}, [el('strong', { text: l.first_name || '-' })]),
      el('td', { text: String(l.fit_score ?? '-'), title: why || null }),
      el('td', { text: l.company || '' }),
      el('td', {}, [el('code', { text: l.email || '' })]),
      el('td', {}, [el('span', { class: 'badge badge-purple', text: l.category || '' })]),
      el('td', { text: l.location || '' }),
      el('td', { text: source }),
      el('td', {}, [statusBadge(l.status)]),
    ]);
  }

  function matches(l, query, status) {
    if (status && l.status !== status) return false;
    if (!query) return true;
    return [l.first_name, l.name, l.company, l.email, l.location, l.category]
      .some(v => String(v || '').toLowerCase().includes(query));
  }

  function renderLeads() {
    const query = document.getElementById('leads-search').value.trim().toLowerCase();
    const status = document.getElementById('leads-status').value;
    const shown = leads.filter(l => matches(l, query, status));
    const body = document.getElementById('leads-table-body');
    body.replaceChildren(...(shown.length ? shown.map(leadRow)
      : [emptyRow(9, leads.length ? 'No leads match that search.' : 'No leads yet. Press Find leads, or import your own.')]));
    document.getElementById('leads-count').textContent =
      shown.length === leads.length ? leads.length + ' leads' : shown.length + ' of ' + leads.length + ' leads';
  }

  function fillStatusFilter() {
    const select = document.getElementById('leads-status');
    const current = select.value;
    const present = Array.from(new Set(leads.map(l => l.status))).filter(Boolean).sort();
    select.replaceChildren(el('option', { value: '', text: 'Every status' }),
      ...present.map(s => el('option', { value: s, text: STATUS_LABELS[s] || s, selected: s === current })));
  }

  async function fetchLeads() {
    try {
      const data = await (await api('/api/leads')).json();
      leads = data.leads || [];
      fillStatusFilter();
      renderLeads();
    } catch (err) {
      console.error('Failed to fetch leads:', err);
    }
  }

  // ── emails ─────────────────────────────────────────────────────────────

  function emailRow(l, index) {
    const view = el('button', { type: 'button', class: 'btn btn-secondary btn-small', text: 'View',
                                'aria-label': 'View the email to ' + (l.lead_email || '') });
    const row = el('tr', { class: 'clickable-row', title: 'Click to read the full email' }, [
      el('td', { text: shortTime(l.sent_at) }),
      el('td', {}, [el('code', { text: l.lead_email || '' })]),
      el('td', { text: l.subject || '' }),
      el('td', {}, [el('span', { class: 'badge badge-blue', text: l.status || '' })]),
      el('td', {}, [view]),
    ]);
    row.addEventListener('click', () => openEmailModal(index));
    return row;
  }

  function campaignRow(c) {
    return el('tr', {}, [
      el('td', {}, [el('strong', { text: c.campaign_name || '' })]),
      el('td', { text: c.subject || '' }),
      el('td', { text: String(c.sent_count ?? 0) }),
      el('td', {}, [el('span', { class: 'badge badge-emerald', text: c.status || '' })]),
    ]);
  }

  async function fetchEmails() {
    try {
      const data = await (await api('/api/emails')).json();
      emailLogs = data.logs || [];
      const campaigns = data.campaigns || [];
      document.getElementById('email-logs-body').replaceChildren(...(emailLogs.length ? emailLogs.map(emailRow)
        : [emptyRow(5, 'Nothing sent yet.')]));
      document.getElementById('campaigns-table-body').replaceChildren(...(campaigns.length ? campaigns.map(campaignRow)
        : [emptyRow(4, 'No sequences yet.')]));
    } catch (err) {
      console.error('Failed to fetch emails:', err);
    }
  }

  // ── modals ─────────────────────────────────────────────────────────────

  function openModal(id) {
    document.getElementById(id).classList.add('open');
    const first = document.querySelector('#' + id + ' .modal-close');
    if (first) first.focus();
  }

  function closeModal(id) {
    document.getElementById(id).classList.remove('open');
  }

  function openEmailModal(index) {
    const email = emailLogs[index];
    if (!email) return;
    setText('modal-to-email', email.lead_email || '');
    setText('modal-subject', email.subject || '');
    setText('modal-sent-at', shortTime(email.sent_at));
    setText('modal-status', email.status || '');
    setText('modal-body-text', email.body || 'No content recorded.');
    openModal('email-modal');
  }

  // ── import: the file is read in the browser, checked first (dry run), then imported ──

  let importCsv = '';

  function openImportModal() {
    importCsv = '';
    document.getElementById('import-file').value = '';
    document.getElementById('import-result').hidden = true;
    document.getElementById('import-confirm').disabled = true;
    openModal('import-modal');
  }

  function showImportResult(data, done) {
    const lines = [];
    if (!data.success) {
      lines.push(el('strong', { text: data.message || 'That file could not be imported.' }));
    } else {
      const count = data.imported + (data.imported === 1 ? ' lead' : ' leads');
      lines.push(el('div', {}, [el('strong', { text: done ? count + ' imported.' : count + ' ready to import' }),
                                el('span', { text: ' (of ' + data.total + ' in the file)' })]));
      if (data.duplicates) lines.push(el('div', { text: data.duplicates + ' skipped: already in your leads, or listed twice' }));
      if (data.blocked) lines.push(el('div', { text: data.blocked + ' skipped: on your do-not-contact list' }));
      if (data.invalid) lines.push(el('div', { text: data.invalid + " skipped: the email address can't be used" }));
      (data.problems || []).slice(0, 8).forEach(p => lines.push(el('div', { class: 'muted', text: p })));
      if (done && data.imported) lines.push(el('div', { text: 'Nothing was emailed. They go out with your next run.' }));
    }
    const box = document.getElementById('import-result');
    box.replaceChildren(...lines);
    box.hidden = false;
  }

  async function sendImport(dryRun) {
    const res = await postJson('/api/import-leads', { csv: importCsv, dry_run: dryRun });
    if (res.status === 413) return { success: false, message: 'That file is larger than 1 MB. Split it into smaller files.' };
    return await res.json().catch(() => ({ success: false }));
  }

  async function previewImport() {
    const file = document.getElementById('import-file').files[0];
    const button = document.getElementById('import-confirm');
    button.disabled = true;
    if (!file) return;
    try {
      importCsv = await file.text();
      const data = await sendImport(true);
      showImportResult(data, false);
      button.disabled = !(data.success && data.imported > 0);
    } catch (err) {
      showImportResult({ success: false, message: 'Could not read that file.' }, false);
    }
  }

  async function confirmImport() {
    document.getElementById('import-confirm').disabled = true;
    try {
      const data = await sendImport(false);
      showImportResult(data, true);
      if (data.success) refreshAllData();
    } catch (err) {
      showImportResult({ success: false, message: 'The import did not finish. Try again.' }, true);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('leads-search').addEventListener('input', renderLeads);
    document.getElementById('leads-status').addEventListener('change', renderLeads);
    document.getElementById('import-file').addEventListener('change', previewImport);
    document.getElementById('import-confirm').addEventListener('click', confirmImport);
  });

  ACTIONS['open-import'] = openImportModal;
  window.closeModal = closeModal;
  window.fetchLeads = fetchLeads;
  window.fetchEmails = fetchEmails;
})();
