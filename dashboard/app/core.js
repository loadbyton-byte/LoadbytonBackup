import { T } from './i18n.js';

export const $ = (s, r = document) => r.querySelector(s);
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const money = n => n ? 'AED ' + (n >= 1e6 ? (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + 'M' : Math.round(n / 1e3) + 'K') : '—';
export const num = v => (v === null || v === undefined) ? '—' : v;
export const state = { lang: 'en', me: null };

export const t = k => (T[state.lang]?.[k] ?? T.en[k] ?? k);
export const when = iso => iso ? new Date(iso).toLocaleString(state.lang === 'ar' ? 'ar-AE' : 'en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : '—';

export function readCookie(name) {
  const m = document.cookie.split('; ').find(c => c.startsWith(name + '='));
  return m ? decodeURIComponent(m.slice(name.length + 1)) : '';
}
export function setLang(lang) {
  state.lang = lang === 'ar' ? 'ar' : 'en';
  document.documentElement.lang = state.lang;
  document.documentElement.dir = state.lang === 'ar' ? 'rtl' : 'ltr';
  document.cookie = `dbl_lang=${state.lang}; path=/; max-age=31536000; samesite=lax`;
}
export function setTheme(theme) {
  const root = document.documentElement;
  if (theme) root.dataset.theme = theme; else delete root.dataset.theme;
  try { localStorage.setItem('dbl_theme', theme || ''); } catch (e) { /* storage unavailable */ }
}
export function currentTheme() {
  const set = document.documentElement.dataset.theme;
  return set || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}

/** Widths/heights set through the CSSOM so the strict style-src CSP never needs inline style attributes. */
export function applyData(root = document) {
  root.querySelectorAll('[data-w]').forEach(e => { e.style.width = Math.max(0, Math.min(100, Number(e.dataset.w) || 0)) + '%'; });
}

export async function api(path, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const headers = { ...(opts.headers || {}) };
  if (opts.body !== undefined && !headers['content-type']) headers['content-type'] = 'application/json';
  if (method !== 'GET' && state.me) headers['X-CSRF-Token'] = state.me.csrf_token;
  const r = await fetch(path, { ...opts, method, headers, credentials: 'same-origin' });
  if (r.status === 401) { location.href = '/login?next=%2Fapp'; throw new Error('Signed out'); }
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch (e) { data = text; }
  if (!r.ok) {
    const d = data && typeof data === 'object' ? (data.detail ?? data.message ?? data.error) : null;
    throw new Error((Array.isArray(d) ? d.map(x => x.msg).join('; ') : d) || `${r.status}`.slice(0, 200));
  }
  return data;
}
export const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body ?? {}) });
export const put = (p, body) => api(p, { method: 'PUT', body: JSON.stringify(body ?? {}) });
export const patch = (p, body) => api(p, { method: 'PATCH', body: JSON.stringify(body ?? {}) });
export const err = e => `<span class="no">${esc(e.message)}</span>`;
export const pill = tier => `<span class="pill ${esc(tier)}">${esc(tier)}</span>`;
export const isManager = () => ['owner', 'manager'].includes(state.me?.role);
