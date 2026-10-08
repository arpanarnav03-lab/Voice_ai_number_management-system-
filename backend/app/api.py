from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import auth, services, sync
from .bolna_client import AgentNotFoundOnBolna, BolnaError
from .errors import AppError
from .models import POOL_STATUSES, Agent, Assignment, Call, Number
from .validation import mask_phone

log = logging.getLogger("bnm.api")

router = APIRouter(prefix="/api")
protected = APIRouter(dependencies=[Depends(auth.require_auth)])


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


# ------------------------------------------------------------------ serializers
def _active_counts(s, agent_ids=None) -> dict[int, int]:
    q = select(Call.agent_id, func.count()).where(Call.settled_at.is_(None)).group_by(Call.agent_id)
    return {a: c for a, c in s.execute(q)}


def _last_used(s) -> dict[int, object]:
    q = select(Call.agent_id, func.max(Call.placed_at)).group_by(Call.agent_id)
    return {a: t for a, t in s.execute(q)}


def agent_rows(s, agents: list[Agent], threshold: int) -> list[dict]:
    active, last = _active_counts(s), _last_used(s)
    rows = []
    for a in agents:
        n = a.current_number
        ac = active.get(a.id, 0)
        if ac > 0:
            status = "on_call"
        elif n is not None and n.missing_on_bolna:
            status = "number_missing"
        elif a.pool_empty:
            status = "pool_empty"
        elif n is None:
            status = "no_number"
        elif n.connected_count >= threshold - 1:
            status = "near_limit"
        else:
            status = "ready"
        rows.append({
            "id": a.id, "bolna_agent_id": a.bolna_agent_id, "name": a.name,
            "number": None if n is None else {
                "id": n.id, "phone_number": n.phone_number, "status": n.status,
                "connected_count": n.connected_count, "provider": n.provider,
                "missing_on_bolna": n.missing_on_bolna},
            "status": status, "on_call": ac > 0, "active_calls": ac,
            "pending_switch": a.pending_switch, "last_used": iso(last.get(a.id)),
        })
    return rows


def number_rows(s, numbers: list[Number], threshold: int) -> list[dict]:
    active = _active_counts(s)
    holders = {a.current_number_id: a for a in s.scalars(select(Agent).where(Agent.current_number_id.is_not(None)))}
    served = {n: c for n, c in s.execute(
        select(Assignment.number_id, func.count(func.distinct(Assignment.agent_id))).group_by(Assignment.number_id))}
    rows = []
    for n in numbers:
        h = holders.get(n.id)
        rows.append({
            "id": n.id, "phone_number": n.phone_number, "provider": n.provider, "status": n.status,
            "connected_count": n.connected_count, "total_calls": n.total_calls,
            "agents_served": served.get(n.id, 0), "threshold": threshold,
            "linked": n.bolna_linked_agent_id is not None, "added_at": iso(n.added_at),
            "linked_in_bolna": n.linked_in_bolna, "bolna_linked_agent_id": n.bolna_linked_agent_id,
            "allow_auto_switch": n.allow_auto_switch, "missing_on_bolna": n.missing_on_bolna,
            "missing_since": iso(n.missing_since), "discovered_at": iso(n.discovered_at),
            "used_by": None if h is None else {
                "id": h.id, "name": h.name, "on_call": active.get(h.id, 0) > 0,
                "pending_switch": h.pending_switch},
        })
    return rows


def call_rows(s, calls: list[Call]) -> list[dict]:
    names = {a.id: a.name for a in s.scalars(select(Agent))}
    phones = {n.id: n.phone_number for n in s.scalars(select(Number))}
    return [{
        "id": c.id, "agent_id": c.agent_id, "agent_name": names.get(c.agent_id, "?"),
        "number_id": c.number_id, "phone_number": phones.get(c.number_id, "?"),
        "recipient": mask_phone(c.recipient), "placed_at": iso(c.placed_at),
        "status": c.status, "duration": c.duration, "result": services.result_of(c),
        "reason": c.reason, "needs_review": c.needs_review,
    } for c in calls]


def _agent_or_404(s, agent_id: int) -> Agent:
    a = s.get(Agent, agent_id)
    if a is None:
        raise AppError("AGENT_NOT_FOUND", "Agent not found", 404)
    return a


# ------------------------------------------------------------------ auth (open routes)
class LoginBody(BaseModel):
    password: str


