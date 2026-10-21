# Market Intelligence (own-data demand map) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each agency where its Dubai-property demand comes from (country/city), how each source market converts and costs, and which markets to focus on next, from data it already holds.

**Architecture:** A pure origin resolver (form fields > phone calling code > explicit self-location phrase) stores `origin_*` on signals and leads at ingest. A pure `market_report` aggregates a workspace DB into country/city tables, a shrinkage-ranked focus list and untapped catalogue cities. Two read routes plus an owner-only backfill sit on the per-workspace app (so isolation and role policy come from the existing dispatcher); a Markets dashboard page renders it.

**Tech Stack:** Python 3.10+, sqlite3, FastAPI (existing), vanilla-JS dashboard modules, pytest + Playwright (existing). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-22-market-intelligence-design.md`

## Global Constraints

- Origin comes only from what the person supplied: explicit `country`/`city` form fields, their own phone number's country code, or an explicit self-location phrase in their own text. No IP geolocation, lookups, enrichment or scraping.
- The Markets page and API return aggregate counts only (no names, phones, emails, handles or lead ids). Unknown origin is reported as unknown.
- Editorial tiers are labelled `editorial` and are never presented as statistics. Starting constants: prior `{1: 80, 2: 55, 3: 30}`, shrinkage `K = 10`, "measured" at `leads >= 10`.
- `AE` (domestic) is shown in tables but never ranked in `focus`.
- Roles: `GET /markets/*` and `POST /spend` stay manager+; `POST /markets/backfill-origin` is owner-only; agents get 403.
- No new runtime dependency. All 520 existing tests keep passing after every task. Commit per task; push at the end of each task. Tests for engine code live in `tests/`, platform/API-through-dispatcher tests in `tests/saas/` (never `tests/platform`).
- Dashboard: all dynamic text through `esc()`, no inline styles/handlers (widths via `data-w`), EN and AR strings for every label.

## Review Focus

- "Interested in London property", "buy a flat in London", "a flat in Dubai Marina", "from AED 1M" must **never** produce an origin; only explicit self-location phrases do (EN and AR).
- Phone numbers that are malformed, shared (`+1`), unmapped, or `None` yield no origin and never raise; `+971` is domestic `AE`; `+7` splits `KZ` (`+76`/`+77`) from `RU`.
- A weaker basis never overrides a stronger one (`form` > `phone` > `text`), including when two profiles merge into one lead and when a later signal arrives.
- City-tagged spend counts for the city **and** its country; untagged spend is reported as `unallocated_spend_aed`; ratios are `null` (never an error or `inf`) with no leads or no tagged spend; the period filter applies to both leads and spend.
- The report contains no personal data, and `/markets/intel*` is invisible across workspaces and forbidden to agents.

---

## File Structure

| File | Responsibility |
|---|---|
| `leadengine/markets/origin.py` (new) | `Origin`, `resolve_origin`, calling-code table, phrase matcher; pure |
| `leadengine/markets/intel.py` (new) | `market_report`, ranking, untapped list, `contact_window`; pure over a `DB` |
| `leadengine/db.py` | migrations for the new columns |
| `leadengine/pipeline.py` | resolve per-signal origin at ingest, pick the best origin in `recompute`, `backfill_origin()` |
| `leadengine/roi.py`, `leadengine/api.py` | spend tagging; `/markets/intel*`, `/markets/backfill-origin`; capture/webhook optional fields |
| `leadengine/capture.py` | accept optional `country`, `city` |
| `leadengine/platform/rbac.py` | owner-only rule for the backfill |
| `dashboard/app/markets.js` (new), `router.js`, `i18n.js`, `roi.js` | Markets page, nav, strings, spend market selectors |

---

### Task 1: Origin resolver

**Files:** Create `leadengine/markets/origin.py`; Test `tests/test_origin.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class Origin: country: str; city: str | None; basis: str` (`basis` in `"form" | "phone" | "text"`; `city` is a catalogue `City.id`).
- `BASIS_RANK = {"form": 3, "phone": 2, "text": 1}`.
- `resolve_origin(text: str | None, phone: str | None, country: str | None = None, city: str | None = None) -> Origin | None` — pure, never raises.
- `country_for_phone(phone: str | None) -> str | None`.
- `CALLING_CODES: dict[str, str]` (ISO2 → code without `+`) covering every ISO2 in `cities.COUNTRIES` except `US`/`CA` (shared `+1`, deliberately absent).
- Consumes: `cities.resolve`, `cities.find_cities`, `cities.COUNTRIES`, `cities.catalogue`, `identity.normalize_phone`.

- [ ] **Step 1: Failing tests (`tests/test_origin.py`)**
  - `test_precedence_form_over_phone_over_text`: `resolve_origin("I'm in Mumbai", "+447911123456", country="PK", city="Karachi")` → `Origin("PK", "karachi-pk", "form")` (use the real `City.id` for Karachi from `cities.resolve("Karachi").id`); without form → `Origin("GB", None, "phone")`; with `phone=None` → text city's country with `basis == "text"`.
  - `test_form_city_fixes_country_and_conflict_prefers_city`: `city="London"` with `country="PK"` → `country == "GB"`; unresolvable `country="Narnia", city="Nowhereville"` and no other signal → `None`.
  - `test_phone_prefixes`: `+971501234567` → `AE`; `+447911123456` → `GB`; `+919876543210` → `IN`; `+923001234567` → `PK`; `+77011234567` → `KZ`; `+74951234567` → `RU`; `+12125550100` → `None`; `+999123` → `None`; `"abc"`, `""`, `None` → `None`; a UAE local number `0501234567` → `AE`. Test `test_calling_codes_cover_catalogue`: every ISO2 in `COUNTRIES` other than `US`, `CA` is in `CALLING_CODES`, and `country_for_phone("+" + code + "5551234567")` returns that ISO2 for every entry except codes shared by several countries (assert no two entries share a code except `KZ`/`RU` handled by `+7`).
  - `test_self_location_phrases_en`: each of `"i am in London"`, `"I'm in London looking for a villa"`, `"we're in London"`, `"based in London"`, `"living in London"`, `"currently in London"`, `"moving from London"`, `"relocating from London"`, `"originally from London"`, `"I'm from London"`, `"writing from London"` → `Origin("GB", <london id>, "text")`.
  - `test_self_location_phrases_ar`: `"أنا في لندن"`, `"مقيم في لندن"`, `"ساكن في لندن"`, `"قادم من لندن"`, `"جاي من لندن"`, `"أعيش في لندن"` (and the hamza-less spellings `انا في`, `اسكن`, `اعيش`) → `GB`/London.
  - `test_property_location_mentions_are_never_origin` (parametrised): `"interested in London property"`, `"buy a flat in London"`, `"looking for a 2BR in Dubai Marina"`, `"invest in London"`, `"properties from AED 1M"`, `"London"`, `"Dubai"`, `"flights from London are cheap, want a Dubai villa"` (no phrase) → `None`.
  - `test_uae_city_after_phrase_is_domestic`: `"I'm in Sharjah"` → `country == "AE"`, `city` set.
  - `test_never_raises`: `resolve_origin` with `text` of 1 MB of random bytes decoded with `errors="ignore"`, with control characters, with `None`/non-str `country`/`city` (`123`, `[]`) → returns without raising.

- [ ] **Step 2: Run** `python -m pytest tests/test_origin.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `leadengine/markets/origin.py` per the Interfaces. Phone: normalise with `identity.normalize_phone` (so local `05…` → `+971…`), then longest-prefix match over `CALLING_CODES` (≤ 4 digits); `+1` and unknown → `None`; `+7` → `KZ` when the next digit is `6` or `7`, else `RU`. Text: fold the text with the same normaliser as `cities._fold` (accent/case/Arabic-diacritic insensitive), find a self-location phrase from the spec §2 lists (also store the hamza-less Arabic variants), then require a catalogue city alias (`find_cities` on the substring that starts right after the phrase and spans at most 4 words) to begin at that position; first valid phrase wins. Form: resolve `city` with `cities.resolve(name)` (exact), country by ISO2 (`COUNTRIES` key) or country name (case-insensitive); a resolved city overrides `country`. Wrap the public function body so any unexpected exception returns `None`.
- [ ] **Step 4: Run** the file → PASS; run `python -m pytest -q` → 520 + new pass.
- [ ] **Step 5: Commit** `feat(markets): origin resolver (form > phone > self-location phrase)`.

---

### Task 2: Storage, ingest, spend tagging, backfill

**Files:** Modify `leadengine/db.py` (MIGRATIONS), `leadengine/pipeline.py`, `leadengine/roi.py`, `leadengine/capture.py`; Test `tests/test_origin_pipeline.py`.

**Interfaces — Consumes:** Task 1's `resolve_origin`, `Origin`, `BASIS_RANK`. **Produces:**
- Columns: `leads.origin_country`, `leads.origin_city`, `leads.origin_basis`, `signals.origin_country`, `signals.origin_city`, `signals.origin_basis`, `spend.country`, `spend.city` (all `TEXT`, nullable).
- `Pipeline.backfill_origin() -> dict` returning `{"updated": int, "unresolved": int}`; idempotent.
- `record_spend(db, source, campaign, period, amount_aed, country: str | None = None, city: str | None = None) -> int` — validates via the catalogue; raises `ValueError` for unknown country/city or a country that conflicts with the city's; a resolved city sets `country` itself.
- `capture.EXTRA_FIELDS` gains `"country": 60, "city": 80`; the form hints are read from `Signal.extra["country"]`/`["city"]` (generic webhook `answers` already land in `extra`).

- [ ] **Step 1: Failing tests (`tests/test_origin_pipeline.py`)**
  - `test_migration_adds_columns_to_an_existing_database`: create a sqlite file with the old `leads`/`signals`/`spend` schema (use `DB(path)` on a file, drop the new columns by creating the tables manually, close, reopen with `DB(path)`) → `PRAGMA table_info` shows all eight new columns; reopening twice is a no-op.
  - `test_ingest_sets_origin_from_phone_text_and_form`: Signal with phone `+447911123456` → lead `origin_country == "GB"`, `origin_basis == "phone"`; a web `lead_form` Signal with `extra={"country": "Pakistan", "city": "Lahore"}` and a UAE phone → `origin_country == "PK"`, `origin_basis == "form"`, `origin_city` is the Lahore id; a public Reddit post `"I'm in Mumbai, want a 2BR in Dubai Marina, budget 2M"` (no phone) → `IN`/`"text"`; a post without a phrase → all three columns `NULL`.
  - `test_weaker_basis_never_overrides_stronger`: lead created with a form origin `PK`, then a signal from the same person (same phone) whose text says `"I'm in London"` → origin stays `PK`/`form`; reverse order (text first, then form) → ends `PK`/`form`; a phone origin is not replaced by a later text origin.
  - `test_merged_profiles_keep_the_strongest_origin`: two leads (different handles) with origins `text` (`GB`) and `form` (`IN`) merged by a signal carrying both identifiers → merged lead is `IN`/`form`.
  - `test_erasure_removes_origin`: after `pipe.erase(lead_id)` no row remains in `leads`/`signals` (and the columns go with the rows).
  - `test_capture_accepts_optional_country_and_city`: `handle_capture(...)` with `{"phone": ..., "consent": True, "country": "India", "city": "Bangalore"}` stores `origin_country == "IN"` with basis `form`; invalid `city="x"*500` is dropped without error.
  - `test_record_spend_tagging`: `record_spend(db, "reddit", None, "2026-10", 100, city="London")` stores `country == "GB"` and the London id; `country="PK"` with `city="London"` → `ValueError`; `country="XX"` → `ValueError`; no tags → both `NULL`; existing 4-argument calls unchanged.
  - `test_backfill_origin_is_idempotent`: insert signals/leads through SQL with `origin_*` NULL (texts containing a phrase, one lead with a `+44` phone and no phrase, one with nothing) → `backfill_origin()` returns `{"updated": 2, "unresolved": 1}`; second call `{"updated": 0, "unresolved": 1}`.

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
  - `db.MIGRATIONS` gains the eight `(table, column, "TEXT")` entries.
  - `Pipeline.ingest`: compute `origin = resolve_origin(sig.text, None, sig.extra.get("country"), sig.extra.get("city"))` (str values only), store it on the inserted `signals` row; phone-based origin is derived from the lead's phone in `recompute`.
  - `Pipeline._best_origin(lead_phone, signal_rows) -> Origin | None`: candidates = every signal's stored origin plus `resolve_origin(None, lead_phone)`; pick the highest `BASIS_RANK`, ties keep the earliest; `recompute` writes the three `origin_*` columns (set to `NULL` when none). Because `_resolve` moves signals on merge and `recompute` reads all signals, merges are covered.
  - `Pipeline.backfill_origin()`: for each signal whose `origin_basis IS NULL`, resolve from its text (text basis only) and store; then call the origin part of `recompute` for every lead whose `origin_basis IS NULL` after that; count `updated` (leads that gained an origin) and `unresolved` (leads still without).
  - `record_spend` validation per the Interfaces; `capture.EXTRA_FIELDS` additions.
- [ ] **Step 4: Run** `python -m pytest -q` → all pass.
- [ ] **Step 5: Commit** `feat(markets): store lead/signal origin, tag spend by market, backfill`.

---

### Task 3: Market report and focus ranking

**Files:** Create `leadengine/markets/intel.py`; Test `tests/test_intel.py`.

**Interfaces — Consumes:** columns from Task 2, `cities.catalogue/resolve`, `roi.QUALIFIED` (HOT/WARM tiers). **Produces:** `market_report(db: DB, now: datetime | None = None, period: str | None = None, limit: int = 25) -> dict` with exactly the top-level keys `known_origin_pct, total_leads, unallocated_spend_aed, countries, cities, focus, untapped, generated_at`; each country/city row has `id` (ISO2 or city id), `name`, `country` (ISO2), `leads, qualified, won, avg_score, share_pct, growth_pct, spend_aed, cost_per_lead, cost_per_qualified, cost_per_won, language, currency, timezone, contact_window, tier`; each `focus` row adds `score, basis ("measured"|"editorial"), reasons` (subset of `high_share, high_quality, growing, low_sample, editorial_prior`); `untapped` rows have `id, name, country, language, timezone, contact_window, tier`. `contact_window(tz: str, now: datetime) -> str` → e.g. `"10:00–21:00 UAE"` (09:00–20:00 local converted to UAE time, `GST` from `compliance`). Validation: bad `period` (not `YYYY-MM`) → `ValueError`.

- [ ] **Step 1: Failing tests (`tests/test_intel.py`)**, seeding leads with explicit SQL inserts (`origin_*`, `tier`, `status`, `score`, `first_seen`) through a small helper, and spend via `record_spend`:
  - `test_counts_and_ratios`: 4 `GB`/London leads (2 HOT, 1 WARM, 1 NURTURE; 1 `won`) and 2 `IN`/Mumbai → London row `leads == 4, qualified == 3, won == 1`, `share_pct == 67`, `avg_score` = mean to 1 decimal; country `GB` row equals London's numbers.
  - `test_spend_tagging_and_unallocated`: spend `1000` tagged `city=London`, `500` tagged `country=IN`, `200` untagged → London `spend_aed == 1000` and `cost_per_lead == 250.0`, `cost_per_qualified == 333.33`, `cost_per_won == 1000.0`; country `GB` also `1000` (city spend counts for its country); India country `500`; Mumbai city `0` with `cost_per_lead is None`; `unallocated_spend_aed == 200`.
  - `test_ratios_are_none_not_errors`: a market with tagged spend but zero won → `cost_per_won is None`; a workspace with no leads → `known_origin_pct is None`, `total_leads == 0`, all lists empty except `untapped`.
  - `test_growth_windows`: with `now` fixed, 6 London leads in the last 28 days and 3 in the 28 before → `growth_pct == 100`; only 2 leads in the earlier window → `growth_pct is None`.
  - `test_period_filter_limits_leads_and_spend`: leads in `2026-09` and `2026-10`, spend in both → `period="2026-10"` counts only October leads and October spend; `period="Oct"` → `ValueError`.
  - `test_shrinkage_and_basis`: market A with 1 lead vs market B with 50 leads of identical quality → A's `focus` score is closer to its editorial prior; A `basis == "editorial"` with `low_sample` in `reasons`, B `basis == "measured"`; assert the exact formula `score == round((n*measured + 10*prior)/(n+10), 1)` for a hand-computed case (e.g. London tier-1 city, `n=20`, `share_pct=50`, `quality=0.5` → `measured = 100*(0.6*1 + 0.4*0.5) = 80`, `prior=80` → `score == 80.0`).
  - `test_domestic_is_tabled_but_not_ranked`: `AE` leads appear in `countries` and Dubai/Sharjah cities in `cities`, none in `focus`.
  - `test_untapped_candidates`: catalogue cities with `tier <= 2`, country ≠ `AE` and no leads are listed ordered by tier then name, at most 10, and a city that has a lead is excluded; each row carries `contact_window` ending in `" UAE"`.
  - `test_contact_window_values`: `contact_window("Europe/London", <winter UTC datetime>)` → `"13:00–00:00 UAE"`; `contact_window("Asia/Kolkata", …)` → `"07:30–18:30 UAE"`; `contact_window("Asia/Dubai", …)` → `"09:00–20:00 UAE"`. (Use real offsets; the implementer computes them with `zoneinfo` for the given instant.)
  - `test_report_contains_no_personal_data`: after seeding leads with names `"Zed Person"`, phones, emails and handles, `json.dumps(market_report(db))` contains none of those strings and no key named `lead_id`, `phone`, `email`, `name_of_lead` or `display_name`.

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `market_report`: one pass over `SELECT origin_country, origin_city, tier, status, score, first_seen FROM leads` (period filter on `first_seen` prefix like `roi_report`); group by city and by country; spend from `SELECT … FROM spend` filtered by `period`, city-tagged → city and its country, country-tagged → country only, untagged → `unallocated_spend_aed`. Language = first of the city's `languages`; country row takes the most-tiered (lowest tier number, then name) catalogue city of that country for language/currency/timezone/tier. Ranking per spec §4 (constants as module-level names `PRIOR`, `SHRINK_K`, `MEASURED_MIN`); reasons: `high_share` when `share_pct >= 25`, `high_quality` when `quality >= 0.5`, `growing` when `growth_pct is not None and growth_pct >= 25`, `low_sample` when `leads < MEASURED_MIN`, `editorial_prior` when `basis == "editorial"`. Round money to 2 decimals.
- [ ] **Step 4: Run** `python -m pytest -q` → all pass.
- [ ] **Step 5: Commit** `feat(markets): market report, shrinkage-ranked focus list, untapped cities`.

---

### Task 4: API and role policy

**Files:** Modify `leadengine/api.py`, `leadengine/platform/rbac.py`; Test `tests/test_markets_api.py` (legacy single-tenant), `tests/saas/test_markets_api.py` (platform).

**Interfaces — Consumes:** Tasks 2–3. **Produces:** `GET /markets/intel?period=&limit=` (422 on bad `period`/`limit`), `GET /markets/intel/{country}` (`{"country": row, "cities": [rows]}`, 404 for unknown/empty country, ISO2 case-insensitive), `POST /markets/backfill-origin` → `{"updated": int, "unresolved": int}`, `SpendIn` gains optional `country`, `city` (422 on invalid via `ValueError` mapping). Route order: `/markets/intel` and `/markets/backfill-origin` are registered **before** any `/markets/{...}` catch-all. `rbac`: add `(frozenset({"POST"}), re.compile(r"^/markets/backfill-origin$"), "owner")` **before** the generic manager rule.

- [ ] **Step 1: Failing tests**
  - `tests/test_markets_api.py` (TestClient on `create_app`): `GET /markets/intel` on an empty workspace → 200 with `total_leads == 0`; after ingesting a `+447911123456` lead → `countries[0]["id"] == "GB"`; `?period=bad` → 422; `?limit=0` → 422; `/markets/intel/gb` → 200 with `cities`; `/markets/intel/zz` → 404; `POST /spend` with `{"source":"reddit","period":"2026-10","amount_aed":100,"city":"London"}` → 200 and shows in the London row; `{"city":"London","country":"PK",…}` → 422; `{"country":"XX",…}` → 422; `POST /markets/backfill-origin` → 200 `{"updated": …, "unresolved": …}`.
  - `tests/saas/test_markets_api.py`: owner/manager `GET /markets/intel` 200, agent 403; `POST /markets/backfill-origin` owner 200, manager 403, agent 403, API key (manager rights) 403; workspace B's `/markets/intel` never contains A's countries; the report JSON of a seeded workspace contains none of the seeded names/phones/emails; `/markets/intel` without session → 401.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the routes (thin wrappers over `market_report` / `Pipeline.backfill_origin`) and the rbac rule; map `ValueError` → 422 for the report and spend tagging.
- [ ] **Step 4: Run** `python -m pytest -q` → all pass (the existing isolation enumeration in `tests/saas/test_isolation.py` now also exercises the new routes).
- [ ] **Step 5: Commit** `feat(markets): intel API, spend tagging, owner-only backfill`.

---

### Task 5: Markets dashboard page

**Files:** Create `dashboard/app/markets.js`; Modify `dashboard/app/router.js` (page `markets`, roles `owner`/`manager`, before `settings`), `dashboard/app/i18n.js` (EN+AR keys `nav_markets`, `mk_*`), `dashboard/app/roi.js` (country/city `<datalist>` selectors on the spend form, values sent as `country`/`city`; the city list comes from `GET /markets/cities?limit=500`); Test `tests/saas/test_markets_browser.py` (skips without Playwright).

- [ ] **Step 1: Failing browser tests** (reuse `workspace()`/`sign_in()` helpers from `test_dashboard_browser.py` by importing them; seed leads by SQL through `live.app.state.tenants.db_for(tid)` with origins `GB/London`×12 (8 HOT/WARM, 2 won), `IN/Mumbai`×3, `AE/Dubai`×5, one lead whose display name is `<img src=x onerror="window.__xss=1">`):
  - `test_markets_page_renders_tables_and_focus`: nav link `#/markets` visible; tiles show known-origin % and top country `United Kingdom`; country table row for GB with `12` leads; clicking the row expands a London city row; the focus list's first item is United Kingdom with a `measured · N leads` badge (focus ranks countries); `AE` is absent from the focus list; untapped list non-empty with a `UAE` contact window; `window.__xss` is `None`; no console problems.
  - `test_period_filter_and_empty_state`: choosing a period with no leads shows the empty state text; a fresh workspace shows the "how origin is derived" explanation.
  - `test_spend_form_tags_a_market`: on `#/roi` fill source `reddit`, period, amount, city `London` → submit → `#/markets` London row shows the spend.
  - `test_markets_rtl_and_phone_width`: Arabic locale → `dir=rtl`, labels translated (assert the Arabic nav label), 375 px has no horizontal scroll.
  - `test_agent_has_no_markets_page`: an agent's nav lacks Markets and `#/markets` falls back to the agent's home page.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `markets.js` (`export async function render(el)`): fetch `/markets/intel?period=`, render tiles, country table with expandable city rows, focus list (reason chips translated via `t('mk_reason_' + key)`, badge `t('mk_measured')`/`t('mk_editorial')` + sample size), untapped list, period input like the ROI page; bars via `data-w`; every string through `esc()`; `applyData(el)` after render.
- [ ] **Step 4: Run** full suite incl. browser → all pass.
- [ ] **Step 5: Commit** `feat(dashboard): Markets page and market-tagged spend`.

---

### Task 6: Docs and final verification

**Files:** Modify `README.md` (feature row, Platform mode note, endpoint list, "Known limits": origin only from supplied data; editorial tiers are priors), `docs/markets/CITY_CATALOGUE.md` (link to the intelligence module and the tier→prior note), `.env.example` (nothing new expected).

- [ ] **Step 1:** Update docs as listed; keep the claims to what the module does (no external data, no posting/listening).
- [ ] **Step 2:** Run the full suite (unit, platform, browser) → all pass. Run the clean-venv production command from sub-project 1's Task 13 against a workspace with seeded origins: `GET /markets/intel` with a session → 200 with the expected top country; without session → 401; as an agent session → 403.
- [ ] **Step 3: Commit and push** `docs: market intelligence`.

**Final review (after Task 6):** dispatch one fresh reviewer on the most capable model over the whole branch diff since the spec commit (use Review Focus plus privacy: no personal data in any response, XSS in the new page); fix Critical/Important findings with RED→GREEN tests; report "Rulings I made" and "Deferred minors"; push.

---

## Spec coverage check

§2 resolver → Task 1. §3 storage/backfill/erasure → Task 2. §4 report/ranking → Task 3. §5 API + roles → Task 4. §6 dashboard + ROI selectors → Task 5. §7 privacy → Global Constraints + Task 3 (`test_report_contains_no_personal_data`) + Task 4 isolation tests. §8 testing → each task. §9 order preserved. §10 open constants → Global Constraints (starting values, module-level names for easy change).
