# DigitalBurj Leads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the UAE lead engine into the branded DigitalBurj Leads product with agent routing, WhatsApp qualification, ROI, inventory matching, capture widget, calculators, viewings and next-best-action.

**Architecture:** Each capability is one small module over the shared SQLite `DB`, exposed through new routes in `api.py`; every outbound path goes through `Compliance.can_contact`; nothing is auto-sent. The dashboard gains the Digital Burj theme and new tabs.

**Tech Stack:** Python 3.10+, FastAPI, pydantic, sqlite3, pytest (+ httpx TestClient), vanilla JS dashboard.

**Spec:** `docs/superpowers/specs/2026-10-19-digitalburj-leads-design.md` (incl. addendum)

## Global Constraints

- Runtime deps stay `fastapi`, `uvicorn`, `pydantic` only; everything else is stdlib.
- Brand tokens verbatim: ink `#0f1714`, paper `#f6f5f1`, signal `#f23a1d` (hover `#c9260e`, soft `#ff7a45`), mark green `#0a211b`, muted `#6b746f`, success `#3ecf6e`; fonts Plus Jakarta Sans (headings) + Instrument Sans (UI), self-hosted.
- Product name in UI/docs/API title: **DigitalBurj Leads**; Python package stays `leadengine`.
- No superlatives or unverifiable claims ("#1", "only", "no one else") in code, UI or docs.
- Legal/market rates (DLD %, agency %, LTV, golden-visa threshold, calling hours) are configuration with defaults commented "verify current rules".
- New outbound-facing actions must call `Compliance.can_contact`; WhatsApp replies only to people who messaged first; sending defaults to dry-run (`WHATSAPP_SEND_ENABLED=false`).
- Existing 46 tests keep passing after every task. Commit per task; push at the end of each task.

## Review Focus

- WhatsApp inbound `STOP` / `إيقاف` / `Stop.` / `ايقاف` (case, diacritics, punctuation) → opt-out + erasure, never a qualification reply.
- No active agents, or all at `max_open` → lead stays unassigned, no exception, escalation reports it as flagged.
- ROI with zero spend, zero qualified or zero won leads → `null` ratios, never `ZeroDivisionError`.
- Public `/capture` with disallowed origin, filled honeypot, missing consent, or oversized payload → rejected; names/notes containing `<script>` are escaped in the dashboard and `.ics`.
- Inventory CSV with malformed rows and price strings (`AED 1,200,000`, `1.2M`, blank) → bad rows skipped and reported, good rows imported.

---

## File Structure

| File | Responsibility |
|---|---|
| `leadengine/db.py` (mod) | new tables + `_migrate()` adding missing columns to existing DBs |
| `leadengine/distribution.py` | agents, assignment, SLA escalation |
| `leadengine/roi.py` | spend + per-source ROI report |
| `leadengine/inventory.py` | projects CSV import + lead↔project matching |
| `leadengine/capture.py` | public capture endpoint logic + widget JS |
| `leadengine/qualify.py` | pure WhatsApp qualification state machine |
| `leadengine/connectors/whatsapp.py` | inbound parser, `Sender` implementations, `handle_inbound` |
| `leadengine/calculators.py` | deal costs, mortgage, yield, LTV |
| `leadengine/viewings.py` | scheduling, overlap check, `.ics` export |
| `leadengine/nba.py` | next-best-action + today's tasks |
| `leadengine/api.py` (mod) | routes for all of the above; `/static` mount |
| `dashboard/` (mod) | `theme.css`, `brand/`, `fonts/`, new tabs, EN/AR toggle |

---

### Task 1: Brand layer and static serving

