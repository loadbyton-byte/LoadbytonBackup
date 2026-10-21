# Accounts, Workspaces, Landing Page and Dashboard Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let agencies discover DigitalBurj Leads on a public site, register, sign in and work in an isolated workspace behind a Loadbyton-OS-style dashboard shell.

**Architecture:** A control SQLite DB holds users, workspaces, sessions and audit. Each workspace owns one SQLite file served by its **own instance of the existing `create_app`**; a platform ASGI app resolves session → workspace, enforces CSRF and role policy, and forwards the request to that workspace's app (structural isolation, no handler rewrite). A server-rendered EN/AR site and a hash-routed dashboard shell sit on top. With `CONTROL_DB` unset the app behaves exactly as today (legacy mode).

**Tech Stack:** Python 3.10+, FastAPI/Starlette, sqlite3, `hashlib.scrypt`, `cryptography` (Fernet, only new dependency), pytest + Playwright, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-10-21-accounts-landing-dashboard-design.md`

## Global Constraints

- Only new runtime dependency: `cryptography` (for encrypting per-workspace integration secrets). Everything else stdlib/FastAPI.
- Passwords: `hashlib.scrypt` n=2^15, r=8, p=1, 16-byte per-user salt (pass `maxmem` ≥ 64 MiB: n=2^15,r=8 needs 32 MiB and OpenSSL's default limit rejects it), ≥12 and ≤256 characters, constant-time compare.
- Sessions: 256-bit random token, only its SHA-256 stored; cookie `dbl_session` `HttpOnly; Secure; SameSite=Lax; Path=/`; 12 h idle, 30 d absolute; rotated on login and on MFA success; all revoked on password change/reset. `Secure` omitted only when `DEV_INSECURE_COOKIES=1`.
- CSRF: cookie `dbl_csrf` + header `X-CSRF-Token` (double submit) on every non-GET/HEAD/OPTIONS request authenticated by cookie; API-key requests exempt.
- Email tokens (verify 24 h, reset 1 h): 256-bit, hashed at rest, single use. Responses identical whether or not an email exists.
- Lockout: 5 failures → 1 min, doubling per further failure up to 1 h; uniform error text; dummy scrypt on unknown users.
- TOTP: RFC 6238, SHA-1, 30 s, ±1 step; 10 single-use hashed recovery codes.
- Roles: `owner`, `manager`, `agent`. Per-workspace API key: shown once, only its hash stored, `manager` rights, header `X-API-Key`.
- `CONTROL_DB` set requires `ENCRYPTION_KEY` (Fernet key) or start-up fails. `CONTROL_DB` unset = legacy mode, unchanged.
- Brand: DigitalBurj tokens from `dashboard/theme.css`; wordmark/favicons/fonts from `dashboard/brand`, `dashboard/fonts`. EN and AR (RTL) for every public page and dashboard chrome string.
- Content rules: no invented testimonials, customer logos, statistics or "as seen in"; prices only from `site/pricing.json` (default "Early access — contact us"); Privacy/Terms are templates marked "for review by UAE counsel before launch".
- All 232 existing tests must keep passing after every task. Commit per task; push at the end of each task.

## Review Focus

- Login with unknown email, wrong password, and locked account → same status, same body shape, comparable timing (dummy hash). Email compared case-insensitively and trimmed (`A@X.com` = `a@x.com`).
- Verify/reset token reuse, expiry, and use after a later password change → rejected; password change or reset revokes every session of that user.
- Cookie-authenticated POST with missing, wrong or other-user CSRF token → 403; API-key request needs no CSRF.
- Dispatcher path tricks: `/leads/../agents`, `//leads`, `/leads/`, percent-encoded slashes, `HEAD`/`OPTIONS`, `/t/{key}/../..` → never bypass role policy or reach another workspace.
- Cached workspace app: eviction during use, two workspaces with identical lead ids, a deleted workspace's cached app → never serves the wrong or a deleted workspace.

---

## File Structure

