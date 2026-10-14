"""Spam Check: test calls to a reviewer phone, a person's verdict, and releasing flagged numbers.

A person decides what is spam. Nothing here detects spam or contacts any spam database.
Test calls are placed one at a time, from the poller. Release is the ONLY place that ever
deletes anything in Bolna, and only when ALLOW_BOLNA_NUMBER_DELETE is on."""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .bolna_client import AgentNotFoundOnBolna, BolnaError
from .errors import AppError
from .models import (
    POOL_STATUSES, Agent, Assignment, Call, Number, NumberEvent, SpamTestItem, SpamTestRun, utcnow,
)
from .validation import validate_phone

log = logging.getLogger("bnm.spam")


# ---------------------------------------------------------------- test runs
def _clean_ids(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(ids))


def create_run(s: Session, number_ids: list[int], agent: Agent, reviewer_phone: str, max_per_run: int,
               expiry_hours: float = 24.0) -> dict:
    """Queue a test run and reserve its numbers, in this one transaction (so two runs created at
    the same moment can never test the same number). Numbers that cannot be tested right now
    (on a call, in another run, awaiting a result, not on Bolna) are refused one by one.
    max_per_run = 0 means no cap."""
    reviewer_phone = validate_phone(reviewer_phone)
    ids = _clean_ids(number_ids)
    if not ids:
        raise AppError("VALIDATION_ERROR", "Invalid input: number_ids", 422)
    if max_per_run and len(ids) > max_per_run:
        raise AppError("TEST_RUN_TOO_LARGE",
                       f"A run can test at most {max_per_run} numbers (each call costs money).", 422,
                       max=max_per_run)
    if agent.current_number_id is None and agent.number_taken_at is not None:
        raise services.number_taken_error(s, agent)
    if agent.pending_switch and agent.pending_switch_reason == "spam":
        raise AppError("AGENT_SWITCHING", f"{agent.name} is waiting to swap a spam number.", 409)
    accepted, refused = [], []
    nums = {n.id: n for n in s.scalars(select(Number).where(Number.id.in_(ids), Number.status.in_(POOL_STATUSES)))}
    blockers = services.stack_blockers(s, list(nums.values()), expiry_hours)
    for nid in ids:
        num = nums.get(nid)
        if num is None:
            refused.append({"number_id": nid, "code": "NUMBER_NOT_FOUND", "message": "Number not found in the pool"})
        elif nid in blockers:
            code, why = blockers[nid]
            refused.append({"number_id": nid, "code": code, "message": f"{num.phone_number}: {why}."})
        else:
            accepted.append(nid)
    if not accepted:
        first = refused[0]
        raise AppError(first["code"], first["message"], 404 if first["code"] == "NUMBER_NOT_FOUND" else 409,
                       refused=refused)
    run = SpamTestRun(test_agent_id=agent.id, reviewer_phone=reviewer_phone, status="running")
    s.add(run)
    s.flush()
    for nid in accepted:
        s.add(SpamTestItem(run_id=run.id, number_id=nid, status="queued"))
    s.flush()
    log.info("spam test run %s queued with %d numbers", run.id, len(accepted))
    out = run_progress(s, run.id)
    out["refused"] = refused
    return out


def _queue_positions(s: Session) -> dict[int, int]:
    """Position (1 = next) of every queued item, across all running runs, in call order."""
    q = (select(SpamTestItem.id).join(SpamTestRun, SpamTestRun.id == SpamTestItem.run_id)
         .where(SpamTestItem.status == "queued", SpamTestRun.status == "running")
         .order_by(SpamTestRun.id, SpamTestItem.id))
    return {iid: i + 1 for i, iid in enumerate(s.scalars(q))}


