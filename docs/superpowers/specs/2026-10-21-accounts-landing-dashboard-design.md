# DigitalBurj Leads — accounts, workspaces, landing page and dashboard shell (design spec)

Date: 2026-10-21 · Status: draft for review · Path: architectural · Sub-project 1 of 4

## 1. Intent and scope

Agencies must be able to find DigitalBurj Leads on a marketing site, register, sign in, and work inside their own private
workspace through a dashboard styled after the Loadbyton-OS interface patterns (sidebar shell, KPI cards, command palette, light/dark,
English/Arabic). Today the engine is single-tenant with one shared API key; this sub-project adds accounts and strict workspace
isolation, the public site, and the dashboard shell.

**Decided with the owner:** brand = DigitalBurj Leads (existing palette and fonts from `dashboard/theme.css`, borrowing Loadbyton-OS
*layout patterns*, not its navy/red tokens); built inside this Python engine; tenancy approach B (one SQLite file per workspace).

**In scope:** control database; registration, email verification, sign-in/out, password reset, optional TOTP MFA, sessions, roles,
team invites, audit log; per-workspace settings and per-workspace public keys for capture/webhooks; landing and legal pages (EN/AR);
dashboard shell, Overview page, onboarding checklist and Settings pages; isolation and auth test suites.

**Out of scope (later specs):** payments/billing and plan enforcement; Google/SSO login; global-market intelligence for Dubai property
by source city (sub-project 3); additional lead tools (sub-project 4: ad offline-conversion feedback, referral/co-broker portal, event
QR check-in, web concierge, lead-magnet calculators, off-plan alerts, nurture sequences, CRM sync, listing monitor); native mobile apps.

## 2. Success criteria

1. A visitor can read the site, register, verify email, land on an empty-state dashboard with a working onboarding checklist.
2. User A cannot read or modify workspace B's data through any endpoint, with guessed ids, stolen/other-tenant sessions, wrong tenant
   key on a webhook, or crafted headers (proved by an automated isolation suite that enumerates every route).
3. Auth defences hold: scrypt hashes, hashed session and reset tokens, single-use expiring tokens, CSRF protection, rate limiting and
   lockout, no account enumeration, session rotation on login (proved by tests).
