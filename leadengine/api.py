from __future__ import annotations

import hmac
import json
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .calculators import deal_costs, max_loan, mortgage_payment, rental_yield
from .capture import MAX_BODY, RateLimiter, handle_capture, origin_allowed, widget_js
from .config import Settings
from .connectors import ConnectorRegistry
from .connectors.apify import ApifyConnector
from .connectors.forms import FormWebhookConnector, MetaLeadAdsConnector, parse_csv, parse_portal_email, verify_hmac
from .connectors.reddit import RedditConnector
from .connectors.whatsapp import handle_inbound, make_sender, parse_inbound
from .db import DB
from .distribution import canon_areas
from .models import Signal, utcnow
from .inventory import import_projects_csv, match as match_projects
from .markets.cities import search as search_cities, stats as city_stats
from .nba import next_actions, todays_tasks
from .outreach import draft
from .pipeline import Pipeline
from .radar import demand_radar
from .viewings import ViewingConflict, feed_token, list_viewings, schedule as schedule_viewing, to_ics
from .roi import record_spend, roi_report

DASH = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


class ConsentIn(BaseModel):
    evidence_text: str


class OptOutIn(BaseModel):
    phone: str | None = None
    email: str | None = None
    handle: str | None = None
    platform: str | None = None


class StatusIn(BaseModel):
    status: str
    assigned_to: str | None = None
    note: str | None = None


