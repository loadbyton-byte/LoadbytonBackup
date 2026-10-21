import { $, esc, t, api, err, num } from './core.js';

export async function render(el) {
  el.innerHTML = `<h1>${esc(t('nav_inventory'))}</h1><div class="card">
    <div class="tablewrap"><table><thead><tr><th>${esc(t('h_project'))}</th><th>${esc(t('h_area'))}</th><th>${esc(t('h_price'))}</th><th>${esc(t('h_beds'))}</th><th>${esc(t('h_handover'))}</th></tr></thead><tbody id="inv-rows"></tbody></table></div>
    <p id="inv-empty" class="empty" hidden>${esc(t('empty_inv'))}</p></div>
  <div class="card"><h2>${esc(t('import_csv'))}</h2>
    <textarea id="inv-csv" spellcheck="false" aria-label="CSV">name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan</textarea>
    <div class="toolbar"><button id="inv-import" class="primary" type="button">${esc(t('import'))}</button><span id="inv-out" class="mute"></span></div></div>`;
  const load = async () => {
    try {
      const rows = await api('/inventory');
      $('#inv-empty', el).hidden = rows.length > 0;
      $('#inv-rows', el).innerHTML = rows.map(p => `<tr><td><b>${esc(p.name)}</b><br><span class="mute">${esc(p.developer || '')}</span></td><td>${esc(p.area || '—')}</td>
        <td>${Number(p.min_price).toLocaleString()} – ${Number(p.max_price).toLocaleString()}</td><td>${esc(p.bedrooms.map(b => b === 0 ? 'Studio' : b).join(', '))}</td><td>${esc(num(p.handover_year))}</td></tr>`).join('');
    } catch (e) { if (!el.isConnected) return; $('#inv-rows', el).innerHTML = `<tr><td colspan="5">${err(e)}</td></tr>`; }
  };
  $('#inv-import', el).onclick = async () => {
    try {
      const r = await api('/inventory', { method: 'POST', body: $('#inv-csv', el).value, headers: { 'content-type': 'text/csv' } });
      $('#inv-out', el).textContent = `${t('imported')} ${r.imported} · ${t('skipped')} ${r.skipped.length}` + (r.skipped.length ? ' (' + r.skipped.map(s => `#${s[0]}: ${s[1]}`).join('; ') + ')' : '');
    } catch (x) { $('#inv-out', el).textContent = x.message; }
    load();
  };
  await load();
}
