"""All business rules. Every function takes an open Session and runs inside ONE transaction
(the caller commits). On SQLite every transaction is BEGIN IMMEDIATE, so these functions
are serialised; on Postgres the agent / number rows are locked with SELECT ... FOR UPDATE."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .connected import classify
from .errors import AppError
from .models import POOL_STATUSES, Agent, Assignment, Call, Number, Setting, utcnow
from .validation import mask_phone, validate_phone

DEFAULT_THRESHOLD = 5


# ---------------------------------------------------------------- settings
def get_threshold(s: Session, default: int = DEFAULT_THRESHOLD) -> int:
    row = s.get(Setting, "threshold")
    return int(row.value) if row else default


def set_threshold(s: Session, value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise AppError("INVALID_THRESHOLD", "Threshold must be a whole number of 1 or more", 422)
    if n < 1 or n > 100000:
        raise AppError("INVALID_THRESHOLD", "Threshold must be a whole number of 1 or more", 422)
    row = s.get(Setting, "threshold")
    if row:
        row.value = str(n)
    else:
        s.add(Setting(key="threshold", value=str(n)))
    s.flush()
    # Does not switch anyone. Free numbers already at/over the new limit are marked so
    # they cannot be handed out; in-use numbers are re-evaluated on their next settled call.
    for num in s.scalars(select(Number).where(Number.status == "available")):
        if num.connected_count >= n:
            num.status = "limit_reached"
    s.flush()
    return n


# ---------------------------------------------------------------- helpers
def active_call_count(s: Session, agent_id: int) -> int:
    return s.scalar(
        select(func.count()).select_from(Call).where(Call.agent_id == agent_id, Call.settled_at.is_(None))
    ) or 0


def holder_of(s: Session, number_id: int) -> Agent | None:
    return s.scalar(select(Agent).where(Agent.current_number_id == number_id))


def lock_agent(s: Session, agent_id: int) -> Agent:
    agent = s.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
    if agent is None:
        raise AppError("AGENT_NOT_FOUND", "Agent not found", 404)
    return agent


def _get_any_number(s: Session, number_id: int) -> Number:
    """Any row, including discovered / ignored ones that are not part of the pool."""
    num = s.scalar(select(Number).where(Number.id == number_id).with_for_update())
    if num is None:
        raise AppError("NUMBER_NOT_FOUND", "Number not found", 404)
    return num


def _get_number(s: Session, number_id: int) -> Number:
    """A number that is in the pool. Discovered / ignored numbers are not."""
    num = s.scalar(select(Number).where(Number.id == number_id).with_for_update())
    if num is None or num.status not in POOL_STATUSES:
        raise AppError("NUMBER_NOT_FOUND", "Number not found in the pool", 404)
    return num


def _missing_error(number: Number) -> AppError:
    return AppError("NUMBER_MISSING_ON_BOLNA",
                    f"{number.phone_number} is not on your Bolna account any more.", 409)


def _check_assignable(s: Session, number: Number, threshold: int) -> None:
    """Invariant: a number can be picked only if it is free AND under the limit."""
    if number.missing_on_bolna:
        raise _missing_error(number)
    holder = holder_of(s, number.id)
    if holder is not None:
        on_call = active_call_count(s, holder.id) > 0
        raise AppError(
            "NUMBER_IN_USE",
            f"{number.phone_number} is already in use by {holder.name}"
            + (", which is on a call right now." if on_call else "."),
            409, agent_id=holder.id, agent_name=holder.name, on_call=on_call,
        )
    if number.status == "limit_reached" or number.connected_count >= threshold:
        raise AppError(
            "NUMBER_LIMIT_REACHED",
            f"{number.phone_number} has reached its limit of {threshold} connected calls.",
            409, threshold=threshold,
        )


def _assign(s: Session, agent: Agent, number: Number, reason: str) -> None:
    agent.current_number_id = number.id
    agent.pool_empty = False
    agent.pending_switch = False
    agent.waiting_since = None
    number.status = "in_use"
    s.add(Assignment(agent_id=agent.id, number_id=number.id, reason=reason, started_at=utcnow()))
    s.flush()
    s.expire(agent, ["current_number"])


def _release(s: Session, agent: Agent, end_reason: str, threshold: int) -> Number | None:
    """End the agent's current assignment; the number goes back to the pool."""
    number = agent.current_number
    if number is None:
        return None
    a = s.scalar(select(Assignment).where(
        Assignment.agent_id == agent.id, Assignment.number_id == number.id, Assignment.ended_at.is_(None)))
    if a is not None:
        a.ended_at = utcnow()
        a.end_reason = end_reason
    number.status = "limit_reached" if (
        number.status == "limit_reached" or number.connected_count >= threshold) else "available"
    agent.current_number_id = None
    s.flush()
    s.expire(agent, ["current_number"])
    return number


