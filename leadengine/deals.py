"""Deals: pipeline from viewing to payment, commission maths, summary and agent leaderboard.

A deal reaching `offer_accepted` or later wins its lead and ends their follow-up sequences; a lost deal closes the lead only if no other
deal for it is still open. Commission rates are configuration with indicative defaults (verify current rules)."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone

from .compliance import GST
from .db import DB
from .models import utcnow

STAGES = ("viewing_done", "offer_made", "offer_accepted", "contract_signed", "paid", "lost")
OPEN = STAGES[:4]                                        # still in the pipeline (paid is closed-won, lost is closed-lost)
WON = ("offer_accepted", "contract_signed", "paid")
KINDS = ("sale", "off_plan", "rental")
MAX_VALUE = 1e10
DEFAULTS = {"commission_sale_pct": 2.0, "commission_rental_pct": 5.0, "vat_pct": 5.0}
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class DealError(ValueError):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


def _num(v, name: str, lo: float, hi: float, lo_open: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DealError(f"{name} must be a number")
    try:
        ok = math.isfinite(v) and abs(v) <= 1e15
    except OverflowError:
        ok = False
    if not ok:
        raise DealError(f"{name} must be a reasonable number")
    if (v <= lo if lo_open else v < lo) or v > hi:
        raise DealError(f"{name} must be {'above' if lo_open else 'between'} {lo}{'' if lo_open else ' and ' + str(hi)}")
    return float(v)


def _text(v, name: str, cap: int) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or len(v) > cap:
        raise DealError(f"{name} must be text of at most {cap} characters")
    return v.strip() or None


def _valid_date(v) -> bool:
    if not (isinstance(v, str) and _DATE.fullmatch(v)):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _month(ts: str) -> str:
    """Calendar month in Dubai time (UTC+4) of a stored UTC timestamp."""
    d = datetime.fromisoformat(ts)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(GST).strftime("%Y-%m")


def _money(x: float) -> float:
    return round(x + 0.0, 2)


class Deals:
    def __init__(self, db: DB, followups=None):
        self.db, self.fu = db, followups

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        out = dict(DEFAULTS)
        for k in out:
            r = self.db.one("SELECT value FROM kv WHERE key=?", (f"deals_{k}",))
            if r:
                out[k] = float(r["value"])
        return out

    def put_settings(self, d) -> dict:
        if not isinstance(d, dict) or not d or set(d) - set(DEFAULTS):
            raise DealError("send one or more of " + ", ".join(DEFAULTS))
        clean = {k: _num(v, k, 0, 100) for k, v in d.items()}
        with self.db.tx() as c:
            for k, v in clean.items():
                c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (f"deals_{k}", str(v)))
        return self.settings()

    # ------------------------------------------------------------------ helpers
    def _calc(self, value: float, pct: float, split: float) -> tuple[float, float]:
        commission = _money(value * pct / 100 * (1 - split / 100))
        return commission, _money(commission * self.settings()["vat_pct"] / 100)

    def _row(self, r) -> dict:
        return dict(r)

    # ------------------------------------------------------------------ create / update
    def create(self, lead_id: int, agent_id: int | None, kind: str, value_aed, *, stage: str = "viewing_done", title=None, area=None,
               commission_pct=None, split_pct=0, expected_close=None, notes=None, actor: str = "api") -> dict:
        if kind not in KINDS:
            raise DealError(f"kind must be one of {KINDS}")
        if stage not in OPEN:
            raise DealError(f"a new deal starts at one of {OPEN}")
        value = _num(value_aed, "value_aed", 0, MAX_VALUE, lo_open=True)
        split = _num(split_pct, "split_pct", 0, 100)
        cfg = self.settings()
        pct = _num(commission_pct, "commission_pct", 0, 100) if commission_pct is not None else (cfg["commission_rental_pct"] if kind == "rental" else cfg["commission_sale_pct"])
        if expected_close is not None and not _valid_date(expected_close):
            raise DealError("expected_close must be a real date, YYYY-MM-DD")
        if agent_id is not None and (isinstance(agent_id, bool) or not isinstance(agent_id, int) or not 0 <= agent_id < 2 ** 31):
            raise DealError("agent_id must be a whole number")
        if isinstance(lead_id, bool) or not isinstance(lead_id, int) or not 0 < lead_id < 2 ** 63:
            raise DealError("lead not found", 404)
        if not self.db.one("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
            raise DealError("lead not found", 404)
        commission, vat = self._calc(value, pct, split)
        now = utcnow().isoformat()
        with self.db.tx() as c:
            did = c.execute("INSERT INTO deals(lead_id,agent_id,kind,title,area,value_aed,commission_pct,commission_aed,vat_aed,split_pct,expected_close,stage,notes,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lead_id, agent_id, kind, _text(title, "title", 200), _text(area, "area", 80), value, pct, commission, vat, split, expected_close, stage,
                             _text(notes, "notes", 2000), now, now)).lastrowid
            c.execute("INSERT INTO deal_events(deal_id,ts,from_stage,to_stage,actor) VALUES(?,?,?,?,?)", (did, now, None, stage, actor))
        self.db.audit(actor, "deal_created", lead_id, deal=did, value=value)
        self._sync_lead(lead_id)
        return self.get(did)

    def update(self, deal_id: int, fields: dict, actor: str) -> dict:
        cur = self.db.one("SELECT * FROM deals WHERE id=?", (deal_id,))
        if not cur:
            raise DealError("deal not found", 404)
        if not isinstance(fields, dict) or set(fields) - {"title", "area", "value_aed", "commission_pct", "split_pct", "expected_close", "notes", "kind", "agent_id"}:
            raise DealError("unknown or read-only field")
        new = dict(cur)
        if "kind" in fields:
            if fields["kind"] not in KINDS:
                raise DealError(f"kind must be one of {KINDS}")
            new["kind"] = fields["kind"]
            cfg = self.settings()
            default_of = lambda k: cfg["commission_rental_pct"] if k == "rental" else cfg["commission_sale_pct"]          # noqa: E731
            if "commission_pct" not in fields and cur["commission_pct"] == default_of(cur["kind"]):
                new["commission_pct"] = default_of(fields["kind"])                  # an unedited default follows the new kind
        if "value_aed" in fields:
            new["value_aed"] = _num(fields["value_aed"], "value_aed", 0, MAX_VALUE, lo_open=True)
        if "commission_pct" in fields:
            new["commission_pct"] = _num(fields["commission_pct"], "commission_pct", 0, 100)
        if "split_pct" in fields:
            new["split_pct"] = _num(fields["split_pct"], "split_pct", 0, 100)
        if "agent_id" in fields:
            a = fields["agent_id"]
            if a is not None and (isinstance(a, bool) or not isinstance(a, int)):
                raise DealError("agent_id must be a whole number")
            new["agent_id"] = a
        for k, cap in (("title", 200), ("area", 80), ("notes", 2000)):
            if k in fields:
                new[k] = _text(fields[k], k, cap)
        if "expected_close" in fields:
            ec = fields["expected_close"]
            if ec is not None and not _valid_date(ec):
                raise DealError("expected_close must be a real date, YYYY-MM-DD")
            new["expected_close"] = ec
        if any(new[k] != cur[k] for k in ("value_aed", "commission_pct", "split_pct")):
            new["commission_aed"], new["vat_aed"] = self._calc(new["value_aed"], new["commission_pct"], new["split_pct"])          # reprice only when money changed
        cols = ("title", "area", "value_aed", "commission_pct", "split_pct", "expected_close", "notes", "kind", "agent_id", "commission_aed", "vat_aed")
        with self.db.tx() as c:
            c.execute(f"UPDATE deals SET {','.join(k + '=?' for k in cols)}, updated_at=? WHERE id=?", (*[new[k] for k in cols], utcnow().isoformat(), deal_id))
        self.db.audit(actor, "deal_updated", cur["lead_id"], deal=deal_id, fields=sorted(fields))
        return self.get(deal_id)

    # ------------------------------------------------------------------ stages
    def move(self, deal_id: int, stage: str, actor: str, lost_reason: str | None = None) -> dict:
        if stage not in STAGES:
            raise DealError(f"stage must be one of {STAGES}")
        cur = self.db.one("SELECT * FROM deals WHERE id=?", (deal_id,))
        if not cur:
            raise DealError("deal not found", 404)
        reason = _text(lost_reason, "lost_reason", 300)
        if stage == "lost" and not reason:
            raise DealError("a lost deal needs a reason")
        now = utcnow().isoformat()
        if cur["stage"] == stage == "lost" and reason != cur["lost_reason"]:
            with self.db.tx() as c:
                c.execute("UPDATE deals SET lost_reason=?, updated_at=? WHERE id=?", (reason, now, deal_id))
        if cur["stage"] != stage:
            with self.db.tx() as c:
                c.execute("UPDATE deals SET stage=?, lost_reason=?, updated_at=? WHERE id=?", (stage, reason if stage == "lost" else None, now, deal_id))
                c.execute("INSERT INTO deal_events(deal_id,ts,from_stage,to_stage,actor) VALUES(?,?,?,?,?)", (deal_id, now, cur["stage"], stage, actor))
            self.db.audit(actor, "deal_stage", cur["lead_id"], deal=deal_id, to=stage)
            self._sync_lead(cur["lead_id"])
        return self.get(deal_id)

    def _sync_lead(self, lead_id: int | None) -> None:
        """The lead's status follows ALL of its deals: any deal at a won stage wins it, otherwise a live deal reopens it, otherwise it is lost."""
        if lead_id is None:
            return
        stages = [r["stage"] for r in self.db.q("SELECT stage FROM deals WHERE lead_id=?", (lead_id,))]
        lead = self.db.one("SELECT status FROM leads WHERE id=?", (lead_id,))
        if not stages or not lead:
            return
        if any(s in WON for s in stages):
            target = "won"
        elif any(s in OPEN for s in stages):
            target = "qualified" if lead["status"] in ("won", "lost") else lead["status"]
        else:
            target = "lost"
        now = utcnow().isoformat()
        if target != lead["status"]:
            with self.db.tx() as c:
                c.execute("UPDATE leads SET status=? WHERE id=?", (target, lead_id))
                c.execute("INSERT INTO activities(lead_id,ts,kind,note) VALUES(?,?,?,?)", (lead_id, now, f"status:{target}", "from deals"))
        if target in ("won", "lost") and self.fu:
            self.fu.stop(lead_id, "closed")

    # ------------------------------------------------------------------ reads
    def get(self, deal_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM deals WHERE id=?", (deal_id,))
        if not r:
            return None
        d = self._row(r)
        d["events"] = [dict(e) for e in self.db.q("SELECT ts, from_stage, to_stage, actor FROM deal_events WHERE deal_id=? ORDER BY id", (deal_id,))]
        return d

    def list(self, stage: str | None = None, agent_id: int | None = None, kind: str | None = None) -> list[dict]:
        where, args = [], []
        for col, val in (("stage", stage), ("agent_id", agent_id), ("kind", kind)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        sql = "SELECT * FROM deals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC, id DESC LIMIT 500"
        return [self._row(r) for r in self.db.q(sql, args)]

    def _paid_ts(self) -> dict[int, str]:
        return {r["deal_id"]: r["ts"] for r in self.db.q(
            "SELECT e.deal_id, MAX(e.ts) ts FROM deal_events e JOIN deals d ON d.id=e.deal_id WHERE e.to_stage='paid' AND d.stage='paid' GROUP BY e.deal_id")}

    def summary(self, agent_id: int | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        month = now.astimezone(GST).strftime("%Y-%m")
        rows = [r for r in self.db.q("SELECT * FROM deals") if agent_id is None or r["agent_id"] == agent_id]
        by_stage = {s: {"count": 0, "value_aed": 0.0, "commission_aed": 0.0} for s in STAGES}
        for r in rows:
            b = by_stage[r["stage"]]
            b["count"] += 1
            b["value_aed"] = _money(b["value_aed"] + r["value_aed"])
            b["commission_aed"] = _money(b["commission_aed"] + r["commission_aed"])
        paid = self._paid_ts()
        closed = [r for r in rows if r["id"] in paid and _month(paid[r["id"]]) == month]
        total_leads = self.db.one("SELECT COUNT(*) n FROM leads" if agent_id is None else "SELECT COUNT(*) n FROM leads WHERE assigned_agent_id=?",
                                  () if agent_id is None else (agent_id,))["n"]
        leads_with_deal = len({r["lead_id"] for r in rows if r["lead_id"] is not None})
        return {"by_stage": by_stage,
                "pipeline_value_aed": _money(sum(by_stage[s]["value_aed"] for s in OPEN)),
                "expected_commission_aed": _money(sum(by_stage[s]["commission_aed"] for s in OPEN)),
                "closed_this_month": {"count": len(closed), "value_aed": _money(sum(r["value_aed"] for r in closed)),
                                      "commission_aed": _money(sum(r["commission_aed"] for r in closed))},
                "lead_to_deal_pct": round(100 * leads_with_deal / total_leads) if total_leads else None}

    def leaderboard(self, period: str | None = None, agent_id: int | None = None) -> list[dict]:
        if period is not None and not re.fullmatch(r"\d{4}-\d{2}", period):
            raise DealError("period must be YYYY-MM")
        paid = self._paid_ts()
        names = {r["id"]: r["name"] for r in self.db.q("SELECT id, name FROM agents")}
        agents: dict = {}
        for r in self.db.q("SELECT * FROM deals"):
            if r["agent_id"] is None or (agent_id is not None and r["agent_id"] != agent_id):
                continue
            a = agents.setdefault(r["agent_id"], {"agent_id": r["agent_id"], "name": names.get(r["agent_id"]), "deals": 0, "closed_deals": 0,
                                                  "closed_value_aed": 0.0, "commission_aed": 0.0, "leads": set()})
            if r["stage"] in OPEN:
                a["deals"] += 1
            if r["lead_id"] is not None:
                a["leads"].add(r["lead_id"])
            ts = paid.get(r["id"])
            if ts and (period is None or _month(ts) == period):
                a["closed_deals"] += 1
                a["closed_value_aed"] = _money(a["closed_value_aed"] + r["value_aed"])
                a["commission_aed"] = _money(a["commission_aed"] + r["commission_aed"])
        out = []
        for a in agents.values():
            if period is not None and a["closed_deals"] == 0:
                continue
            assigned = self.db.one("SELECT COUNT(*) n FROM leads WHERE assigned_agent_id=?", (a["agent_id"],))["n"]
            a["lead_to_deal_pct"] = round(100 * len(a.pop("leads")) / assigned) if assigned else None
            out.append(a)
        return sorted(out, key=lambda a: (-a["commission_aed"], -a["closed_value_aed"], a["agent_id"]))