| File | Responsibility |
|---|---|
| `leadengine/platform/security.py` | password hashing, tokens, TOTP, Fernet wrapper |
| `leadengine/platform/control.py` | control DB schema, helpers, audit |
| `leadengine/platform/mailer.py` | `Mailer` protocol, `ConsoleMailer`, `SmtpMailer` |
| `leadengine/platform/accounts.py` | `AccountService`: register/verify/login/MFA/reset/invites/lockout |
| `leadengine/platform/tenancy.py` | `TenantManager`: create/delete workspaces, cached per-workspace app + overlaid settings |
| `leadengine/platform/rbac.py` | role policy table `allowed(role, method, path)` |
| `leadengine/platform/app.py` | `create_platform_app`: dispatcher, CSRF, cookies, auth + account JSON routes, public `/t/{key}` routes |
| `leadengine/platform/site.py` + `site/` | template renderer, EN/AR dictionaries, pages, pricing.json, SEO |
| `dashboard/app/` + `dashboard/app.html` | dashboard shell, router, Overview, Settings, onboarding |
| `leadengine/cli.py` (mod) | platform mode, `maintain` command |
| `leadengine/api.py` (mod) | small additions only (agent-scoped list filter, tenant runtime hooks) |

---

### Task 1: Security primitives

**Files:** Create `leadengine/platform/__init__.py`, `leadengine/platform/security.py`; modify `requirements.txt` (add `cryptography>=42`); Test `tests/saas/test_security.py` (create `tests/saas/__init__.py`).

**Interfaces — Produces:**
- `hash_password(password: str) -> str` (format `scrypt$n$r$p$salt_b64$hash_b64`); `verify_password(password: str, stored: str) -> bool`; `check_password_policy(password: str) -> str | None` (error text or None).
- `new_token() -> tuple[str, str]` (raw url-safe 256-bit, sha256 hex); `hash_token(raw: str) -> str`.
- `new_totp_secret() -> str` (base32); `totp_code(secret: str, at: float | None = None) -> str`; `totp_verify(secret: str, code: str, at: float | None = None) -> bool` (±1 step); `otpauth_uri(secret: str, email: str, issuer: str = "DigitalBurj Leads") -> str`; `new_recovery_codes(n: int = 10) -> list[str]`.
- `class Encryptor(key: str)`: `encrypt(text: str) -> str`, `decrypt(token: str) -> str`; `generate_key() -> str`.