def pick_available(s: Session, provider: str | None, threshold: int) -> Number | None:
    """First available number (pool order = date added) of the same provider that may be
    auto-switched to: approved (status 'available'), allow_auto_switch, and still on Bolna."""
    q = select(Number).where(
        Number.status == "available",
        Number.connected_count < threshold,
        Number.allow_auto_switch.is_(True),
        Number.missing_on_bolna.is_(False),
        ~exists().where(Agent.current_number_id == Number.id),
    )
    if provider:
        q = q.where(Number.provider == provider)
    q = q.order_by(Number.added_at, Number.id).limit(1).with_for_update()
    return s.scalar(q)


def fill_waiting(s: Session) -> list[int]:
    """Give numbers to agents in 'pool_empty', oldest waiting first. Returns agent ids filled."""
    threshold = get_threshold(s)
    filled = []
    waiting = s.scalars(
        select(Agent).where(Agent.pool_empty.is_(True)).order_by(Agent.waiting_since, Agent.id)
    ).all()
    for agent in waiting:
        num = pick_available(s, agent.pool_provider, threshold)
        if num is None:
            continue
        _assign(s, agent, num, "auto_refill")
        filled.append(agent.id)
    return filled


# ---------------------------------------------------------------- numbers
def add_number(s: Session, phone: str, bolna_numbers: list[dict]) -> tuple[Number, str | None]:
    phone = validate_phone(phone)
    match = next((n for n in bolna_numbers
                  if str(n.get("phone_number", "")).replace(" ", "") == phone), None)
    if match is None:
        raise AppError("NUMBER_NOT_FOUND_ON_BOLNA",
                       f"{phone} was not found in your Bolna account, so it was not added.", 422)
    existing = s.scalar(select(Number).where(Number.phone_number == phone).with_for_update())
    if existing is not None and existing.status in POOL_STATUSES:
        raise AppError("NUMBER_ALREADY_IN_POOL", f"{phone} is already in the pool.", 409)
    linked = match.get("agent_id") or None
    provider = str(match.get("telephony_provider") or "unknown").lower()
    if existing is not None:  # discovered / ignored: typing it in by hand counts as approval
        num = existing
        num.provider, num.status, num.connected_count = provider, "available", 0
        num.added_at = utcnow()
        num.linked_in_bolna = linked is not None
        num.bolna_linked_agent_id = str(linked) if linked else None
        num.allow_auto_switch = not linked
        num.missing_on_bolna, num.missing_since = False, None
    else:
        num = Number(
            phone_number=phone, provider=provider, status="available",
            linked_in_bolna=linked is not None, allow_auto_switch=not linked,
            bolna_linked_agent_id=str(linked) if linked else None,
        )
        s.add(num)
    s.flush()
    warning = None
    if linked:
        warning = (f"{phone} is linked to an agent inside Bolna (probably for inbound calls). "
                   "It was added, but it is excluded from auto-switching.")
    fill_waiting(s)
    return num, warning


# ---------------------------------------------------------------- discovered numbers
def _approve(num: Number) -> None:
    num.status = "available"
    num.connected_count = 0
    num.added_at = utcnow()  # pool order is the date added to the pool


def approve_number(s: Session, number_id: int, confirm_linked: bool = False) -> Number:
    num = _get_any_number(s, number_id)
    if num.status in POOL_STATUSES:
        raise AppError("NUMBER_ALREADY_IN_POOL", f"{num.phone_number} is already in the pool.", 409)
    if num.status != "discovered":
        raise AppError("NUMBER_NOT_DISCOVERED", f"{num.phone_number} is ignored. Restore it first.", 409)
    if num.linked_in_bolna and not confirm_linked:
        raise AppError(
            "NUMBER_LINKED_IN_BOLNA",
            f"{num.phone_number} is linked to an agent in Bolna (probably for inbound calls).",
            409, linked_agent_id=num.bolna_linked_agent_id)
    _approve(num)
    s.flush()
    fill_waiting(s)
    return num


def approve_many(s: Session, ids: list[int] | None, all_unlinked: bool, confirm_linked: bool = False) -> dict:
    """Approve discovered numbers. Numbers already in the pool or ignored are left alone."""
    q = select(Number).where(Number.status == "discovered").order_by(Number.discovered_at, Number.id)
    if all_unlinked:
        q = q.where(Number.linked_in_bolna.is_(False))
    else:
        q = q.where(Number.id.in_(ids or []))
    approved, skipped_linked = 0, []
    for num in s.scalars(q.with_for_update()):
        if num.linked_in_bolna and not confirm_linked:
            skipped_linked.append(num.id)
            continue
        _approve(num)
        approved += 1
    s.flush()
    if approved:
        fill_waiting(s)
    return {"approved": approved, "skipped_linked": skipped_linked}


