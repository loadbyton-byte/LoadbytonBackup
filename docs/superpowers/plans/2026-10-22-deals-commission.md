# Deals, commission and leaderboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (native). Steps use checkboxes.

**Goal:** Track deals from viewing to payment with commission maths, pipeline value, an agent leaderboard and revenue in the ROI report.
**Architecture:** New `leadengine/deals.py` (`Deals` service over the per-workspace DB), API routes in `api.py`, agent row scoping in the platform dispatcher, a Deals dashboard page. Deal outcomes drive lead status and `Followups.stop`.
**Tech Stack:** Python/FastAPI, SQLite, vanilla-JS dashboard.
**Spec:** `docs/superpowers/specs/2026-10-22-deals-commission-design.md`

## Global Constraints
- Money REAL AED rounded to 2 decimals; value in (0, 1e10]; pct 0-100; split 0-100; defaults configurable (sale 2, rental 5, VAT 5), labelled "verify current rules".
- Agents touch only their own deals (IDOR-proof by id); stage changes audited with `deal_events`.
- Tests in `tests/`, platform tests in `tests/saas/`.

## Review Focus
- Agent reads/moves another agent's deal by id; agent creates a deal for a lead not assigned to them.
- Bad numbers (0, negative, NaN, huge, split>100) never stored.
- Lead with one lost and one open deal stays open; reopening a lost deal never restarts follow-ups.
- Lead erasure removes its deals without breaking totals; moving back from `paid` drops ROI revenue.

### Task 1: Deals core
**Files:** Modify `leadengine/db.py` (`deals`, `deal_events`); Create `leadengine/deals.py`; Test `tests/test_deals_core.py`.
**Interfaces — Produces:** `STAGES`, `class DealError(ValueError)` (`.code`), `class Deals(db, followups=None)` with `settings() -> dict`, `put_settings(d)`, `create(lead_id, agent_id, kind, value_aed, *, stage="viewing_done", title=None, area=None, commission_pct=None, split_pct=0, expected_close=None, notes=None, actor="api") -> dict`, `update(deal_id, fields, actor)`, `move(deal_id, stage, actor, lost_reason=None)`, `get(deal_id) -> dict|None` (with events), `list(stage=None, agent_id=None, kind=None) -> list[dict]`, `summary(agent_id=None, now=None)`, `leaderboard(period=None, agent_id=None)`.
- [ ] Failing tests: commission = value*pct/100*(1-split/100), VAT on it, recomputed on update; bad values rejected (0, -1, nan, 1e11, split 101, pct 101, bad kind/stage); stage move writes event; lost needs reason; `offer_accepted`+ sets lead `won` and stops follow-ups; lost sets lead `lost` only without another open deal; reopening lost does not restart follow-ups; summary sums open pipeline and expected commission per stage; leaderboard per agent with period filter; agent_id filter limits rows.
- [ ] RED, implement, GREEN, commit.

### Task 2: API, RBAC, agent scoping
**Files:** Modify `leadengine/api.py` (`/deals*` routes, `/deals/settings`), `leadengine/platform/rbac.py`, `leadengine/platform/app.py` (`deals` prefix + `_scope_agent`); Test `tests/test_deals_api.py`, `tests/saas/test_deals_platform.py`.
- [ ] Failing tests: CRUD/stage/summary/leaderboard over HTTP with proper 4xx; agent role sees only own deals, 403 on others' id for GET/PATCH/stage, `agent_id` forced on create, 403 creating for a lead not assigned to them, leaderboard/summary limited to their row; manager sees all; workspace isolation; PUT settings manager only.
- [ ] RED, implement, GREEN, commit.

### Task 3: ROI revenue and Overview
**Files:** Modify `leadengine/roi.py`, `leadengine/api.py` overview; Test `tests/test_deals_roi.py`.
- [ ] Failing tests: `revenue_aed`, `commission_aed`, `commission_per_spend` per source from `paid` deals (null-safe); moving back from `paid` drops it; overview has `pipeline_value_aed`, `expected_commission_aed`.
- [ ] RED, implement, GREEN, commit.

### Task 4: Dashboard, docs, browser test
**Files:** Create `dashboard/app/deals.js`; Modify `router.js`, `i18n.js` (EN/AR), `app.css`, README; Test `tests/saas/test_deals_browser.py`.
- [ ] Board with move buttons, add form, lost-reason prompt, leaderboard, overview tiles; browser test (add, move, lost reason, agent sees own only, RTL); README; full suite; commit; push.

### Task 5: Final review
- [ ] Fresh reviewer on the diff; fix findings RED→GREEN; full suite; push; report.
