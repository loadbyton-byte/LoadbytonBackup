from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .connectors import ConnectorRegistry
from .connectors.apify import ApifyConnector
from .connectors.forms import FormWebhookConnector, MetaLeadAdsConnector, parse_csv, parse_portal_email, verify_hmac
from .connectors.reddit import RedditConnector
from .db import DB
from .models import Signal
from .outreach import draft
from .pipeline import Pipeline
from .radar import demand_radar
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
    weight: int = 1
    max_open: int = 25


class AgentPatch(BaseModel):
    active: bool | None = None
    weight: int | None = None
    max_open: int | None = None
    areas: list[str] | None = None
    languages: list[str] | None = None


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


def create_app(settings: Settings | None = None, db: DB | None = None) -> FastAPI:
    s = settings or Settings()
    db = db or DB(s.db_path)
    pipe = Pipeline(db, s)
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
        from .demo import demo_signals
        return pipe.ingest_many(demo_signals())

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

    @app.post("/webhooks/form/{platform}")
    async def form_hook(platform: str, request: Request, x_signature: str = Header(default="")):
        body = await request.body()
        if not verify_hmac(s.form_webhook_secret, body, x_signature, prefix=""):
            raise HTTPException(401, "bad signature")
        return pipe.ingest(forms.parse(platform, json.loads(body))).to_dict()

    # ---------------------------------------------------------------- leads
    @app.get("/leads")
    def leads(tier: str | None = None, label: str | None = None, area: str | None = None, min_score: int = 0,
              status: str | None = None, overdue: bool = False, limit: int = Query(100, le=500), _=Depends(auth)):
        where, args = ["score >= ?"], [min_score]
        for col, val in (("tier", tier), ("label", label), ("primary_area", area), ("status", status)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if overdue:
            from .models import utcnow
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
        from .models import utcnow
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

    # ---------------------------------------------------------------- agents & routing
    def agent_dict(r, open_leads: int = 0) -> dict:
        d = dict(r)
        d["areas"], d["languages"], d["active"], d["open_leads"] = json.loads(d["areas"]), json.loads(d["languages"]), bool(d["active"]), open_leads
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

    @app.post("/spend")
    def spend(p: SpendIn, _=Depends(auth)):
        try:
            return {"id": record_spend(db, p.source, p.campaign, p.period, p.amount_aed)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/roi")
    def roi(_=Depends(auth)):
        return roi_report(db)

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