@router.get("/auth/status")
def auth_status(request: Request):
    return {"required": auth.auth_required(request), "authenticated": auth.is_authenticated(request),
            "mode": request.app.state.settings.bolna_mode}


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not auth.auth_required(request):
        return {"ok": True}
    if not auth.check_password(request, body.password):
        await asyncio.sleep(0.6)  # slow down guessing
        raise AppError("UNAUTHORIZED", "Wrong password", 401)
    response.set_cookie(auth.COOKIE, auth.make_token(request.app.state.settings.app_password),
                        httponly=True, samesite="lax", max_age=auth.TTL_SECONDS)
    return {"ok": True}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


# ------------------------------------------------------------------ settings
class ThresholdBody(BaseModel):
    threshold: int


@protected.get("/settings")
def get_settings(request: Request):
    with request.app.state.db.session() as s:
        return {"threshold": services.get_threshold(s, request.app.state.settings.default_threshold)}


@protected.put("/settings/threshold")
def put_threshold(body: ThresholdBody, request: Request):
    with request.app.state.db.session() as s:
        return {"threshold": services.set_threshold(s, body.threshold)}


@protected.get("/account")
async def account(request: Request):
    """Mode and Bolna's concurrency (used for a soft warning before triggering)."""
    st = request.app.state
    out = {"mode": st.settings.bolna_mode, "concurrency": None}
    try:
        me = await st.bolna.get_me()
        c = me.get("concurrency")
        if isinstance(c, dict):
            out["concurrency"] = {"max": c.get("max"), "current": c.get("current")}
    except Exception as e:  # never fail the page because of this
        log.warning("account lookup failed: %s", type(e).__name__)
    return out


# ------------------------------------------------------------------ numbers
class NumberBody(BaseModel):
    phone_number: str


@protected.get("/numbers")
def list_numbers(request: Request, status: str | None = None, q: str = "", page: int = 1,
                 page_size: int = 50):
    """Without `status`: the whole pool (discovered / ignored numbers are not in it).
    With `status`: that status only, paginated and searchable."""
    with request.app.state.db.session() as s:
        th = services.get_threshold(s)
        if status is None:
            nums = s.scalars(select(Number).where(Number.status.in_(POOL_STATUSES))
                             .order_by(Number.added_at, Number.id)).all()
            return {"threshold": th, "items": number_rows(s, nums, th)}
        if status not in (*POOL_STATUSES, "discovered", "ignored"):
            raise AppError("VALIDATION_ERROR", "Invalid input: status", 422)
        page_size = min(max(page_size, 1), 200)
        page = max(page, 1)
        flt = [Number.status == status]
        if q.strip():
            flt.append(Number.phone_number.contains(q.strip(), autoescape=True))
        total = s.scalar(select(func.count()).select_from(Number).where(*flt)) or 0
        order = (Number.discovered_at, Number.id) if status in ("discovered", "ignored") \
            else (Number.added_at, Number.id)
        nums = s.scalars(select(Number).where(*flt).order_by(*order)
                         .limit(page_size).offset((page - 1) * page_size)).all()
        out = {"threshold": th, "items": number_rows(s, nums, th), "total": total,
               "page": page, "page_size": page_size}
        if status == "discovered":
            out["unlinked_total"] = s.scalar(select(func.count()).select_from(Number).where(
                Number.status == "discovered", Number.linked_in_bolna.is_(False))) or 0
        return out


@protected.post("/numbers", status_code=201)
async def add_number(body: NumberBody, request: Request):
    st = request.app.state
    from .validation import validate_phone
    phone = validate_phone(body.phone_number)  # reject bad input before calling Bolna
    try:
        bolna_numbers = await st.bolna.list_phone_numbers()
    except BolnaError as e:
        raise AppError("BOLNA_ERROR", f"Bolna: {e.message}", 502)

    def work():
        with st.db.session() as s:
            num, warning = services.add_number(s, phone, bolna_numbers)
            th = services.get_threshold(s)
            return {"number": number_rows(s, [num], th)[0], "warning": warning}
    return await run_in_threadpool(work)


