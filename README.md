# DigitalBurj Leads

Lead capture, qualification and follow-up for UAE real-estate agencies — branded for [DigitalBurj](https://digitalburj.com).
It turns public buying/renting/selling intent and first-party lead forms (Reddit, Facebook, Instagram, TikTok, LinkedIn, the property
portals, WhatsApp, your website) into a **scored, explainable, compliance-gated** pipeline, routes each lead to an agent, and tells the
agent what to do next.

```
sources ─► connectors ─► suppression check ─► bilingual intent NLP ─► identity resolution ─► explainable score ─► routing + SLA
(official API / licensed   (opt-outs are        (EN + Gulf Arabic:      (phone/email/handle,    (0-100, every point   (agent assigned,
 provider / first-party)    never re-ingested)   area, budget, beds…)    cross-platform merge)   attributed)           escalated if idle)
                                                                                                         │
                              WhatsApp qualification · inventory matches · deal sheet · viewings (.ics) · next-best-action tasks · ROI
```

## What it does

| Area | Detail |
|---|---|
| **Bilingual intent engine** | English + Gulf Arabic (diacritics, Arabic-Indic digits, "غرفة وصالة" = 1BR, "مليون" = million). Buyer / investor / renter / seller / landlord / undecided; stage (research → shortlisting → ready); broker spam and noise filtered. Every decision returns its evidence phrases. |
| **UAE entities** | 35 communities across 7 emirates with EN/AR aliases (longest match wins), budgets ("3-4M", "800k"), bedrooms, off-plan vs ready, cash/mortgage/payment plan, golden-visa interest, urgency. |
| **Explainable scoring** | 0-100 = intent + stage + specificity + urgency + financial readiness + source quality + contactability (+ asked-for-agent, cross-platform and repeat-enquiry bonuses) × recency decay. HOT/WARM/NURTURE with reply SLAs of 5 min / 1 h / 24 h. |
| **Agent routing** | Weighted assignment by area, language (Arabic leads → Arabic speakers) and capacity; unanswered HOT/WARM leads are reassigned after their SLA (max 2 hops) and then flagged for the manager. |
| **WhatsApp qualification** | Cloud-API webhook (HMAC-verified): a short EN/AR flow collects intent, area, budget, timeline, then asks for consent. YES upgrades the lead to `opted_in` with the transcript stored as evidence; STOP / إيقاف erases the lead. Replies only to people who messaged first; **sending is dry-run until you enable it**. |
| **Inventory matching** | Import your own project list (CSV). Leads are matched by area (hard filter), budget fit, bedrooms and off-plan timing; golden-visa fit uses a configurable threshold. |
| **Deal sheet & calculators** | Transaction costs (DLD, agency + VAT, mortgage registration), monthly payment, gross/net yield, cash needed. All rates are configuration marked *verify current rules*. |
| **Viewings** | Per-agent overlap detection, lead status → `viewing`, RFC 5545 `.ics` feed per agent for calendar subscription. |
| **Next-best-action** | Per-lead tasks (assign, call overdue, first response, send matches, nurture day 2/5/10, valuation visit, re-engage) each showing whether the channel is *permitted*. `/tasks/today` per agent. |
| **ROI by source** | Spend per source (optionally per month) → leads, qualified, won, median first-response time, % within SLA, cost per lead / qualified / won. Ratios are `null` (never an error) when there's nothing to divide by. |
| **Capture widget** | `widget.js` + `POST /capture`: origin allow-list, honeypot, rate limit, explicit consent checkbox whose wording is stored as evidence. |
| **Source-market catalogue** | 231 cities in 109 countries (Bangalore, Chennai, Mumbai, London, Riyadh, Moscow, Lagos, Singapore, New York…) with language, currency, timezone and aliases; free-text city detection in EN/AR/RU. See `docs/markets/CITY_CATALOGUE.md`. |
| **Market intelligence** | Where your leads come from, by country and city: leads, qualified, won, growth and cost per lead/qualified/won (tag ad spend with a country or city). A "where to focus next" list blends *your measured results* with the catalogue's editorial priority (few leads → mostly editorial, labelled as such) and lists untapped priority cities with their local contact window in UAE time. Origin is read only from what people gave you (a country/city form field, their own phone's country code, or an explicit "I am in London" phrase) — no IP lookups or enrichment — and the page shows aggregates only. `GET /markets/intel`, Markets page in the dashboard; `POST /markets/backfill-origin` (owner) fills in older leads. |
| **Demand Radar** | De-identified demand by community with growth and buy/rent/supply mix. |
| **Dashboard** | DigitalBurj brand (wordmark, palette, fonts), light/dark, English and Arabic (RTL), phone-friendly. In platform mode: accounts, isolated workspaces, roles, marketing site and an app shell (see below). |

