import { $, esc, t, state, api, post, setLang, setTheme, currentTheme, readCookie } from './core.js';
import { pagesFor, startRouter, renderCurrent, navigate } from './router.js';

function actions() {
  return [
    ...pagesFor(state.me.role).map(p => ({ label: `${t('go')} ${t('nav_' + p.id)}`, hint: t('go'), run: () => navigate(p.id) })),
    { label: t('act_theme'), hint: t('action'), run: () => { setTheme(currentTheme() === 'dark' ? 'light' : 'dark'); } },
    { label: t('act_lang'), hint: t('action'), run: toggleLang },
    { label: t('act_signout'), hint: t('action'), run: signOut },
  ];
}

async function signOut() {
  try { await post('/auth/logout'); } catch (e) { /* already signed out */ }
  location.href = '/login';
}
function toggleLang() { setLang(state.lang === 'ar' ? 'en' : 'ar'); build(); renderCurrent(); }

function build() {
  const me = state.me;
  const nav = pagesFor(me.role).map(p => `<a href="#/${p.id}" data-page="${p.id}">${esc(t('nav_' + p.id))}</a>`).join('');
  $('#app').innerHTML = `
    <aside class="side" id="side" aria-label="${esc(t('menu'))}">
      <div class="brand"><img src="/static/brand/wordmark-clear.png" alt="DigitalBurj" width="120" height="23"><span>Leads</span></div>
      <nav aria-label="${esc(t('menu'))}">${nav}</nav>
      <div class="grow"></div>
      <div class="who">${esc(me.user.email)}</div>
      <nav><a href="#/signout" id="signout">${esc(t('signout'))}</a></nav>
    </aside>
    <div class="col">
      <header class="topbar">
        <button id="menu-btn" type="button" aria-label="${esc(t('menu'))}" aria-controls="side">☰</button>
        <span class="ws" id="ws">${esc(me.workspace.name)}</span>
        <span class="role">${esc(t('role_' + me.role))}</span>
        <button id="open-palette" type="button" class="ghost small" aria-label="${esc(t('search'))}">${esc(t('search').replace(/ \(.*/, ''))} <kbd>Ctrl K</kbd></button>
        <button id="theme-btn" type="button" class="ghost small" aria-label="${esc(t('theme'))}">◐</button>
        <button id="lang-btn" type="button" class="ghost small" aria-label="${esc(t('act_lang'))}">${state.lang === 'ar' ? 'English' : 'العربية'}</button>
      </header>
      <main id="view" tabindex="-1"></main>
    </div>`;
  $('#menu-btn').onclick = () => document.body.classList.toggle('nav-open');
  $('#theme-btn').onclick = () => setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
  $('#lang-btn').onclick = toggleLang;
  $('#open-palette').onclick = openPalette;
  $('#signout').onclick = e => { e.preventDefault(); signOut(); };
}

/* ---------------------------------------------------------------- command palette */
function openPalette() {
  const dlg = $('#palette');
  dlg.innerHTML = `<input id="pq" type="text" autocomplete="off" role="combobox" aria-expanded="true" aria-controls="plist" aria-label="${esc(t('search'))}" placeholder="${esc(t('search_ph'))}"><ul id="plist" role="listbox"></ul>`;
  const list = $('#plist', dlg), input = $('#pq', dlg);
  const all = actions();
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    const hits = all.filter(a => a.label.toLowerCase().includes(q));
    list.innerHTML = hits.length ? hits.map((a, i) => `<li role="option"><button type="button" data-i="${all.indexOf(a)}"><span>${esc(a.label)}</span><span class="mute">${esc(a.hint)}</span></button></li>`).join('')
      : `<li class="empty">${esc(t('no_results'))}</li>`;
  };
  const run = btn => { if (!btn) return; const a = all[Number(btn.dataset.i)]; dlg.close(); a.run(); };
  input.addEventListener('input', draw);
  input.addEventListener('keydown', e => {
    const btns = [...list.querySelectorAll('button')];
    if (e.key === 'Enter') { e.preventDefault(); run(btns[0]); }
    if (e.key === 'ArrowDown') { e.preventDefault(); btns[0]?.focus(); }
  });
  list.addEventListener('keydown', e => {
    const btns = [...list.querySelectorAll('button')], i = btns.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); btns[Math.min(i + 1, btns.length - 1)]?.focus(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); (i <= 0 ? input : btns[i - 1]).focus(); }
  });
  list.addEventListener('click', e => run(e.target.closest('button')));
  draw();
  if (!dlg.open) dlg.showModal();
  input.focus();
}
addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); if (state.me) openPalette(); }
});

/* ---------------------------------------------------------------- boot */
async function boot() {
  try {
    const saved = (() => { try { return localStorage.getItem('dbl_theme'); } catch (e) { return ''; } })();
    if (saved === 'dark' || saved === 'light') setTheme(saved);
    state.me = await api('/auth/me');
    setLang(readCookie('dbl_lang') || state.me.user.lang);
    document.title = t('app_title');
    $('#boot').remove();
    $('#app').hidden = false;
    build();
    await startRouter();
  } catch (e) {
    const b = $('#boot');
    if (b) b.textContent = e.message;
  }
}
boot();
