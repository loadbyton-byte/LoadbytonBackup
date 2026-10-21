import { $, esc, t, api, post, err, pill, money, when, state, isManager, applyData } from './core.js';

export async function render(el) {
  const mgr = isManager();
  el.innerHTML = `<h1>${esc(t('nav_leads'))}</h1><div class="grid2"><section>
    <div class="toolbar">
      <select id="tier" aria-label="${esc(t('tier'))}"><option value="">${esc(t('all_tiers'))}</option><option>HOT</option><option>WARM</option><option>NURTURE</option></select>
      <select id="label" aria-label="${esc(t('intent'))}"><option value="">${esc(t('all_intents'))}</option>${['buyer', 'investor', 'renter', 'seller', 'landlord', 'seeker'].map(x => `<option>${x}</option>`).join('')}</select>
    </div>
    ${mgr ? '<div class="tiles" id="tiles"></div>' : ''}
    <div class="card tablewrap"><table><thead><tr><th>${esc(t('h_score'))}</th><th>${esc(t('h_lead'))}</th><th>${esc(t('h_looking'))}</th><th>${esc(t('h_sources'))}</th><th>${esc(t('h_consent'))}</th></tr></thead><tbody id="rows"></tbody></table>
    <p id="empty" class="empty" hidden>${esc(t('empty_leads'))}</p></div></section>
    <aside>${mgr ? `<div class="card"><h2>${esc(t('radar'))} <span class="mute">${esc(t('radar_note'))}</span></h2><div id="radar"></div></div>` : ''}
    <div class="card"><h2>${esc(t('compliance'))}</h2><p class="mute">${esc(t('compliance_text'))}</p></div></aside></div>`;
  const load = async () => {
    const q = new URLSearchParams();
    if ($('#tier', el).value) q.set('tier', $('#tier', el).value);
    if ($('#label', el).value) q.set('label', $('#label', el).value);
    try {
      const [leads, stats, radar] = await Promise.all([api('/leads?' + q), mgr ? api('/stats') : null, mgr ? api('/radar') : null]);
      if (!el.isConnected) return;
      if (stats) {
        const c = stats.counters, tr = stats.tiers;
        $('#tiles', el).innerHTML = [['HOT', tr.HOT || 0], ['WARM', tr.WARM || 0], ['NURTURE', tr.NURTURE || 0], [t('noise'), (c.discarded_noise || 0) + (c.discarded_agent_spam || 0)]]
          .map(([k, v]) => `<div class="tile"><b>${Number(v)}</b><span>${esc(k)}</span></div>`).join('');
      }
      $('#empty', el).hidden = leads.length > 0;
      $('#rows', el).innerHTML = leads.map(l => `<tr class="row" data-id="${Number(l.id)}">
        <td>${pill(l.tier)} <span class="score">${Number(l.score)}</span></td>
        <td><b>${esc(l.display_name || 'Lead #' + l.id)}</b><br><span class="mute">${esc(l.label)} · ${esc(l.stage)}${l.profile?.wants_agent ? ' ★' : ''}</span></td>
        <td>${esc(l.primary_area || l.emirate || '—')}<br><span class="mute">${esc(money(l.budget_max))}${l.bedrooms != null ? ' · ' + Number(l.bedrooms) + 'BR' : ''}${l.timeline_days ? ' · ≤' + Number(l.timeline_days) + 'd' : ''}</span></td>
        <td>${(l.platforms || []).map(p => `<span class="tag">${esc(p)}</span>`).join('')}</td>
        <td class="${l.consent_status === 'opted_in' ? 'ok' : 'mute'}">${esc(l.consent_status)}</td></tr>`).join('');
      if (radar) {
        const max = Math.max(1, ...radar.map(r => r.demand_index));
        $('#radar', el).innerHTML = radar.slice(0, 10).map(r => `<div class="heat"><div><b>${esc(r.area)}</b><div class="bar"><i data-w="${Math.round(100 * r.demand_index / max)}"></i></div></div>
          <div><span class="${r.trend === 'heating' ? 'up' : r.trend === 'cooling' ? 'down' : 'mute'}">${r.trend === 'new' ? 'new' : r.growth_pct != null ? (r.growth_pct > 0 ? '▲' : '▼') + Math.abs(r.growth_pct) + '%' : ''}</span>
          <br><bdi class="mute">${Number(r.signals)} · ${Number(r.buy_share_pct)}% buy</bdi></div></div>`).join('') || '<span class="mute">—</span>';
        applyData(el);
      }
    } catch (e) { if (!el.isConnected) return; $('#rows', el).innerHTML = `<tr><td colspan="5">${err(e)}</td></tr>`; }
  };
  $('#rows', el).addEventListener('click', e => { const tr = e.target.closest('tr.row'); if (tr) openLead(Number(tr.dataset.id), load); });
  $('#tier', el).addEventListener('change', load);
  $('#label', el).addEventListener('change', load);
  await load();
}

