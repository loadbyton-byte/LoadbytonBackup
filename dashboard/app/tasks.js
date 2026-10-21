import { $, esc, t, api, err, pill, when, isManager } from './core.js';
import { openLead } from './leads.js';

export async function render(el) {
  const mgr = isManager();
  el.innerHTML = `<h1>${esc(t('nav_tasks'))}</h1><div class="card">
    ${mgr ? '<div class="toolbar"><select id="task-agent" aria-label="Agent"></select></div>' : ''}
    <div class="tablewrap"><table><thead><tr><th>${esc(t('h_lead'))}</th><th>${esc(t('h_action'))}</th><th>${esc(t('h_channel'))}</th><th>${esc(t('h_due'))}</th><th>${esc(t('h_reason'))}</th></tr></thead><tbody id="task-rows"></tbody></table></div>
    <p id="task-empty" class="empty" hidden>${esc(t('empty_tasks'))}</p></div>`;
  const sel = $('#task-agent', el);
  const load = async () => {
    try {
      if (sel && !sel.dataset.ready) {
        const agents = await api('/agents');
        sel.innerHTML = `<option value="">${esc(t('all_agents'))}</option>` + agents.map(a => `<option value="${Number(a.id)}">${esc(a.name)}</option>`).join('');
        sel.dataset.ready = '1';
      }
      const tasks = await api('/tasks/today' + (sel && sel.value ? '?agent_id=' + Number(sel.value) : ''));
      $('#task-empty', el).hidden = tasks.length > 0;
      $('#task-rows', el).innerHTML = tasks.map(k => `<tr class="row" data-id="${Number(k.lead_id)}">
        <td>${pill(k.tier)} <b>${esc(k.display_name || 'Lead #' + k.lead_id)}</b></td><td>${esc(t('a_' + k.action))}</td>
        <td>${k.channel ? `<span class="chip ${k.allowed ? 'ok' : 'no'}" title="${esc(k.compliance_reason)}">${esc(k.channel)} · ${esc(t(k.allowed ? 'allowed' : 'blocked'))}</span>` : '—'}</td>
        <td>${esc(when(k.due))}</td><td class="mute">${esc(k.reason)}</td></tr>`).join('');
    } catch (e) { $('#task-rows', el).innerHTML = `<tr><td colspan="5">${err(e)}</td></tr>`; }
  };
  if (sel) sel.addEventListener('change', load);
  $('#task-rows', el).addEventListener('click', e => { const tr = e.target.closest('tr.row'); if (tr) openLead(Number(tr.dataset.id), load); });
  await load();
}