def ignore_number(s: Session, number_id: int) -> Number:
    num = _get_any_number(s, number_id)
    if num.status != "discovered":
        raise AppError("NUMBER_NOT_DISCOVERED", f"{num.phone_number} is not in the discovered list.", 409)
    num.status = "ignored"
    s.flush()
    return num


def ignore_many(s: Session, ids: list[int]) -> int:
    count = 0
    for num in s.scalars(select(Number).where(Number.status == "discovered", Number.id.in_(ids))):
        num.status = "ignored"
        count += 1
    s.flush()
    return count


def restore_number(s: Session, number_id: int) -> Number:
    num = _get_any_number(s, number_id)
    if num.status != "ignored":
        raise AppError("NUMBER_NOT_IGNORED", f"{num.phone_number} is not ignored.", 409)
    num.status = "discovered"
    s.flush()
    return num


def set_auto_switch(s: Session, number_id: int, allow: bool, confirm: bool) -> Number:
    num = _get_number(s, number_id)  # pool numbers only
    if confirm is not True:
        raise AppError("VALIDATION_ERROR", "Confirm this change first.", 422)
    num.allow_auto_switch = bool(allow)
    s.flush()
    return num


def reset_number(s: Session, number_id: int) -> Number:
    num = _get_number(s, number_id)
    num.connected_count = 0
    holder = holder_of(s, num.id)
    if holder is not None:
        num.status = "in_use"
        holder.pending_switch = False  # the limit no longer applies
    else:
        num.status = "available"
    s.flush()
    fill_waiting(s)
    return num


# ---------------------------------------------------------------- agents
def assign_new_agent(s: Session, bolna_agent_id: str, number_id: int, name: str | None) -> Agent:
    bolna_agent_id = (bolna_agent_id or "").strip()
    if not bolna_agent_id:
        raise AppError("AGENT_NOT_FOUND", "Enter a Bolna agent ID", 422)
    threshold = get_threshold(s)
    number = _get_number(s, number_id)
    agent = s.scalar(select(Agent).where(Agent.bolna_agent_id == bolna_agent_id).with_for_update())
    if agent is not None and agent.current_number_id is not None:
        raise AppError("AGENT_HAS_NUMBER",
                       f"{agent.name} already has a number. Use Change number on its page.", 409)
    _check_assignable(s, number, threshold)
    if agent is None:
        agent = Agent(bolna_agent_id=bolna_agent_id, name=name or f"Agent {bolna_agent_id[:8]}")
        s.add(agent)
        s.flush()
    elif name:
        agent.name = name
    had_before = s.scalar(select(func.count()).select_from(Assignment).where(Assignment.agent_id == agent.id))
    _assign(s, agent, number, "manual" if had_before else "initial")
    return agent


def change_number(s: Session, agent_id: int, number_id: int) -> Agent:
    agent = lock_agent(s, agent_id)
    if active_call_count(s, agent.id) > 0:  # THE call lock, enforced on the server
        raise AppError("AGENT_ON_CALL",
                       f"{agent.name} is on a call. The number can be changed after the call ends.", 409)
    threshold = get_threshold(s)
    number = _get_number(s, number_id)
    if agent.current_number_id == number.id:
        return agent
    _check_assignable(s, number, threshold)
    had_before = s.scalar(select(func.count()).select_from(Assignment).where(Assignment.agent_id == agent.id))
    _release(s, agent, "manual", threshold)
    _assign(s, agent, number, "manual" if had_before else "initial")
    fill_waiting(s)  # the old number may now serve someone who is waiting
    return agent


# ---------------------------------------------------------------- calls
@dataclass
class CallPlan:
    call_id: int
    bolna_agent_id: str
    from_number: str
    recipient: str


