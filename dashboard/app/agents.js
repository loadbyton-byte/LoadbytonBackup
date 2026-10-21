import { $, esc, t, api, post, patch, err, pill, when } from './core.js';
import { openLead } from './leads.js';

export async function render(el) {
  el.innerHTML = `<h1>${esc(t('nav_agents'))}</h1>
  <div class="card"><div class="toolbar"><h2 class="grow">${esc(t('nav_agents'))}</h2>
    <button id="escalate" class="primary small" type="button">${esc(t('escalate'))}</button><span id="escalate-out" class="mute"></span></div>
    <div class="tablewrap"><table><thead><tr><th>${esc(t('h_agent'))}</th><th>${esc(t('h_langs'))}</th><th>${esc(t('h_areas'))}</th><th>${esc(t('h_weight'))}</th><th>${esc(t('h_open'))}</th><th></th></tr></thead><tbody id="agent-rows"></tbody></table></div>
    <form id="agent-form" class="inline">
      <label><span>${esc(t('h_agent'))}</span><input name="name" required maxlength="80"></label>
      <label><span>${esc(t('h_langs'))}</span><select name="languages"><option value="en">English</option><option value="en,ar">English + العربية</option><option value="ar">العربية</option></select></label>
      <label><span>${esc(t('h_areas'))}</span><input name="areas" placeholder="Dubai Marina, JVC"></label>
      <label><span>${esc(t('h_weight'))}</span><input name="weight" type="number" min="1" value="1"></label>
      <label><span>${esc(t('max_open'))}</span><input name="max_open" type="number" min="1" value="25"></label>
      <button class="primary" type="submit">${esc(t('add_agent'))}</button></form></div>
  <div class="card"><h2>${esc(t('overdue'))}</h2><div class="tablewrap"><table><tbody id="overdue-rows"></tbody></table></div></div>`;
  const load = async () => {
    try {
      const [agents, overdue] = await Promise.all([api('/agents'), api('/leads?overdue=true')]);
      $('#agent-rows', el).innerHTML = agents.map(a => `<tr><td><b>${esc(a.name)}</b></td><td>${esc(a.languages.join(', '))}</td><td>${esc(a.areas.join(', ') || '—')}</td>
        <td>${Number(a.weight)}</td><td>${Number(a.open_leads)} / ${Number(a.max_open)}</td>
        <td><button class="small" data-agent="${Number(a.id)}" data-active="${a.active ? 0 : 1}">${esc(t(a.active ? 'deactivate' : 'activate'))}</button></td></tr>`).join('');
      $('#overdue-rows', el).innerHTML = overdue.map(l => `<tr class="row" data-id="${Number(l.id)}"><td>${pill(l.tier)} ${Number(l.score)}</td><td><b>${esc(l.display_name || 'Lead #' + l.id)}</b></td>
        <td>${esc(l.primary_area || '—')}</td><td class="mute">${esc(t('reply_by'))} ${esc(when(l.sla_due_at))}</td><td>${l.assigned_agent_id ? '#' + Number(l.assigned_agent_id) : '—'}</td></tr>`).join('')
        || `<tr><td class="mute">${esc(t('none'))}</td></tr>`;
    } catch (e) { if (!el.isConnected) return; $('#agent-rows', el).innerHTML = `<tr><td colspan="6">${err(e)}</td></tr>`; }
  };
  $('#agent-rows', el).addEventListener('click', async e => {
    const b = e.target.closest('[data-agent]'); if (!b) return;
    try { await patch('/agents/' + Number(b.dataset.agent), { active: b.dataset.active === '1' }); } catch (x) { alert(x.message); }
    load();
  });
  $('#overdue-rows', el).addEventListener('click', e => { const tr = e.target.closest('tr.row'); if (tr) openLead(Number(tr.dataset.id), load); });
  $('#agent-form', el).addEventListener('submit', async e => {
    e.preventDefault(); const f = new FormData(e.target);
    try {
      await post('/agents', { name: f.get('name'), languages: f.get('languages').split(','), weight: Number(f.get('weight')) || 1, max_open: Number(f.get('max_open')) || 25,
        areas: String(f.get('areas')).split(',').map(s => s.trim()).filter(Boolean) });
      e.target.reset();
    } catch (x) { alert(x.message); }
    load();
  });
  $('#escalate', el).onclick = async () => {
    try { const r = await post('/maintenance/escalate'); $('#escalate-out', el).textContent = `${r.reassigned} ${t('escalated')} · ${r.flagged} ${t('flagged')}`; } catch (x) { $('#escalate-out', el).textContent = x.message; }
    load();
  };
  await load();
}