## Platform mode: accounts, workspaces, marketing site, dashboard

Set `CONTROL_DB` and the app becomes a multi-agency service (leave it empty for the single-tenant app described above, which is unchanged):

- **Marketing site** (EN/AR, RTL): `/`, `/features`, `/how-it-works`, `/security`, `/compliance`, `/pricing`, `/faq`, `/contact`, `/privacy`, `/terms`. Copy lives in `site/i18n/{en,ar}.json`, price text in `site/pricing.json` (default: "Early access — contact us"), contact address in `CONTACT_EMAIL`. Privacy and Terms are **templates for review by UAE counsel**.
- **Accounts**: `/start` (create a workspace), `/login`, `/forgot`, `/reset`, `/verify`, `/invite` work without JavaScript. Passwords use scrypt; sessions are hashed server-side with `HttpOnly`/`SameSite` cookies (`Secure` unless `DEV_INSECURE_COOKIES=1`); CSRF tokens on every cookie-authenticated write; progressive lockout; optional TOTP two-factor with recovery codes; e-mail links work once. JSON equivalents live under `/auth/*`.
- **Workspaces**: a person can belong to several workspaces and switches between them in the top bar. Every agency gets its own SQLite file served by its own app instance, so one agency's request cannot read another's rows. Roles: `owner`, `manager`, `agent` (agents see only the leads assigned to them). Machine access: per-workspace API key (`X-API-Key`, manager rights, shown once). Public routes per workspace: `/t/<public key>/{capture,widget.js,optout,webhooks/...}`.
- **Dashboard** at `/app`: sidebar shell, Overview KPIs and onboarding checklist, command palette (`Ctrl+K`), light/dark and EN/AR toggles, and Settings (agency profile, website embed + allowed origins, integration secrets, API key, two-factor, team invitations).

```bash
export CONTROL_DB=./data/control.db DATA_DIR=./data BASE_URL=http://127.0.0.1:8000 DEV_INSECURE_COOKIES=1   # local http only
export ENCRYPTION_KEY=$(python -c "from leadengine.platform.security import generate_key; print(generate_key())")   # keep it: it decrypts workspace secrets
python -m leadengine.cli serve                                   # http://127.0.0.1:8000  (e-mail links are printed in the log while DEV_INSECURE_COOKIES=1)
OWNER_PASSWORD='a long passphrase' python -m leadengine.cli create-owner you@agency.ae --name "You" --agency "Your Agency"   # works with SIGNUP_MODE=invite
python -m leadengine.cli maintain                                # cron: SLA escalation + retention purge for every workspace; exit 1 if any workspace failed
```

`CONTROL_DB` without `ENCRYPTION_KEY` refuses to start. Workspaces do **not** inherit the operator's connector credentials or licence identity: each agency sets its own WhatsApp phone ID and token, Meta and form-webhook secrets and website origins in Settings (Reddit/Apify connectors, the DNC file and WhatsApp sending are single-tenant features for now). Use `MAIL_BACKEND=smtp` (`SMTP_*`) for real e-mail, put the app behind HTTPS and set `TRUSTED_PROXIES` so per-IP limits see real addresses. Back up the whole `DATA_DIR` and keep `ENCRYPTION_KEY` safe.

## Sources: what's supported and on what basis

The engine **refuses to register a connector** that doesn't declare a lawful basis (`official_api`, `first_party_form`,
`licensed_provider`, `user_upload`). It does not do login-wall scraping, fake accounts or rate-limit evasion — those breach platform
terms and UAE data-protection law (PDPL), and get accounts banned.

