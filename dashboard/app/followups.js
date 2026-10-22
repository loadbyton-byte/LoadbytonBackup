import { $, esc, t, api, post, put, err, when, num } from './core.js';

const CHANNELS = ['auto', 'whatsapp', 'email'];

function stepRow(s, i) {
  return `<fieldset class="fu-step" data-i="${i}"><legend>${esc(t('fu_step'))} ${i + 1}</legend>
    <label>${esc(t('fu_delay'))}<input class="fu-delay" type="number" min="0" max="2160" value="${Number(s.delay_hours)}"></label>
    <label>${esc(t('fu_channel'))}<select class="fu-channel">${CHANNELS.map(c => `<option value="${c}"${c === s.channel ? ' selected' : ''}>${esc(t('fu_ch_' + c))}</option>`).join('')}</select></label>
    <label>${esc(t('fu_text_en'))}<textarea class="fu-en" rows="3" maxlength="1000">${esc(s.template_en)}</textarea></label>
    <label>${esc(t('fu_text_ar'))}<textarea class="fu-ar" rows="3" maxlength="1000" dir="rtl">${esc(s.template_ar || '')}</textarea></label></fieldset>`;
}

export async function render(el) {
  el.innerHTML = `<h1>${esc(t('nav_followups'))}</h1>
    <div class="card"><label class="check"><input id="fu-auto" type="checkbox"> <b>${esc(t('fu_autosend'))}</b></label>
      <p class="mute">${esc(t('fu_autosend_note'))}</p><p id="fu-live" class="mute"></p><p id="fu-msg" role="status"></p></div>
    <div class="card"><h2>${esc(t('fu_stats'))}</h2><div id="fu-stats" class="tiles"></div></div>
    <div class="card"><h2>${esc(t('fu_queue'))}</h2><div id="fu-queue"></div><p id="fu-empty" class="empty" hidden>${esc(t('fu_empty'))}</p></div>
    <div class="card"><h2>${esc(t('fu_sequences'))}</h2><div id="fu-seq"></div><button id="fu-save" class="primary" type="button">${esc(t('fu_save'))}</button> <span id="fu-save-msg" role="status"></span></div>`;
  let seqs = [];
  const say = (id, html) => { const n = $(id, el); if (n) n.innerHTML = html; };

  const loadSettings = async () => {
    const s = await api('/followups/settings');
    $('#fu-auto', el).checked = s.autosend;
    say('#fu-live', esc(t('fu_live')) + ' WhatsApp: ' + esc(t(s.whatsapp_live ? 'fu_yes' : 'fu_no')) + ' · Email: ' + esc(t(s.email_live ? 'fu_yes' : 'fu_no')));
  };
  const loadStats = async () => {
    const s = await api('/followups/stats');
    const k = (key, label, v) => `<div class="tile"><b data-kpi="${key}">${esc(v)}</b><span>${esc(label)}</span></div>`;
    say('#fu-stats', k('median', t('fu_median'), s.first_response_median_minutes === null ? '—' : s.first_response_median_minutes + ' min')
      + k('sla', t('fu_sla'), s.within_sla_pct === null ? '—' : s.within_sla_pct + '%') + k('queued', t('fu_queued'), num(s.queued)) + k('failed', t('fu_failed'), num(s.failed)));
  };
  const loadQueue = async () => {
    const q = await api('/followups/queue');
    $('#fu-empty', el).hidden = q.length > 0;
    say('#fu-queue', q.map(x => `<div class="fu-item" data-id="${Number(x.id)}">
      <p><b>${esc(x.display_name || 'Lead #' + x.lead_id)}</b> <span class="chip">${esc(x.channel)}</span> <span class="mute">${esc(when(x.due_at))}</span></p>
      ${x.note ? `<p class="mute">${esc(x.note)}</p>` : ''}
      <textarea class="fu-body" rows="4" maxlength="2000" aria-label="${esc(t('fu_message'))}">${esc(x.body || '')}</textarea>
      <button class="primary small fu-approve" type="button">${esc(t('fu_approve'))}</button> <button class="ghost small fu-skip" type="button">${esc(t('fu_skip'))}</button>
      <span class="fu-row-msg" role="status"></span></div>`).join(''));
  };
  const loadSeqs = async () => {
    seqs = await api('/followups/sequences');
    say('#fu-seq', seqs.map((s, si) => `<details class="fu-sequence" data-si="${si}"${si === 0 ? ' open' : ''}><summary><b>${esc(s.name)}</b></summary>
      <label class="check"><input class="fu-enabled" type="checkbox"${s.enabled ? ' checked' : ''}> ${esc(t('fu_enabled'))}</label>${s.steps.map(stepRow).join('')}</details>`).join(''));
  };
  const all = async () => { try { await Promise.all([loadSettings(), loadStats(), loadQueue(), loadSeqs()]); } catch (e) { if (el.isConnected) say('#fu-msg', err(e)); } };

  $('#fu-auto', el).addEventListener('change', async ev => {
    const on = ev.target.checked;
    try { await put('/followups/settings', { autosend: on }); say('#fu-msg', esc(t(on ? 'fu_auto_on' : 'fu_auto_off'))); } catch (e) { ev.target.checked = !on; say('#fu-msg', err(e)); }
  });
  $('#fu-queue', el).addEventListener('click', async ev => {
    const b = ev.target.closest('button'); const item = ev.target.closest('.fu-item');
    if (!b || !item) return;
    const id = Number(item.dataset.id), msg = $('.fu-row-msg', item);
    try {
      if (b.classList.contains('fu-approve')) await post(`/followups/${id}/approve`, { body: $('.fu-body', item).value });
      else if (b.classList.contains('fu-skip')) await post(`/followups/${id}/skip`);
      else return;
      await Promise.all([loadQueue(), loadStats()]);
    } catch (e) { msg.innerHTML = err(e); }
  });
  $('#fu-save', el).addEventListener('click', async () => {
    const out = [...el.querySelectorAll('.fu-sequence')].map(d => ({
      name: seqs[Number(d.dataset.si)].name, trigger: seqs[Number(d.dataset.si)].trigger, enabled: $('.fu-enabled', d).checked,
      steps: [...d.querySelectorAll('.fu-step')].map(f => ({ delay_hours: Number($('.fu-delay', f).value), channel: $('.fu-channel', f).value,
        template_en: $('.fu-en', f).value, template_ar: $('.fu-ar', f).value || null })) }));
    try { await put('/followups/sequences', out); say('#fu-save-msg', esc(t('fu_saved'))); await loadSeqs(); } catch (e) { say('#fu-save-msg', err(e)); }
  });
  await all();
}