def begin_call(s: Session, agent_id: int, recipient: str) -> CallPlan:
    """Take the lock and register the call BEFORE Bolna is contacted. From this moment the
    agent counts as 'on a call', so no number change can sneak in."""
    recipient = validate_phone(recipient)
    agent = lock_agent(s, agent_id)
    if agent.pool_empty:
        raise AppError("POOL_EMPTY", f"No number is available for {agent.name}. Add or reset a number first.", 409)
    if agent.current_number_id is None:
        raise AppError("AGENT_HAS_NO_NUMBER", f"{agent.name} has no number assigned.", 409)
    if agent.current_number is not None and agent.current_number.missing_on_bolna:
        raise _missing_error(agent.current_number)  # blocked, but never switched automatically
    if agent.pending_switch:
        raise AppError("SWITCH_PENDING",
                       f"{agent.name}'s number reached its limit and will switch when the current call ends.", 409)
    number = agent.current_number
    call = Call(agent_id=agent.id, number_id=number.id, recipient=recipient, status="placing")
    number.total_calls += 1
    s.add(call)
    s.flush()
    return CallPlan(call.id, agent.bolna_agent_id, number.phone_number, recipient)


def attach_execution(s: Session, call_id: int, execution_id: str, status: str = "queued") -> None:
    call = s.get(Call, call_id)
    if call is not None and call.settled_at is None:
        call.execution_id = execution_id
        call.status = status or "queued"


def abort_call(s: Session, call_id: int) -> None:
    """Bolna refused the call: it was never placed, so forget it and undo the count."""
    call = s.get(Call, call_id)
    if call is None or call.settled_at is not None:
        return
    number = s.get(Number, call.number_id)
    if number is not None and number.total_calls > 0:
        number.total_calls -= 1
    s.delete(call)
    s.flush()


def _after_settle(s: Session, agent: Agent, threshold: int) -> None:
    """Threshold check + switch (or pending switch) once a call has ended."""
    active = active_call_count(s, agent.id)
    num = agent.current_number
    if num is not None and num.connected_count >= threshold:
        num.status = "limit_reached"
    if num is not None and num.status == "limit_reached":
        if active > 0:
            agent.pending_switch = True  # never change a number mid-call
        else:
            _switch(s, agent, threshold)
    elif agent.pending_switch and active == 0:
        agent.pending_switch = False


def _switch(s: Session, agent: Agent, threshold: int) -> None:
    old = agent.current_number
    provider = old.provider if old is not None else agent.pool_provider
    _release(s, agent, "auto_threshold", threshold)
    new = pick_available(s, provider, threshold)
    if new is not None:
        _assign(s, agent, new, "auto_threshold")
    else:
        agent.pool_empty = True
        agent.pending_switch = False
        agent.waiting_since = utcnow()
        agent.pool_provider = provider
        s.flush()


def settle_call(s: Session, call_id: int, execution: dict, min_seconds: float = 1.0) -> bool:
    """Apply a polled execution record. Idempotent: a settled call is never counted twice.
    Returns True when the call became final."""
    call = s.get(Call, call_id)
    if call is None or call.settled_at is not None:
        return False
    is_final, connected, status, duration, reason = classify(execution, min_seconds)
    if not is_final:
        if status and status != call.status:
            call.status = status
        return False
    agent = lock_agent(s, call.agent_id)
    threshold = get_threshold(s)
    call.status = status
    call.duration = duration
    call.connected = connected
    call.reason = reason
    call.settled_at = utcnow()
    if connected is None:
        call.needs_review = True  # 'unclear': shown in the UI, never counted
    s.flush()
    if connected:
        number = s.get(Number, call.number_id)
        number.connected_count += 1
        a = s.scalar(select(Assignment).where(
            Assignment.agent_id == agent.id, Assignment.number_id == call.number_id
        ).order_by(Assignment.started_at.desc(), Assignment.id.desc()).limit(1))
        if a is not None:
            a.connected_calls += 1
        s.flush()
    _after_settle(s, agent, threshold)
    return True


def mark_unknown(s: Session, call_id: int, why: str) -> bool:
    """Give up waiting for a call so the agent is not locked forever."""
    call = s.get(Call, call_id)
    if call is None or call.settled_at is not None:
        return False
    agent = lock_agent(s, call.agent_id)
    call.status = "unknown"
    call.connected = None
    call.reason = why
    call.needs_review = True
    call.settled_at = utcnow()
    s.flush()
    _after_settle(s, agent, get_threshold(s))
    return True


def result_of(call: Call) -> str:
    """Result pill: connected | voicemail | no_answer | busy | in_progress | unclear | failed"""
    if call.settled_at is None:
        return "in_progress"
    if call.connected:
        return "connected"
    if call.connected is None:
        return "unclear"
    if call.reason.startswith("voicemail"):
        return "voicemail"
    if call.status == "busy":
        return "busy"
    if call.status == "no-answer" or call.reason.startswith("nobody picked up"):
        return "no_answer"
    return "failed"


def log_ref(call: Call) -> str:
    return f"call#{call.id} to {mask_phone(call.recipient)}"
