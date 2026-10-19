# UAE Real Estate Lead Engine

Turns public buying/renting/selling intent and first-party lead forms across Reddit, Facebook, Instagram, TikTok,
LinkedIn and the property portals into a **scored, explainable, compliance-gated** lead pipeline for UAE agencies.

```
sources ──► connectors ──► suppression check ──► bilingual intent NLP ──► identity resolution ──► explainable score ──► SLA queue
(official API / licensed        (opt-outs never      (EN + Gulf Arabic:        (phone/email/handle,     (0-100, every point     (HOT = reply
 provider / first-party form)    re-ingested)         area, budget, beds,       cross-platform merge)    attributed)             in 5 min)
                                                      timeline, financing)
```

## What it does

| Capability | Detail |
|---|---|
| **Bilingual intent engine** | English + Gulf-Arabic (diacritics, Arabic-Indic digits, "غرفة وصالة" = 1BR, "مليون" = million). Detects buyer / investor / renter / seller / landlord / seeker, stage (research → shortlisting → ready), and filters broker spam and noise. Returns the *evidence phrases* that triggered each decision. |
| **UAE entity extraction** | 35 communities across 7 emirates with EN/AR aliases (JVC, نخلة جميرا, Al Reem…), longest-match-wins; budget ranges ("3-4M", "800k", "AED 1,200,000"); bedrooms; off-plan vs ready; cash / mortgage / payment plan; golden-visa interest; urgency windows. |
| **Explainable scoring** | 0-100 = intent + stage + specificity + urgency + financial readiness + source quality + contactability + "asked for an agent", × recency decay, + cross-platform corroboration. Unrealistic budgets are flagged. Tiers HOT/WARM/NURTURE with reply SLAs (5 min / 1 h / 24 h). |
| **Identity resolution** | Exact match on E.164 phone (UAE formats normalised), email, handle. Strict soft-link of the same handle across platforms only when a community also overlaps. Duplicate profiles merge. |
| **Demand Radar** | De-identified per-community demand, week-over-week growth, buy/rent/supply mix, median budget — see areas heating up before listings reflect it. |
| **Outreach drafts** | EN/AR drafts per channel carrying agent name, BRN, Trakheesi permit and an opt-out line. **Never auto-sent**; each shows its compliance verdict. |
| **Compliance gate** | See below — enforced in code, with tests. |

## Sources: what's supported and on what basis

The engine **refuses to register a connector** that doesn't declare a lawful basis (`official_api`, `first_party_form`,
`licensed_provider`, `user_upload`). It does not do login-wall scraping, fake accounts or rate-limit evasion — those breach
platform terms and UAE PDPL, and get accounts and ad budgets banned.

| Source | How | Basis | Notes |
|---|---|---|---|
| **Reddit** | `connectors/reddit.py` — official OAuth API, UAE subreddits × intent queries | `official_api` | Needs `REDDIT_CLIENT_ID/SECRET`. Follow Reddit Data API terms. |
| **Facebook + Instagram** | `connectors/forms.py::MetaLeadAdsConnector` — Lead Ads webhook → Graph API (`/webhooks/meta`, HMAC-verified) | `first_party_form` | Highest-value source: people typed their own details. Needs `leads_retrieval`. |
| **TikTok / LinkedIn lead forms** | `POST /webhooks/form/{platform}` — HMAC-signed JSON (native integration, Zapier or Make) | `first_party_form` | Field aliases handle name/phone/email/consent + custom questions. |
| **Public posts/comments on LinkedIn, FB, IG, TikTok, X** | `connectors/apify.py` — run an actor *you are licensed to use*, map items to signals | `licensed_provider` | You own the vendor/ToS diligence. Public data only. |
| **Bayut / Property Finder / Dubizzle** | `POST /ingest/portal-email` parses enquiry notification emails | `first_party_form` | Forward the portal's lead emails to a webhook. |
| **Your CRM / event lists** | `POST /ingest/csv` | `user_upload` | Include a `consent` column; rows without it are treated as no consent. |

