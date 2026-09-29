// The Settings tab: every setting from `sdr setup`, one section at a time.
// Forms are built from /api/settings, so a new setup question shows up here by itself.
// Everything is created with textContent / value, never as markup, so saved text cannot run as code.

(function () {
  'use strict';

  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  let openSection = null;

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

  // ── one field ────────────────────────────────────────────────────────────

  function control(field) {
    const id = 'set-' + field.key.replace(/\./g, '-');
    const value = field.value;
    if (field.type === 'bool') {
      return el('input', { id, type: 'checkbox', checked: !!value, 'data-key': field.key, 'data-kind': 'bool' });
    }
    if (field.type === 'choice') {
      const select = el('select', { id, 'data-key': field.key, 'data-kind': 'text', class: 'set-input' });
      if (!value) select.appendChild(el('option', { value: '', text: 'Choose...' }));
      (field.choices || []).forEach(choice => {
        select.appendChild(el('option', { value: choice.value, text: choice.label, selected: choice.value === value }));
      });
      return select;
    }
    if (field.type === 'days') {
      const chosen = value || [];
      return el('div', { id, class: 'set-days', 'data-key': field.key, 'data-kind': 'days' }, DAYS.map(day =>
        el('label', { class: 'set-day' }, [
          el('input', { type: 'checkbox', value: day, checked: chosen.includes(day) }),
          el('span', { text: day }),
        ])));
    }
    if (field.type === 'list') {
      const area = el('textarea', { id, rows: 3, class: 'set-input', 'data-key': field.key, 'data-kind': 'lines',
                                    placeholder: 'One per line' });
      area.value = (value || []).join('\n');
      return area;
    }
    if (field.type === 'secret') {
      return el('input', { id, type: 'password', class: 'set-input', autocomplete: 'new-password',
                           'data-key': field.key, 'data-kind': 'secret',
                           placeholder: field.is_set ? 'Saved. Leave empty to keep it.' : 'Not set yet' });
    }
    const input = el('input', { id, class: 'set-input', 'data-key': field.key,
                                type: field.type === 'int' ? 'number' : 'text',
                                'data-kind': field.type === 'int' ? 'int' : field.type === 'time_list' ? 'times' : 'text',
                                min: field.type === 'int' ? 0 : null,
                                placeholder: field.type === 'time_list' ? '09:00, 14:00' : '' });
    input.value = Array.isArray(value) ? value.join(', ') : (value ?? '');
    return input;
  }

  function fieldRow(field) {
    const input = control(field);
    const label = el('label', { for: input.id, class: 'set-label', text: field.label + (field.required ? ' *' : '') });
    const row = el('div', { class: 'set-row' + (field.type === 'bool' ? ' set-row-check' : '') },
      field.type === 'bool' ? [input, label] : [label, input]);
    if (field.help) row.appendChild(el('div', { class: 'set-help', text: field.help }));
    if (field.only_for_provider) row.setAttribute('data-only-provider', field.only_for_provider);
    return row;
  }

  function readValues(form) {
    const values = {};
    form.querySelectorAll('[data-key]').forEach(node => {
      const kind = node.getAttribute('data-kind');
      const key = node.getAttribute('data-key');
      if (node.closest('[data-only-provider]') && node.closest('[data-only-provider]').hidden) return;
      if (kind === 'bool') values[key] = node.checked;
      else if (kind === 'days') values[key] = Array.from(node.querySelectorAll('input:checked')).map(box => box.value);
      else if (kind === 'lines') values[key] = node.value.split('\n').map(line => line.trim()).filter(Boolean);
      else if (kind === 'times') values[key] = node.value.split(/[,\s]+/).map(time => time.trim()).filter(Boolean);
      else if (kind === 'int') { if (node.value !== '') values[key] = Number(node.value); }
      else if (kind === 'secret') { if (node.value) values[key] = node.value; }
      else values[key] = node.value;
    });
    return values;
  }

  // ── one section ──────────────────────────────────────────────────────────

  function showOnlyForProvider(form) {
    const provider = form.querySelector('[data-key="email.provider"]');
    if (!provider) return;
    form.querySelectorAll('[data-only-provider]').forEach(row => {
      row.hidden = row.getAttribute('data-only-provider') !== provider.value;
    });
  }

  async function save(section, form, status, button) {
    button.disabled = true;
    status.className = 'set-status';
    status.textContent = section.name === 'email' ? 'Checking your login (nothing is sent)...' : 'Saving...';
    let data;
    try {
      await sessionReady;
      const res = await api('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section: section.name, values: readValues(form) }),
      });
      data = await res.json();
    } catch (err) {
      data = { success: false, messages: ['That could not be saved. Reload the page and try again.'] };
    }
    button.disabled = false;
    status.className = 'set-status ' + (data.success ? 'set-ok' : 'set-error');
    status.textContent = '';
    (data.messages || []).forEach(message => status.appendChild(el('div', { text: message })));
    if (data.sign_in_again) {
      status.appendChild(el('div', { text: 'Your dashboard is now protected. Sign in with your new password.' }));
      setTimeout(() => { window.location.href = '/login'; }, 2500);
      return;
    }
    if (data.success) {
      openSection = section.name;
      loadSettings();
      if (typeof refreshAllData === 'function') refreshAllData();
    }
  }

  function sectionCard(section) {
    const form = el('form', { class: 'set-form', onsubmit: event => event.preventDefault() },
      section.fields.map(fieldRow));
    const status = el('div', { class: 'set-status', role: 'status' });
    const button = el('button', { class: 'btn', type: 'submit', text: 'Save' });
    form.appendChild(el('div', { class: 'set-actions' }, [button, status]));
    form.addEventListener('submit', () => save(section, form, status, button));
    form.addEventListener('change', () => showOnlyForProvider(form));
    showOnlyForProvider(form);
    const details = el('details', { class: 'set-section', id: 'settings-' + section.name,
                                    open: section.name === openSection }, [
      el('summary', {}, [
        el('span', { class: 'set-title', text: section.label }),
        el('span', { class: 'set-summary', text: section.help }),
      ]),
      form,
    ]);
    return details;
  }

  // ── the page ─────────────────────────────────────────────────────────────

  function checklistCard(data) {
    if (data.setup_complete) return null;
    const remaining = data.checklist.filter(item => !item.done).length;
    return el('div', { class: 'section-card set-checklist' }, [
      el('div', { class: 'section-title', text: 'Finish setting up (' + remaining + ' left)' }),
      el('ul', {}, data.checklist.map(item => el('li', { class: item.done ? 'set-done' : '' }, [
        el('span', { class: 'set-tick', text: item.done ? '✓' : '○' }),
        el('a', { href: '#settings-' + item.section, text: item.label,
                  onclick: () => openSettingsSection(item.section) }),
        item.why && !item.done ? el('span', { class: 'set-help', text: ' ' + item.why }) : null,
      ]))),
    ]);
  }

  function openSettingsSection(name) {
    document.querySelectorAll('.set-section').forEach(node => { node.open = node.id === 'settings-' + name; });
    const target = document.getElementById('settings-' + name);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function loadSettings() {
    const root = document.getElementById('settings-root');
    if (!root) return;
    let data;
    try {
      const res = await api('/api/settings');
      data = await res.json();
    } catch (err) {
      root.textContent = 'Your settings could not be loaded. Reload the page.';
      return;
    }
    root.textContent = '';
    if (!data.configured) {
      root.appendChild(el('div', { class: 'section-card' }, [
        el('div', { class: 'section-title', text: 'Start in your terminal' }),
        el('p', { class: 'set-help', text: 'Run "sdr setup" first. It asks about your business and your ideal clients, '
          + 'then you can finish everything else here.' }),
      ]));
      return;
    }
    const todo = checklistCard(data);
    if (todo) root.appendChild(todo);
    if (!openSection) {
      const next = data.checklist.find(item => !item.done);
      openSection = next ? next.section : null;
    }
    root.appendChild(el('div', { class: 'section-card' },
      [el('div', { class: 'section-title', text: 'Settings' })].concat(data.sections.map(sectionCard))));
    root.appendChild(el('p', { class: 'set-help set-foot',
      text: 'Closed this page by mistake? Type "' + data.reopen_command + '" in your terminal to open it again.' }));
  }

  window.loadSettings = loadSettings;
  window.openSettingsSection = openSettingsSection;
})();