4. All 221 existing tests pass unchanged in *legacy mode* (no control DB configured = today's single-tenant behaviour and Docker demo).
5. Landing, sign-up and dashboard pass browser smoke tests at 1280 px and 375 px, in English and Arabic (RTL), with zero console errors,
   no horizontal scroll, and no untranslated chrome strings.
6. No invented testimonials, customer logos, statistics or "as seen in" claims anywhere on the site.

## 3. Architecture

- **Control DB** (`CONTROL_DB`, SQLite): `users`, `tenants`, `memberships(user, tenant, role)`, `sessions`, `invites`, `auth_tokens`
  (verify/reset), `login_attempts`, `audit`, `tenant_settings`.
- **Tenant data DB**: `DATA_DIR/tenants/<tenant_id>.db`, the existing schema, opened through the existing `DB(path)`.
- **`TenantManager`** (`leadengine/tenancy.py`): `create(name) -> Tenant`, `runtime(tenant_id) -> TenantRuntime(db, pipeline, settings)`;
  LRU-cached (default 50 open runtimes); per-tenant `Settings` built from the base env settings overlaid with `tenant_settings`.
- **Request resolution**: session cookie → user → active tenant (membership) → `TenantRuntime`. A request holds only its own tenant's
  connection, so a missing filter cannot leak across workspaces.
- **Public tenant routes** use an unguessable per-tenant public key: `/t/{key}/capture`, `/t/{key}/widget.js`,
  `/t/{key}/webhooks/{meta,whatsapp,form/{platform}}`. Unknown key = 404 with no timing difference from a wrong key.
- **Legacy mode**: if `CONTROL_DB` is unset, `create_app` behaves exactly as today (one runtime, optional shared API key).
- **Refactor required**: `api.py` currently closes over `pipe/db/s`. Handlers move to a `runtime` dependency; legacy mode supplies a
  singleton. Existing route paths stay valid for the dashboard; the dashboard keeps calling them with its session.
- **Cron**: `maintenance/escalate` and `maintenance/purge` iterate all tenants (CLI `python -m leadengine.cli maintain`).
- **Integration secrets** (WhatsApp token, Meta app secret, Reddit/Apify keys) are stored per tenant, encrypted with a master
  `ENCRYPTION_KEY` using the `cryptography` package (the only new runtime dependency). Plain env values remain the legacy-mode source.

## 4. Authentication and authorisation

| Concern | Decision |
|---|---|
| Passwords | `hashlib.scrypt` (n=2^15, r=8, p=1), 16-byte per-user salt, ≥12 characters, constant-time compare; max length 256 |
| Sessions | 256-bit random token; only SHA-256 stored; cookie `dbl_session` `HttpOnly; Secure; SameSite=Lax; Path=/`; 12 h idle, 30 d absolute; rotated on login and MFA success; revoke-all on password change |
| CSRF | double-submit: cookie `dbl_csrf` + header `X-CSRF-Token` required on non-GET requests authenticated by cookie; API-key requests exempt |
| Email tokens | verify (24 h) and reset (1 h): 256-bit, hashed at rest, single use; reset invalidates all sessions; responses identical whether or not the email exists |
| Brute force | per-IP and per-account limits on login/register/forgot; progressive lockout (5 failures → 1 min doubling to 1 h); uniform error text and timing (dummy hash on unknown user) |
| MFA | optional TOTP (RFC 6238, SHA-1, 30 s, ±1 step), 10 single-use recovery codes (hashed); enforced for owners once enabled |
| Roles | `owner` (everything incl. team, security, delete workspace), `manager` (all lead operations, inventory, agents, ROI, settings except team/security/delete), `agent` (leads assigned to them: read, status, activities, viewings; tasks; no erase, export, settings) |
| Machine access | per-tenant API key (shown once, only its hash stored) with `manager` rights, header `X-API-Key` |
| Audit | login/logout/failed login, MFA changes, role changes, invites, erasures, API-key rotation, settings changes |
| Mail | `Mailer` protocol: `ConsoleMailer` (dev/tests capture links), SMTP or Resend implementation selected by env; no provider keys in the repo |

Registration: name, email, password, agency name, preferred language, acceptance of Terms and Privacy; honeypot + rate limit; creates user
(unverified), tenant, owner membership, and sends the verification email. An unverified owner can use the dashboard but cannot invite
users or store integration secrets until verified.

## 5. Landing page and public site

Server-rendered by a small template renderer (`leadengine/site.py`, no new dependency), pages as HTML templates with EN and AR
dictionaries (`site/i18n/en.json`, `ar.json`), language toggle persisted in a cookie, `hreflang`, canonical URLs, Open Graph/Twitter
cards, `sitemap.xml`, `robots.txt`, JSON-LD `SoftwareApplication`/`Organization`.

Pages: `/` (hero, problem, what it does, how it works in four steps, WhatsApp/portal/social sources and their lawful basis, Arabic +
English intent detection, compliance by design, security, pricing, FAQ, final CTA), `/features`, `/security`, `/compliance`, `/pricing`,
`/contact`, `/privacy`, `/terms`, `/start`, `/login`, `/forgot`, `/reset`, `/verify`.

Content rules: only facts the product does today (taken from the README and tests); every integration labelled with its basis; prices
read from `site/pricing.json` (default: "Early access — contact us" until the owner sets prices); Privacy and Terms are templates marked
"for review by UAE counsel before launch"; DigitalBurj wordmark/favicons/fonts from `dashboard/brand`; hero visuals use the brand's
real-estate art and real product screenshots generated from the demo seed. Accessibility: semantic landmarks, visible focus,
4.5:1 contrast, reduced-motion respected, works without JavaScript except the dashboard.

## 6. Dashboard shell

Loadbyton-OS-inspired chrome applied with DigitalBurj tokens: left sidebar (Overview, Leads, Tasks, Viewings, Agents, Inventory, ROI,
Settings; later: Markets), topbar (search/command palette `Ctrl/⌘K`, workspace name, notifications placeholder-free, theme, language, user
menu), responsive (sidebar collapses to a drawer below 900 px). Hash routing, one module per view under `dashboard/app/`.

**Overview:** KPI cards (new today, HOT/WARM, overdue first responses, within-SLA %), pipeline by status, 14-day lead trend (inline SVG),
Demand Radar, today's tasks, onboarding checklist (add agent → import inventory → embed widget → connect WhatsApp → invite team) that
reads real state. **Settings:** agency profile (name, BRN, Trakheesi, contact hours), team and invites, security (password, MFA,
sessions), API key, integrations (secrets write-only), capture origins and embed snippet.

## 7. Error handling and operations

Auth failures return uniform messages; all state changes are audited; tenant DB open failures return 503 without leaking paths;
workspace deletion (owner only) erases the tenant file after a typed confirmation and a 24 h grace backup flag; env validation at
start-up refuses `CONTROL_DB` without `ENCRYPTION_KEY`; cookies are `Secure` unless `DEV_INSECURE_COOKIES=1`.

## 8. Testing

- Isolation suite: two tenants, every registered route called with the other tenant's session, ids, key, and webhook key; assert 401/403/404 and unchanged data.
- Auth suite: hashing parameters, token single-use/expiry, enumeration parity, lockout timing, session rotation and revocation, CSRF
  missing/mismatch, MFA enrol/verify/recovery/replay, role matrix per endpoint.
- Legacy-mode regression: the 221 existing tests unchanged.
- Site: rendered-HTML checks (titles, canonical, hreflang, JSON-LD validity), link checker, i18n key parity EN/AR.
- Browser (Playwright): landing, register → verify (ConsoleMailer) → onboarding, login/logout, forgot/reset, RTL, mobile, console errors.

## 9. Delivery order (each step ships with tests)

1 control DB, password/session/token primitives, mailer → 2 `TenantManager` + `api.py` runtime refactor (legacy mode green) →
3 auth routes, CSRF, rate limits, lockout, MFA → 4 RBAC + isolation suite → 5 per-tenant public keys/routes + encrypted settings →
6 site renderer, landing and legal pages (EN/AR, SEO) → 7 register/login/forgot/verify pages → 8 dashboard shell, Overview, Settings,
onboarding → 9 docs, Docker (volumes for control DB and tenants), maintain CLI.

## 10. Open items for the owner

1. Pricing text and plan names (default shown as "Early access — contact us").
2. Email provider for production (SMTP or Resend) and sender domain.
3. Counsel review of Privacy, Terms and cookie notice before launch.
4. Confirmation that DigitalBurj assets may be used on the public site (same assumption as the dashboard).
5. Whether first-time sign-up should be open or invite/approval-based during beta.

## 11. Roadmap after this sub-project

*Update 2026-10-21:* the city catalogue for sub-project 3 already exists as a standalone data asset (`leadengine/markets/cities.psv`, 231 cities, see `docs/markets/CITY_CATALOGUE.md`); the market module will build on it.

(2) lead tools: ad-platform offline conversions, referral/co-broker portal, event QR check-in, web concierge, lead-magnet calculators,
off-plan alerts, consent-based nurture, CRM sync. (3) global demand intelligence for Dubai property by source city (London, Mumbai,
Delhi, Karachi, Riyadh, Moscow, Lagos, Singapore, Hong Kong, Toronto, New York…): per-city language/market config, aggregated demand from
own analytics, ad lead forms and portal origin statistics, scheduled posting to the agency's own social channels through official APIs,
public listening through licensed providers — no harvesting of individuals. (4) billing.
