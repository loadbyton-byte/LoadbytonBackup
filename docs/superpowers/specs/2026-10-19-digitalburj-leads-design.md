# DigitalBurj Leads — design spec

Date: 2026-10-19 · Status: draft for review · Path: architectural (approach A approved)

## 1. Intent

Turn the existing UAE Real Estate Lead Engine (`leadengine/`) into **DigitalBurj Leads**: a branded product that helps UAE
real-estate agencies capture, qualify, route and convert leads fast, while staying inside platform terms and UAE data rules.
It sits under the Digital Burj *Industries → Real estate* offering, and operationalises the Digital Burj guide
*"Real estate lead response: why the first hour decides the deal"* (agents stay in control; no auto-spam).

**Success =** (a) every screen uses the real Digital Burj identity; (b) a hot lead is assigned to a named agent within seconds and
escalated if unanswered; (c) WhatsApp enquiries are qualified in-chat and consent is captured as evidence; (d) an agency can see
cost-per-qualified-lead by channel; (e) nothing violates the compliance gate; (f) all new behaviour is tested.

**Non-goals:** bulk scraping, logged-in/fake-account collection, auto-sending marketing, pushing to the Digital Burj repo (read-only
access), unverifiable marketing claims ("#1", "only tool") anywhere in the product or docs, rebuilding in Next.js.

## 2. Brand layer (source: `shamhar07-max/DigitalBurjFinalMain`, read-only, public)

| Token | Value |
|---|---|
| ink / text | `#0f1714` |
| paper / background | `#f6f5f1` (dark mode: ink background, paper text) |
| signal | `#f23a1d`, hover `#c9260e`, soft `#ff7a45` |
| mark green | `#0a211b` |
| muted text | `#6b746f` / `#4a544f` |
| success | `#3ecf6e` |
| fonts | Plus Jakarta Sans (headings), Instrument Sans (UI) — self-hosted woff2 |

Assets copied (not hot-linked) into `dashboard/brand/` and `dashboard/fonts/`: `wordmark-clear.png`, `wordmark.webp`, `favicon.svg`,
`favicon-192/512.png`, `apple-touch-icon.png`, `opengraph.png`, `industry/04-real-estate.webp`, the two woff2 fonts.
`dashboard/brand/NOTICE.md` records provenance. **Open item:** the repo being public is not a licence — the owner confirms they
are authorised to reuse the assets (the user stated this is their brand; recorded as an assumption).
UI voice: plain, evidence-first, no invented numbers. Product name in UI/docs/API title: **DigitalBurj Leads**; Python package stays
`leadengine` (no churn). Served via FastAPI `StaticFiles` at `/static`.

## 3. New capabilities (each a small, isolated module)

1. **Distribution & SLA escalation** — `distribution.py`, table `agents(id,name,phone,email,areas,languages,weight,max_open,active)`.
   `assign(lead)`: candidates = active agents under `max_open`; prefer area + language match (Arabic lead → Arabic speaker);
   pick lowest `open_leads/weight` (weighted round-robin, deterministic tie-break by id). Falls back to all active agents.
   `escalate_overdue(now)`: HOT/WARM leads still `new` past `sla_due_at` are reassigned to the next eligible agent and an
   `escalated` activity is logged (max 2 hops, then flagged for the manager). Driven by `POST /maintenance/escalate` (cron).
2. **WhatsApp qualification** — `qualify.py` + `connectors/whatsapp.py`. Meta Cloud API inbound webhook (`/webhooks/whatsapp`,
   HMAC-verified). Deterministic state machine: buy/rent → area → budget → timeline → consent question. Answers are parsed with the
   existing NLP and merged into the lead. Explicit yes ⇒ `opted_in` with the transcript as evidence; STOP/إيقاف ⇒ opt-out + erasure.
   Replies only to people who messaged first (service window). Outbound goes through a `Sender` interface; default is a **dry-run**
   (`WHATSAPP_SEND_ENABLED=false`), the Meta sender is opt-in. Bilingual EN/AR scripts.
3. **Portal adapters + multi-enquiry heat** — fixtures and parsers for Bayut / Property Finder / Dubizzle notification emails;
   `listing_ref` stored per signal; a person enquiring on ≥3 listings in 7 days gets a documented score bump (`repeat_enquiry`).
4. **Off-plan / inventory matching** — `inventory.py`, table `projects(name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan)`
   loaded from the agency's own CSV. `match(lead)` ranks by area, budget fit, beds; flags golden-visa fit when budget ≥ a configurable
   threshold (default AED 2,000,000; documented as "verify current rules"). `GET /leads/{id}/matches`.
5. **Source ROI** — `roi.py`, table `spend(source,campaign,period,amount_aed)` (API/CSV). `GET /roi` per platform/campaign: leads,
   qualified (HOT/WARM), % first-contact within SLA, median first-response minutes, won, cost per lead / qualified / won, win rate.
   First-contact time comes from `activities` (status → `contacted`).
6. **Embeddable capture widget** — `GET /widget.js` + `POST /capture` (public): origin allow-list (`CAPTURE_ORIGINS`), honeypot field,
   per-IP rate limit, required consent checkbox whose exact wording is stored as evidence. Mirrors the pattern in the site's
   `api/lead.js` (same-site check, honeypot `website`, rate limit). No forwarding to the site's API (YAGNI).

## 4. Data & API changes

New tables: `agents`, `projects`, `spend`, `conversations(lead_id,channel,state,transcript,updated_at)`. New lead columns:
`assigned_agent_id`, `escalation_level`. New endpoints: `/agents` (CRUD), `/leads/{id}/assign`, `/maintenance/escalate`,
`/webhooks/whatsapp` (GET verify + POST), `/inventory` (CSV import), `/leads/{id}/matches`, `/spend`, `/roi`, `/capture`, `/widget.js`.
Existing endpoints and the compliance gate are unchanged; every new outbound path must call `Compliance.can_contact`.

## 5. Dashboard

Re-skin `dashboard/index.html` with the tokens above, real wordmark in the header, light/dark via `prefers-color-scheme`.
New views: Agents & queue (who owns what, overdue/escalated), ROI by source, Inventory matches inside the lead dialog.
Accessible: visible focus, 4.5:1 text contrast, works at phone width, no horizontal scroll.

## 6. Testing (TDD per module)

Unit: distribution tie-breaks/capacity/language match/escalation hops; qualification state machine incl. Arabic, STOP, consent evidence;
inventory scoring; ROI arithmetic with fixture activities; widget origin/honeypot/rate-limit/consent-required. Integration: signed
WhatsApp webhook → lead updated → consent upgraded → compliance verdict changes. UI: headless-browser screenshot checks that brand assets
load (no 404s) and no console errors. Existing 46 tests keep passing.

## 7. Risks & honest limits

- Meta WhatsApp Cloud API needs a verified business, templates for out-of-window messages, and review; tested here with mocks only.
- Golden-visa threshold and DNCR/calling-hour rules change; they are configuration, not hard-coded law.
- Portal email formats vary; parsers are fixture-tested, not validated against live feeds.
- Market claims in docs are limited to what was observed in the survey sample; no superlatives.

## 8. Delivery order

(1) brand layer + static serving → (2) distribution/escalation → (3) ROI → (4) inventory matching → (5) widget → (6) WhatsApp qualification →
(7) portal adapters/repeat-enquiry → (8) docs/README rebrand. Each step ships with tests and a commit.
