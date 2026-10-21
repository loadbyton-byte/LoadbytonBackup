# Public Lead Magnets and Web Concierge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each agency public, agency-branded calculator pages and a web concierge that turn visitors into consented leads in the existing pipeline.

**Architecture:** A `leadengine/magnets/` package installs `/p…` routes on the per-workspace app (so the dispatcher's public `/t/<key>/p…` allow-list, isolation and the legacy single-agency mode all work unchanged). Pages reuse the stdlib renderer from `platform/site.py` with a lean base template, the existing calculators, `qualify.advance` and `Pipeline.ingest`. Concierge answers live in a hashed-token `web_sessions` table until consent; no lead exists before it.

**Tech Stack:** Python 3.10+, FastAPI (existing), sqlite3, server-rendered HTML + CSS (no JS on the public pages), vanilla-JS dashboard modules, pytest + Playwright. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-23-lead-magnets-concierge-design.md`

## Global Constraints

- A lead is created only with explicit consent: the calculator lead form's checkbox (wording = `capture.CONSENT_TEXT`, stored as evidence) or a concierge "yes" plus contact details. Honeypot field `website` filled → pretend success, store nothing. Duplicate/suppressed/stored answers look identical.
- Calculator results are shown with no contact details; they carry the existing indicative-only disclaimer. Nothing is ever sent automatically.
- Every hidden calculator input is re-validated server-side; invalid input → HTTP 422 re-render with a translated message, never a stack trace; non-finite or absurd numbers (NaN, inf, > 1e10, negative where not allowed) are rejected.
- Public pages: CSP `default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self' <capture_origins>`; `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`; results/concierge/thank-you pages `Cache-Control: no-store` + `noindex`; tool landing pages indexable with canonical and EN/AR `hreflang`. No inline styles or scripts, all reflected values escaped, no `{{!raw}}` for user input.
- No personal data (names, phones, emails, free text) in application logs. Only counters (`stats` table) and the existing audit entries.
- Rate limits: lead and concierge POSTs share the `/capture` per-IP `RateLimiter`; at most 3 live concierge sessions per IP hash. Bodies ≤ 8 KB.
- English and Arabic (RTL) for every string; i18n parity enforced by the existing test (new templates are picked up automatically). No superlatives or invented claims in copy.
- No new runtime dependency. Single-agency mode and all 653 existing tests keep passing after every task. Commit per task and push at the end of each task. Engine tests live in `tests/`, platform tests in `tests/saas/` (never `tests/platform`).

## Review Focus

- Tampered/extreme calculator inputs (NaN, `inf`, `1e308`, negative income, `years=0`, huge strings, repeated fields) never crash, never render `nan`/`inf`, and never create a lead with garbage.
- A lead exists only after consent: calculator form without the checkbox, concierge answered "no"/STOP/abandoned/expired, forged or replayed session tokens, honeypot — none stores a lead; duplicate and suppressed submissions are indistinguishable from success.
- Reflected XSS in every field and in `lang`/`tool`/`token`; CSP, `frame-ancestors` and `X-Frame-Options` behave (framable only from the workspace's listed origins on `/p…`, never elsewhere).
- Only `/p…` is public per workspace: unknown tool → 404, wrong/unknown workspace key → identical 404, every private route still 401 without a session; workspaces never see each other's counters or sessions.
- Concierge state: token stored hashed only, 24 h expiry, per-IP session cap, purge removes expired rows, deleting on completion/decline; the transcript stored as consent evidence contains only the questions and answers.

---

## File Structure

| File | Responsibility |
|---|---|
| `leadengine/calculators.py`, `leadengine/config.py` | `affordability()`; `Rates.dbr_pct` |
| `leadengine/platform/site.py` | `render_with_base(template, base, lang, ctx)` for lean public pages (stdlib only) |
| `site/templates/pub_*.html`, `site/static/pub.css`, `site/i18n/{en,ar}.json` | public page templates, styles, strings (`pub.*`) |
| `leadengine/magnets/forms.py` (new) | input parsing/validation per tool; result summaries |
| `leadengine/magnets/sessions.py` (new) | `WebSessions` over `web_sessions` |
| `leadengine/magnets/pages.py` (new) | `install(app, …)` registers `/p…` routes, CSP, counters |
| `leadengine/db.py`, `leadengine/compliance.py` | `web_sessions` table; purge of expired sessions |
| `leadengine/api.py` | call `magnets.pages.install`; `GET /magnets/stats`; `/site-static` mount |
| `leadengine/platform/app.py`, `rbac.py` | public allow-list for `/p…`, frame headers, `magnets` prefix, settings API `public_pages` |
| `dashboard/app/settings.js`, `i18n.js` | "Public pages" card |

---

### Task 1: Affordability calculator

**Files:** Modify `leadengine/calculators.py`, `leadengine/config.py` (`Rates.dbr_pct: float = 50.0`, env `DBR_PCT` via the existing `Rates.from_env`); Test `tests/test_affordability.py`.

**Interfaces — Produces:** `affordability(monthly_income: float, monthly_debts: float, down_payment: float, years: int, rate_pct: float, *, rates: Rates) -> dict` with keys `max_monthly_payment, max_loan, max_price, ltv_capped_price, dbr_pct, limited_by` (`"dbr" | "ltv"`). `max_monthly_payment = max(0, income × dbr_pct/100 − debts)`; `max_loan = principal for which mortgage_payment(principal, rate, years) == max_monthly_payment` (0 when payment 0; closed-form annuity inverse, `rate_pct == 0` handled); `max_price = max_loan + down_payment`; `ltv_capped_price = min(max_price, down_payment / (1 − ltv/100))` when `ltv < 100` (the price at which the loan equals `ltv`% of price), `limited_by = "ltv"` when the cap binds. All money rounded to 2 decimals. Validation via `_need`: income > 0, debts ≥ 0, down_payment ≥ 0, `1 ≤ years ≤ 35`, `0 ≤ rate ≤ 30`, every amount finite and ≤ 1e10.

- [ ] **Step 1: Failing tests (`tests/test_affordability.py`)**
  - `test_dbr_limits_the_payment`: income 30000, debts 5000, `Rates()` → `max_monthly_payment == 10000.0`, `dbr_pct == 50.0`.
  - `test_inverse_of_mortgage_payment`: for several `(income, rate, years)`, `mortgage_payment(r["max_loan"], rate, years)` is within 0.05 of `r["max_monthly_payment"]`; `rate_pct=0` → `max_loan == payment × years × 12`.
  - `test_debts_above_the_limit_floor_at_zero`: debts 20000, income 30000 → payment 0, `max_loan == 0`, `max_price == down_payment`.
  - `test_ltv_cap`: tiny down payment (e.g. 10000) with high income → `limited_by == "ltv"` and `ltv_capped_price == round(10000 / 0.2, 2)` with `ltv_pct=80`; large down payment → `limited_by == "dbr"`, `ltv_capped_price == max_price`.
  - `test_custom_dbr_from_rates`: `Rates(dbr_pct=40)` changes the payment; `Rates.from_env` reads `DBR_PCT=35` (monkeypatch env).
  - `test_validation` (parametrised): income 0 / negative; debts negative; years 0 and 36; rate −1 and 31; `float("nan")`, `float("inf")`, `1e11` anywhere → `ValueError`.
- [ ] **Step 2:** run `python -m pytest tests/test_affordability.py -q` → FAIL. **Step 3:** implement. **Step 4:** run it and the full suite → PASS. **Step 5: Commit** `feat(calc): affordability calculator with debt-burden ratio`.

---

### Task 2: Public-page renderer, templates, strings and styles

**Files:** Modify `leadengine/platform/site.py` (add `render_with_base`), `site/i18n/en.json`, `site/i18n/ar.json`; Create `site/templates/pub_base.html`, `pub_index.html`, `pub_calc.html`, `pub_result.html`, `pub_thanks.html`, `pub_concierge.html`, `pub_error.html`, `site/static/pub.css`; Test `tests/test_magnet_render.py`.

**Interfaces — Produces:** `site.render_with_base(template: str, base: str, lang: str, ctx: dict) -> str` — renders `template` into `base`'s `{{!content}}`, escaping `{{$x}}` ctx values and `{{key}}` strings exactly like `render_page`; requires `ctx["title"]`, `ctx["description"]` (already-translated plain strings) and sets `lang`, `dir`, `canonical`, hreflang alternates from `ctx["canonical_path"]` + `ctx["prefix"]`. Strings live under `pub.*` (nav/labels per tool, field labels, hints, errors, disclaimer, consent label, thanks, concierge prompts framing, footer "Powered by DigitalBurj Leads", counters labels). The pub templates use only `{{key}}`, `{{$ctx}}` and `{{!form}}`/`{{!results}}` blocks built server-side from escaped parts.

- [ ] **Step 1: Failing tests (`tests/test_magnet_render.py`)**
  - `test_render_with_base_escapes_and_sets_direction`: `render_with_base("pub_error", "pub_base", "ar", {"title": "x", "description": "<b>y</b>", …})` contains `dir="rtl"`, `lang="ar"`, escapes `<b>`, contains no `<script`.
  - `test_pub_strings_have_parity_and_every_template_key_exists`: for each `pub_*.html`, `site.template_keys(name) <= set(site.strings("en"))` and the same for `ar`; `en`/`ar` key sets equal (the existing parity test already runs; this asserts the new templates are included).
  - `test_pub_css_has_no_external_urls_and_rtl_rules`: `site/static/pub.css` contains no `http`, uses logical properties (`margin-inline`, `padding-inline`, `text-align: start`).
- [ ] **Step 2:** run → FAIL. **Step 3:** implement `render_with_base` by factoring the shared body of `render_page` (keep `render_page` behaviour identical), write the templates/strings/CSS (calm, mobile-first, DigitalBurj tokens, focus rings, ≥ 4.5:1 contrast, reduced-motion). **Step 4:** run the new file + `tests/saas/test_site.py` + full suite → PASS. **Step 5: Commit** `feat(magnets): public page renderer, templates, strings, styles`.

---

### Task 3: Calculator pages and lead capture

**Files:** Create `leadengine/magnets/__init__.py`, `leadengine/magnets/forms.py`, `leadengine/magnets/pages.py`; Modify `leadengine/api.py` (call `install`, `/site-static` mount); Test `tests/test_magnet_pages.py`.

**Interfaces — Produces:**
- `forms.TOOLS = ("affordability", "costs", "yield", "mortgage")`; `forms.parse(tool, data: Mapping[str, str]) -> tuple[dict, str | None]` returning validated inputs and an error **key** (e.g. `"pub.err.number"`) or `None`; field specs per tool: affordability (`income`, `debts`, `down`, `years`, `rate`), costs (`price`, `loan`), yield (`price`, `rent`, `service`, `vacancy`), mortgage (`principal`, `rate`, `years`). Numbers accept ASCII/Arabic-Indic digits, commas and spaces, reject `nan/inf/e`-notation overflow, cap length at 20 characters.
- `forms.compute(tool, inputs, rates) -> dict` (calls the calculators) and `forms.summary(tool, inputs, result) -> tuple[str, dict]` (one-line English text for the lead + `extra` entries such as `{"budget": "1200000"}` for affordability/costs/yield).
- `pages.install(app, *, db, pipe, settings, limiter, client_ip) -> None` registering `GET /p`, `GET|POST /p/{tool}`, `POST /p/lead`; counters `magnet_<tool>_view|submit|lead`.
- Page URLs are built with `prefix = urlparse(settings.public_url).path.rstrip("/")` (empty in single-agency mode).

- [ ] **Step 1: Failing tests (`tests/test_magnet_pages.py`, `TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))`)**
  - `test_every_page_renders_en_and_ar`: `/p` and each `/p/<tool>` → 200, `<h1>` once, `dir` by language (`?lang=ar` and `Accept-Language`), every input has a `<label for>`, header `Content-Security-Policy` contains `script-src 'none'` and `frame-ancestors 'self'`, `X-Content-Type-Options: nosniff`; `/p/unknown` → 404.
  - `test_calculators_show_results_without_contact_details`: POST `/p/affordability` with income 30000 / debts 5000 / down 200000 / years 25 / rate 4.5 → 200, contains the formatted `max_price` from `calculators.affordability` and the disclaimer; response has `Cache-Control: no-store` and `noindex`; no lead exists (`/leads` empty). Same for costs, yield, mortgage (numbers equal the calculators' outputs).
  - `test_invalid_and_extreme_inputs_are_rejected_cleanly` (parametrised): `nan`, `inf`, `1e308`, `-5`, `0` years, 40-digit string, empty, `"abc"`, repeated fields → 422, translated message (EN and AR), the response never contains `nan`/`inf`/`Traceback`.
  - `test_reflected_values_are_escaped`: `<script>alert(1)</script>` in every field and in `lang`/`tool` → no raw `<script>` in any response.
  - `test_lead_requires_consent_and_contact`: POST `/p/lead` without `consent` → 422/400 and no lead; with consent but no phone/email → 422; honeypot `website=x` → 200 and no lead.
  - `test_lead_created_with_tag_prefill_and_consent_evidence`: valid affordability inputs + name + phone `0501234567` + consent → 200 thank-you; `/leads` has one lead whose consent is opted-in with evidence text `CONSENT_TEXT`; its signal `campaign == "lead-magnet:affordability"`; budget pre-filled from the result; the lead is scored; counters `magnet_affordability_view/submit/lead` increment as expected.
  - `test_tampered_hidden_inputs_are_revalidated`: POST `/p/lead` with garbage calculator fields (valid contact + consent) → 422 and no lead; with a tampered `tool` → 404/422.
  - `test_duplicate_and_suppressed_look_identical`: same submission twice, and after `pipe.opt_out(phone=…)` → responses are byte-identical in status and body (apart from nothing).
  - `test_rate_limit`: 6 lead POSTs from one IP within the window → the 6th is 429 with a retry hint; honeypot hits also count.
  - `test_body_limit`: 9 KB body → 413.
  - `test_logs_contain_no_personal_data`: with `caplog` at DEBUG for root logger, a full lead submission leaves no log record containing the name, phone, email or free text.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement `forms.py`, `pages.py` (render with `site.render_with_base`; lead path builds the `Signal` exactly as `capture.handle_capture` does, reusing its constants; shared limiter and `client_ip`), wire `install` in `create_app`, mount `/site-static` (single-agency mode). **Step 4:** run → PASS; full suite → PASS. **Step 5: Commit** `feat(magnets): calculator pages and consented lead capture`.

---

### Task 4: Web concierge

**Files:** Create `leadengine/magnets/sessions.py`; Modify `leadengine/db.py` (SCHEMA: `web_sessions(token_hash TEXT PRIMARY KEY, state TEXT NOT NULL, lang TEXT, ip_hash TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)` + index on `expires_at`), `leadengine/compliance.py` (`purge_expired` also deletes expired sessions and reports `sessions_deleted`), `leadengine/magnets/pages.py` (concierge routes); Test `tests/test_concierge.py`.

**Interfaces — Produces:** `WebSessions(db)` with `start(lang: str, ip: str) -> str` (raw 256-bit token; stores only its SHA-256; initial state `{"step": "intent", "answers": {}, "retries": {}, "lang": lang}`; `ValueError("too many sessions")` when the IP hash already has 3 unexpired rows), `load(token) -> dict | None` (None when unknown/expired/malformed), `save(token, state)`, `delete(token)`, `purge(now=None) -> int`. Consent mapping: concierge `advance` result `done` with `answers["consent"] is True` → show the contact step; `False` or STOP (`qualify.is_stop`) → delete the session and show the decline page. Lead creation: `Signal(platform="web", source_type="lead_form", consent_marketing=True, consent_text=<consent question> + " → YES", campaign="concierge", text=<answers joined as one English line>, extra={"looking_to": …, "area": …, "budget": …})`.

- [ ] **Step 1: Failing tests (`tests/test_concierge.py`)**
  - `test_full_english_flow_creates_a_lead_only_after_consent`: GET `/p/concierge` shows the intent question and a hidden `token`; POST answers `"buy"`, `"Dubai Marina"`, `"2M"`, `"this month"` (each re-renders the next question); `/leads` is empty until after `"yes"` **and** contact details; then one lead exists with area/budget pre-filled, tag `concierge`, consent evidence containing the consent question and `YES`; counters `concierge_start`, `concierge_done`.
  - `test_full_arabic_flow`: Arabic answers (`"اشتري"`, `"دبي مارينا"`, `"2 مليون"`, `"هذا الشهر"`, `"نعم"`) with Arabic questions, `dir="rtl"`, lead created with the same fields.
  - `test_no_stop_and_abandon_store_nothing`: consent `"no"` → decline page, session deleted, no lead; `"STOP"` mid-flow → session deleted, no lead; abandoning leaves a session row but no lead, and `purge` deletes it after expiry.
  - `test_token_is_stored_hashed_and_forged_or_expired_tokens_fail`: `web_sessions.token_hash` ≠ raw token and equals `sha256(raw)`; a forged/malformed/other-length token → clean 200 restart page or 400, never a 500; a token past `expires_at` (advance the clock via direct SQL) → restarts; a completed token cannot be replayed to create a second lead.
  - `test_replay_of_an_old_step_is_idempotent`: re-POSTing the same answer for the same step does not skip steps or duplicate answers.
  - `test_per_ip_session_cap`: 4th `GET /p/concierge` from one IP within 24 h → 429 page; other IPs unaffected.
  - `test_contact_step_validation_and_honeypot`: invalid phone and email → re-render with message, session kept; honeypot → looks successful, no lead; missing consent state cannot be bypassed by POSTing contact details directly with a fresh/other token.
  - `test_concierge_pages_headers_and_escaping`: `no-store`, `noindex`, CSP; reflected answer text escaped.
  - `test_purge_deletes_expired_sessions`: `Compliance.purge_expired()` returns `sessions_deleted` and removes only expired rows.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS; full suite → PASS. **Step 5: Commit** `feat(magnets): web concierge with hashed session tokens`.

**Checkpoint review (after Task 4):** dispatch one fresh reviewer on the most capable model over Tasks 1–4 (use Review Focus); fix Critical/Important findings with RED→GREEN tests before Task 5.

---

### Task 5: Platform integration, stats endpoint, settings API

**Files:** Modify `leadengine/platform/app.py` (public allow-list `PUBLIC_PAGES = re.compile(r"^/p(/(affordability|costs|yield|mortgage|concierge|lead))?$")`; `TENANT_PREFIXES` gains `magnets`; `_hardened` skips `X-Frame-Options` for forwarded public `/p…` responses; `GET /auth/settings` returns `public_pages`), `leadengine/platform/rbac.py` (`magnets` in the manager rule), `leadengine/api.py` (`GET /magnets/stats`); Test `tests/saas/test_magnets_platform.py`.

**Interfaces — Produces:** `GET /magnets/stats` (manager+) → `{"tools": {tool: {"views": n, "submits": n, "leads": n}}, "concierge": {"started": n, "completed": n, "declined": n}}`; `GET /auth/settings` gains `"public_pages": [{"tool": "affordability", "url": "<base>/t/<key>/p/affordability"}, …, {"tool": "concierge", …}]` and `"embed": {… , "iframe": "<iframe src=… title=… loading=lazy></iframe>"}` per tool.

- [ ] **Step 1: Failing tests (`tests/saas/test_magnets_platform.py`)**
  - `test_pages_are_public_per_workspace_and_private_paths_stay_private`: `/t/<key>/p`, `/t/<key>/p/costs` → 200 for an anonymous client; `/t/<key>/p/unknown`, `/t/pk_nope/p`, `/t/<key>x/p` → identical 404 bodies; `/t/<key>/leads`, `/t/<key>/magnets/stats`, `/t/<key>/p/../leads`, `/t/<key>/p//x` → 404; `/leads` and `/magnets/stats` → 401 without a session.
  - `test_lead_through_the_public_path_lands_only_in_that_workspace`: two workspaces; a lead from `/t/<A key>/p/lead` appears in A's `/leads` and not B's; concierge session created via A's path is invisible to B.
  - `test_frame_ancestors_follow_capture_origins_and_no_xfo_on_pages`: with `capture_origins=["https://www.agency.ae"]` the `/p` response's CSP contains `frame-ancestors 'self' https://www.agency.ae` and has no `X-Frame-Options`; with none, `frame-ancestors 'self'`; `/auth/me` and platform pages still send `X-Frame-Options: DENY`.
  - `test_stats_endpoint_roles_and_counts`: owner/manager 200 with counts matching actions taken; agent 403; API key 200; per-workspace isolation.
  - `test_settings_lists_public_pages_and_iframe_snippets`: owner `GET /auth/settings` includes the six `public_pages` URLs ending with the workspace key path and an `iframe` snippet with `title`; manager 403.
  - Existing `tests/saas/test_isolation.py` route enumeration keeps passing (new tenant routes appear automatically); add `/p…` and `/magnets/stats` to its expectations: `/p…` in `OPEN_ROUTES`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS; full suite → PASS. **Step 5: Commit** `feat(platform): public lead-magnet routes, stats and settings`.

---

### Task 6: Dashboard "Public pages" card

**Files:** Modify `dashboard/app/settings.js`, `dashboard/app/i18n.js` (EN+AR keys `st_pub_*`); Test `tests/saas/test_magnets_browser.py` (skips without Playwright).

- [ ] **Step 1: Failing browser tests** (reuse `workspace()`/`sign_in()` from `test_dashboard_browser.py`):
  - `test_settings_has_public_pages_card_with_urls_and_counters`: Settings shows six rows (each tool name, a link ending in `/p/<tool>`, an iframe snippet in a `<pre>`, counters views/submits/leads); after an anonymous calculator POST made through `TestClient(live.app)` the views counter on a reloaded Settings page is ≥ 1.
  - `test_public_calculator_flow_without_javascript`: open `live.url + /t/<key>/p/affordability` in a context with `java_script_enabled=False`, submit the form, see the result, submit the lead form with consent → thank-you page; the lead then exists (`/leads`). No console errors on the pages.
  - `test_concierge_in_arabic_rtl_at_phone_width_and_keyboard_only`: 375 px, `?lang=ar`, `dir=rtl`, no horizontal scroll; answer all steps with keyboard (Tab/Enter); thank-you page shown; lead created.
  - `test_framed_only_from_listed_origin`: with a workspace whose `capture_origins` lists `http://localhost.test`, `/p` has the matching `frame-ancestors`; an iframe from another origin is refused (assert via the CSP header text, not a live frame).
- [ ] **Step 2:** run → FAIL. **Step 3:** implement (all strings via `esc()`, counters from `/magnets/stats`, snippets via `textContent`/escaped). **Step 4:** run full suite incl. browser → PASS. **Step 5: Commit** `feat(dashboard): public pages card`.

---

### Task 7: Docs, verification, final review

**Files:** Modify `README.md` (feature row, "Public pages" section with URLs, DBR note, limits), `.env.example` (`DBR_PCT=50`).

- [ ] **Step 1:** Update docs: what each page does, the consent rule, `DBR_PCT`, embedding (`frame-ancestors` from `CAPTURE_ORIGINS`/Settings origins), data retention of `web_sessions` (24 h, purged by `maintain`), counters; limits (results not emailed, no PDF, indicative figures).
- [ ] **Step 2:** Run the full suite (unit, platform, browser) → PASS. Run the clean-venv production command from sub-project 1 Task 13: register/verify/login, then with `curl` GET `/t/<key>/p/costs` (200), POST a calculator result, POST `/p/lead` with consent (200) and confirm the lead via `GET /leads` with the session cookie; confirm `/t/<key>/leads` is 404 and `/leads` without session 401.
- [ ] **Step 3: Commit and push** `docs: lead magnets and concierge`.

**Final review (after Task 7):** dispatch one fresh reviewer on the most capable model over the whole branch since the spec commit (Review Focus + XSS/CSP/framing + privacy in logs + dashboard card); fix Critical/Important findings with RED→GREEN tests; report "Rulings I made" and "Deferred minors"; push.

---

## Spec coverage check

§2 routes → Tasks 3, 4, 5. §3 calculators → Task 1 (+ Task 3 forms). §4 lead capture → Task 3. §5 concierge + sessions + purge → Task 4. §6 pages/branding/security → Tasks 2, 3, 5 (CSP, frame-ancestors, headers, static). §7 counters/stats/Settings card/logging/failures → Tasks 3, 5, 6. §8 testing → each task. §9 order preserved with the checkpoint review after Task 4. §10 open items → Global Constraints (DBR configurable; results visible; indexable landing pages). *Plan note:* `web_sessions` gains `ip_hash` (spec §5 says a per-IP cap; the table needs the column to enforce it) — update the spec's column list when implementing.
