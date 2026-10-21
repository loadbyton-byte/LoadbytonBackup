import { $, esc, t, api, err, num, applyData } from './core.js';

const pct = v => (v === null || v === undefined) ? '—' : v + '%';
const growth = v => v === null || v === undefined ? '—' : `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : 'mute'}">${v > 0 ? '▲' : v < 0 ? '▼' : ''}${Math.abs(Number(v))}%</span>`;
const money = v => v === null || v === undefined ? '—' : Number(v).toLocaleString();

function row(r, cls = '') {
  return `<tr class="${cls}" data-id="${esc(r.id)}" data-country="${esc(r.country)}"><td><b>${esc(r.name)}</b>${r.top_city ? `<br><span class="mute">${esc(r.top_city)}</span>` : ''}</td>
    <td>${Number(r.leads)}</td><td>${Number(r.qualified)}</td><td>${Number(r.won)}</td><td>${num(r.avg_score)}</td><td>${Number(r.share_pct)}%</td><td>${growth(r.growth_pct)}</td>
    <td>${money(r.spend_aed)}</td><td>${num(r.cost_per_lead)}</td><td class="mute">${esc(r.contact_window || '—')}</td></tr>`;
}

export async function render(el) {
  el.innerHTML = `<h1>${esc(t('nav_markets'))}</h1>
    <div class="toolbar"><label class="mute" for="mk-period">${esc(t('h_period'))}</label><input id="mk-period" placeholder="2026-10" maxlength="7" aria-label="${esc(t('h_period'))}"><span class="mute">${esc(t('period_note'))}</span></div>
    <div id="mk-body"><p class="mute">${esc(t('loading'))}</p></div>`;
  const load = async () => {
    const per = $('#mk-period', el).value.trim();
    const body = $('#mk-body', el);
    try {
      const r = await api('/markets/intel' + (per ? '?period=' + encodeURIComponent(per) : ''));
      if (!el.isConnected) return;
      if (!r.countries.length) {
        body.innerHTML = `<div class="card empty"><p>${esc(t('mk_empty'))}</p><p class="mute">${esc(t('mk_empty_help'))}</p></div>${untapped(r)}`;
        return;
      }
      const top = r.countries.find(c => c.country !== 'AE') || r.countries[0];
      body.innerHTML = `<div class="tiles">
          <div class="tile"><b data-kpi="known">${esc(pct(r.known_origin_pct))}</b><span>${esc(t('mk_known'))}</span></div>
          <div class="tile"><b data-kpi="top-country">${esc(top.name)}</b><span>${esc(t('mk_top_country'))}</span></div>
          <div class="tile"><b data-kpi="top-focus">${esc(r.focus[0] ? r.focus[0].name : '—')}</b><span>${esc(t('mk_top_focus'))}</span></div>
          <div class="tile"><b>${money(r.unallocated_spend_aed)}</b><span>${esc(t('mk_unallocated'))}</span></div></div>
        <div class="card"><h2>${esc(t('mk_countries'))}</h2><div class="tablewrap"><table id="mk-table"><thead><tr><th>${esc(t('mk_h_market'))}</th><th>${esc(t('h_leads'))}</th><th>${esc(t('h_qualified'))}</th><th>${esc(t('h_won'))}</th>
          <th>${esc(t('mk_h_score'))}</th><th>${esc(t('mk_h_share'))}</th><th>${esc(t('mk_h_growth'))}</th><th>${esc(t('h_spend'))}</th><th>${esc(t('mk_h_cpl'))}</th><th>${esc(t('mk_h_window'))}</th></tr></thead>
          <tbody>${r.countries.map(c => row(c, 'row mk-country')).join('')}</tbody></table></div><p class="mute">${esc(t('mk_expand'))}</p></div>
        <div class="card"><h2>${esc(t('mk_focus'))}</h2><p class="mute">${esc(t('mk_prior_note'))}</p>
          ${r.focus.length ? r.focus.map(f => `<div class="heat"><div><b>${esc(f.name)}</b> <span class="chip">${esc(t(f.basis === 'measured' ? 'mk_measured' : 'mk_editorial'))} · ${Number(f.leads)} ${esc(t('mk_leads_n'))}</span>
              <div class="bar"><i data-w="${Math.round(f.score)}"></i></div><div class="mute">${f.reasons.map(k => `<span class="tag">${esc(t('mk_reason_' + k))}</span>`).join('')}</div></div>
              <div><b>${esc(f.score)}</b></div></div>`).join('') : `<p class="mute">—</p>`}</div>
        ${untapped(r)}`;
      body.dataset.cities = JSON.stringify(r.cities);
      applyData(body);
    } catch (e) { if (el.isConnected) body.innerHTML = `<p class="msg err" role="alert">${err(e)}</p>`; }
  };
  const untapped = r => `<div class="card"><h2>${esc(t('mk_untapped'))}</h2><p class="mute">${esc(t('mk_untapped_note'))}</p>
    <div class="tablewrap"><table id="mk-untapped"><tbody>${r.untapped.map(u => `<tr><td><b>${esc(u.name)}</b> <span class="mute">${esc(u.country)}</span></td><td>${esc(u.language || '—')}</td><td class="mute">${esc(u.contact_window || '—')}</td></tr>`).join('')}</tbody></table></div></div>`;
  el.addEventListener('click', e => {
    const tr = e.target.closest('tr.mk-country');
    if (!tr) return;
    const open = tr.dataset.open === '1';
    el.querySelectorAll(`tr.mk-city[data-country="${CSS.escape(tr.dataset.country)}"]`).forEach(x => x.remove());
    tr.dataset.open = open ? '0' : '1';
    if (open) return;
    const cities = JSON.parse($('#mk-body', el).dataset.cities || '[]').filter(c => c.country === tr.dataset.id);
    tr.insertAdjacentHTML('afterend', cities.map(c => row(c, 'mk-city')).join(''));
  });
  $('#mk-period', el).addEventListener('keydown', e => { if (e.key === 'Enter') load(); });
  $('#mk-period', el).addEventListener('change', load);
  await load();
}