def run_progress(s: Session, run_id: int) -> dict:
    run = s.get(SpamTestRun, run_id)
    if run is None:
        raise AppError("TEST_RUN_NOT_FOUND", "Test run not found", 404)
    pos = _queue_positions(s)
    items = s.scalars(select(SpamTestItem).where(SpamTestItem.run_id == run_id).order_by(SpamTestItem.id)).all()
    phones = {n.id: n.phone_number for n in s.scalars(select(Number).where(Number.id.in_([i.number_id for i in items])))}
    iso = lambda dt: dt.isoformat() + "Z" if dt else None  # noqa: E731
    return {
        "id": run.id, "status": run.status, "created_at": iso(run.created_at),
        "test_agent_id": run.test_agent_id, "reviewer_phone": run.reviewer_phone,
        "items": [{"id": i.id, "number_id": i.number_id, "phone_number": phones.get(i.number_id),
                   "status": i.status, "position": pos.get(i.id), "detail": i.detail,
                   "placed_at": iso(i.placed_at)} for i in items],
    }


def expire_awaiting(db, settings) -> int:
    """Dismiss test results nobody marked within TEST_RESULT_EXPIRY_HOURS. Never places a call."""
    cutoff = utcnow() - timedelta(hours=settings.test_result_expiry_hours)
    with db.session() as s:
        rows = s.scalars(select(Number).where(Number.test_awaiting_since.is_not(None),
                                              Number.test_awaiting_since <= cutoff)).all()
        for n in rows:
            n.test_awaiting_since = None
        return len(rows)


def queue_state(s: Session) -> dict[int, dict]:
    """number_id -> {status, position, run_id} for numbers waiting or being called."""
    pos = _queue_positions(s)
    out: dict[int, dict] = {}
    rows = s.execute(select(SpamTestItem).join(SpamTestRun, SpamTestRun.id == SpamTestItem.run_id)
                     .where(SpamTestItem.status.in_(("queued", "calling")), SpamTestRun.status == "running"))
    for (it,) in rows:
        out[it.number_id] = {"status": it.status, "position": pos.get(it.id), "run_id": it.run_id}
    return out


def _next_step(db, settings) -> tuple | None:
    """Decide, in one short transaction, whether to place the next test call.
    Returns (item_id, CallPlan) or None. Finalises finished items and runs on the way."""
    with db.session() as s:
        calling = s.scalars(select(SpamTestItem).where(SpamTestItem.status == "calling")
                            .order_by(SpamTestItem.id)).all()
        last_end = None
        for it in calling:
            call = s.get(Call, it.call_id) if it.call_id else None
            if call is not None and call.settled_at is None:
                return None  # one call at a time: wait for this one to end
            it.status = "done"
            it.detail = None if call is None else (call.reason or call.status)[:255]
            if call is not None and call.settled_at is not None:
                last_end = max(last_end or call.settled_at, call.settled_at)
        s.flush()
        runs = s.scalars(select(SpamTestRun).where(SpamTestRun.status == "running").order_by(SpamTestRun.id)).all()
        for run in runs:
            queued = s.scalars(select(SpamTestItem).where(SpamTestItem.run_id == run.id, SpamTestItem.status == "queued")
                               .order_by(SpamTestItem.id)).all()
            if not queued:
                run.status = "done"
                continue
            if last_end is None:  # the pause starts when the previous call ended
                last_end = s.scalar(select(func.max(Call.settled_at)).where(Call.purpose == "test"))
            if last_end is not None and utcnow() - last_end < timedelta(seconds=settings.spam_test_delay_seconds):
                return None
            for it in queued:
                try:
                    plan = services.begin_test_call(s, run.test_agent_id, it.number_id, run.reviewer_phone)
                except AppError as e:
                    it.status, it.detail = "failed", f"{e.code}: {e.message}"[:255]
                    s.flush()
                    continue
                it.status, it.call_id, it.placed_at = "calling", plan.call_id, utcnow()
                s.flush()
                return it.id, plan
            if not s.scalar(select(func.count()).select_from(SpamTestItem).where(
                    SpamTestItem.run_id == run.id, SpamTestItem.status.in_(("queued", "calling")))):
                run.status = "done"
        return None