@protected.get("/numbers/{number_id}")
def number_detail(number_id: int, request: Request):
    with request.app.state.db.session() as s:
        th = services.get_threshold(s)
        n = s.get(Number, number_id)
        if n is None:
            raise AppError("NUMBER_NOT_FOUND", "Number not found", 404)
        assigns = s.scalars(select(Assignment).where(Assignment.number_id == n.id)
                            .order_by(Assignment.started_at.desc(), Assignment.id.desc())).all()
        agents = []
        for a in assigns:
            q = select(func.count()).select_from(Call).where(
                Call.agent_id == a.agent_id, Call.number_id == n.id, Call.placed_at >= a.started_at)
            if a.ended_at:
                q = q.where(Call.placed_at <= a.ended_at)
            agents.append({
                "agent_id": a.agent_id, "agent_name": a.agent.name, "started_at": iso(a.started_at),
                "ended_at": iso(a.ended_at), "calls": s.scalar(q) or 0,
                "connected_calls": a.connected_calls, "reason": a.reason,
            })
        calls = s.scalars(select(Call).where(Call.number_id == n.id)
                          .order_by(Call.placed_at.desc()).limit(200)).all()
        return {"number": number_rows(s, [n], th)[0], "agents": agents, "calls": call_rows(s, calls)}


class ApproveBody(BaseModel):
    confirm_linked: bool = False


class ApproveManyBody(BaseModel):
    ids: list[int] | None = None
    all_unlinked: bool = False
    confirm_linked: bool = False


class IdsBody(BaseModel):
    ids: list[int] = Field(max_length=1000)


class AutoSwitchBody(BaseModel):
    allow: bool
    confirm: bool = False


@protected.post("/numbers/approve")
def approve_numbers(body: ApproveManyBody, request: Request):
    if not body.all_unlinked and not body.ids:
        raise AppError("VALIDATION_ERROR", "Invalid input: ids", 422)
    with request.app.state.db.session() as s:
        return services.approve_many(s, body.ids, body.all_unlinked, body.confirm_linked)


@protected.post("/numbers/ignore")
def ignore_numbers(body: IdsBody, request: Request):
    with request.app.state.db.session() as s:
        return {"ignored": services.ignore_many(s, body.ids)}


@protected.post("/numbers/{number_id}/approve")
def approve_number(number_id: int, request: Request, body: ApproveBody | None = None):
    with request.app.state.db.session() as s:
        n = services.approve_number(s, number_id, bool(body and body.confirm_linked))
        return number_rows(s, [n], services.get_threshold(s))[0]


@protected.post("/numbers/{number_id}/ignore")
def ignore_number(number_id: int, request: Request):
    with request.app.state.db.session() as s:
        n = services.ignore_number(s, number_id)
        return number_rows(s, [n], services.get_threshold(s))[0]


@protected.post("/numbers/{number_id}/restore")
def restore_number(number_id: int, request: Request):
    with request.app.state.db.session() as s:
        n = services.restore_number(s, number_id)
        return number_rows(s, [n], services.get_threshold(s))[0]


@protected.put("/numbers/{number_id}/auto-switch")
def put_auto_switch(number_id: int, body: AutoSwitchBody, request: Request):
    with request.app.state.db.session() as s:
        n = services.set_auto_switch(s, number_id, body.allow, body.confirm)
        return number_rows(s, [n], services.get_threshold(s))[0]


@protected.post("/numbers/{number_id}/reset")
def reset_number(number_id: int, request: Request):
    with request.app.state.db.session() as s:
        n = services.reset_number(s, number_id)
        th = services.get_threshold(s)
        return number_rows(s, [n], th)[0]


# ------------------------------------------------------------------ sync
@protected.post("/sync/run")
async def sync_run(request: Request):
    return await sync.run_sync(request.app.state)


@protected.get("/sync/status")
def sync_status(request: Request):
    return sync.status(request.app.state.db)


# ------------------------------------------------------------------ agents
class AssignBody(BaseModel):
    bolna_agent_id: str
    number_id: int


class ChangeBody(BaseModel):
    number_id: int


@protected.get("/agents")
def list_agents(request: Request):
    with request.app.state.db.session() as s:
        th = services.get_threshold(s)
        agents = s.scalars(select(Agent).order_by(Agent.created_at, Agent.id)).all()
        return {"threshold": th, "items": agent_rows(s, agents, th)}


@protected.post("/agents", status_code=201)
async def assign_agent(body: AssignBody, request: Request):
    st = request.app.state
    agent_id = body.bolna_agent_id.strip()
    if not agent_id:
        raise AppError("AGENT_NOT_FOUND", "Enter a Bolna agent ID", 422)
    name = None
    try:
        info = await st.bolna.get_agent(agent_id)
        name = (info or {}).get("name")
    except AgentNotFoundOnBolna:
        raise AppError("AGENT_NOT_FOUND", "Bolna does not know this agent ID. Check it and try again.", 404)
    except BolnaError as e:
        raise AppError("BOLNA_ERROR", f"Bolna: {e.message}", 502)

    def work():
        with st.db.session() as s:
            a = services.assign_new_agent(s, agent_id, body.number_id, name)
            th = services.get_threshold(s)
            return agent_rows(s, [a], th)[0]
    return await run_in_threadpool(work)