**Files:**
- Create: `dashboard/brand/` (copy from `/home/user/shamhar07-max/digitalburjfinalmain/dist/brand/`: `wordmark-clear.png`, `wordmark.webp`, `favicon.png`, `favicon-192.png`, `favicon-512.png`, `apple-touch-icon.png`, `opengraph.png`), `dashboard/brand/favicon.svg` (from `dist/favicon.svg`), `dashboard/brand/real-estate.webp` (from `dist/industry/04-real-estate.webp`), `dashboard/fonts/` (the two `*.woff2` from `dist/fonts/`), `dashboard/brand/NOTICE.md`, `dashboard/theme.css`
- Modify: `leadengine/api.py` (mount static), `dashboard/index.html` (use theme, wordmark, favicon)
- Test: `tests/test_brand.py`

**Interfaces:**
- Produces: static files served at `/static/...` (`/static/theme.css`, `/static/brand/wordmark-clear.png`, `/static/fonts/*.woff2`); CSS custom properties `--ink --paper --signal --signal-hover --signal-soft --mark --muted --ok` defined in `theme.css` for light and dark.

- [ ] **Step 1: Write failing tests** in `tests/test_brand.py`: `test_brand_assets_exist` (each file above exists and is non-empty); `test_static_served` (`GET /static/brand/wordmark-clear.png` → 200 `image/png`; `/static/theme.css` → 200); `test_theme_has_brand_tokens` (`#f23a1d`, `#0f1714`, `#f6f5f1`, `Plus Jakarta Sans`, `Instrument Sans`, `prefers-color-scheme: dark` all in `theme.css`); `test_dashboard_uses_brand` (`/` HTML contains `/static/theme.css`, `wordmark-clear.png`, `<title>DigitalBurj Leads`).
- [ ] **Step 2: Run** `python -m pytest tests/test_brand.py -q` → FAIL (files missing).
- [ ] **Step 3: Copy assets with `cp`**, write `NOTICE.md` (source repo URL, "© Digital Burj; reused with owner's authorisation, see spec §2"), write `theme.css` (`@font-face` for both fonts, tokens, dark-mode override, shared component classes reused by the dashboard), mount `StaticFiles(directory=DASH.parent)` at `/static` in `create_app`, rewrite `index.html` head/header to use them (wordmark `<img alt="DigitalBurj">`, `<link rel="icon" href="/static/brand/favicon.svg">`).
- [ ] **Step 4: Run** `python -m pytest -q` → all pass.
- [ ] **Step 5: Commit** `feat: DigitalBurj brand layer and static serving`.

### Task 2: Agent distribution and SLA escalation

**Files:**
- Modify: `leadengine/db.py` (tables `agents`; lead columns `assigned_agent_id INTEGER`, `escalation_level INTEGER DEFAULT 0`; add `DB._migrate()` called from `__init__` that runs `ALTER TABLE leads ADD COLUMN` only when `PRAGMA table_info` lacks the column), `leadengine/pipeline.py` (auto-assign HOT/WARM new leads), `leadengine/api.py`
- Create: `leadengine/distribution.py`
- Test: `tests/test_distribution.py`

**Interfaces:**
- Produces:
  - `class Distribution(db: DB)` with
  - `add_agent(name: str, *, phone: str | None = None, email: str | None = None, areas: tuple[str, ...] = (), languages: tuple[str, ...] = ("en",), weight: int = 1, max_open: int = 25) -> int`
  - `assign(lead_id: int) -> int | None` (writes `assigned_agent_id`, logs `assigned` activity, returns agent id or `None`)
  - `escalate_overdue(now: datetime | None = None) -> dict` → `{"reassigned": int, "flagged": int}`
  - `Pipeline.distribution: Distribution`
- API: `POST/GET /agents`, `PATCH /agents/{id}` (`active`, `weight`, `max_open`), `POST /leads/{id}/assign`, `POST /maintenance/escalate`.

