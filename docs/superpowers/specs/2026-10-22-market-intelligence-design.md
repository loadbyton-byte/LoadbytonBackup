# Global market intelligence (own-data demand map) — design

**Status:** draft for review · **Sub-project 3, first release (option A)** · builds on `leadengine/markets/cities.psv` (231 cities) and the per-workspace app from sub-project 1.

## 1. Purpose and success criteria

An agency wants to know **where its Dubai-property demand comes from** (country and city), **how well each source market converts and what it costs**, and **which markets to focus on next**. It must work only from data the agency already holds lawfully (leads it captured, ad spend it recorded). Success:

1. For a workspace with leads, the Markets page shows leads, qualified, won, average score and growth per country and per city, plus cost per lead / qualified / won where spend is tagged to a market.
2. A ranked "where to focus next" list explains each row (sample size; measured vs editorial) and lists untapped catalogue cities.
3. Origin is derived only from what the person supplied (form fields, their phone number's country code, an explicit origin phrase in their own text). Nothing is looked up, enriched or scraped; no individual is shown on the page.
4. Workspaces stay isolated; roles: `manager` and `owner` read, `owner` runs the backfill.

**Out of scope (later):** scheduled posting to the agency's social channels, public listening through licensed providers, per-city ad copy/playbooks, external statistics, currency conversion of prices.

## 2. Origin resolution — `leadengine/markets/origin.py`

`resolve_origin(text: str | None, phone: str | None, country: str | None = None, city: str | None = None) -> Origin | None`
`Origin(country: str, city: str | None, basis: "form" | "phone" | "text")` — `country` is ISO 3166 alpha-2, `city` a catalogue city id (slug).

Precedence, strongest first; the first that yields a country wins:

1. **form** — explicit `country` (ISO2 or catalogue country name) and/or `city` (resolved with `cities.resolve(name, fuzzy=False)`; a resolved city fixes the country). Unresolvable values are ignored, never stored.
2. **phone** — E.164 number → country by longest calling-code prefix over the countries in the catalogue. `+971` → `AE` (domestic). `+1` (shared US/Canada/Caribbean) and any unmapped prefix → no result. `+7` → `KZ` for `+76`/`+77`, else `RU`. Country only (no city).
3. **text** — a catalogue city (EN/AR/RU aliases, via `find_cities`) that immediately follows a **self-location phrase**; else no result. Phrases (case/diacritic-insensitive):
   - EN: `i am in`, `i'm in`, `im in`, `we are in`, `we're in`, `based in`, `living in`, `live in`, `residing in`, `currently in`, `(moving|relocating|flying|coming|calling|writing) from`, `originally from`, `i am from`, `i'm from`, `we are from`.
   - AR: `انا في`, `أنا في`, `نحن في`, `مقيم في`, `مقيمة في`, `ساكن في`, `ساكنة في`, `أسكن في`, `اسكن في`, `أعيش في`, `اعيش في`, `قادم من`, `قادمة من`, `جاي من`, `مسافر من`, `مسافرة من`.
   Not origin: any other mention ("interested in London property", "flat in Dubai Marina", "buy in London"). A UAE city after a phrase yields `country=AE`, `city` set.

If several signals disagree, the strongest basis wins; ties within `text` keep the first phrase in the text. The resolver is pure (no I/O) and never raises on odd input.

## 3. Storage

- `leads` gains `origin_country TEXT`, `origin_city TEXT`, `origin_basis TEXT` (idempotent migration in `db.MIGRATIONS`).
- `spend` gains `country TEXT`, `city TEXT` (both optional; same encoding).
- `Pipeline.ingest` resolves origin for the lead after identity merge, using the signal text, the lead's normalised phone and the signal's optional `country`/`city` (carried from the capture payload and the form-webhook payload as optional string fields, each ≤ 80 chars). It writes only if the new basis is **stronger** than the stored one (`form` > `phone` > `text`) or none is stored.
- `POST /markets/backfill-origin` (**owner only**) resolves origin for leads that have none, from their stored phone and signal texts, and returns `{"updated": n, "unresolved": m}`. Idempotent. Added to the role policy as owner-only before the generic `/markets` manager rule.
- Erasure: origin columns live on the lead row, so existing erasure/purge removes them with the lead; aggregates are recomputed on read (nothing cached).

## 4. Report — `leadengine/markets/intel.py`

`market_report(db, now=None, period=None, limit=25) -> dict` (pure over the workspace DB; `period` = `YYYY-MM` limits leads by first-seen month and spend by its period label, as `roi_report` does).

Per **market** (a country, and each city within it that has leads):

| field | meaning |
|---|---|
| `leads`, `qualified`, `won` | counts; qualified = tier HOT/WARM (same as ROI), `won` = status `won` |
| `avg_score` | mean lead score, 1 decimal |
| `share_pct` | leads ÷ leads with a known origin, integer % |
| `growth_pct` | last 28 days vs the 28 before, `null` when the earlier window has < 3 leads |
| `spend_aed`, `cost_per_lead`, `cost_per_qualified`, `cost_per_won` | from spend rows tagged with that country/city (city-tagged spend counts for the city and its country); ratios `null` when nothing to divide by or no tagged spend |
| `language`, `currency`, `timezone`, `contact_window` | from the catalogue (country-level: its most-tiered city); `contact_window` = `09:00–20:00` local expressed in UAE time for today, e.g. `"10:00–21:00 UAE"` |
| `tier` | the catalogue's editorial tier (city) or its best city's tier (country) |

Top-level: `known_origin_pct` (leads with origin ÷ all leads, `null` when no leads), `total_leads`, `unallocated_spend_aed` (spend with no market tag), `countries[]`, `cities[]`, `focus[]`, `untapped[]`, `generated_at`.

**Focus ranking** (international markets only: `AE` is shown in the tables but never ranked):

- `quality = (qualified + won) / leads` (a lead can count once; use qualified OR won).
- `measured = 100 × (0.6 × min(1, share_pct/25) + 0.4 × quality)`.
- `prior = {1: 80, 2: 55, 3: 30}[tier]`.
- `score = (leads × measured + 10 × prior) / (leads + 10)`, rounded to 1 decimal.
- Each focus row carries `basis`: `"measured"` when `leads ≥ 10`, otherwise `"editorial"`, the `leads` sample size, and `reasons[]` (short English keys the UI translates: `high_share`, `high_quality`, `growing`, `low_sample`, `editorial_prior`).
- `focus` = **countries** ranked by `score` (ties: more leads, then name), limited to `limit`. *Ruling during implementation:* most origins come from phone numbers, which identify a country but not a city, so ranking cities would leave the list nearly empty; each row names its `top_city` when known, and a country's prior uses its best catalogue tier. Cities appear in `cities` and `untapped`.
- `untapped` = catalogue cities with tier ≤ 2, country ≠ `AE`, **zero** leads, ordered by tier then name, at most 10.

All numbers are deterministic from the workspace data and the catalogue; no randomness, no clock beyond `now`.

## 5. API (per workspace, via the dispatcher)

- `GET /markets/intel?period=YYYY-MM&limit=N` → the report above. `limit` 1–100 (default 25); bad `period` → 422.
- `GET /markets/intel/{country}` (ISO2, case-insensitive) → that country's row plus its cities; unknown/empty country → 404.
- `POST /spend` accepts optional `country` (ISO2 in the catalogue) and `city` (catalogue city name or id). Unknown values → 422; a resolved city sets `country` itself and a conflicting `country` → 422. Existing callers are unaffected.
- `POST /markets/backfill-origin` (owner). Roles: `GET /markets/*` and `POST /spend` stay manager+; agents get 403 (already the default).
- `POST /capture` and `POST /webhooks/form/{platform}` accept optional `country`, `city` strings (ignored if invalid).

## 6. Dashboard — Markets page (`#/markets`, manager and owner)

New module `dashboard/app/markets.js`, nav entry between ROI and Settings, EN/AR strings in `i18n.js`. Contents: tiles (known-origin %, top country by leads, top focus city); a period filter; a country table (click expands its cities); the focus list with reason chips and a "measured / editorial · n leads" badge; the untapped-candidates list with language and local contact window; an empty state explaining how origin is derived and how to tag spend. All dynamic text through `esc()`; no inline styles (widths via `data-w`).
The ROI page's spend form gains optional country/city selectors (catalogue-backed `<datalist>`).

## 7. Compliance and privacy

Origin is inferred only from information the person gave the agency (form fields, their own phone number, their own words). No IP geolocation, no profile lookups, no third-party enrichment. The page shows aggregate counts only; unknown origin is "Unknown". Marketing in a market still follows the consent ladder (unchanged). Editorial tiers are labelled as editorial priors, never as statistics.

## 8. Testing

- `tests/test_origin.py`: precedence (form > phone > text), prefix edge cases (`+971`, `+1`, `+7`, `+44`, malformed), every phrase in EN/AR, **false positives** ("interested in London property", "buy a flat in Dubai Marina", "from AED 1M"), diacritics/Arabic-Indic digits in numbers, never raises.
- `tests/test_intel.py`: counts/ratios/growth windows, tagged vs unallocated spend, shrinkage (1 lead vs 50 leads), AE excluded from ranking, untapped list, period filter, empty workspace.
- `tests/test_origin_pipeline.py`: ingest sets origin, stronger basis overrides weaker (never the reverse), capture/webhook optional fields, migration on an existing DB, backfill idempotent, erasure removes origin.
- `tests/saas/test_markets_api.py`: manager/owner/agent access, backfill owner-only, tenant isolation (the route enumeration in `test_isolation.py` covers the new routes automatically), invalid inputs.
- Browser: Markets page renders seeded data, expand a country, filter by period, RTL, zero console errors, XSS-safe names.
- All existing tests keep passing.

## 9. Delivery order

(1) resolver + tests → (2) migration/pipeline/spend tagging → (3) report + ranking → (4) API + role rule → (5) dashboard page → (6) docs (README, CITY_CATALOGUE cross-link). Each step lands with its tests; a fresh reviewer checks the branch at the end.

## 10. Open items for the owner

- The tier→prior mapping (80/55/30), the shrinkage constant (10) and the "measured" threshold (10 leads) are starting values; confirm or change.
- Whether agencies should also be able to override a lead's origin manually (not in this release).
