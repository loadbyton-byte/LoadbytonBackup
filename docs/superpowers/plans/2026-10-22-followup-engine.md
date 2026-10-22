# Follow-up engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (native). Steps use checkboxes.

**Goal:** Speed-to-lead first response plus consent-gated 90-day nurture, with an approval queue by default.
**Architecture:** New `leadengine/followups.py` (`Followups` service) owns sequences, runs, touches and the worker `tick`. `Pipeline` and the API call small hooks; every send re-runs `Compliance.can_contact`. Storage is three new tables plus a `kv` table in the per-workspace SQLite.
**Tech Stack:** Python/FastAPI, SQLite, existing `WhatsApp Sender` and `platform.mailer`, vanilla-JS dashboard.
**Spec:** `docs/superpowers/specs/2026-10-22-followup-engine-design.md`

## Global Constraints
- Compliance gate at send time, never trusted from scheduling time; opt-out/suppression/DNC never receive a touch.
- WhatsApp stays dry-run unless `WHATSAPP_SEND_ENABLED`+credentials; autosend default off (approval queue).
- Idempotent worker: unique `(run_id, step_index)`; a touch is claimed with `UPDATE ... WHERE status='pending'` before sending.
- Messages carry disclosure + opt-out line; Arabic leads fall back to English text if no Arabic template; no literal `{}` in output.
- Tests in `tests/`, platform tests in `tests/saas/`. Commit trailers per session reminder.

## Review Focus
- Lead opts out between scheduling and sending → nothing sent (test in Task 2).
- Two ticks in a row / concurrent claim → one send (Task 2).
- Due at 03:00 UAE → deferred to contact window, not dropped (Task 2).
- Missing name/budget/area → no literal braces (Task 1).
- Autosend turned off after queueing → nothing sent without approval (Task 2).

### Task 1: Schema, templates, enrolment
**Files:** Modify `leadengine/db.py` (tables `sequences`, `sequence_runs`, `touches`, `kv`); Create `leadengine/followups.py`; Test `tests/test_followups_core.py`.
**Interfaces — Produces:** `DEFAULT_SEQUENCES: list[dict]` ("first_response" trigger `new_lead`, one step delay 0 whatsapp; "nurture_90" days 2,5,10,21,45,90, whatsapp+email alternating); `render(template_en, template_ar, lead, settings) -> str`; `class Followups(db, compliance, settings, wa_sender, mailer)` with `ensure_defaults()`, `sequences() -> list[dict]`, `put_sequences(list[dict])` (validates delay 0–2160h, channel in whatsapp|email, ≤12 steps, templates ≤1000 chars), `enroll(lead_id, trigger, now=None) -> int|None`, `autosend() -> bool`, `set_autosend(bool)`.
- [ ] Write failing tests: defaults seeded once; `render` drops/neutralises missing placeholders (name→"there", no budget → phrase omitted) and falls back to English for `language=="ar"` without Arabic template; enrol creates one run and one touch per step with correct `due_at`; enrol twice → still one run (unique); validation rejects bad channel/delay.
- [ ] Run RED, implement, run GREEN, commit.

### Task 2: Worker tick, gate, caps, stop rules
**Files:** Modify `leadengine/followups.py`; Test `tests/test_followups_tick.py`.
**Interfaces — Produces:** `tick(now=None) -> dict(sent, queued, skipped, deferred, failed)`; `stop(lead_id, reason)`; `stop_for_phone(phone, reason)`; `approve(touch_id, actor, body=None) -> dict`; `skip(touch_id, actor)`; `queue(agent=None) -> list[dict]`.
- [ ] Failing tests: autosend off → touch `queued_for_approval`, nothing sent; autosend on + opted_in → sent via sender (DryRunSender.sent / mailer outbox) with disclosure and opt-out line; `enquiry`-status lead allowed on whatsapp/email per `can_contact`; public_signal lead → `skipped` with reason; opted_out between enrol and tick → `skipped`, nothing sent; due 03:00 UAE → `deferred` to 09:00 (contact_hour_start) and `due_at` moved; second tick right after → no double send; frequency cap (default 3 per rolling 7 days) reschedules extra; `stop(lead,"replied")` cancels pending and marks run stopped; autosend turned off after queueing → stays queued; sender raises → `attempts` increments with backoff, 3rd failure → `failed` and `activities` row `followup_failed`; missing lead row (erased) → `cancelled`; `approve` re-runs the gate (opted out → refuses) and edits body; `skip` sets `skipped`.
- [ ] RED, implement (claim row with conditional UPDATE; gate via `compliance.can_contact(lead_dict, channel, now)`; WhatsApp touches outside 24h window are allowed only because the first touch follows an inbound/enquiry — record `meta.window` and mark template-needed touches `queued_for_approval` with a note when no inbound within 24h), GREEN, commit.

### Task 3: Wiring, API, RBAC, maintenance
**Files:** Modify `leadengine/pipeline.py` (optional `on_lead_event` callback: `(lead_id, created: bool)`), `leadengine/api.py` (construct `Followups`, set callback, status/viewing/whatsapp hooks, endpoints `GET/PUT /followups/sequences`, `GET /followups/queue`, `POST /followups/{id}/approve|skip`, `POST /followups/runs/{lead_id}/pause`, `GET/PUT /followups/settings`, `POST /maintenance/followups`), `leadengine/platform/rbac.py`, `leadengine/platform/maintenance.py` (call `tick()` per workspace), `leadengine/platform/app.py` only if route allow-lists need `followups`; Test `tests/test_followups_api.py`, `tests/saas/test_followups_platform.py`.
- [ ] Failing tests: new lead via `/capture` or `/ingest` enrols and first touch queues (autosend off); status `won/lost`, a booked viewing, WhatsApp inbound reply, and a merged repeat signal each stop the run; erase/opt-out cancels; agent role sees only own leads' queue and cannot edit sequences (403); manager can; `maintain` runs the tick for every workspace and reports counts; `POST /maintenance/followups` works in single mode.
- [ ] RED, implement, GREEN, commit.

### Task 4: Metrics and off-plan alert
**Files:** Modify `leadengine/followups.py`, `leadengine/roi.py` or `api.py` stats; Test `tests/test_followups_metrics.py`.
**Interfaces:** `stats() -> {first_response_median_minutes, within_sla_pct, per_step: [{step, sent, replied}]}` (null when empty); `alert_new_project(project_id)` enrols matching consented leads (area hard match, budget fit via `inventory`), ≤1 alert per lead per week; called after inventory import.
- [ ] Failing tests for median/ratios null-safe, reply attribution, alert matching and weekly cap; RED→GREEN→commit.

### Task 5: Dashboard page, docs, browser test
**Files:** Modify `dashboard/index.html`, `dashboard/app/*.js` (new `followups.js`), `dashboard/app/i18n.js` (EN/AR keys), README feature row + limits; Test `tests/saas/test_followups_browser.py` (Playwright: queue shows a touch, edit + approve sends, toggle autosend).
- [ ] Build page (queue list with approve/skip/edit, sequences editor, autosend switch with warning text), tests, README, full suite, commit, push.

### Task 6: Final review
- [ ] Run full suite; fresh reviewer on branch diff; fix findings RED→GREEN; commit and push; report rulings and deferred items.
