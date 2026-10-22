import { esc, t, api, post, state, err, applyData, isManager, money } from './core.js';
import { navigate } from './router.js';

export async function render(el) {
  const [o, team] = await Promise.all([api('/overview'), state.me.role === 'owner' ? api('/auth/team').catch(() => null) : null]);
  const ob = { ...o.onboarding, ...(team ? { team: team.members.length > 1 } : {}) };
  const steps = [['agents', 'ov_ob_agents', 'agents'], ['inventory', 'ov_ob_inventory', 'inventory'], ['widget_origin', 'ov_ob_origin', 'settings'],
    ['whatsapp', 'ov_ob_whatsapp', 'settings'], ...(team ? [['team', 'ov_ob_team', 'settings']] : [])];
  const open = steps.filter(([k]) => !ob[k]);
  const total = Object.values(o.by_status).reduce((a, b) => a + b, 0);
  const max = Math.max(1, ...Object.values(o.by_status)), tmax = Math.max(1, ...o.trend.map(d => d.leads));
  const bw = 100 / o.trend.length;
  const tile = (k, v) => `<div class="tile"><b data-kpi="${esc(k)}">${esc(v)}</b><span>${esc(t(k))}</span></div>`;
  el.innerHTML = `<h1>${esc(t('ov_title'))}</h1>
    <div class="tiles">${tile('ov_new_today', o.new_today)}${tile('ov_hot', o.hot)}${tile('ov_warm', o.warm)}${tile('ov_overdue', o.overdue)}${tile('ov_sla', o.within_sla_pct == null ? '—' : o.within_sla_pct + '%')}${tile('ov_pipeline', money(o.pipeline_value_aed))}${tile('ov_expected', money(o.expected_commission_aed))}</div>
    ${total === 0 ? `<div class="card empty"><p>${esc(t('ov_empty'))}</p>${state.me.role === 'owner' ? `<p><button id="seed" class="primary" type="button">${esc(t('ov_seed'))}</button></p><p id="seed-msg" class="mute"></p>` : ''}</div>` : ''}
    <div class="grid2"><div>
      <div class="card"><h2>${esc(t('ov_trend'))}</h2>
        <svg viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label="${esc(t('ov_trend'))}" width="100%" height="90">
          ${o.trend.map((d, i) => { const h = d.leads / tmax * 38; return `<rect x="${(i * bw + bw * .15).toFixed(2)}" y="${(40 - h).toFixed(2)}" width="${(bw * .7).toFixed(2)}" height="${h.toFixed(2)}" fill="currentColor" class="up"><title>${esc(d.date)}: ${Number(d.leads)}</title></rect>`; }).join('')}
        </svg></div>
      <div class="card"><h2>${esc(t('ov_status'))}</h2>
        ${Object.entries(o.by_status).map(([k, v]) => `<div class="bd"><span>${esc(t('s_' + k))}</span><div class="bar"><i data-w="${Math.round(100 * v / max)}"></i></div><span>${Number(v)}</span></div>`).join('')}</div>
    </div><aside>
      <div class="card"><h2>${esc(t('ov_onboarding'))}</h2>
        ${open.length ? steps.map(([k, label, page]) => `<div class="check ${ob[k] ? 'done' : ''}"><span class="dot" aria-hidden="true">${ob[k] ? '✓' : ''}</span>
          <a class="t" href="#/${page}" data-ob="${esc(k)}" aria-label="${esc(t(label))}${ob[k] ? ' ✓' : ''}">${esc(t(label))}</a></div>`).join('') : `<p class="ok">${esc(t('ov_all_done'))}</p>`}
      </div></aside></div>`;
  applyData(el);
  const seed = el.querySelector('#seed');
  if (seed) seed.onclick = async () => {
    try { await post('/demo/seed'); navigate('leads'); } catch (e) { el.querySelector('#seed-msg').innerHTML = err(e); }
  };
}