@protected.get("/agents/{agent_id}")
def agent_detail(agent_id: int, request: Request):
    with request.app.state.db.session() as s:
        th = services.get_threshold(s)
        a = _agent_or_404(s, agent_id)
        history = s.scalars(select(Assignment).where(Assignment.agent_id == a.id)
                            .order_by(Assignment.started_at.desc(), Assignment.id.desc())).all()
        calls = s.scalars(select(Call).where(Call.agent_id == a.id)
                          .order_by(Call.placed_at.desc()).limit(200)).all()
        placed = s.scalar(select(func.count()).select_from(Call).where(Call.agent_id == a.id)) or 0
        connected = s.scalar(select(func.count()).select_from(Call).where(
            Call.agent_id == a.id, Call.connected.is_(True))) or 0
        return {
            "agent": agent_rows(s, [a], th)[0], "threshold": th,
            "summary": {"calls_placed": placed, "connected_calls": connected,
                        "number_switches": sum(1 for h in history if h.reason != "initial")},
            "number_history": [{
                "id": h.id, "number_id": h.number_id, "phone_number": h.number.phone_number,
                "started_at": iso(h.started_at), "ended_at": iso(h.ended_at),
                "connected_calls": h.connected_calls, "reason": h.reason, "end_reason": h.end_reason,
            } for h in history],
            "calls": call_rows(s, calls),
        }


@protected.post("/agents/{agent_id}/change-number")
def change_number(agent_id: int, body: ChangeBody, request: Request):
    with request.app.state.db.session() as s:
        a = services.change_number(s, agent_id, body.number_id)
        th = services.get_threshold(s)
        return agent_rows(s, [a], th)[0]


# ------------------------------------------------------------------ calls
class TriggerItem(BaseModel):
    agent_id: int
    recipient: str


class TriggerBody(BaseModel):
    items: list[TriggerItem] = Field(min_length=1, max_length=50)


@protected.get("/calls")
def list_calls(request: Request, agent_id: int | None = None, number_id: int | None = None, limit: int = 200):
    with request.app.state.db.session() as s:
        q = select(Call).order_by(Call.placed_at.desc()).limit(min(max(limit, 1), 500))
        if agent_id is not None:
            q = q.where(Call.agent_id == agent_id)
        if number_id is not None:
            q = q.where(Call.number_id == number_id)
        return {"items": call_rows(s, s.scalars(q).all())}


async def _trigger_one(st, item: TriggerItem) -> dict:
    def fail(e: AppError | str, code: str = "BOLNA_ERROR") -> dict:
        if isinstance(e, AppError):
            return {"agent_id": item.agent_id, "ok": False, "code": e.code, "message": e.message}
        return {"agent_id": item.agent_id, "ok": False, "code": code, "message": e}

    def begin():
        with st.db.session() as s:
            return services.begin_call(s, item.agent_id, item.recipient)

    try:
        plan = await run_in_threadpool(begin)
    except AppError as e:
        return fail(e)

    try:
        resp = await st.bolna.place_call(plan.bolna_agent_id, plan.recipient, plan.from_number)
        execution_id = resp.get("execution_id") or resp.get("run_id")
        if not execution_id:
            raise BolnaError("Bolna accepted the call but returned no execution id")
    except BolnaError as e:
        await run_in_threadpool(_abort, st, plan.call_id)
        return fail(f"Bolna refused the call: {e.message}")
    except Exception as e:  # pragma: no cover - defensive
        await run_in_threadpool(_abort, st, plan.call_id)
        log.exception("unexpected error placing call")
        return fail(f"Unexpected error placing the call ({type(e).__name__})")

    def attach():
        with st.db.session() as s:
            services.attach_execution(s, plan.call_id, str(execution_id), str(resp.get("status") or "queued"))
    await run_in_threadpool(attach)
    return {"agent_id": item.agent_id, "ok": True, "execution_id": str(execution_id)}


def _abort(st, call_id: int):
    with st.db.session() as s:
        services.abort_call(s, call_id)


@protected.post("/calls/trigger")
async def trigger(body: TriggerBody, request: Request):
    st = request.app.state
    results = await asyncio.gather(*[_trigger_one(st, it) for it in body.items])
    return {"results": list(results)}


router.include_router(protected)
