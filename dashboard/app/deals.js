import { $, esc, t, api, post, err, money, num, isManager } from './core.js';

const STAGES = ['viewing_done', 'offer_made', 'offer_accepted', 'contract_signed', 'paid'];

export async function render(el) {
  el.innerHTML = `<h1>${esc(t('nav_deals'))}</h1>
    <div class="card"><div id="dl-tiles" class="tiles"></div></div>
    <div class="card"><h2>${esc(t('dl_add'))}</h2><form id="dl-form" class="toolbar">
      <select id="dl-lead" aria-label="${esc(t('dl_lead'))}" required></select>
      <select id="dl-kind" aria-label="${esc(t('dl_kind'))}"><option value="sale">${esc(t('dl_k_sale'))}</option><option value="off_plan">${esc(t('dl_k_off_plan'))}</option><option value="rental">${esc(t('dl_k_rental'))}</option></select>
      <input id="dl-value" type="number" min="1" step="1" placeholder="${esc(t('dl_value'))}" aria-label="${esc(t('dl_value'))}" required>
      <input id="dl-title" type="text" maxlength="200" placeholder="${esc(t('dl_title'))}" aria-label="${esc(t('dl_title'))}">
      <button class="primary" type="submit">${esc(t('dl_create'))}</button> <span id="dl-msg" role="status"></span></form></div>
    <div id="dl-board" class="dl-board"></div>
    <div class="card"><h2>${esc(t('dl_leaderboard'))}</h2><div class="tablewrap"><table><thead><tr><th>${esc(t('h_agent'))}</th><th>${esc(t('dl_deals'))}</th><th>${esc(t('dl_closed'))}</th><th>${esc(t('dl_closed_value'))}</th><th>${esc(t('dl_commission'))}</th><th>${esc(t('dl_conv'))}</th></tr></thead><tbody id="dl-lb"></tbody></table></div></div>`;
  const say = (html) => { const n = $('#dl-msg', el); if (n) n.innerHTML = html; };

  const loadTiles = async () => {
    const s = await api('/deals/summary');
    const tile = (k, label, v) => `<div class="tile"><b data-kpi="${k}">${esc(v)}</b><span>${esc(label)}</span></div>`;
    $('#dl-tiles', el).innerHTML = tile('pipeline', t('dl_pipeline'), money(s.pipeline_value_aed)) + tile('expected', t('dl_expected'), money(s.expected_commission_aed))
      + tile('closed-month', t('dl_closed_month'), num(s.closed_this_month.count)) + tile('commission-month', t('dl_commission_month'), money(s.closed_this_month.commission_aed));
  };
  const loadBoard = async () => {
    const deals = await api('/deals');
    const cols = [...STAGES, 'lost'];
    $('#dl-board', el).innerHTML = cols.map(st => {
      const items = deals.filter(d => d.stage === st);
      return `<section class="card dl-col" data-stage="${st}"><h3>${esc(t('dl_s_' + st))} <span class="chip">${items.length}</span></h3>${items.map(d => {
        const i = STAGES.indexOf(d.stage);
        return `<div class="dl-card" data-id="${Number(d.id)}"><b>${esc(d.title || t('dl_k_' + d.kind))}</b><p class="mute">${esc(money(d.value_aed))} · ${esc(money(d.commission_aed))}</p>
          ${d.lost_reason ? `<p class="mute">${esc(d.lost_reason)}</p>` : ''}
          ${i > 0 ? `<button class="ghost small dl-move" data-to="${STAGES[i - 1]}" type="button">← ${esc(t('dl_s_' + STAGES[i - 1]))}</button>` : ''}
          ${i >= 0 && i < STAGES.length - 1 ? `<button class="primary small dl-move" data-to="${STAGES[i + 1]}" type="button">${esc(t('dl_s_' + STAGES[i + 1]))} →</button>` : ''}
          ${d.stage !== 'lost' ? `<button class="ghost small dl-lose" type="button">${esc(t('dl_s_lost'))}</button>` : `<button class="ghost small dl-move" data-to="viewing_done" type="button">${esc(t('dl_reopen'))}</button>`}
          <span class="dl-row" role="status"></span></div>`;
      }).join('')}</section>`;
    }).join('');
  };
  const loadBoardLb = async () => {
    const lb = await api('/deals/leaderboard');
    $('#dl-lb', el).innerHTML = lb.map(r => `<tr><td>${esc(r.name || '#' + r.agent_id)}</td><td>${num(r.deals)}</td><td>${num(r.closed_deals)}</td><td>${esc(money(r.closed_value_aed))}</td><td>${esc(money(r.commission_aed))}</td><td>${r.lead_to_deal_pct == null ? '—' : r.lead_to_deal_pct + '%'}</td></tr>`).join('');
  };
  const loadLeads = async () => {
    const leads = await api('/leads?limit=200');
    $('#dl-lead', el).innerHTML = leads.map(l => `<option value="${Number(l.id)}">${esc(l.display_name || 'Lead #' + l.id)}</option>`).join('');
  };
  const all = async () => { try { await Promise.all([loadTiles(), loadBoard(), loadBoardLb()]); } catch (e) { if (el.isConnected) say(err(e)); } };

  $('#dl-form', el).addEventListener('submit', async ev => {
    ev.preventDefault();
    try {
      await post('/deals', { lead_id: Number($('#dl-lead', el).value), kind: $('#dl-kind', el).value, value_aed: Number($('#dl-value', el).value), title: $('#dl-title', el).value || null });
      $('#dl-value', el).value = ''; $('#dl-title', el).value = ''; say(esc(t('dl_added'))); await all();
    } catch (e) { say(err(e)); }
  });
  $('#dl-board', el).addEventListener('click', async ev => {
    const b = ev.target.closest('button'); const card = ev.target.closest('.dl-card');
    if (!b || !card) return;
    const id = Number(card.dataset.id), row = $('.dl-row', card);
    try {
      if (b.classList.contains('dl-move')) await post(`/deals/${id}/stage`, { stage: b.dataset.to });
      else if (b.classList.contains('dl-lose')) {
        row.innerHTML = `<input class="dl-reason" type="text" maxlength="300" placeholder="${esc(t('dl_reason'))}" aria-label="${esc(t('dl_reason'))}"> <button class="primary small dl-confirm-lose" type="button">${esc(t('dl_confirm'))}</button>`;
        return;
      } else if (b.classList.contains('dl-confirm-lose')) {
        const reason = $('.dl-reason', card).value.trim();
        if (!reason) { $('.dl-reason', card).focus(); return; }
        await post(`/deals/${id}/stage`, { stage: 'lost', lost_reason: reason });
      } else return;
      await all();
    } catch (e) { row.innerHTML = err(e); }
  });
  await loadLeads().catch(e => say(err(e)));
  await all();
}