- [ ] **Step 1: Write failing tests** — `test_hash_format_and_roundtrip` (parts == `["scrypt","32768","8","1",…]`, verify True, wrong False, two hashes of same password differ), `test_policy` (11 chars → error, 12 ok, 257 → error), `test_verify_rejects_garbage_stored_values` (`""`, `"x"`, `"scrypt$1"` → False, no exception), `test_token_pair` (hash_token(raw)==hash, 64 hex chars, tokens unique), `test_totp_rfc6238_vector` (secret base32 of `12345678901234567890`, `totp_code(secret, 59) == "287082"`, at=1111111109 → `"081804"`), `test_totp_window` (code valid at ±30 s, invalid at ±90 s, 5-digit/letters rejected), `test_recovery_codes_unique_and_formatted`, `test_encryptor_roundtrip_and_tamper` (tampered token raises `ValueError`, wrong key raises `ValueError`).
- [ ] **Step 2: Run** `python -m pytest tests/saas/test_security.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** per interfaces: scrypt with `maxmem=64*1024*1024`; `hmac.compare_digest`; TOTP via HMAC-SHA1 dynamic truncation, 6 digits; wrap Fernet errors into `ValueError`.
- [ ] **Step 4: Run** full suite `python -m pytest -q` → all pass.
- [ ] **Step 5: Commit** `feat(platform): password, token, TOTP and encryption primitives`.

### Task 2: Control database

**Files:** Create `leadengine/platform/control.py`; Test `tests/saas/test_control.py`.

**Interfaces — Consumes:** nothing. **Produces:** `class ControlDB(path: str)` with `.conn` (sqlite3, row_factory Row, WAL, foreign keys ON), `q(sql, args=()) -> list[Row]`, `one(sql, args=()) -> Row | None`, `tx()` context manager, `audit(action: str, *, user_id: int | None = None, tenant_id: int | None = None, ip: str | None = None, **detail) -> None`. Tables: `users(id, email UNIQUE COLLATE NOCASE, password_hash, name, lang, email_verified_at, totp_secret_enc, totp_enabled INT, failed_logins INT, locked_until TEXT, created_at)`, `tenants(id, name, public_key UNIQUE, data_path, status, api_key_hash, created_at)`, `memberships(user_id, tenant_id, role, agent_id, PRIMARY KEY(user_id, tenant_id))`, `sessions(token_hash PRIMARY KEY, user_id, tenant_id, mfa_ok INT, csrf_hash, created_at, last_seen, expires_at)`, `invites(token_hash PRIMARY KEY, tenant_id, email, role, agent_id, expires_at, accepted_at)`, `auth_tokens(token_hash PRIMARY KEY, user_id, kind, expires_at, used_at)`, `recovery_codes(user_id, code_hash, used_at)`, `login_attempts(key, ts)`, `tenant_settings(tenant_id, key, value_enc, PRIMARY KEY(tenant_id,key))`, `audit(id, ts, user_id, tenant_id, action, detail, ip)`.

- [ ] **Step 1: Write failing tests** — `test_schema_created_idempotently` (open twice), `test_email_unique_case_insensitive` (insert `A@x.com` then `a@X.COM` → IntegrityError), `test_foreign_keys_cascade` (deleting a tenant removes memberships, settings, invites; deleting a user removes sessions, tokens, recovery codes), `test_audit_records_detail_json_and_never_stores_secrets` (audit called with `password="x"` raises `ValueError`; keys `password`, `token`, `secret`, `code` rejected).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** full suite → pass. **Step 5: Commit** `feat(platform): control database`.

### Task 3: Mailer

**Files:** Create `leadengine/platform/mailer.py`; modify `leadengine/config.py` (`mail_backend`, `mail_from`, `smtp_host/port/user/password/tls`, `base_url`, `dev_insecure_cookies`, env `MAIL_BACKEND` default `console`); Test `tests/saas/test_mailer.py`.

**Interfaces — Produces:** `class Mailer(Protocol): send(to: str, subject: str, text: str, html: str | None = None) -> None`; `ConsoleMailer` with `.outbox: list[dict]` and logging; `SmtpMailer(settings)` (STARTTLS, `smtplib`); `make_mailer(settings) -> Mailer` (`smtp` requires host else `ValueError`).

- [ ] **Step 1: Failing tests** — `test_console_mailer_records_message`, `test_make_mailer_defaults_to_console`, `test_smtp_requires_host`, `test_smtp_sends_via_starttls` (monkeypatched `smtplib.SMTP` records `starttls`, `login`, `send_message`; header injection: subject with `\r\n` raises `ValueError`).
- [ ] **Step 2–5:** run FAIL → implement → full suite → commit `feat(platform): mailer`.

### Task 4: Account service — register, verify, login, sessions, lockout

**Files:** Create `leadengine/platform/accounts.py`; Test `tests/saas/test_accounts_login.py`.

**Interfaces — Consumes:** `ControlDB`, `security.*`, `Mailer`, `TenantManager.create_tenant(name: str) -> Tenant` (Task 6; use a stub in tests until then — define `Tenant = dataclass(id, name, public_key)` in `tenancy.py` as part of this task's stub import). **Produces:** `class AccountService(control, mailer, tenants, settings, now=time.time)` with:
- `register(email, password, name, agency_name, lang="en", ip="") -> RegisterResult(user_id, tenant_id)`; raises `AccountError(code, message)` (`weak_password`, `invalid_email`, `rate_limited`); existing email returns the *same* success shape without revealing existence (and mails the owner "someone tried to register").
- `verify_email(raw_token) -> bool`.
- `login(email, password, ip="") -> LoginResult(status: "ok"|"mfa_required"|"failed", session_token: str | None, mfa_token: str | None)`; always identical `failed` shape; progressive lockout; dummy hash for unknown email.
- `resolve_session(raw_token, touch=True) -> SessionInfo | None` (`user_id, tenant_id, role, agent_id, mfa_ok, email_verified, csrf_token`), enforcing idle/absolute expiry; `logout(raw_token)`; `revoke_all(user_id)`.
- Constants `SESSION_IDLE_S=12*3600`, `SESSION_ABSOLUTE_S=30*86400`, `VERIFY_TTL_S=86400`.

- [ ] **Step 1: Failing tests** — `test_register_creates_user_tenant_owner_and_sends_verify_mail`; `test_register_existing_email_is_indistinguishable` (same return type/fields, no second user, outbox gets a notice not a verify link); `test_weak_password_and_bad_email_rejected`; `test_verify_token_single_use_and_expires` (second use False; after `VERIFY_TTL_S+1` False); `test_login_ok_returns_session_and_stores_only_hash` (raw token not in DB); `test_login_failures_are_uniform` (unknown email, wrong password, locked account → same `LoginResult` fields and `status=="failed"`); `test_unknown_email_still_runs_a_hash` (monkeypatch `verify_password` counter ≥1 for unknown email); `test_lockout_progression` (5 failures → locked 60 s even with correct password; after 61 s success; 6th failure doubles); `test_session_rotation_on_login` (second login yields new token, old still valid until logout); `test_idle_and_absolute_expiry`; `test_email_case_and_whitespace_insensitive_login`; `test_logout_and_revoke_all`.
- [ ] **Step 2–5:** run FAIL → implement (uniform failure helper; attempts counted per account and per IP via `login_attempts`) → full suite → commit `feat(platform): registration, login, sessions, lockout`.

### Task 5: Account service — reset, password change, MFA, invites, API key

**Files:** Modify `leadengine/platform/accounts.py`; Test `tests/saas/test_accounts_security.py`.

**Interfaces — Produces:** `request_reset(email, ip="") -> None` (always returns None; mails link only if user exists); `reset_password(raw_token, new_password) -> bool` (single use, 1 h, revokes all sessions); `change_password(user_id, old, new) -> bool` (revokes all other sessions); `mfa_begin(user_id) -> MfaSetup(secret, uri)`; `mfa_confirm(user_id, code) -> list[str]` (recovery codes, shown once); `mfa_disable(user_id, password) -> bool`; `login_mfa(mfa_token, code_or_recovery, ip="") -> LoginResult` (consumes recovery code; same code/timestep cannot be replayed); `invite(tenant_id, inviter_id, email, role, agent_id=None) -> str` (raises `AccountError("not_verified")` if inviter email unverified; 7-day TTL); `accept_invite(raw_token, password, name) -> int`; `rotate_api_key(tenant_id, actor_id) -> str` (raw shown once); `verify_api_key(raw) -> int | None` (tenant id).

- [ ] **Step 1: Failing tests** — `test_reset_flow_single_use_expiry_and_session_revocation`; `test_reset_request_identical_for_unknown_email`; `test_old_reset_token_dead_after_password_change`; `test_change_password_requires_old_and_revokes_others`; `test_mfa_enrol_confirm_login_roundtrip` (login returns `mfa_required` + `mfa_token`, correct TOTP → session, session `mfa_ok`); `test_mfa_replay_rejected` (same code twice within window → second fails); `test_recovery_code_single_use`; `test_mfa_disable_needs_password`; `test_unverified_owner_cannot_invite`; `test_invite_accept_creates_membership_with_role_and_expires`; `test_invite_for_existing_user_adds_membership`; `test_api_key_rotation_invalidates_previous_and_stores_hash`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat(platform): reset, MFA, invites, API keys`.