- [ ] **Step 1: Write failing tests**: `test_prefers_area_and_language_match` (Arabic lead in Dubai Marina goes to the Arabic-speaking Marina agent over a closer-loaded English agent); `test_weighted_round_robin` (weights 2:1 over 6 leads → 4:2); `test_capacity_respected_and_no_agents` (all at `max_open` → `assign` returns `None`; zero agents → `None`, no exception); `test_escalation_reassigns_then_flags` (HOT `new` lead past `sla_due_at`: hop 1 → different agent, `escalation_level == 1`; hop 2 → level 2; third pass → `flagged == 1`, no further reassignment); `test_migration_adds_columns_to_old_db` (create DB with old schema file, reopen, columns exist).
- [ ] **Step 2: Run** `python -m pytest tests/test_distribution.py -q` → FAIL.
- [ ] **Step 3: Implement** per interfaces. Selection: candidates active and under `max_open` (open = status in `new/contacted/qualified/viewing`); rank by (area match desc, language match desc, `open/weight` asc, id asc); fall back to all candidates when no match. Escalation excludes the current agent; max 2 hops.
- [ ] **Step 4: Run** full suite → pass.
- [ ] **Step 5: Commit** `feat: agent distribution and SLA escalation`.

### Task 3: Source ROI

**Files:**
- Modify: `leadengine/db.py` (table `spend(id, source, campaign, period, amount_aed)`), `leadengine/api.py`
- Create: `leadengine/roi.py`
- Test: `tests/test_roi.py`