def _fail_item(db, item_id: int, call_id: int, why: str) -> None:
    with db.session() as s:
        it = s.get(SpamTestItem, item_id)
        if it is not None:  # unlink first: the call row is about to be deleted
            it.status, it.call_id, it.detail = "failed", None, why[:255]
            s.flush()
        services.abort_call(s, call_id)
        if it is not None and not s.scalar(select(func.count()).select_from(SpamTestItem).where(
                SpamTestItem.run_id == it.run_id, SpamTestItem.status.in_(("queued", "calling")))):
            s.get(SpamTestRun, it.run_id).status = "done"


def _attach(db, item_id: int, call_id: int, execution_id: str, status: str) -> None:
    with db.session() as s:
        services.attach_execution(s, call_id, execution_id, status)
        it = s.get(SpamTestItem, item_id)
        if it is not None:
            it.execution_id = execution_id


async def advance_runs(db, bolna, settings) -> None:
    """Called by the poller on every pass: place the next queued test call, if it is time."""
    step = await run_in_threadpool(_next_step, db, settings)
    if step is None:
        return
    item_id, plan = step
    try:
        resp = await bolna.place_call(plan.bolna_agent_id, plan.recipient, plan.from_number)
        execution_id = resp.get("execution_id") or resp.get("run_id")
        if not execution_id:
            raise BolnaError("Bolna accepted the call but returned no execution id")
    except BolnaError as e:
        await run_in_threadpool(_fail_item, db, item_id, plan.call_id, f"Bolna refused the call: {e.message}")
        return
    except Exception as e:  # pragma: no cover - defensive
        log.exception("unexpected error placing a test call")
        await run_in_threadpool(_fail_item, db, item_id, plan.call_id, f"Unexpected error ({type(e).__name__})")
        return
    await run_in_threadpool(_attach, db, item_id, plan.call_id, str(execution_id),
                            str(resp.get("status") or "queued"))


# ---------------------------------------------------------------- release
def release_blocker(s: Session, num: Number) -> tuple[str, str] | None:
    """Why this number cannot be released right now, or None when it can."""
    if num.status == "releasing":
        return "RELEASE_IN_PROGRESS", "A release for this number is already running."
    if num.status not in POOL_STATUSES:
        return "NUMBER_NOT_FOUND", "Number is not in the pool."
    if not num.spam_flag:
        return "NUMBER_NOT_MARKED_SPAM", "Only numbers marked as spam can be released."
    holder = services.holder_of(s, num.id)
    if holder is not None:
        return "NUMBER_HELD_BY_AGENT", f"Held by {holder.name}. Wait for the agent to swap it."
    if services.number_active_calls(s, num.id) > 0:
        return "NUMBER_BUSY", "A call is in progress from this number."
    if num.linked_in_bolna:
        return "NUMBER_LINKED_IN_BOLNA", "Linked to an agent in Bolna. Unlink it in Bolna first."
    if not num.bolna_phone_number_id:
        return "NUMBER_ID_UNKNOWN", "Bolna's id for this number is unknown. Run a sync first."
    if num.bolna_rented is not True:
        return "NUMBER_NOT_RENTED", ("Not confirmed as bought through Bolna, so deleting it is not "
                                     "safe. Run a sync, or remove it in Bolna yourself.")
    return None


def _snapshot(s: Session, num: Number) -> str:
    assigns = s.scalar(select(func.count()).select_from(Assignment).where(Assignment.number_id == num.id)) or 0
    calls = s.scalar(select(func.count()).select_from(Call).where(Call.number_id == num.id)) or 0
    return (f"connected {num.connected_count}, total calls {num.total_calls}, "
            f"assignments {assigns}, calls on record {calls}")