### Task 6: Workspace manager

**Files:** Create `leadengine/platform/tenancy.py`; modify `leadengine/api.py` (accept `api_key_check=None` no-op unchanged; none required if per-tenant app is created with `api_key=""`); Test `tests/saas/test_tenancy.py`.

**Interfaces — Produces:** `@dataclass Tenant(id: int, name: str, public_key: str)`; `class TenantManager(control, data_dir: str, base_settings: Settings, encryptor: Encryptor, max_open: int = 50)`:
- `create_tenant(name: str) -> Tenant` (public key `pk_` + 22 url-safe chars; creates `data_dir/tenants/<id>.db` via `DB(path)`);
- `settings_for(tenant_id) -> Settings` (base settings, `api_key=""`, `db_path` = tenant file, plus decrypted `tenant_settings` overlay for allowed keys: `agency_name, agent_name, brn, trakheesi, capture_origins, contact_hour_start, contact_hour_end, meta_app_secret, meta_verify_token, meta_page_token, whatsapp_*, form_webhook_secret`);
- `set_setting(tenant_id, key, value)` (secret keys encrypted; unknown key → `ValueError`);
- `app_for(tenant_id) -> ASGIApp` (LRU `max_open`; evicted apps' DB connections closed only when idle: keep a refcount via `with manager.lease(tenant_id) as app:`);
- `tenant_by_key(public_key) -> Tenant | None` (constant-time dummy lookup on miss);
- `db_for(tenant_id) -> DB`; `delete_tenant(tenant_id)` (evicts cache, deletes control rows and the file, audit).

- [ ] **Step 1: Failing tests** — `test_create_tenant_makes_isolated_file_and_unique_keys`; `test_two_tenants_have_independent_data` (same lead external_id ingested in both → lead id 1 in each, data not shared); `test_settings_overlay_encrypts_secrets` (raw DB value ≠ plaintext; `settings_for` returns plaintext; wrong `ENCRYPTION_KEY` → `ValueError`); `test_unknown_setting_key_rejected`; `test_lru_eviction_keeps_in_flight_lease_open` (max_open=1, hold lease on A, request B → A's DB still usable until lease ends); `test_deleted_tenant_cache_never_served` (delete then `app_for` raises `KeyError`; file gone); `test_tenant_by_key_miss_returns_none`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat(platform): workspace manager with per-workspace app instances`.

### Task 7: Role policy

**Files:** Create `leadengine/platform/rbac.py`; Test `tests/saas/test_rbac.py`.

**Interfaces — Produces:** `allowed(role: str, method: str, path: str) -> bool` and `normalize_path(path: str) -> str | None` (None for suspicious paths: contains `..`, `//` after decoding, backslash, NUL, or percent-encoded `/` or `.`; strips one trailing slash). Policy (first match wins, default **owner-only**): `agent` may `GET /leads`, `GET /leads/{id}` and sub-reads (`matches`, `next-actions`, `deal-sheet`), `POST /leads/{id}/{status,activities,viewings,outreach}`, `GET /tasks/today`, `GET /viewings`; `manager` may everything on the tenant API except `/maintenance/purge`, `DELETE /leads/{id}`, `POST /demo/seed`, API-key and team routes (owner); `owner` all. Unknown paths and methods → owner only.

- [ ] **Step 1: Failing tests** (parametrised matrix) — agent allowed/denied rows (e.g. `agent GET /leads` True, `agent GET /agents` False, `agent DELETE /leads/3` False, `agent POST /leads/3/consent` False); manager denied `DELETE /leads/3` and `/maintenance/purge`; owner allowed everything known; `test_unknown_route_is_owner_only`; `test_suspicious_paths_rejected` for `/leads/../agents`, `//leads`, `/leads/%2e%2e/agents`, `/leads%2Fx`, `/leads\\x`, `/leads\x00`; `test_trailing_slash_and_case` (`/Leads` not normalised to `/leads` → default owner-only).
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat(platform): role policy`.

### Task 8: Platform app — dispatcher, CSRF, auth routes, public tenant routes

**Files:** Create `leadengine/platform/app.py`; modify `leadengine/cli.py` (use platform app when `CONTROL_DB` set); modify `leadengine/config.py` (`control_db`, `data_dir`, `encryption_key`); Test `tests/saas/test_app_auth.py`, `tests/saas/test_isolation.py`.

**Interfaces — Consumes:** Tasks 1–7. **Produces:** `create_platform_app(settings: Settings, mailer: Mailer | None = None) -> FastAPI` with `app.state.accounts`, `.tenants`, `.control`.
- JSON auth routes (all under `/auth`, no tenant): `POST /auth/register`, `POST /auth/login`, `POST /auth/login/mfa`, `POST /auth/logout`, `POST /auth/forgot`, `POST /auth/reset`, `POST /auth/verify`, `GET /auth/me` (user, role, workspace name, verified, csrf token, language), `POST /auth/password`, `POST /auth/mfa/begin|confirm|disable`, `POST /auth/invite`, `POST /auth/invite/accept`, `POST /auth/api-key`. Login sets `dbl_session` and `dbl_csrf` cookies per Global Constraints.
- Dispatcher: any path under the tenant API allow-list (`/leads /agents /tasks /roi /spend /inventory /viewings /viewings.ics /radar /stats /audit /ingest /connectors /tools /markets /maintenance /demo`) → resolve identity (session cookie, or `X-API-Key` → manager) → `normalize_path` → `rbac.allowed` → CSRF (cookie auth, unsafe method) → `with tenants.lease(tid) as app:` forward the ASGI scope unchanged. Missing session → 401; role denied → 403; unknown path → 404.
- Public: `/t/{key}/{capture,widget.js,optout}` and `/t/{key}/webhooks/{meta,whatsapp,form/{platform}}` forwarded to that workspace's app with the prefix stripped (CORS/origin rules, signatures and rate limits stay in the tenant app); unknown key → 404 identical to a wrong key.
- `/healthz`, `/static` (shared assets).

- [ ] **Step 1: Failing tests (`test_app_auth.py`)** — `test_register_login_me_logout_roundtrip` (cookies set with `HttpOnly`, `SameSite=lax`, `Secure` unless `DEV_INSECURE_COOKIES`); `test_login_failure_shapes_identical`; `test_csrf_required_on_cookie_post` (no header 403, wrong header 403, other user's token 403, right token 200); `test_api_key_requests_skip_csrf_and_get_manager_rights`; `test_unauthenticated_api_401`; `test_forgot_and_reset_over_http`; `test_session_cookie_rotates_on_login`; `test_path_tricks_never_bypass_policy` (the five Review Focus path forms as agent → 403/404, never 200; `HEAD`/`OPTIONS` follow policy); `test_control_db_without_encryption_key_refuses_to_start`.
- [ ] **Step 2: Failing tests (`test_isolation.py`)** — fixtures: two verified workspaces A and B, each with leads/agents/inventory/viewings/spend. `test_every_tenant_route_is_scoped`: enumerate `inner_app.routes` of a tenant app (introspect, so new routes are covered automatically); for each (method, path template) with ids filled from A's data, call it with **B's session** and assert the response never contains A's marker strings (names, phones, emails) and that A's data is unchanged afterwards; `test_guessed_ids_return_404_not_other_tenant_data`; `test_public_key_of_other_tenant_cannot_write_here` (POST `/t/<A key>/capture` creates a lead only in A); `test_wrong_or_unknown_key_identical_404`; `test_stolen_session_for_tenant_b_cannot_use_tenant_a_api_key_header_mix` (session of B + API key of A → request treated as A's key only, never merges identities; assert it acts on exactly one tenant); `test_webhook_signature_is_per_tenant` (A's Meta secret cannot sign for B).
- [ ] **Step 3: Run** both files → FAIL. **Step 4: Implement** `create_platform_app` (ASGI forwarding via `await app(scope, receive, send)` with rewritten `path`/`raw_path`; strip any client-supplied `X-API-Key`/`Cookie` before forwarding and never forward the session cookie). **Step 5:** `python -m pytest -q` all pass (legacy tests untouched). **Step 6: Commit** `feat(platform): dispatcher, auth routes, CSRF, public tenant routes, isolation suite`.

**Checkpoint review (after Task 8):** dispatch one fresh reviewer on the most capable model over Tasks 1–8 (security-focused, using Review Focus). Fix Critical/Important findings with RED→GREEN tests before Task 9.

### Task 9: Agent scoping

**Files:** Modify `leadengine/api.py` (`GET /leads` gains `assigned_agent_id: int | None` filter), `leadengine/platform/app.py` (agent role: inject `assigned_agent_id=<membership.agent_id>` into `GET /leads`/`GET /tasks/today?agent_id=`, and for `/leads/{id}…` check lead ownership via `tenants.db_for` before forwarding); Test `tests/saas/test_agent_scope.py`.

- [ ] **Step 1: Failing tests** — `test_agent_sees_only_assigned_leads` (list excludes others even with `?assigned_agent_id=<other>` override attempt); `test_agent_cannot_open_unassigned_lead_or_its_subroutes` (403); `test_agent_can_update_own_lead_status`; `test_agent_without_linked_agent_sees_nothing`; `test_manager_unaffected`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat(platform): agent-scoped access`.

### Task 10: Site renderer, i18n and public pages

**Files:** Create `leadengine/platform/site.py`, `site/templates/{base,home,features,how,security,compliance,pricing,faq,contact,privacy,terms,auth_*}.html`, `site/i18n/{en,ar}.json`, `site/pricing.json`; Test `tests/saas/test_site.py`.

**Interfaces — Produces:** `render_page(name: str, lang: str, ctx: dict | None = None) -> str` (escaping all dynamic values, `{{key}}`-style i18n and `{{>partial}}` includes, no new dependency); `detect_lang(accept_language: str, cookie: str | None) -> "en" | "ar"`; `sitemap_xml(base_url) -> str`; routes in the platform app: `/`, `/features`, `/how-it-works`, `/security`, `/compliance`, `/pricing`, `/faq`, `/contact`, `/privacy`, `/terms`, `/sitemap.xml`, `/robots.txt`, `/lang/{en|ar}` (sets cookie, redirects back).

- [ ] **Step 1: Failing tests** — `test_every_page_renders_in_both_languages` (200, `<html lang>` and `dir="rtl"` for ar, one `<h1>`, `<title>` ≤ 70 chars, `<meta name="description">`, canonical, `hreflang` alternates, Open Graph tags, valid JSON-LD parses); `test_i18n_keys_have_parity` (en and ar key sets equal, no empty values, every `{{key}}` used in templates exists); `test_dynamic_values_are_escaped` (`<script>` in a ctx value is escaped); `test_pricing_comes_from_json_and_defaults_to_contact_us`; `test_no_invented_claims` (templates contain none of: "testimonial", "trusted by", "as seen", digits followed by `%`/`+ agencies|customers|clients`); `test_legal_pages_carry_counsel_notice`; `test_sitemap_lists_public_pages_only`; `test_internal_links_resolve` (every `href="/…"` returns 200/redirect); `test_lang_cookie_switch`.
- [ ] **Step 2–5:** FAIL → implement (copy facts only from README capabilities; hero uses `dashboard/brand/real-estate.webp` and product screenshots generated by Task 12's seed) → full suite → commit `feat(site): EN/AR marketing site`.

### Task 11: Auth pages (HTML forms)

**Files:** Create `site/templates/auth_{start,login,mfa,forgot,reset,verify,invite}.html`; modify `leadengine/platform/app.py` (form routes `GET/POST /start /login /login/mfa /forgot /reset /verify /invite`, working without JavaScript via redirect-after-POST with a CSRF-protected form token); Test `tests/saas/test_auth_pages.py`, `tests/saas/test_site_browser.py` (skips without Playwright).

- [ ] **Step 1: Failing tests** — `test_start_form_creates_account_and_redirects_to_verify_notice`; `test_login_form_sets_cookie_and_redirects_to_app`; `test_forms_reject_missing_csrf`; `test_error_messages_are_uniform_and_translated` (en/ar); `test_open_redirect_blocked` (`next=https://evil.example` ignored; only same-site paths); `test_reset_page_with_bad_token_shows_generic_error`. Browser: register → read link from `ConsoleMailer.outbox` → verify → lands on dashboard empty-state; login/logout; forgot → reset; RTL layout; 375 px no horizontal scroll; zero console errors; keyboard-only submit.
- [ ] **Step 2–5:** FAIL → implement → full suite + browser → commit `feat(site): register, sign-in and recovery pages`.

### Task 12: Dashboard shell, Overview, Settings, onboarding

**Files:** Create `dashboard/app.html`, `dashboard/app/{router,shell,overview,leads,tasks,agents,inventory,roi,settings}.js`, `dashboard/app/app.css`; modify `leadengine/platform/app.py` (`GET /app` serves shell to authenticated users, redirects others to `/login`); modify `leadengine/api.py` (add `GET /overview` returning KPI numbers, status counts, 14-day lead trend, onboarding state flags `{agents, inventory, widget_origin, whatsapp, team}`); Test `tests/saas/test_overview.py`, `tests/saas/test_dashboard_browser.py`.

**Interfaces — Produces:** `GET /overview` → `{"new_today": int, "hot": int, "warm": int, "overdue": int, "within_sla_pct": int | None, "by_status": {status: n}, "trend": [{"date": "YYYY-MM-DD", "leads": n} ×14], "onboarding": {...bool}}`.

- [ ] **Step 1: Failing tests** — `test_overview_counts_and_trend_length_14`; `test_overview_empty_workspace_all_zero_and_onboarding_false`; `test_onboarding_flags_flip_when_agent_inventory_origin_added`; `test_overview_is_tenant_scoped` (added to isolation enumeration automatically). Browser: shell has sidebar, topbar (command palette opens on `Ctrl+K`, filters pages), workspace name, theme + language toggles; AR → RTL; sidebar collapses to a drawer < 900 px; Overview KPI cards render real numbers from a seeded workspace; onboarding checklist ticks after adding an agent; Settings: save agency profile, see embed snippet with the tenant public key, rotate API key (shown once), enable MFA with TOTP computed in the test, invite a member; existing Leads/Tasks/Agents/Inventory/ROI views work inside the shell; XSS payload names render as text; zero console errors/404s.
- [ ] **Step 2–5:** FAIL → implement (hash routing, one module per view, all dynamic strings through `esc()`; `/app` only mounts the old single-file dashboard logic as modules) → full suite + browser → commit `feat(dashboard): app shell, overview, settings, onboarding`.

### Task 13: CLI, Docker, docs, final verification

**Files:** Modify `leadengine/cli.py` (`maintain` command: iterate all workspaces running escalate + purge; `create-owner` for first operator), `Dockerfile` (include `site/`, volumes `/data` for control and tenants), `README.md`, `.env.example` (`CONTROL_DB`, `DATA_DIR`, `ENCRYPTION_KEY`, `BASE_URL`, `MAIL_*`, `DEV_INSECURE_COOKIES`); Test `tests/saas/test_cli_maintain.py`.

- [ ] **Step 1: Failing tests** — `test_maintain_runs_escalation_and_purge_for_every_workspace_and_isolates_failures` (one workspace DB corrupt → others still processed, exit code non-zero and error listed); `test_serve_uses_platform_app_only_when_control_db_set`; `test_startup_validation_messages`.
- [ ] **Step 2–3:** FAIL → implement. **Step 4:** run full suite (all unit, isolation, auth, site, browser) and the clean-venv run of the production command with `CONTROL_DB`/`ENCRYPTION_KEY`/`DEV_INSECURE_COOKIES=1`: register via curl, verify via the console mail link, call `/leads` with the session cookie → 200, without → 401. **Step 5: Commit and push** `docs: platform mode, Docker and maintain CLI`.

---

## Spec coverage check

§3 architecture → Tasks 2, 6, 8 (deviation: per-workspace app instances instead of a handler refactor; same isolation guarantee, legacy mode untouched). §4 auth → Tasks 1, 4, 5, 8. Roles/agent scope → Tasks 7, 9. §5 site → Task 10, 11. §6 dashboard → Task 12. §7 operations → Tasks 6, 13. §8 testing → isolation (8), auth (4, 5, 8), legacy regression (every task), site (10), browser (11, 12). §9 delivery order preserved with the dispatcher swap. §10 open items remain owner decisions (pricing text, mail provider, counsel review, asset licence, sign-up openness: plan implements open sign-up behind a `SIGNUP_MODE=open|invite` setting defaulting to `open`; add the setting in Task 8).