| Source | How | Basis |
|---|---|---|
| Reddit | official OAuth API, UAE subreddits × intent queries (`REDDIT_CLIENT_ID/SECRET`) | `official_api` |
| Facebook + Instagram | Lead Ads webhook → Graph API (`POST /webhooks/meta`, HMAC-verified) | `first_party_form` |
| TikTok / LinkedIn lead forms, website forms | signed JSON webhook `POST /webhooks/form/{platform}` (native integration, Zapier or Make) | `first_party_form` |
| Public posts on LinkedIn/FB/IG/TikTok/X | an Apify actor **you are licensed to use**, mapped to signals | `licensed_provider` |
| Bayut / Property Finder / Dubizzle | enquiry notification emails → `POST /ingest/portal-email` (EN/AR labels) | `first_party_form` |
| WhatsApp | Cloud API webhook `POST /webhooks/whatsapp` | `first_party_form` |
| Your site | `widget.js` / `POST /capture` | `first_party_form` |
| Your CRM / event lists | `POST /ingest/csv` (include a `consent` column) | `user_upload` |

Phone/email are only ever populated from first-party forms, portals, WhatsApp or uploads — never inferred from social profiles.

## Compliance by design

Engineering guidance, **not legal advice**: confirm retention, consent wording, calling hours and DNCR handling with UAE counsel for
your licence type (PDPL — Federal Decree-Law 45/2021; TDRA telemarketing rules; DLD/RERA advertising rules).

- **Consent ladder:** `public_signal` (public reply only) → `enquiry` (they asked about a listing: call/WhatsApp/email, no SMS/DM) → `opted_in`
  (explicit, wording stored). A public post is never treated as marketing consent.
- **Opt-out / erasure:** `POST /optout` (unauthenticated on purpose), `DELETE /leads/{id}` and WhatsApp STOP delete the person and write a one-way
  hash to a suppression list, so they're never re-ingested and no plaintext is kept.
- **Quiet hours** (UAE time, default 09:00-20:00) and a **DNCR file** gate calls/SMS/WhatsApp *outreach*; blocked actions return `retry_after`.
  Replies inside a conversation the person started are not delayed, but never override opt-out.
- **Retention:** public-signal leads purged after 90 days, opted-in after 730 (`POST /maintenance/purge` — schedule it daily).
- **Data minimisation:** noise and broker spam are counted, never stored. **Audit log** of ingestion, consent, drafts, erasure.
- **Nothing is auto-sent.** Outreach drafts carry agent name, BRN, Trakheesi permit and an opt-out line; a human approves them.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in what you use; export it (set -a; . ./.env; set +a)
python -m leadengine.cli serve --demo      # seeds synthetic agents, projects, spend and EN/AR leads → http://127.0.0.1:8000
python -m leadengine.cli classify "ابغى اشتري شقه في مارينا ميزانيتي 2 مليون"

