# Public lead magnets and web concierge — design

**Status:** draft for review · **Lead tools, group A** (later groups: B QR events, C referral portal, D alerts/nurture) · builds on `calculators.py`, `qualify.py`, `capture.py`, the per-workspace app and the dispatcher's public `/t/<key>/…` routes.

## 1. Purpose and success criteria

Give every agency public, shareable, agency-branded pages that attract buyers with useful tools and turn visitors into **consented** leads in the existing pipeline.

1. A visitor opens a calculator page and sees a result immediately, without giving any details.
2. A visitor becomes a lead only by giving a phone or email **and** ticking an explicit consent box (wording stored as evidence), or by finishing the concierge with a "yes" and contact details.
3. Leads arrive tagged (`lead-magnet:<tool>` / `concierge`) with the calculator inputs or concierge answers pre-filled, are scored, deduped and routed like any other lead.
4. English and Arabic (RTL), usable without JavaScript, phone-first, accessible (labels, focus, contrast, keyboard).
5. Production-ready: strict CSP, bounded inputs, rate limits, no personal data in logs, graceful failures, counters, tests, docs.

**Out of scope:** emailing or PDF-ing results, saved calculations, A/B tests, analytics dashboards beyond counters, sending anything automatically.

## 2. Public routes (per workspace)

Served by the workspace app at `/p…`; reached publicly as `/t/<public key>/p…` (dispatcher) and at `/p…` in single-agency mode (open like `/capture`).

| Path | Methods | Purpose |
|---|---|---|
| `/p` | GET | index of the tools |
| `/p/affordability`, `/p/costs`, `/p/yield`, `/p/mortgage` | GET, POST | calculator form (GET) / result + lead form (POST) |
| `/p/lead` | POST | lead form submission from a calculator result |
| `/p/concierge` | GET, POST | step-by-step guided chat |

The dispatcher's public allow-list gains exactly these patterns (`^/p$`, `^/p/(affordability|costs|yield|mortgage|concierge|lead)$`); every other workspace path stays private. Unknown tool → 404.

## 3. Calculators

Reuse `deal_costs`, `mortgage_payment`, `rental_yield`, `max_loan`. New `calculators.affordability(monthly_income, monthly_debts, down_payment, years, rate_pct, *, rates) -> dict` returns `max_monthly_payment` (= `income × dbr_pct/100 − debts`, floored at 0), `max_loan` (inverse of `mortgage_payment`), `max_price` (= `max_loan + down_payment`), `dbr_pct`, and the LTV-capped price (`max_price` limited so `loan ≤ price × ltv/100`). `Rates` gains `dbr_pct: float = 50.0` (env `DBR_PCT`, marked *verify current rules*). Inputs are validated with the same bounds style as the existing tools (positive numbers, `years` 1–35, `rate_pct` 0–30, amounts ≤ 1e10); invalid input re-renders the form with a translated message (HTTP 422), never a stack trace. Every result page carries the existing disclaimer ("indicative — verify current rules").

## 4. Lead capture from a calculator

POST `/p/lead` fields: `tool`, the calculator inputs (re-validated server-side, never trusted), `name`, `phone`, `email`, `consent` (checkbox), `website` (honeypot), `lang`. Rules: consent required; phone or email required and valid (`normalize_phone`/`normalize_email`); honeypot filled → pretend success, store nothing; per-IP rate limit shared with `/capture` (same `RateLimiter`); body ≤ 8 KB. On success: `Pipeline.ingest(Signal(platform="web", source_type="lead_form", consent_marketing=True, consent_text=CONSENT_TEXT, campaign=f"lead-magnet:{tool}", text=<one-line summary of the inputs/result>, extra={budget, bedrooms?, …}))` and a thank-you page that says an agent may contact them and how to opt out. Identical answer for stored / duplicate / suppressed (never reveal lead state), as `/capture` does.

## 5. Concierge

Reuses `qualify.advance(state, text)` (steps `intent → area → budget → timeline → consent`) as a web flow.