**Interfaces:**
- Produces: `record_spend(db, source: str, campaign: str | None, period: str, amount_aed: float) -> int`; `roi_report(db, now: datetime | None = None) -> list[dict]` — one row per source (`platform` of the lead's earliest signal) with keys `source, leads, qualified, within_sla_pct, median_first_response_min, won, spend_aed, cost_per_lead, cost_per_qualified, cost_per_won, win_rate_pct` (ratios `None` when the denominator is 0). First response = first `status:contacted` activity minus `leads.first_seen`; within SLA = first response ≤ SLA minutes of the lead's tier at creation.
- API: `POST /spend`, `GET /roi`.

- [ ] **Step 1: Write failing tests**: `test_roi_arithmetic` (fixture: 4 reddit leads, 2 qualified, 1 won, AED 1,000 spend → `cost_per_lead == 250`, `cost_per_qualified == 500`, `cost_per_won == 1000`, `win_rate_pct == 25`); `test_zero_denominators_are_none` (spend 500, no leads / no won → ratios `None`, no exception); `test_response_time_and_sla` (contacted after 3 min for HOT → within SLA; 20 min → not; median computed); `test_api_roundtrip`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run full suite → pass. **Step 5:** Commit `feat: source ROI report`.

### Task 4: Inventory import and matching

**Files:**
- Modify: `leadengine/db.py` (table `projects`), `leadengine/config.py` (`golden_visa_min_aed: float = 2_000_000.0`, env `GOLDEN_VISA_MIN_AED`, comment "verify current rules"), `leadengine/api.py`
- Create: `leadengine/inventory.py`
- Test: `tests/test_inventory.py`

**Interfaces:**
- Produces: `import_projects_csv(db, text: str) -> dict` → `{"imported": int, "skipped": list[tuple[int, str]]}` (columns `name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan`; prices parsed with `nlp.intent.parse_budget` plus plain integers); `match(db, lead: dict, golden_min: float, limit: int = 5) -> list[dict]` with `project, score, reasons: list[str], golden_visa_fit: bool`. Score: area match 50, budget within range 30 (partial 15 within ±15%), bedrooms 10, off-plan lead + future handover 10; zero-score projects excluded.
- API: `POST /inventory` (CSV body), `GET /leads/{id}/matches`.

- [ ] **Step 1: Write failing tests**: `test_import_reports_bad_rows` (rows with blank price, `AED 1,200,000`, `1.2M`, non-numeric → 2 imported, skipped list has row numbers + reasons); `test_match_ranks_area_then_budget`; `test_golden_visa_flag_uses_config` (budget 1.9M → False, 2.1M → True, threshold override respected); `test_no_match_returns_empty`.
- [ ] **Step 2–5:** Run FAIL → implement → full suite → commit `feat: inventory matching`.

### Task 5: Public capture endpoint and widget

**Files:**
- Modify: `leadengine/config.py` (`capture_origins: list[str]` from `CAPTURE_ORIGINS` comma list), `leadengine/api.py`
- Create: `leadengine/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: `CONSENT_TEXT: str` (exact wording stored as evidence); `class RateLimiter(n: int, window_s: int)` with `hit(key: str, now: float | None = None) -> bool` (True = limited); `handle_capture(pipe, settings, payload: dict, origin: str | None, ip: str, limiter: RateLimiter) -> tuple[int, dict]`; `widget_js(base_url: str) -> str`.
- Rules: origin must be in `capture_origins` (empty list = same-origin only); `payload["website"]` non-empty → 200 `{"ok": true}` without storing (honeypot, mirrors site `api/lead.js`); `consent` must be `true` → else 400; body fields capped (`name` 100, `phone` 30, `email` 200, `message` 1000); phone or email required; creates `Signal(platform="web", source_type="lead_form", consent_marketing=True, consent_text=CONSENT_TEXT)`.
- API: `POST /capture` (public, CORS headers only for allowed origin), `GET /widget.js`.

- [ ] **Step 1: Write failing tests**: `test_rejects_bad_origin`, `test_honeypot_silently_ok_not_stored`, `test_requires_consent_and_contact`, `test_rate_limited_after_n`, `test_oversize_truncated_or_rejected`, `test_happy_path_creates_opted_in_lead_with_consent_evidence`, `test_widget_js_posts_to_capture_and_contains_consent_checkbox`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat: capture endpoint and embeddable widget`.

### Task 6: WhatsApp qualification

**Files:**
- Modify: `leadengine/db.py` (table `conversations(lead_id, channel, state, transcript, updated_at)`), `leadengine/api.py`, `leadengine/config.py` (`whatsapp_send_enabled: bool`, `whatsapp_token`, `whatsapp_phone_id`)
- Create: `leadengine/qualify.py`, `leadengine/connectors/whatsapp.py`
- Test: `tests/test_qualify.py`, `tests/test_whatsapp.py`

**Interfaces:**
- Produces: `advance(state: dict, text: str) -> tuple[dict, str, bool]` (pure; returns new state, reply text, `done`); steps `intent → area → budget → timeline → consent`; reply language follows the inbound text; `STOP_RE` matches `stop|unsubscribe|cancel|ايقاف|إيقاف|وقف` after `normalize()` and punctuation strip; yes-set `yes|y|ok|sure|نعم|ايوه|أيوا|تمام`.
- `parse_inbound(payload: dict) -> list[Inbound]` (`Inbound = {wa_id, name, text, msg_id, ts}`); `class Sender(Protocol): send_text(to: str, text: str) -> None`; `DryRunSender` (records `.sent`), `MetaSender(settings, http=http_json)`; `handle_inbound(pipe, sender, inbound: Inbound) -> dict` — STOP → `pipe.opt_out(phone=wa_id)`; otherwise ingest as `Signal(platform="whatsapp", source_type="lead_form", phone=wa_id)` (status `enquiry`), `advance`, merge parsed entities, on consent-yes call `pipe.record_consent` with transcript, send reply via sender.
- API: `GET/POST /webhooks/whatsapp` (verify token + HMAC with `meta_app_secret`).

- [ ] **Step 1: Write failing tests**: `test_flow_collects_area_budget_timeline_then_asks_consent`; `test_arabic_flow_replies_in_arabic`; `test_stop_variants_opt_out` (parametrised over `STOP`, `Stop.`, `ايقاف`, `إيقاف`, `وقف`) asserting lead erased + suppression written + no qualification reply; `test_consent_yes_upgrades_to_opted_in_with_transcript`; `test_dry_run_default_never_calls_http`; `test_webhook_signature_required`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat: WhatsApp qualification flow`.

### Task 7: Portal adapters and repeat-enquiry heat

**Files:**
- Modify: `leadengine/db.py` (`signals.listing_ref TEXT` + migration), `leadengine/models.py` (`Signal.listing_ref: str | None`), `leadengine/pipeline.py` (store `listing_ref`; in `recompute` add +6 `repeat_enquiry` to breakdown and score when ≥3 distinct refs in 7 days), `leadengine/connectors/forms.py` (`parse_portal_email` sets `listing_ref`, handles Bayut / Property Finder / Dubizzle label variants and Arabic labels `الاسم`, `الهاتف`)
- Test: `tests/test_portals.py` with fixtures in `tests/fixtures/portal_*.txt`

**Interfaces:** Consumes `parse_portal_email(portal, subject, body, message_id) -> Signal` (existing). Produces `Signal.listing_ref`, `breakdown["repeat_enquiry"]`.

- [ ] **Step 1: Write failing tests**: one fixture-based parse test per portal (`bayut`, `propertyfinder`, `dubizzle`) asserting name/phone/email/listing_ref; `test_repeat_enquiry_bonus_at_three_distinct_listings` (2 refs → no bonus, 3 → +6, same ref twice counts once); `test_cross_portal_merge_by_phone`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat: portal adapters and repeat-enquiry heat`.

### Task 8: Deal-cost, mortgage and yield calculators

**Files:**
- Modify: `leadengine/config.py` (rate defaults, each commented "verify current rules": `dld_pct=4.0`, `dld_admin_fee=580.0`, `agency_pct=2.0`, `vat_pct=5.0`, `mortgage_reg_pct=0.25`, `mortgage_reg_fee=290.0`), `leadengine/api.py`
- Create: `leadengine/calculators.py`
- Test: `tests/test_calculators.py`

**Interfaces:**
- Produces (all pure, all rates passed in or read from a `Rates` dataclass):
  - `deal_costs(price: float, rates: Rates, *, loan: float = 0.0) -> dict` → `{dld, dld_admin, agency, agency_vat, mortgage_registration, total_costs, cash_needed_excl_down_payment}`
  - `mortgage_payment(principal: float, annual_rate_pct: float, years: int) -> float` (standard amortisation; rate 0 → `principal / months`)
  - `rental_yield(price: float, annual_rent: float, *, service_charge: float = 0.0, vacancy_pct: float = 5.0) -> dict` → `{gross_pct, net_pct}`
  - `max_loan(price: float, *, ltv_pct: float) -> float`
- API: `POST /tools/deal-costs`, `/tools/mortgage`, `/tools/yield`, `GET /leads/{id}/deal-sheet` (uses lead `budget_max`).

- [ ] **Step 1: Write failing tests** with explicit rates: `test_deal_costs_1m` (price 1,000,000, defaults → `dld == 40000`, `agency == 20000`, `agency_vat == 1000`); `test_mortgage_payment_known_value` (800,000 @ 4.5% × 25y ≈ 4,446.6 ±0.5); `test_zero_rate`; `test_yield` (price 1,000,000, rent 70,000, service 10,000, vacancy 5% → gross 7.0, net ≈ 5.65); `test_non_positive_inputs_raise_valueerror`; `test_deal_sheet_endpoint_without_budget_is_422`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat: deal-cost, mortgage and yield calculators`.

### Task 9: Viewing scheduler and calendar export

**Files:**
- Modify: `leadengine/db.py` (table `viewings(id, lead_id, agent_id, start_ts, duration_min, location, status)`), `leadengine/api.py`
- Create: `leadengine/viewings.py`
- Test: `tests/test_viewings.py`

**Interfaces:**
- Produces: `class ViewingConflict(Exception)`; `schedule(db, lead_id: int, start: datetime, *, duration_min: int = 30, location: str = "", agent_id: int | None = None) -> int` (defaults to the lead's assigned agent; raises `ViewingConflict` on overlap for that agent; sets lead status `viewing` and logs an activity); `to_ics(rows: list[dict], *, prodid: str = "-//DigitalBurj Leads//EN") -> str` (CRLF, line folding at 75 octets, escapes `\ ; , \n`, UTC `DTSTART`, stable `UID`).
- API: `POST /leads/{id}/viewings` (409 on conflict), `GET /viewings.ics?agent_id=`.

- [ ] **Step 1: Write failing tests**: `test_overlap_conflict_same_agent_only`; `test_schedule_sets_status_and_activity`; `test_ics_escaping_and_folding` (location `Marina, Tower <script>;x` and a 200-char note → escaped, folded, CRLF); `test_ics_has_stable_uid`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat: viewing scheduler and ICS export`.

### Task 10: Next-best-action and today's tasks

**Files:**
- Create: `leadengine/nba.py`
- Modify: `leadengine/api.py`
- Test: `tests/test_nba.py`

**Interfaces:**
- Consumes: `Compliance.can_contact`, `Distribution`, `inventory.match`, `activities`.
- Produces: `next_actions(db, comp: Compliance, lead: dict, now: datetime | None = None) -> list[dict]` — each `{action, reason, due, channel, allowed, compliance_reason}`; rules in priority order: unassigned HOT/WARM → `assign`; `new` past `sla_due_at` → `call`; has matches → `send_matches`; contacted with no activity ≥ 3 days → `follow_up` (nurture cadence days 2/5/10 from first contact, max 3); seller/landlord → `book_valuation_visit`; no activity ≥ 14 days → `reengage`. `todays_tasks(db, comp, agent_id: int | None, now=None) -> list[dict]` sorted by tier then due.
- API: `GET /leads/{id}/next-actions`, `GET /tasks/today?agent_id=`.

- [ ] **Step 1: Write failing tests**: one per rule above; `test_blocked_channel_marked_not_allowed` (public-signal lead → `call` action has `allowed == False` and the compliance reason); `test_nurture_cadence_stops_after_three`; `test_tasks_sorted_hot_first`.
- [ ] **Step 2–5:** FAIL → implement → full suite → commit `feat: next-best-action and daily tasks`.

### Task 11: Dashboard views and EN/AR interface

**Files:**
- Modify: `dashboard/index.html`, `dashboard/theme.css`
- Test: `tests/test_dashboard_smoke.py` (skips when `playwright` or `/opt/pw-browsers/chromium` is unavailable)

**Interfaces:** Consumes all new endpoints. Produces tabs Leads · Agents & queue · Tasks · ROI · Inventory, a language toggle (EN/AR, `dir="rtl"` on `<html>` for AR, persisted in `localStorage` with try/catch), and per-lead panels for matches, deal sheet, viewing form and next actions.

- [ ] **Step 1: Write failing smoke test**: boot the app with demo seed on a free port; load `/`; assert wordmark image `naturalWidth > 0`, zero console errors / 404s, each tab renders without error, AR toggle sets `dir="rtl"`, and a lead with `<script>alert(1)</script>` as its name renders as text (no dialog fired).
- [ ] **Step 2–5:** FAIL → implement (escape every interpolated value with the existing `esc()`) → run smoke + full suite → commit `feat: DigitalBurj Leads dashboard`.

### Task 12: Docs, demo data and final verification

**Files:**
- Modify: `README.md` (retitle, brand, new capabilities, honest limits; no superlatives), `leadengine/demo.py` (agents, projects, spend, a viewing), `.env.example` (new vars), `leadengine/api.py` (`title="DigitalBurj Leads"`)
- Test: `tests/test_demo_end_to_end.py`

- [ ] **Step 1: Write failing test**: seeding demo data yields ≥3 agents, ≥3 projects, assigned HOT leads, non-empty `/roi`, a viewing, and `/tasks/today` non-empty.
- [ ] **Step 2–3:** FAIL → implement. **Step 4:** run full suite (`python -m pytest -q`) → all pass; run server with `--demo` and `curl /health`.
- [ ] **Step 5: Commit and push** `docs: rebrand and document DigitalBurj Leads`.