class AgentIn(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None
    areas: list[str] = []
    languages: list[str] = ["en"]
    weight: int = Field(1, ge=1, le=100)
    max_open: int = Field(25, ge=0, le=10000)


class AgentPatch(BaseModel):
    active: bool | None = None
    weight: int | None = Field(None, ge=1, le=100)
    max_open: int | None = Field(None, ge=0, le=10000)
    areas: list[str] | None = None
    languages: list[str] | None = None


class DealCostsIn(BaseModel):
    price: float
    loan: float = 0.0


class MortgageIn(BaseModel):
    principal: float
    annual_rate_pct: float
    years: int


class YieldIn(BaseModel):
    price: float
    annual_rent: float
    service_charge: float = 0.0
    vacancy_pct: float = 5.0


class ViewingIn(BaseModel):
    start: datetime
    duration_min: int = 30
    location: str = ""
    agent_id: int | None = None


class ActivityIn(BaseModel):
    kind: str
    channel: str | None = None
    note: str | None = None


class SpendIn(BaseModel):
    source: str
    campaign: str | None = None
    period: str
    amount_aed: float


class PortalEmailIn(BaseModel):
    portal: str
    subject: str = ""
    body: str
    message_id: str


def create_app(settings: Settings | None = None, db: DB | None = None, whatsapp_sender=None) -> FastAPI:
    s = settings or Settings()
    db = db or DB(s.db_path)
    pipe = Pipeline(db, s)
    limiter = RateLimiter(5, 10 * 60)
    wa_sender = whatsapp_sender or make_sender(s)
    reg = ConnectorRegistry()
    meta, forms = MetaLeadAdsConnector(s), FormWebhookConnector(s)
    for c in (RedditConnector(s), ApifyConnector(s), meta, forms):
        reg.register(c)

    app = FastAPI(title="DigitalBurj Leads", version="1.0.0")
    app.state.pipeline, app.state.db, app.state.settings = pipe, db, s
    app.mount("/static", StaticFiles(directory=DASH.parent), name="static")

    def auth(x_api_key: str = Header(default="")) -> str:
        if s.api_key and x_api_key != s.api_key:
            raise HTTPException(401, "invalid API key")
        return "api"

    def lead_dict(r, full: bool = False) -> dict:
        d = {k: r[k] for k in r.keys()}
        for k in ("platforms", "breakdown", "profile"):
            d[k] = json.loads(d[k] or "null")
        d["consent_evidence"] = json.loads(d["consent_evidence"]) if d.get("consent_evidence") else None
        if full:
            d["signals"] = [{k: x[k] for k in ("platform", "source_type", "text", "url", "author_handle", "created_at", "score", "label")}
                            for x in db.q("SELECT * FROM signals WHERE lead_id=? ORDER BY created_at DESC", (r["id"],))]
            d["activities"] = [dict(x) for x in db.q("SELECT ts,kind,channel,note FROM activities WHERE lead_id=? ORDER BY ts DESC", (r["id"],))]
        return d

    # ---------------------------------------------------------------- dashboard / health
    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(DASH)

    @app.get("/health")
    def health():
        return {"ok": True, "leads": db.one("SELECT COUNT(*) n FROM leads")["n"]}

    # ---------------------------------------------------------------- ingestion
    @app.post("/ingest/signals")
    def ingest(signals: list[Signal], _=Depends(auth)):
        return pipe.ingest_many(signals)

    @app.post("/ingest/csv")
    async def ingest_csv(request: Request, _=Depends(auth)):
        return pipe.ingest_many(parse_csv((await request.body()).decode("utf-8-sig")))

    @app.post("/ingest/portal-email")
    def ingest_portal(p: PortalEmailIn, _=Depends(auth)):
        return pipe.ingest(parse_portal_email(p.portal, p.subject, p.body, p.message_id)).to_dict()

    @app.get("/connectors")
    def connectors(_=Depends(auth)):
        return reg.describe()

    @app.post("/connectors/{name}/run")
    def run_connector(name: str, params: dict | None = None, _=Depends(auth)):
        try:
            c = reg.get(name)
            sigs = list(c.fetch(**(params or {})))
        except KeyError as e:
            raise HTTPException(404, str(e))
        except (RuntimeError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"fetched": len(sigs), **pipe.ingest_many(sigs)}

    @app.post("/demo/seed")
    def seed(_=Depends(auth)):
        from .demo import seed_demo
        return seed_demo(pipe)

    # ---------------------------------------------------------------- webhooks (signed, no API key)
    @app.get("/webhooks/meta", response_class=PlainTextResponse)
    def meta_verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and s.meta_verify_token and q.get("hub.verify_token") == s.meta_verify_token:
            return q.get("hub.challenge", "")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/meta")
    async def meta_hook(request: Request, x_hub_signature_256: str = Header(default="")):
        body = await request.body()
        if not verify_hmac(s.meta_app_secret, body, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
        return pipe.ingest_many(meta.parse_webhook(json.loads(body)))

    @app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
    def wa_verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and s.meta_verify_token and q.get("hub.verify_token") == s.meta_verify_token:
            return q.get("hub.challenge", "")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/whatsapp")
    async def wa_hook(request: Request, x_hub_signature_256: str = Header(default="")):
        body = await request.body()
        if not verify_hmac(s.meta_app_secret, body, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
        return [handle_inbound(pipe, wa_sender, m) for m in parse_inbound(json.loads(body))]

    @app.post("/webhooks/form/{platform}")
    async def form_hook(platform: str, request: Request, x_signature: str = Header(default="")):
        body = await request.body()
        if not verify_hmac(s.form_webhook_secret, body, x_signature, prefix=""):
            raise HTTPException(401, "bad signature")
        return pipe.ingest(forms.parse(platform, json.loads(body))).to_dict()

    # ---------------------------------------------------------------- leads
    @app.get("/leads")
    def leads(tier: str | None = None, label: str | None = None, area: str | None = None, min_score: int = 0,
              status: str | None = None, overdue: bool = False, assigned_agent_id: int | None = None, limit: int = Query(100, le=500), _=Depends(auth)):
        where, args = ["score >= ?"], [min_score]
        if assigned_agent_id is not None:
            where.append("assigned_agent_id = ?")
            args.append(assigned_agent_id)
        for col, val in (("tier", tier), ("label", label), ("primary_area", area), ("status", status)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if overdue:
            where.append("status='new' AND sla_due_at IS NOT NULL AND sla_due_at < ?")
            args.append(utcnow().isoformat())
        rows = db.q(f"SELECT * FROM leads WHERE {' AND '.join(where)} ORDER BY score DESC, last_seen DESC LIMIT ?", (*args, limit))
        return [lead_dict(r) for r in rows]

    @app.get("/leads/{lead_id}")
    def lead(lead_id: int, _=Depends(auth)):
        r = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not r:
            raise HTTPException(404, "lead not found")
        return lead_dict(r, full=True)

    @app.post("/leads/{lead_id}/outreach")
    def outreach(lead_id: int, channel: str = "public_reply", _=Depends(auth)):
        r = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not r:
            raise HTTPException(404, "lead not found")
        d = lead_dict(r)
        out = draft(d, channel, pipe.compliance, s)
        db.audit("api", "outreach_drafted", lead_id, channel=channel, allowed=out["compliance"]["allowed"])
        return out

    @app.post("/leads/{lead_id}/status")
    def set_status(lead_id: int, p: StatusIn, _=Depends(auth)):
        if p.status not in ("new", "contacted", "qualified", "viewing", "won", "lost"):
            raise HTTPException(400, "invalid status")
        with db.tx() as c:
            n = c.execute("UPDATE leads SET status=?, assigned_to=COALESCE(?,assigned_to) WHERE id=?", (p.status, p.assigned_to, lead_id)).rowcount
            if n:
                c.execute("INSERT INTO activities(lead_id,ts,kind,note) VALUES(?,?,?,?)", (lead_id, utcnow().isoformat(), f"status:{p.status}", p.note))
        if not n:
            raise HTTPException(404, "lead not found")
        return {"ok": True}

    @app.post("/leads/{lead_id}/consent")
    def consent(lead_id: int, p: ConsentIn, _=Depends(auth)):
        if not db.one("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
            raise HTTPException(404, "lead not found")
        pipe.record_consent(lead_id, p.evidence_text)
        return {"ok": True}

    @app.delete("/leads/{lead_id}")
    def erase(lead_id: int, _=Depends(auth)):
        if not pipe.erase(lead_id):
            raise HTTPException(404, "lead not found")
        return {"erased": True}

    @app.post("/optout")
    def optout(p: OptOutIn):  # deliberately unauthenticated: anyone can opt out
        return {"leads_erased": pipe.opt_out(p.phone, p.email, p.handle, p.platform)}

    # ---------------------------------------------------------------- public capture + widget (no API key by design)
    def cors(origin: str | None, request: Request) -> dict:
        ok = origin and origin_allowed(origin, s.capture_origins, request.headers.get("host"))
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if ok else {"Vary": "Origin"}

    def client_ip(request: Request) -> str:
        """The socket peer, unless TRUSTED_PROXIES says N proxies append to X-Forwarded-For — then the hop the outermost trusted proxy saw.
        A client-supplied X-Forwarded-For prefix must never choose its own rate-limit bucket."""
        peer = request.client.host if request.client else "?"
        n = s.trusted_proxies
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        return hops[-n] if n > 0 and len(hops) >= n else peer

    @app.options("/capture", include_in_schema=False)
    def capture_preflight(request: Request):
        h = cors(request.headers.get("origin"), request)
        if "Access-Control-Allow-Origin" in h:
            h.update({"Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})
        return Response(status_code=204, headers=h)

    @app.post("/capture")
    async def capture(request: Request):
        origin = request.headers.get("origin")
        raw = await request.body()
        if len(raw) > MAX_BODY * 2:
            return JSONResponse({"error": "payload too large"}, 413, headers=cors(origin, request))
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, 400, headers=cors(origin, request))
        status, body = handle_capture(pipe, s, payload, origin, client_ip(request), limiter, host=request.headers.get("host"))
        return JSONResponse(body, status, headers=cors(origin, request) if status != 403 else {"Vary": "Origin"})

    @app.get("/widget.js", include_in_schema=False)
    def widget(request: Request):
        return Response(widget_js(s.public_url or str(request.base_url)), media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=300"})

    # ---------------------------------------------------------------- agents & routing
    def agent_dict(r, open_leads: int = 0) -> dict:
        d = dict(r)
        d["areas"], d["languages"], d["active"], d["open_leads"] = json.loads(d["areas"]), json.loads(d["languages"]), bool(d["active"]), open_leads
        d["feed_token"] = feed_token(s.api_key, d["id"]) if s.api_key else None     # per-agent, read-only calendar feed token
        return d

    @app.post("/agents")
    def add_agent(a: AgentIn, _=Depends(auth)):
        aid = pipe.distribution.add_agent(a.name, phone=a.phone, email=a.email, areas=tuple(a.areas), languages=tuple(a.languages),
                                          weight=a.weight, max_open=a.max_open)
        return agent_dict(db.one("SELECT * FROM agents WHERE id=?", (aid,)))

    @app.get("/agents")
    def list_agents(_=Depends(auth)):
        counts = pipe.distribution.open_counts()
        return [agent_dict(r, counts.get(r["id"], 0)) for r in db.q("SELECT * FROM agents ORDER BY id")]

    @app.patch("/agents/{agent_id}")
    def patch_agent(agent_id: int, p: AgentPatch, _=Depends(auth)):
        if not db.one("SELECT 1 FROM agents WHERE id=?", (agent_id,)):
            raise HTTPException(404, "agent not found")
        fields = {k: v for k, v in p.model_dump(exclude_none=True).items()}
        if "areas" in fields:
            fields["areas"] = canon_areas(fields["areas"])
        with db.tx() as c:
            for k, v in fields.items():
                c.execute(f"UPDATE agents SET {k}=? WHERE id=?",
                          (json.dumps(v) if isinstance(v, list) else int(v) if isinstance(v, bool) else v, agent_id))
        return agent_dict(db.one("SELECT * FROM agents WHERE id=?", (agent_id,)), pipe.distribution.open_counts().get(agent_id, 0))

    @app.post("/leads/{lead_id}/assign")
    def assign(lead_id: int, _=Depends(auth)):
        if not db.one("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
            raise HTTPException(404, "lead not found")
        return {"assigned_agent_id": pipe.distribution.assign(lead_id, actor="api")}

    @app.post("/maintenance/escalate")
    def escalate(_=Depends(auth)):
        return pipe.distribution.escalate_overdue()

    # ---------------------------------------------------------------- intelligence / ops
    @app.get("/radar")
    def radar(days: int = 14, _=Depends(auth)):
        return demand_radar(db, days=days)

    # ---------------------------------------------------------------- calculators
    def calc(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/tools/deal-costs")
    def tool_deal_costs(p: DealCostsIn, _=Depends(auth)):
        return calc(deal_costs, p.price, s.rates, loan=p.loan)

    @app.post("/tools/mortgage")
    def tool_mortgage(p: MortgageIn, _=Depends(auth)):
        return {"monthly_payment": calc(mortgage_payment, p.principal, p.annual_rate_pct, p.years)}

    @app.post("/tools/yield")
    def tool_yield(p: YieldIn, _=Depends(auth)):
        return calc(rental_yield, p.price, p.annual_rent, service_charge=p.service_charge, vacancy_pct=p.vacancy_pct)

    @app.get("/leads/{lead_id}/deal-sheet")
    def deal_sheet(lead_id: int, ltv_pct: float | None = None, rate_pct: float | None = None, years: int | None = None,
                   _=Depends(auth)):
        r = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not r:
            raise HTTPException(404, "lead not found")
        if r["label"] in ("renter", "landlord") or r["purchase_type"] == "rent":
            raise HTTPException(422, "deal sheet is for purchase leads")
        if not r["budget_max"]:
            raise HTTPException(422, "lead has no stated budget")
        rt = s.rates
        price = r["budget_max"]
        ltv, rate, yrs = ltv_pct if ltv_pct is not None else rt.default_ltv_pct, rate_pct if rate_pct is not None else rt.default_rate_pct, years or rt.default_years
        loan = calc(max_loan, price, ltv_pct=ltv)
        costs = calc(deal_costs, price, rt, loan=loan)
        down = round(price - loan, 2)
        return {"price": price, "ltv_pct": ltv, "loan": loan, "down_payment": down, "costs": costs,
                "cash_needed_total": round(down + costs["total_costs"], 2), "rate_pct": rate, "years": yrs,
                "monthly_payment": calc(mortgage_payment, loan, rate, yrs) if loan > 0 else 0.0,
                "disclaimer": "Indicative only. Fees, LTV limits and rates change - verify current rules with the DLD and lenders."}

    # ---------------------------------------------------------------- viewings
    @app.post("/leads/{lead_id}/viewings")
    def add_viewing(lead_id: int, p: ViewingIn, _=Depends(auth)):
        try:
            vid = schedule_viewing(db, lead_id, p.start, duration_min=p.duration_min, location=p.location, agent_id=p.agent_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ViewingConflict as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"id": vid}

    @app.get("/viewings")
    def viewings(agent_id: int | None = None, _=Depends(auth)):
        return list_viewings(db, agent_id)

    @app.get("/viewings.ics", include_in_schema=False)
    def viewings_ics(agent_id: int | None = None, token: str = "", x_api_key: str = Header(default="")):
        # Calendar apps can't send headers: they use ?agent_id=&token= (a per-agent read-only token). The master key is header-only.
        if s.api_key:
            master = hmac.compare_digest(x_api_key.encode(), s.api_key.encode())
            scoped = agent_id is not None and bool(token) and hmac.compare_digest(token.encode(), feed_token(s.api_key, agent_id).encode())
            if not (master or scoped):
                raise HTTPException(401, "invalid feed token")
        return Response(to_ics(list_viewings(db, agent_id)), media_type="text/calendar; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- next-best-action & activity log
    ACTIVITY_KINDS = ("note", "call", "message", "follow_up", "matches_sent")

    @app.get("/leads/{lead_id}/next-actions")
    def lead_next_actions(lead_id: int, _=Depends(auth)):
        r = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not r:
            raise HTTPException(404, "lead not found")
        return next_actions(db, pipe.compliance, lead_dict(r))

    @app.get("/tasks/today")
    def tasks_today(agent_id: int | None = None, _=Depends(auth)):
        return todays_tasks(db, pipe.compliance, agent_id)

    @app.post("/leads/{lead_id}/activities")
    def log_activity(lead_id: int, p: ActivityIn, _=Depends(auth)):
        if p.kind not in ACTIVITY_KINDS:
            raise HTTPException(400, f"kind must be one of {ACTIVITY_KINDS}")
        if not db.one("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
            raise HTTPException(404, "lead not found")
        with db.tx() as c:
            c.execute("INSERT INTO activities(lead_id,ts,kind,channel,note) VALUES(?,?,?,?,?)",
                      (lead_id, utcnow().isoformat(), p.kind, p.channel, (p.note or "")[:1000]))
        return {"ok": True}

    # ---------------------------------------------------------------- source-market catalogue
    @app.get("/markets/cities")
    def markets_cities(q: str | None = None, country: str | None = None, region: str | None = None, tier: int | None = Query(None, ge=1, le=3),
                       lang: str | None = None, limit: int = Query(100, ge=1, le=500), _=Depends(auth)):
        return [c.to_dict() for c in search_cities(q, country, region, tier, lang, limit)]

    @app.get("/markets/summary")
    def markets_summary(_=Depends(auth)):
        return city_stats()

    @app.post("/inventory")
    async def inventory(request: Request, _=Depends(auth)):
        return import_projects_csv(db, (await request.body()).decode("utf-8-sig"))

    @app.get("/inventory")
    def list_inventory(_=Depends(auth)):
        out = []
        for r in db.q("SELECT * FROM projects ORDER BY id"):
            d = dict(r)
            d["bedrooms"] = json.loads(d["bedrooms"] or "[]")
            out.append(d)
        return out

    @app.get("/leads/{lead_id}/matches")
    def matches(lead_id: int, _=Depends(auth)):
        r = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not r:
            raise HTTPException(404, "lead not found")
        return match_projects(db, lead_dict(r), s.golden_visa_min_aed)

    @app.post("/spend")
    def spend(p: SpendIn, _=Depends(auth)):
        try:
            return {"id": record_spend(db, p.source, p.campaign, p.period, p.amount_aed)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/roi")
    def roi(period: str | None = None, _=Depends(auth)):
        return roi_report(db, period=period)

    @app.get("/stats")
    def stats(_=Depends(auth)):
        tiers = {r["tier"]: r["n"] for r in db.q("SELECT tier, COUNT(*) n FROM leads GROUP BY tier")}
        return {"tiers": tiers, "counters": {r["key"]: r["value"] for r in db.q("SELECT * FROM stats")}}

    @app.post("/maintenance/purge")
    def purge(_=Depends(auth)):
        return pipe.compliance.purge_expired()

    @app.post("/maintenance/rescore")
    def rescore(_=Depends(auth)):
        return {"rescored": pipe.rescore_all()}

    @app.get("/audit")
    def audit(limit: int = 100, _=Depends(auth)):
        return [dict(r) for r in db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    return app


app = None  # `uvicorn leadengine.api:create_app --factory`
