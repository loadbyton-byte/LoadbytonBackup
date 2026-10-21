import { $, esc, t, api, post, put, err, state } from './core.js';

const SECRETS = [['meta_verify_token', 'st_meta_verify'], ['meta_app_secret', 'st_meta_secret'], ['meta_page_token', 'st_meta_page'], ['whatsapp_token', 'st_wa_token'], ['form_webhook_secret', 'st_form_secret']];

export async function render(el) {
  const [s, team, agents] = await Promise.all([api('/auth/settings'), api('/auth/team'), api('/agents').catch(() => [])]);
  const me = state.me;
  const flash = (sel, text, bad) => { const m = $(sel, el); m.className = 'msg ' + (bad ? 'err' : 'good'); m.textContent = text; };
  el.innerHTML = `<h1>${esc(t('st_title'))}</h1>

  <div class="card"><h2>${esc(t('st_profile'))}</h2>
    <form id="profile" class="stack" data-form="profile">
      <label>${esc(t('st_agency'))}<input name="agency_name" required maxlength="120" value="${esc(s.agency_name)}"></label>
      <label>${esc(t('st_agent'))}<input name="agent_name" maxlength="120" value="${esc(s.agent_name)}"></label>
      <label>${esc(t('st_brn'))}<input name="brn" maxlength="120" value="${esc(s.brn)}"></label>
      <label>${esc(t('st_trak'))}<input name="trakheesi" maxlength="120" value="${esc(s.trakheesi)}"></label>
      <div><button class="primary" type="submit">${esc(t('st_save'))}</button></div><p id="profile-msg" class="msg" role="status"></p></form></div>

  <div class="card"><h2>${esc(t('st_embed'))}</h2><p class="mute">${esc(t('st_embed_note'))}</p>
    <pre id="snippet">${esc(s.embed.snippet)}</pre>
    <form id="origins" class="stack"><label>${esc(t('st_origins'))}<textarea name="origins" spellcheck="false" placeholder="https://www.example.ae">${esc(s.capture_origins.join('\n'))}</textarea></label>
      <div><button class="primary" type="submit">${esc(t('st_save'))}</button></div><p id="origins-msg" class="msg" role="status"></p></form>
    <p class="mute">${esc(t('st_webhooks'))}: <code id="hook-base">${esc(s.embed.widget_url.replace(/\/widget\.js$/, ''))}</code></p></div>

  <div class="card"><h2>${esc(t('st_integrations'))}</h2><p class="mute">${esc(t('st_integrations_note'))}</p>
    <form id="integrations" class="stack">
      <label>${esc(t('st_wa_phone'))}<input name="whatsapp_phone_id" maxlength="120" value="${esc(s.whatsapp_phone_id)}"></label>
      ${SECRETS.map(([k, label]) => `<label>${esc(t(label))}${s.secrets[k] ? ` <span class="ok">(${esc(t('st_is_set'))})</span>` : ''}<input name="${k}" type="password" autocomplete="off" maxlength="300"></label>`).join('')}
      <div><button class="primary" type="submit">${esc(t('st_save'))}</button></div><p id="integrations-msg" class="msg" role="status"></p></form></div>

  <div class="card"><h2>${esc(t('st_apikey'))}</h2><p class="mute">${esc(t('st_apikey_note'))}</p>
    <button id="rotate" type="button">${esc(t('st_rotate'))}</button><div id="newkey"></div></div>

  <div class="card"><h2>${esc(t('st_security'))}</h2>
    <h3>${esc(t('st_mfa'))}</h3><div id="mfa"></div>
    <h3>${esc(t('st_password'))}</h3>
    <form id="pwform" class="stack"><label>${esc(t('st_pw_now'))}<input name="old_password" type="password" autocomplete="current-password" required></label>
      <label>${esc(t('st_pw_new'))}<input name="new_password" type="password" autocomplete="new-password" minlength="12" required></label>
      <div><button type="submit">${esc(t('st_pw_change'))}</button></div><p id="pw-msg" class="msg" role="status"></p></form></div>

  <div class="card"><h2>${esc(t('st_team'))}</h2>
    <h3>${esc(t('st_members'))}</h3>
    <table id="members"><tbody>${team.members.map(m => `<tr><td>${esc(m.name || '')}</td><td>${esc(m.email)}</td><td>${esc(t('role_' + m.role))}</td></tr>`).join('')}</tbody></table>
    <div id="pending">${team.invites.length ? `<h3>${esc(t('st_pending'))}</h3><table><tbody>${team.invites.map(i => `<tr><td>${esc(i.email)}</td><td>${esc(t('role_' + i.role))}</td></tr>`).join('')}</tbody></table>` : ''}</div>
    <h3>${esc(t('st_invite'))}</h3>
    <form id="invite" class="stack"><label>${esc(t('st_invite_email'))}<input name="email" type="email" required maxlength="254"></label>
      <label>${esc(t('st_invite_role'))}<select name="role"><option value="agent">${esc(t('st_role_agent'))}</option><option value="manager">${esc(t('st_role_manager'))}</option></select></label>
      <label>${esc(t('st_invite_agent'))}<select name="agent_id"><option value="">${esc(t('st_none'))}</option>${agents.map(a => `<option value="${Number(a.id)}">${esc(a.name)}</option>`).join('')}</select></label>
      <div><button class="primary" type="submit">${esc(t('st_invite_send'))}</button></div><p id="invite-msg" class="msg" role="status"></p></form></div>`;

  const save = async (body, msgSel) => {
    try { await put('/auth/settings', body); flash(msgSel, t('saved')); } catch (x) { flash(msgSel, x.message, true); }
  };
  $('#profile', el).addEventListener('submit', e => { e.preventDefault(); const f = new FormData(e.target); save(Object.fromEntries(['agency_name', 'agent_name', 'brn', 'trakheesi'].map(k => [k, f.get(k)])), '#profile-msg'); });
  $('#origins', el).addEventListener('submit', e => {
    e.preventDefault();
    save({ capture_origins: String(new FormData(e.target).get('origins')).split('\n').map(x => x.trim()).filter(Boolean) }, '#origins-msg');
  });
  $('#integrations', el).addEventListener('submit', async e => {
    e.preventDefault(); const f = new FormData(e.target);
    await save(Object.fromEntries(['whatsapp_phone_id', ...SECRETS.map(x => x[0])].map(k => [k, f.get(k)])), '#integrations-msg');
    e.target.querySelectorAll('input[type=password]').forEach(i => { i.value = ''; });
  });
  $('#rotate', el).onclick = async () => {
    try { const r = await post('/auth/api-key'); $('#newkey', el).innerHTML = `<p>${esc(t('st_new_key'))}</p><div class="secret" id="apikey">${esc(r.api_key)}</div>`; }
    catch (x) { $('#newkey', el).innerHTML = err(x); }
  };
  $('#pwform', el).addEventListener('submit', async e => {
    e.preventDefault(); const f = new FormData(e.target);
    try { await post('/auth/password', { old_password: f.get('old_password'), new_password: f.get('new_password') }); flash('#pw-msg', t('saved')); e.target.reset(); }
    catch (x) { flash('#pw-msg', x.message, true); }
  });
  $('#invite', el).addEventListener('submit', async e => {
    e.preventDefault(); const f = new FormData(e.target);
    try {
      await post('/auth/invite', { email: f.get('email'), role: f.get('role'), agent_id: f.get('agent_id') ? Number(f.get('agent_id')) : null });
      flash('#invite-msg', t('st_invited')); e.target.reset();
    } catch (x) { flash('#invite-msg', x.message, true); }
  });

  /* two-factor authentication */
  const mfa = $('#mfa', el);
  const drawMfa = enabled => {
    mfa.innerHTML = enabled
      ? `<p class="ok">${esc(t('st_mfa_on'))}</p><form id="mfa-off" class="stack"><label>${esc(t('st_pw_now'))}<input name="password" type="password" autocomplete="current-password" required></label><div><button type="submit">${esc(t('st_mfa_disable'))}</button></div><p id="mfa-msg" class="msg" role="status"></p></form>`
      : `<p class="mute">${esc(t('st_mfa_off'))}</p><button id="mfa-begin" type="button">${esc(t('st_mfa_enable'))}</button><div id="mfa-setup"></div>`;
    const begin = $('#mfa-begin', mfa);
    if (begin) begin.onclick = async () => {
      try {
        const r = await post('/auth/mfa/begin');
        begin.hidden = true;
        $('#mfa-setup', mfa).innerHTML = `<p>${esc(t('st_mfa_scan'))}</p><p>${esc(t('st_mfa_secret'))}: <span class="secret" id="mfa-secret">${esc(r.secret)}</span></p>
          <form id="mfa-confirm" class="stack"><label>${esc(t('st_mfa_code'))}<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" required></label><div><button class="primary" type="submit">${esc(t('st_mfa_confirm'))}</button></div><p id="mfa-msg" class="msg" role="status"></p></form>`;
      } catch (x) { mfa.insertAdjacentHTML('beforeend', `<p class="msg err">${esc(x.message)}</p>`); }
    };
  };
  mfa.addEventListener('submit', async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      if (e.target.id === 'mfa-confirm') {
        const r = await post('/auth/mfa/confirm', { code: f.get('code') });
        me.mfa_enabled = true;
        mfa.innerHTML = `<p class="ok">${esc(t('st_mfa_on'))}</p><p>${esc(t('st_recovery'))}</p><div class="secret" id="recovery">${r.recovery_codes.map(esc).join('<br>')}</div>`;
      } else if (e.target.id === 'mfa-off') {
        await post('/auth/mfa/disable', { password: f.get('password') });
        me.mfa_enabled = false; drawMfa(false);
      }
    } catch (x) { const m = $('#mfa-msg', mfa); if (m) { m.className = 'msg err'; m.textContent = x.message; } }
  });
  drawMfa(!!me.mfa_enabled);
}