- **State:** table `web_sessions(token_hash TEXT PRIMARY KEY, state TEXT, lang TEXT, created_at TEXT, expires_at TEXT)` (migration; created idempotently). The raw token (256-bit) lives in a hidden form field and an `HttpOnly; SameSite=Lax; Path=/…/p/concierge` cookie is **not** used (forms carry it). Only its SHA-256 is stored. Expiry 24 h; rows deleted on completion/decline and by the existing `purge` maintenance; at most 3 live sessions per IP (rate limit) and 5 steps.
- **Flow:** GET starts a session and asks the first question; each POST submits one answer, `advance` returns the next question; replaying an old step re-renders the current one (idempotent). After consent **yes**, a final form asks name + phone/email (+ the same consent text, pre-ticked-not: the "yes" answer is the consent and is stored as evidence together with the transcript, as the WhatsApp flow does). Then `Pipeline.ingest` with `campaign="concierge"`, answers mapped to `extra`, `consent_text` = the question asked + the answer. **Consent "no" or STOP deletes the session and stores nothing.**
- **No lead exists before consent.** Answers held in `web_sessions` are not a lead, are not scored, and are never shown to agents.
- Language follows `?lang=`/`Accept-Language`; Arabic uses the existing Arabic question set.

## 6. Pages, branding, security

- Server-rendered with the existing renderer (`platform/site.py` gains a `base=` parameter so tenant pages use a lean `pub_base.html`); strings in `site/i18n/{en,ar}.json` under `pub.*` (parity test covers them). The renderer module imports only the standard library so `api.py` can use it without a platform import cycle.
- Branding: the agency name (workspace setting `agency_name`), DigitalBurj tokens, "Powered by DigitalBurj Leads" footer link. Agent name/BRN/permit are shown when set (RERA/DLD advertising rules).
- CSP: `default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self' <capture_origins…>` (pages are framable only from the agency's own listed origins; `'none'` extras when the list is empty). `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`. GET pages `Cache-Control: no-cache`; results/concierge pages `no-store` + `noindex`; tool landing pages indexable with canonical + EN/AR `hreflang`.
- Static assets: a small `site/static/pub.css` served from the platform's `/site-static` in platform mode and from the app's `/static` in single mode (same file, no inline styles).
- All dynamic values escaped; no `{{!raw}}` for user input; numbers formatted server-side.

## 7. Operations and observability

- Counters in the existing `stats` table: `magnet_<tool>_view`, `_submit`, `_lead` (and `concierge_start`, `concierge_done`, `concierge_declined`) — counts only. `GET /magnets/stats` (manager+) returns them; the dashboard Settings page gains a **Public pages** card (owner): each tool's URL, an iframe snippet, the counters.
- Audit entries for `lead_created` come from the pipeline; no personal data is logged (no names, phones, emails, free text) in application logs.
- Failures: invalid input → 422 re-render; rate limit → 429 page with retry hint; unexpected error → generic 500 page (no details); database error never leaks.
- Backwards compatible: single-tenant mode and every existing route/test unchanged; one idempotent migration (`web_sessions`).

## 8. Testing

- `tests/test_affordability.py`: formula, LTV cap, bounds, zero/negative floors, inverse-payment consistency.
- `tests/test_magnet_pages.py` (single-tenant app): every page renders EN/AR with `dir`, labels, one `<h1>`, CSP/headers; escaping of reflected values; invalid input → 422 translated; no-JS flow end to end; consent required; honeypot; rate limit; lead created with tag/pre-fill/consent evidence; duplicate/suppressed look identical; calculator inputs re-validated (tampered hidden fields).
- `tests/test_concierge.py`: full English and Arabic flows, consent yes/no/STOP, replay, expiry, session token hashing, no lead before consent, purge removes expired rows, per-IP session cap.
- `tests/saas/test_magnets_platform.py`: reachable at `/t/<key>/p…`, wrong/unknown key 404 identical, other workspaces' data untouched, private routes still private, `frame-ancestors` follows `capture_origins`, `/magnets/stats` roles, Settings card API.
- `tests/saas/test_magnets_browser.py`: calculator → lead form → thank-you without JS; concierge in Arabic RTL at 375 px; zero console errors; keyboard-only.
- All existing tests keep passing.

## 9. Delivery order

(1) affordability + `Rates.dbr_pct` → (2) renderer `base=`, `pub_base`, strings, CSS → (3) calculator pages + lead capture → (4) `web_sessions` + concierge → (5) dispatcher allow-list, CSP/frame-ancestors, stats endpoint → (6) dashboard Public-pages card → (7) docs, clean-venv verification, final review. Each step lands with its tests; a fresh reviewer checks after step 4 and at the end.

## 10. Open items for the owner

- Default debt-burden ratio 50% (configurable, *verify current rules*).
- Results are visible before any contact details (chosen to maximise trust; can be gated later).
- Whether to add a visible "indexable" switch per workspace (not in this release; tool landing pages are indexable by default).
