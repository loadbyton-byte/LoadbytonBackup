# Follow-up engine (speed-to-lead + consent-based nurture) — design

## Goal
Answer every new lead within seconds and keep it warm for up to 90 days with bilingual, consent-gated messages, without weakening any existing compliance rule. Auto-sending is opt-in per workspace; the default is an approval queue.

## Decisions (agreed with the owner)
- Automation level: **approval queue by default**. Per workspace, a manager may switch auto-send on. With it on, consented leads receive touches directly, inside allowed hours.
- Channels: WhatsApp and email only. No SMS, no voice.
- Out of scope: visual workflow builder, A/B testing, SMS/voice, new lead sources.

## Global constraints
- Every touch passes `Compliance.can_contact(lead, channel)` immediately before sending; a verdict computed at scheduling time is never trusted.
- `opted_out` leads, suppressed identifiers and DNC numbers never receive a touch. STOP (existing path) cancels all pending touches for that lead.
- WhatsApp sending stays dry-run until `WHATSAPP_SEND_ENABLED` and credentials are set (existing behaviour); outside the 24-hour window only approved template messages may be used.
- Every message carries the agent/agency/BRN disclosure and an opt-out line (reuse `outreach.py` wording).
- Idempotent: running the worker twice must not send a touch twice.
- Per-workspace SQLite; tests under `tests/`, platform tests under `tests/saas/`.

## Data model
- `sequences(id, name, trigger, enabled, steps JSON, created_at)`; `trigger` is `new_lead` or `status:<status>`.
- `sequence_runs(id, lead_id, sequence_id, state, started_at, stopped_at, stop_reason)`; `state` is `active|stopped|completed`; unique `(lead_id, sequence_id)`.
- `touches(id, run_id, lead_id, step_index, channel, due_at, status, body, attempts, last_error, sent_at, approved_by)`; `status` is `pending|queued_for_approval|sent|skipped|failed|cancelled`; unique `(run_id, step_index)`.
- Settings: `followup_autosend` (bool, default false), `max_touches_per_week` (default 3).
- Step: `{delay_hours, channel, template_en, template_ar}`; placeholders `{name} {area} {budget} {agent}`.

## Behaviour
1. **Enrolment.** After `Pipeline.ingest` stores a new lead, or a status changes, enabled sequences whose trigger matches enrol the lead and create all touches with `due_at`.
2. **Worker `followups.tick(now)`.** Selects due `pending` touches. For each: reload lead; run the compliance gate; if denied with a retry time, reschedule to it; if denied permanently, mark `skipped` with the reason. If allowed and `followup_autosend` is off, mark `queued_for_approval`. If on, send through the channel sender and mark `sent`. Send errors retry with backoff (3 attempts) then `failed` and an agent task is raised.
3. **Approval.** `POST /followups/{id}/approve` (optionally with edited body) re-runs the gate and sends; `POST /followups/{id}/skip`. Agents see only touches for their own leads.
4. **Stop rules.** Inbound reply, opt-out, booked viewing, closed deal (won/lost) or manual pause set the run to `stopped` and cancel pending touches.
5. **Frequency cap.** No more than `max_touches_per_week` sent touches per lead in any rolling 7 days; excess touches are rescheduled.
6. **Off-plan alert.** When projects are imported, consented leads whose area and budget match (existing inventory matching) get one alert touch, at most one per lead per week.
7. **Metrics.** Median time to first response, share within SLA, and replies per step are added to the ROI/Overview figures. Ratios are `null` when there is nothing to divide by.

## Interfaces
- API: `GET/PUT /followups/sequences`, `GET /followups/queue`, `POST /followups/{id}/approve|skip`, `POST /followups/runs/{lead_id}/pause`, `POST /maintenance/followups` (single mode), `GET /followups/stats`.
- Roles: owner/manager manage sequences and settings; agents approve touches for their own leads.
- Dashboard: Follow-up page (queue, sequences editor, on/off switch) EN/AR.
- Platform: `maintain` calls the worker for every workspace.

## Review focus (not covered by obvious tests)
- A lead opts out between scheduling and sending: nothing is sent.
- Worker crashes mid-tick or runs concurrently: no double send.
- Touch due at 03:00 UAE time: deferred to the contact window, not dropped.
- Arabic lead with an English-only template: falls back to the English text, never a blank message.
- Template placeholder with missing data (no name, no budget): rendered without literal braces.
- Auto-send switched off after touches were queued: nothing is sent without approval.

## Testing
Unit tests for enrolment, gate, caps, stop rules and templates; API tests with RBAC; a platform test that `maintain` runs the worker per workspace; one browser test of the approval queue.
