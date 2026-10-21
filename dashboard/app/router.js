import { $, t, state, esc, err } from './core.js';

export const PAGES = [
  { id: 'overview', roles: ['owner', 'manager'], load: () => import('./overview.js') },
  { id: 'leads', roles: ['owner', 'manager', 'agent'], load: () => import('./leads.js') },
  { id: 'tasks', roles: ['owner', 'manager', 'agent'], load: () => import('./tasks.js') },
  { id: 'agents', roles: ['owner', 'manager'], load: () => import('./agents.js') },
  { id: 'inventory', roles: ['owner', 'manager'], load: () => import('./inventory.js') },
  { id: 'roi', roles: ['owner', 'manager'], load: () => import('./roi.js') },
  { id: 'markets', roles: ['owner', 'manager'], load: () => import('./markets.js') },
  { id: 'settings', roles: ['owner'], load: () => import('./settings.js') },
];
export const pagesFor = role => PAGES.filter(p => p.roles.includes(role));
export const homePage = role => pagesFor(role)[0].id;

let token = 0;
export function current() {
  const id = location.hash.replace(/^#\/?/, '').split(/[/?]/)[0];
  const ok = pagesFor(state.me.role).find(p => p.id === id);
  return ok ? ok.id : homePage(state.me.role);
}

/** Render the page named by the URL hash into #view. A stale render (the user navigated meanwhile) never overwrites a newer one. */
export async function renderCurrent() {
  const id = current();
  const my = ++token;
  document.querySelectorAll('.side nav a').forEach(a => a.removeAttribute('aria-current'));
  document.querySelectorAll('.side nav a').forEach(a => { if (a.dataset.page === id) a.setAttribute('aria-current', 'page'); });
  const view = $('#view');
  view.innerHTML = `<p class="mute">${esc(t('loading'))}</p>`;
  try {
    const mod = await pagesFor(state.me.role).find(p => p.id === id).load();
    if (my !== token) return;
    view.innerHTML = '';
    await mod.render(view);
    if (my !== token) return;
    document.title = `${t('nav_' + id)} · ${t('app_title')}`;
    view.focus({ preventScroll: true });
  } catch (e) {
    if (my === token) view.innerHTML = `<p class="msg err" role="alert">${err(e)}</p>`;
  }
}

export function navigate(id) {
  if (location.hash === '#/' + id) return renderCurrent();
  location.hash = '#/' + id;
}
export function startRouter() {
  addEventListener('hashchange', () => { document.body.classList.remove('nav-open'); renderCurrent(); });
  return renderCurrent();
}