pip install -r requirements-dev.txt && python -m pytest        # browser smoke tests skip if Playwright/Chromium is absent
docker build -t digitalburj-leads . && docker run -p 8000:8000 -v $PWD/data:/data --env-file .env digitalburj-leads   # platform mode if .env sets CONTROL_DB + ENCRYPTION_KEY
```

Set `LEADENGINE_API_KEY` for anything non-local (header `X-API-Key`), `PUBLIC_URL` (the base URL baked into `widget.js`) and, behind a reverse proxy, `TRUSTED_PROXIES=<number of proxies>` so the capture rate limit sees real client IPs instead of a spoofable header. Webhooks authenticate by signature; `/capture`, `/widget.js` and
`/optout` are public by design. **Schedule** `POST /maintenance/escalate` (every few minutes) and `POST /maintenance/purge` (daily).
OpenAPI docs: `/docs`. Embed the widget:

```html
<div id="digitalburj-leads"></div>
<script src="https://leads.your-agency.ae/widget.js" data-source="landing-marina" defer></script>
```

## Deploy (Docker)

```bash
docker build -t digitalburj-leads .
# production: persistent volume + secrets from your env file
docker run -d --name digitalburj-leads -p 8000:8000 -v digitalburj-data:/data --env-file .env digitalburj-leads
# throwaway demo with synthetic data (no secrets):
docker run --rm -p 8000:8000 digitalburj-leads python -m leadengine.cli serve --demo --host 0.0.0.0
```

The image runs as an unprivileged user, stores SQLite in the `/data` volume and has a `/health` healthcheck. **Without `LEADENGINE_API_KEY`
every endpoint is open** (including `/demo/seed` and lead data) — acceptable for a local or throwaway demo, never for real leads. Put it behind
HTTPS (reverse proxy or your host's TLS); webhooks and the capture widget need a public HTTPS URL. SQLite means one running instance.

## Main endpoints

`/ingest/{signals,csv,portal-email}` · `/connectors/{name}/run` · `/webhooks/{meta,whatsapp,form/{platform}}` · `GET /leads` · `GET /leads/{id}` ·
`/leads/{id}/{outreach,consent,status,activities,assign,matches,deal-sheet,next-actions,viewings}` · `/agents` · `/tasks/today` ·
`/viewings` + `/viewings.ics` · `/inventory` · `/spend` + `/roi` · `/markets/{cities,summary,intel}` · `/radar` · `/tools/{deal-costs,mortgage,yield}` · `/capture` · `/optout` · `/audit`

## Known limits (read before relying on it)

- Platform mode runs on SQLite too: one process, one volume. Calendar (`.ics`) feeds and per-agent feed tokens are not available per workspace yet; sessions are required.
- Weights/capacity are validated, but assignment and escalation are not transactional across concurrent workers — run one instance.
- Intent detection is rule-based: auditable and free to run, but recall on slang/typos/Arabizi is limited. Plug an LLM in via `Pipeline(llm_refiner=fn)`.
- **Connectors are tested against mocked/fixture data only** (Reddit, Meta, WhatsApp, Apify, portal emails — the portal fixtures are representative
  layouts, not captured feeds). Validate each against your live accounts first. Meta and WhatsApp need app/business verification.
- Fees, LTV, mortgage rates, golden-visa threshold and calling hours are **configuration with indicative defaults** — verify current rules.
- SQLite suits a single agency; swap `db.py` for Postgres at scale. The Demand Radar loads recent signals in Python.
- The Arabic UI translates labels and chrome; server-generated explanations (compliance reasons, action reasons) are English.
- Calendar feeds (`/viewings.ics?agent_id=…&token=…`) use a per-agent, read-only token derived from the API key (shown on the Agents tab); the master key is header-only and is never accepted in a URL.
- Opted-out people are ignored permanently (a returning WhatsApp message or widget submission is dropped silently). Whether to let an explicit new opt-in lift the suppression is a policy/legal decision left to you.
- ROI groups spend by source (not campaign); use the Period filter (`YYYY-MM`) so spend and leads cover the same window. Names like "Bayut" are matched to `portal:bayut`.
- Market origin is known only for leads whose phone has a country code (not shared +1 numbers), who filled a country/city field, or who wrote an explicit self-location phrase; a plain mention of a city is not treated as origin. Arabic city names are matched only where the catalogue has an Arabic alias (London, Mumbai, Karachi, Lahore, Delhi, Riyadh, Cairo… — add more in `cities.psv`). Editorial tiers are priors, not statistics.
- Nothing verifies that a BRN or Trakheesi permit is valid; drafts just include what you configure.

## Brand

Wordmark, favicons, palette and fonts come from the DigitalBurj website repository (`dashboard/brand/NOTICE.md`), reused with the brand owner's
authorisation. Tokens live in `dashboard/theme.css`.

## Layout

```
leadengine/nlp/          text normalisation, area gazetteer, intent + entity extraction
leadengine/connectors/   reddit · apify (licensed provider) · forms (Meta/TikTok/LinkedIn/portals/CSV) · whatsapp
leadengine/pipeline.py   ingest → classify → resolve → score → aggregate; consent, erasure, opt-out
leadengine/markets/      city catalogue · origin resolver · market report
leadengine/{scoring,compliance,distribution,qualify,inventory,calculators,viewings,nba,roi,radar,capture,outreach}.py
leadengine/api.py        FastAPI app            dashboard/   index.html + theme.css + brand/ + fonts/
leadengine/platform/     accounts, security, control DB, workspaces (tenancy), role policy, dispatcher app, site renderer, maintenance
site/                    marketing-site templates, EN/AR strings, pricing.json
dashboard/app/           platform dashboard (ES modules) · dashboard/app.html
docs/superpowers/        design specs and implementation plans
```