Public-post intent is stored as a *signal with evidence*; phone/email are **only ever populated from first-party forms,
portals or uploads** — never inferred or harvested from social profiles.

## Compliance by design

This is engineering guidance, **not legal advice**. Confirm retention, consent wording, calling hours and DNCR handling with UAE
counsel for your licence type (PDPL — Federal Decree-Law 45/2021; TDRA telemarketing rules; DLD/RERA advertising rules).

- **Consent ladder:** `public_signal` (public reply only) → `enquiry` (they asked about a listing: reply by call/WhatsApp/email, no SMS/DM)
  → `opted_in` (explicit, with the consent wording stored as evidence). A public post is never treated as marketing consent.
- **Opt-out / erasure:** `POST /optout` (unauthenticated on purpose) and `DELETE /leads/{id}` delete the person and write a **one-way hash**
  of their identifiers to a suppression list, so they're never re-ingested and we keep no plaintext.
- **Quiet hours** (UAE time, default 09:00-20:00) and a **DNCR file** (`DNC_FILE`) gate calls/SMS/WhatsApp; blocked actions return `retry_after`.
- **Retention:** public-signal leads purged after 90 days, opted-in after 730 (`POST /maintenance/purge`; schedule it daily).
- **Data minimisation:** noise and broker spam are counted, never stored.
- **Audit log** of ingestion, consent, drafts, erasure.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in what you use; export vars (e.g. `set -a; . ./.env; set +a`)
python -m leadengine.cli serve --demo      # seeds synthetic EN/AR demo leads; open http://127.0.0.1:8000
python -m leadengine.cli classify "ابغى اشتري شقه في مارينا ميزانيتي 2 مليون"

pip install -r requirements-dev.txt && python -m pytest      # 46 tests
docker build -t uae-lead-engine . && docker run -p 8000:8000 -v $PWD/data:/data --env-file .env uae-lead-engine
```

Set `LEADENGINE_API_KEY` for any non-local deployment (send as `X-API-Key`). Webhooks authenticate by signature instead.
OpenAPI docs: `/docs`.

### Key endpoints

`POST /ingest/signals` · `POST /connectors/{name}/run` · `GET /leads?tier=HOT&area=Dubai Marina&overdue=true` · `GET /leads/{id}`
(evidence + score breakdown) · `POST /leads/{id}/outreach?channel=whatsapp` · `POST /leads/{id}/consent` · `POST /leads/{id}/status` ·
`GET /radar` · `GET /stats` · `POST /optout` · `DELETE /leads/{id}` · `GET /audit`

## Known limits (read before relying on it)

- Intent detection is rule-based: fast, auditable, no API cost — but recall on slang/typos/Arabizi is limited. Plug an LLM in via
  `Pipeline(llm_refiner=fn)`; it runs only on posts that already passed the rules.
- Platform APIs gate what's reachable: Meta needs app review for `leads_retrieval`; Reddit rate-limits; LinkedIn/TikTok public-content access
  is vendor-dependent. The generic webhook/Apify mappers are written against documented shapes but **have only been tested with mocked
  responses** — validate each connector against your live accounts before production.
- SQLite is fine for a single agency; swap `db.py` for Postgres at scale. The Demand Radar loads recent signals in Python.
- Nothing here sends messages. Wire approved drafts into WhatsApp Business (template messages) or your CRM yourself.
- The tool can't verify that a Trakheesi permit or BRN is valid; it just makes sure they're present in drafts.

## Layout

```
leadengine/nlp/        text normalisation, area gazetteer, intent + entity extraction
leadengine/connectors/ reddit, apify (licensed provider), forms (Meta/TikTok/LinkedIn/portal/CSV)
leadengine/pipeline.py ingest → classify → resolve → score → aggregate; consent, erasure, opt-out
leadengine/scoring.py  explainable score + SLA     leadengine/compliance.py  the gate
leadengine/radar.py    demand radar                leadengine/outreach.py    gated EN/AR drafts
leadengine/api.py      FastAPI app                 dashboard/index.html      single-file UI
```
