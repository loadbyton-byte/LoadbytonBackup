# Deals, commission and leaderboard — design

## Goal
Track every deal from viewing to payment, show pipeline value and expected commission, rank agents, and give the ROI page real revenue per source.

## Decisions (agreed with the owner)
- Out of scope: invoicing, payment collection, tax filing, co-broker/referral portal (the split field is only stored).
- Owners/managers see all deals; agents see and edit only their own deals and only their own commission.

## Global constraints
- Money is stored as REAL AED; percentages 0-100; all defaults are configuration marked "verify current rules" (sale 2%, rental 5% of annual rent, VAT 5%).
- Every stage change is time-stamped and audited. Nothing is hard-deleted by agents.
- Per-workspace SQLite; platform tests in `tests/saas/`.

## Data model
- `deals(id, lead_id, agent_id, kind, title, area, value_aed, commission_pct, commission_aed, vat_aed, split_pct, expected_close, stage, lost_reason, notes, created_at, updated_at)`; `kind` is `sale|off_plan|rental`; `value_aed` is price (sale/off-plan) or annual rent (rental).
- `deal_events(id, deal_id, ts, from_stage, to_stage, actor)`.
- Stages in order: `viewing_done, offer_made, offer_accepted, contract_signed, paid`; terminal `lost` (needs `lost_reason`).
- Settings (kv): `commission_sale_pct` (2), `commission_rental_pct` (5), `vat_pct` (5).

## Behaviour
1. Create: needs a lead, positive value <= 10 billion, kind, stage (default `viewing_done`); `commission_pct` defaults from kind; `commission_aed = value * pct/100 * (1 - split_pct/100)` (the agency's share), `vat_aed = commission_aed * vat_pct/100`, both recomputed whenever value, pct or split changes.
2. Move stage: forward or back one or more steps allowed for managers; agents move only their own deals. Moving to `offer_accepted` or later sets the lead status to `won` and calls `Followups.stop(lead, "closed")`. Moving to `lost` requires a reason, sets the lead status to `lost` only if the lead has no other open deal, and stops follow-ups likewise. Re-opening a lost deal to an open stage does not restart follow-ups.
3. `GET /deals` (filters: stage, agent_id, kind), `POST /deals`, `PATCH /deals/{id}` (fields above), `POST /deals/{id}/stage`, `GET /deals/{id}` (with events), `GET /deals/summary` (pipeline value and expected commission per stage, closed this month, conversion lead->deal), `GET /deals/leaderboard` (per agent: deals, closed value, commission, lead->deal conversion, optional `period=YYYY-MM`).
4. ROI: `roi` rows gain `revenue_aed`, `commission_aed` and `commission_per_spend` (null when spend is 0) from deals in `paid` stage, attributed to the lead's first source.
5. Dashboard: Deals page (board of stage columns with move buttons, add/edit form, lost-reason prompt), Overview tiles for pipeline value and expected commission, leaderboard table. EN/AR.

## Interfaces / roles
- RBAC: managers+ everything; agents `GET /deals`, `GET /deals/{id}`, `POST /deals`, `PATCH`, `POST .../stage` scoped to their own agent_id (row scoping by the dispatcher); leaderboard and summary show an agent only their own row.

## Review focus
- Agent tries to read or move another agent's deal (IDOR) by id.
- Value 0, negative, NaN, huge, or split over 100: rejected, not stored.
- Two deals for one lead, one lost and one open: lead stays open.
- Deleting/erasing a lead (GDPR) must remove its deals' personal link without breaking totals.
- Commission recalculated after edits; rounding to 2 decimals; VAT shown separately.
- Moving back from `paid` to an earlier stage removes it from ROI revenue.

## Testing
Unit tests for calculations and stage rules; API + RBAC + IDOR tests; platform isolation test; ROI attribution test; one browser test of the board and lost-reason prompt.