/* ---------------------------------------------------------------- lead dialog (shared by Overview, Tasks and Agents) */
function loadNba(id) {
  const live = () => Number($('#dlg').dataset.id) === id;           // a slower answer for a previously opened lead is dropped
  return api(`/leads/${id}/next-actions`).then(a => {
    if (!live()) return;
    $('#nba').innerHTML = a.length ? a.map(k => `<div><b>${esc(t('a_' + k.action))}</b> ${k.channel ? `<span class="chip ${k.allowed ? 'ok' : 'no'}" title="${esc(k.compliance_reason)}">${esc(k.channel)}</span>` : ''}
      <span class="mute">${esc(k.reason)}</span></div>`).join('') : t('no_actions');
  }).catch(e => { if (live()) $('#nba').innerHTML = err(e); });
}

export async function openLead(id, onChange = () => {}) {
  const dlg = $('#dlg');
  let l;
  try { l = await api('/leads/' + id); } catch (e) { alert(e.message); return; }
  const bd = l.breakdown || {};
  const bars = ['intent', 'stage', 'specificity', 'urgency', 'financial_readiness', 'source_quality', 'contactability', 'asked_for_agent'].map(k =>
    `<div class="bd"><span class="mute">${esc(k.replace(/_/g, ' '))}</span><div class="bar"><i data-w="${Math.min(100, (bd[k] || 0) * 3.4)}"></i></div><span>${esc(bd[k] ?? 0)}</span></div>`).join('');
  const sigs = (l.signals || []).map(s => `<pre class="${/[؀-ۿ]/.test(s.text || '') ? 'ar' : ''}"><b>${esc(s.platform)}</b> · ${esc(s.source_type)} · ${Number(s.score)}\n${esc(s.text)}</pre>`).join('');
  dlg.innerHTML = `<div class="stack">
    <h2>${esc(l.display_name || 'Lead #' + l.id)} ${pill(l.tier)} ${Number(l.score)}</h2>
    <p class="mute">${esc(l.label)} · ${esc(l.stage)} · ${esc(l.primary_area || '')} · ${esc(money(l.budget_max))} · ${esc(l.consent_status)}${l.sla_due_at ? ' · ' + esc(t('reply_by')) + ' ' + esc(when(l.sla_due_at)) : ''}</p>
    <div><h2>${esc(t('why'))}</h2>${bars}<p class="mute"><bdi>×${esc(bd.recency_multiplier)} · +${Number(bd.cross_platform_bonus || 0)}${bd.repeat_enquiry ? ' · +' + Number(bd.repeat_enquiry) + ' repeat' : ''} ${(bd.flags || []).map(f => '· ⚠ ' + esc(f)).join(' ')}</bdi></p></div>
    <div><h2>${esc(t('nba'))}</h2><div id="nba" class="mute">…</div></div>
    <div><h2>${esc(t('matches'))}</h2><div id="matches" class="mute">…</div></div>
    <div><h2>${esc(t('dealsheet'))}</h2><button class="small" data-act="dealsheet" type="button">${esc(t('calc'))}</button><div id="dealsheet"></div></div>
    <div><h2>${esc(t('viewing'))}</h2><form id="viewform" class="inline" data-lead="${Number(l.id)}">
      <label>${esc(t('start'))}<input type="datetime-local" name="start" required></label>
      <label>${esc(t('duration'))}<select name="duration_min"><option>30</option><option>45</option><option>60</option></select></label>
      <label>${esc(t('location'))}<input name="location" maxlength="200"></label>
      <button class="primary" type="submit">${esc(t('save'))}</button></form><div id="viewmsg" class="mute"></div></div>
    <div><h2>${esc(t('status_log'))}</h2><div class="toolbar">
      <select id="statussel" aria-label="${esc(t('status'))}">${['new', 'contacted', 'qualified', 'viewing', 'won', 'lost'].map(s => `<option value="${s}"${l.status === s ? ' selected' : ''}>${esc(t('s_' + s))}</option>`).join('')}</select>
      <button class="small primary" data-act="status" type="button">${esc(t('save'))}</button>
      ${['call', 'message', 'follow_up', 'matches_sent', 'note'].map(k => `<button class="small" data-act="log" data-kind="${k}" type="button">${esc(t('k_' + k))}</button>`).join(' ')}
      </div><div id="statusmsg" class="mute"></div></div>
    <div><h2>${esc(t('drafts'))} <span class="mute">${esc(t('needs_approval'))}</span></h2>
      ${['public_reply', 'whatsapp', 'call', 'email'].map(c => `<button class="small" data-act="draft" data-ch="${c}" type="button">${c}</button>`).join(' ')}<div id="draft"></div></div>
    <div><h2>${esc(t('evidence'))}</h2>${sigs}</div>
    <p><button id="close" type="button">${esc(t('close'))}</button></p></div>`;
  dlg.dataset.id = l.id;
  dlg._onChange = onChange;
  applyData(dlg);
  if (dlg.open) dlg.close();
  dlg.showModal();
  $('#close').onclick = () => dlg.close();
  loadNba(id);
  api(`/leads/${id}/matches`).then(m => {
    if (Number($('#dlg').dataset.id) !== id) return;
    $('#matches').innerHTML = m.length ? m.map(x => `<div><b>${esc(x.project)}</b> <span class="chip">${Number(x.score)}</span>${x.golden_visa_fit ? '<span class="chip ok">golden visa</span>' : ''}
      <span class="mute">${esc(x.reasons.join(' · '))}</span></div>`).join('') : t('no_matches');
  }).catch(e => { if (Number($('#dlg').dataset.id) === id) $('#matches').innerHTML = err(e); });
}