def _result(num: Number, ok: bool, outcome: str, code: str | None = None, message: str = "") -> dict:
    return {"id": num.id, "phone_number": num.phone_number, "ok": ok, "outcome": outcome,
            "code": code, "message": message}


def _begin_release(db, number_id: int):
    """Check eligibility and set 'releasing' in one transaction. Returns a final result dict,
    or (number_id, phone, bolna_id, previous_status) when the DELETE should go ahead."""
    with db.session() as s:
        num = s.scalar(select(Number).where(Number.id == number_id).with_for_update())
        if num is None:
            return {"id": number_id, "phone_number": None, "ok": False, "outcome": "refused",
                    "code": "NUMBER_NOT_FOUND", "message": "Number not found"}
        if num.status == "released":
            return _result(num, True, "already_released", None, "Already released.")
        holder = services.holder_of(s, num.id)
        if holder is not None:
            services.lock_agent(s, holder.id)
        block = release_blocker(s, num)
        if block:
            return _result(num, False, "refused", block[0], block[1])
        prev = num.status
        num.status = "releasing"
        services.reconcile_number_in_stack(s, num)  # leaves the stack at once
        services.add_event(s, num.id, "release_started", f"release started from status {prev}")
        return num.id, num.phone_number, num.bolna_phone_number_id, prev


def _finish_release(db, number_id: int, outcome: str, prev: str, error: str | None = None) -> dict:
    with db.session() as s:
        num = s.scalar(select(Number).where(Number.id == number_id).with_for_update())
        if outcome == "failed":
            num.status = prev
            services.reconcile_number_in_stack(s, num)  # flagged, so it stays out
            services.add_event(s, num.id, "release_failed", (error or "unknown error")[:300])
            return _result(num, False, "failed", "BOLNA_DELETE_FAILED", f"Bolna could not delete it: {error}")
        num.status = "released"
        services.reconcile_number_in_stack(s, num)
        note = "Bolna said it no longer exists, so it was marked released. " if outcome == "gone" else ""
        services.add_event(s, num.id, "release_succeeded", f"{note}snapshot: {_snapshot(s, num)}")
        return _result(num, True, "released", None,
                       "Bolna no longer had this number; marked as released." if outcome == "gone" else "Released.")


async def release_numbers(st, ids: list[int]) -> list[dict]:
    """Release flagged numbers from the pool and from Bolna, one at a time. One failure never
    stops the others."""
    if not st.settings.allow_bolna_number_delete:
        raise AppError("BOLNA_DELETE_DISABLED",
                       "Bolna deletion is switched off in settings (ALLOW_BOLNA_NUMBER_DELETE).", 403)
    results = []
    for nid in _clean_ids(ids):
        step = await run_in_threadpool(_begin_release, st.db, nid)
        if isinstance(step, dict):
            results.append(step)
            continue
        number_id, _phone, bolna_id, prev = step
        try:
            await st.bolna.delete_phone_number(bolna_id)
            outcome, error = "released", None
        except BolnaError as e:
            outcome, error = ("gone", None) if e.status == 404 else ("failed", e.message)
        except Exception as e:  # pragma: no cover - defensive
            outcome, error = "failed", f"Unexpected error ({type(e).__name__})"
        res = await run_in_threadpool(_finish_release, st.db, number_id, outcome, prev, error)
        log.info("release of number %s: %s", number_id, res["outcome"])
        results.append(res)
    return results


def recover_releasing(db) -> int:
    """A crash between 'releasing' and the end of the DELETE would strand a number. At startup,
    put any such number back in the pool (it was never held, so it is available or at its limit)."""
    with db.session() as s:
        th = services.get_threshold(s)
        stuck = s.scalars(select(Number).where(Number.status == "releasing")).all()
        for num in stuck:
            num.status = "limit_reached" if num.connected_count >= th else "available"
            services.reconcile_number_in_stack(s, num)
            services.add_event(s, num.id, "release_failed", "release was interrupted; restored at startup")
        return len(stuck)
