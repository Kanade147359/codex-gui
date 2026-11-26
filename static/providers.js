"use strict";
window.ProviderUI = (() => {
  const q = (s) => document.querySelector(s);
  const escape = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  let data = [], pending;
  async function load(force = false) {
    if (!pending || force) pending = fetch('/api/providers').then(async r => {
      if (!r.ok) throw new Error('Provider metadata unavailable');
      data = (await r.json()).providers;
      render();
      return data;
    }).catch(e => { pending = null; if (q('#provider-settings-message')) q('#provider-settings-message').textContent = e.message; return data; });
    return pending;
  }
  const info = (id) => data.find(p => p.id === id);
  function render() {
    const host = q('#provider-settings');
    if (!host) return;
    host.innerHTML = data.map(p => {
      const s = p.settings;
      const input = (key, label, type = 'text') => `<label>${label}<input name="${key}" type="${type}" ${type === 'checkbox' ? (s[key] ? 'checked' : '') : `value="${escape(s[key])}"`} ${type === 'number' ? 'min="0" max="128"' : ''}></label>`;
      const modes = p.id === 'claude' ? p.permission_modes || [] : ['auto_review', 'never'];
      return `<form data-provider-settings="${escape(p.id)}"><h3>${p.id === 'claude' ? 'Claude Code' : 'Codex'}</h3>
        <p>${p.available ? 'Available' : 'Unavailable'} · ${escape(p.version || 'Version unknown')} · Auth: ${p.authenticated === true ? 'signed in' : p.authenticated === false ? 'sign-in required' : 'Unknown'} · Billing detected: ${escape(p.billing)} · Quota: Unknown</p>
        <p class="error">${escape(p.error || '')}</p><div class="row">
        ${input('binary', 'CLI executable')}${input('default_model', 'Default model (CLI alias or full ID)')}
        ${input('default_effort', 'Default effort (default = CLI default)')}${input('max_concurrent', 'Maximum concurrent (0 = unlimited)', 'number')}</div>
        <div class="row">${input('default_subagents', 'Default subagents', 'checkbox')}
        <label>Default permission mode<select name="permission_mode">${[...new Set([s.permission_mode, ...modes])].map(m => `<option ${m === s.permission_mode ? 'selected' : ''}>${escape(m)}</option>`).join('')}</select></label>
        <label>Billing route<select name="billing_mode"><option value="unknown">Unknown / execution blocked for Claude</option><option value="subscription" ${s.billing_mode === 'subscription' ? 'selected' : ''}>Subscription</option></select></label>
        ${p.id === 'claude' ? input('extra_usage_disabled_confirmed', 'I confirmed extra usage / purchased credits are disabled in my Claude account', 'checkbox') : ''}</div>
        <button type="submit">Save provider settings</button>${p.paused_for_quota ? '<button type="button" data-resume-provider>Resume queue after resolving provider stop</button>' : ''}
        <p data-provider-message role="status"></p></form>`;
    }).join('');
    host.querySelectorAll('form').forEach(form => {
      form.addEventListener('submit', async e => {
        e.preventDefault();
        const values = Object.fromEntries(new FormData(form));
        values.default_subagents = form.elements.default_subagents.checked;
        if (form.elements.extra_usage_disabled_confirmed) values.extra_usage_disabled_confirmed = form.elements.extra_usage_disabled_confirmed.checked;
        values.max_concurrent = Number(values.max_concurrent);
        const message = form.querySelector('[data-provider-message]');
        try {
          const r = await fetch(`/api/providers/${form.dataset.providerSettings}/settings`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({settings:values})});
          if (!r.ok) throw new Error((await r.json()).detail);
          message.textContent = 'Saved';
          await load(true);
        } catch (e) { message.textContent = e.message; }
      });
      form.querySelector('[data-resume-provider]')?.addEventListener('click', async () => {
        await fetch(`/api/providers/${form.dataset.providerSettings}/resume-queue`, {method:'POST'});
        await load(true);
      });
    });
  }
  function task(t) {
    if (!q('#provider-task-detail')) return;
    const host = q('#provider-task-detail');
    host.hidden = t.provider !== 'claude';
    if (host.hidden) return;
    q('#provider-task-usage').textContent = JSON.stringify(t.provider_usage || {}, null, 2);
    q('#provider-task-subagents').textContent = (t.provider_subagents || []).map(s => `${s.id}: ${s.status} ${s.description || ''}`).join('\n') || 'No subagent events';
    q('#provider-task-error').textContent = t.last_failure_kind ? `${t.last_failure_kind}: ${t.last_failure_message}` : '';
  }
  document.addEventListener('DOMContentLoaded', () => { if (q('#provider-settings')) load(); });
  return { load, info, task };
})();