const dlgEl = document.getElementById('dlg');
dlgEl.addEventListener('click', async e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const id = Number(dlgEl.dataset.id);
  if (b.dataset.act === 'dealsheet') {
    try {
      const d = await api(`/leads/${id}/deal-sheet`), n = x => Number(x).toLocaleString();
      $('#dealsheet').innerHTML = `<dl class="kv"><dt>${esc(t('cash_needed'))}</dt><dd><b>AED ${n(d.cash_needed_total)}</b></dd><dt>${esc(t('down'))}</dt><dd>AED ${n(d.down_payment)}</dd>
        <dt>${esc(t('loan'))}</dt><dd>AED ${n(d.loan)} (${Number(d.ltv_pct)}%)</dd><dt>${esc(t('monthly'))}</dt><dd>AED ${n(d.monthly_payment)}</dd><dt>${esc(t('costs'))}</dt><dd>AED ${n(d.costs.total_costs)}</dd></dl>
        <p class="mute">${esc(d.disclaimer)}</p>`;
    } catch (x) { $('#dealsheet').innerHTML = err(x); }
  } else if (b.dataset.act === 'status' || b.dataset.act === 'log') {
    const msg = $('#statusmsg'); msg.textContent = '';
    try {
      if (b.dataset.act === 'status') await post(`/leads/${id}/status`, { status: $('#statussel').value });
      else await post(`/leads/${id}/activities`, { kind: b.dataset.kind });
      msg.textContent = t('saved');
      loadNba(id); dlgEl._onChange?.();
    } catch (x) { msg.innerHTML = err(x); }
  } else if (b.dataset.act === 'draft') {
    try {
      const d = await post(`/leads/${id}/outreach?channel=${encodeURIComponent(b.dataset.ch)}`);
      $('#draft').innerHTML = `<p class="${d.compliance.allowed ? 'ok' : 'no'}">${d.compliance.allowed ? '✔ ' + esc(t('allowed')) : '✖ ' + esc(t('blocked'))} — ${esc(d.compliance.reason)}</p>
        <pre class="${d.language === 'ar' ? 'ar' : ''}">${esc(d.body)}</pre>${d.notes.map(n => `<div class="mute">• ${esc(n)}</div>`).join('')}`;
    } catch (x) { $('#draft').innerHTML = err(x); }
  }
});
dlgEl.addEventListener('submit', async e => {
  if (e.target.id !== 'viewform') return;
  e.preventDefault(); const f = new FormData(e.target);
  try {
    await post(`/leads/${Number(e.target.dataset.lead)}/viewings`, { start: f.get('start'), duration_min: Number(f.get('duration_min')), location: f.get('location') });
    $('#viewmsg').textContent = t('saved');
  } catch (x) { $('#viewmsg').innerHTML = err(x); }
});
