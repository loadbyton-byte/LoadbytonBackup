import { $, esc, t, api, post, err, num } from './core.js';

let CITY_CACHE = null;

export async function render(el) {
  el.innerHTML = `<h1>${esc(t('nav_roi'))}</h1><div class="card">
    <div class="toolbar"><label class="mute" for="roi-period">${esc(t('h_period'))}</label><input id="roi-period" placeholder="2026-10" maxlength="7" aria-label="${esc(t('h_period'))}"><span class="mute">${esc(t('period_note'))}</span></div>
    <div class="tablewrap"><table><thead><tr><th>${esc(t('h_source'))}</th><th>${esc(t('h_leads'))}</th><th>${esc(t('h_qualified'))}</th><th>${esc(t('h_won'))}</th><th>${esc(t('h_sla'))}</th><th>${esc(t('h_resp'))}</th><th>${esc(t('h_spend'))}</th><th>${esc(t('h_cpq'))}</th><th>${esc(t('h_cpw'))}</th></tr></thead><tbody id="roi-rows"></tbody></table></div>
    <form id="spend-form" class="inline">
      <label><span>${esc(t('h_source'))}</span><input name="source" required placeholder="reddit"></label>
      <label><span>${esc(t('campaign'))}</span><input name="campaign"></label>
      <label><span>${esc(t('h_period'))}</span><input name="period" required placeholder="2026-10"></label>
      <label><span>${esc(t('roi_country'))}</span><select name="country" id="spend-country"><option value="">—</option></select></label>
      <label><span>${esc(t('roi_city'))}</span><input name="city" id="spend-city" list="spend-cities" maxlength="80" autocomplete="off"><datalist id="spend-cities"></datalist></label>
      <label><span>${esc(t('h_spend'))}</span><input name="amount_aed" type="number" min="0" step="0.01" required></label>
      <button class="primary" type="submit">${esc(t('add_spend'))}</button></form>
    <p class="mute">${esc(t('roi_note'))}</p></div>`;
  const load = async () => {
    try {
      const per = $('#roi-period', el).value.trim();
      const rows = await api('/roi' + (per ? '?period=' + encodeURIComponent(per) : ''));
      $('#roi-rows', el).innerHTML = rows.map(r => `<tr><td><b>${esc(r.source)}</b></td><td>${Number(r.leads)}</td><td>${Number(r.qualified)}</td><td>${Number(r.won)}</td>
        <td>${r.within_sla_pct == null ? '—' : Number(r.within_sla_pct) + '%'}</td><td>${esc(num(r.median_first_response_min))}</td><td>${Number(r.spend_aed).toLocaleString()}</td>
        <td>${esc(num(r.cost_per_qualified))}</td><td>${esc(num(r.cost_per_won))}</td></tr>`).join('');
    } catch (e) { if (!el.isConnected) return; $('#roi-rows', el).innerHTML = `<tr><td colspan="9">${err(e)}</td></tr>`; }
  };
  (async () => {                                              // catalogue-backed market selectors for tagging spend
    try {
      CITY_CACHE = CITY_CACHE || await api('/markets/cities?limit=500');
      if (!el.isConnected) return;
      const seen = new Map(CITY_CACHE.map(c => [c.country, c.country_name]));
      $('#spend-country', el).insertAdjacentHTML('beforeend', [...seen].sort((a, b) => a[1].localeCompare(b[1])).map(([cc, n]) => `<option value="${esc(cc)}">${esc(n)}</option>`).join(''));
      $('#spend-cities', el).innerHTML = CITY_CACHE.map(c => `<option value="${esc(c.name)}"></option>`).join('');
    } catch (e) { /* selectors are optional */ }
  })();
  $('#spend-form', el).addEventListener('submit', async e => {
    e.preventDefault(); const f = new FormData(e.target);
    try { await post('/spend', { source: f.get('source'), campaign: f.get('campaign') || null, period: f.get('period'), amount_aed: Number(f.get('amount_aed')), country: f.get('country') || null, city: f.get('city') || null }); e.target.reset(); }
    catch (x) { alert(x.message); }
    load();
  });
  $('#roi-period', el).addEventListener('keydown', e => { if (e.key === 'Enter') load(); });
  $('#roi-period', el).addEventListener('change', load);
  await load();
}
