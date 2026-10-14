"""All business rules. Every function takes an open Session and runs inside ONE transaction
(the caller commits). On SQLite every transaction is BEGIN IMMEDIATE, so these functions
are serialised; on Postgres the agent / number rows are locked with SELECT ... FOR UPDATE."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .connected import classify
from .errors import AppError
from .stack import (  # noqa: F401  (re-exported for callers)
    in_stack, reconcile_number_in_stack, reconcile_stack, stack_bottom, stack_health, stack_leave, stack_top,
    stack_top_many,
)
from .models import (
    POOL_STATUSES, Agent, Assignment, Call, Number, NumberEvent, Setting, SpamTestItem, SpamTestRun, utcnow,
)
from .validation import mask_phone, validate_phone

DEFAULT_THRESHOLD = 5
log = logging.getLogger("bnm.alloc")


@dataclass
class AllocationRules:
    """Settings that allocation needs. The app puts them in every session's `info`."""
    idle_first: bool = True
    idle_hours: float = 24.0
    cooldown_hours: float = 24.0
    test_expiry_hours: float = 24.0


def _rules(s: Session) -> AllocationRules:
    return s.info.get("alloc") or AllocationRules()


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
            reconcile_number_in_stack(s, num)
    s.flush()
    return n


# ---------------------------------------------------------------- helpers
def active_call_count(s: Session, agent_id: int) -> int:
    return s.scalar(
        select(func.count()).select_from(Call).where(Call.agent_id == agent_id, Call.settled_at.is_(None))
    ) or 0


def number_active_calls(s: Session, number_id: int) -> int:
    """Calls (lead or test) still in progress from this number."""
    return s.scalar(
        select(func.count()).select_from(Call).where(Call.number_id == number_id, Call.settled_at.is_(None))
    ) or 0


def add_event(s: Session, number_id: int, event: str, detail: str = "") -> None:
    """Audit trail for a number. `detail` must never hold a full phone number."""
    s.add(NumberEvent(number_id=number_id, event=event, detail=detail[:500]))


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
    if number.spam_flag:
        raise AppError("NUMBER_MARKED_SPAM", f"{number.phone_number} is marked as spam.", 409)
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


def _assign(s: Session, agent: Agent, number: Number, reason: str, from_pool: bool = True) -> None:
    if from_pool:
        number.taken_from_pool_at = utcnow()  # an idle move between agents keeps the original time
    agent.current_number_id = number.id
    agent.number_taken_at = None  # whoever gets a number is no longer "Number taken"
    agent.number_taken_by_agent_id = None
    agent.number_taken_reason = None
    agent.pool_empty = False
    agent.pending_switch = False
    agent.pending_switch_reason = None
    agent.waiting_since = None
    number.status = "in_use"
    reconcile_number_in_stack(s, number)  # still a member: assignment never changes membership
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
    number.taken_from_pool_at = None  # back in the pool
    reconcile_number_in_stack(s, number)  # released back to the pool: still a member
    agent.current_number_id = None
    s.flush()
    s.expire(agent, ["current_number"])
    return number


def pick_available(s: Session, provider: str | None, threshold: int) -> Number | None:
    """First available number (pool order = date added) of the same provider that may be
    auto-switched to: approved (status 'available'), allow_auto_switch, still on Bolna and not marked as spam."""
    q = select(Number).where(
        Number.status == "available",
        Number.connected_count < threshold,
        Number.allow_auto_switch.is_(True),
        Number.missing_on_bolna.is_(False),
        Number.spam_flag.is_(False),
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
    if existing is not None and existing.status in ("releasing", "released"):
        raise AppError("NUMBER_RELEASED", f"{phone} was released and cannot be added again.", 409)
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
        num.bolna_phone_number_id = str(match["id"])[:64] if match.get("id") else num.bolna_phone_number_id
        num.bolna_rented = match["rented"] if isinstance(match.get("rented"), bool) else num.bolna_rented
    else:
        num = Number(
            phone_number=phone, provider=provider, status="available",
            linked_in_bolna=linked is not None, allow_auto_switch=not linked,
            bolna_linked_agent_id=str(linked) if linked else None,
            bolna_phone_number_id=str(match["id"])[:64] if match.get("id") else None,
            bolna_rented=match["rented"] if isinstance(match.get("rented"), bool) else None,
        )
        s.add(num)
    stack_top(s, num)
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
    # (the caller puts it on top of the test stack)


def approve_number(s: Session, number_id: int, confirm_linked: bool = False) -> Number:
    num = _get_any_number(s, number_id)
    if num.status in POOL_STATUSES:
        raise AppError("NUMBER_ALREADY_IN_POOL", f"{num.phone_number} is already in the pool.", 409)
    if num.status != "discovered":
        raise AppError("NUMBER_NOT_DISCOVERED", f"{num.phone_number} is not in the discovered list.", 409)
    if num.spam_flag:
        raise AppError("NUMBER_MARKED_SPAM", f"{num.phone_number} is marked as spam.", 409)
    if num.linked_in_bolna and not confirm_linked:
        raise AppError(
            "NUMBER_LINKED_IN_BOLNA",
            f"{num.phone_number} is linked to an agent in Bolna (probably for inbound calls).",
            409, linked_agent_id=num.bolna_linked_agent_id)
    _approve(num)
    stack_top(s, num)
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
    approved, skipped_linked, done = 0, [], []
    for num in s.scalars(q.where(Number.spam_flag.is_(False)).with_for_update()):
        if num.linked_in_bolna and not confirm_linked:
            skipped_linked.append(num.id)
            continue
        _approve(num)
        done.append(num)
        approved += 1
    stack_top_many(s, done)
    s.flush()
    if approved:
        fill_waiting(s)
    return {"approved": approved, "skipped_linked": skipped_linked}


def ignore_number(s: Session, number_id: int) -> Number:
    num = _get_any_number(s, number_id)
    if num.status != "discovered":
        raise AppError("NUMBER_NOT_DISCOVERED", f"{num.phone_number} is not in the discovered list.", 409)
    num.status = "ignored"
    reconcile_number_in_stack(s, num)
    s.flush()
    return num


def ignore_many(s: Session, ids: list[int]) -> int:
    count = 0
    for num in s.scalars(select(Number).where(Number.status == "discovered", Number.id.in_(ids))):
        num.status = "ignored"
        reconcile_number_in_stack(s, num)
        count += 1
    s.flush()
    return count


def restore_number(s: Session, number_id: int) -> Number:
    num = _get_any_number(s, number_id)
    if num.status != "ignored":
        raise AppError("NUMBER_NOT_IGNORED", f"{num.phone_number} is not ignored.", 409)
    num.status = "discovered"
    reconcile_number_in_stack(s, num)
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
        if holder.pending_switch_reason != "spam":
            holder.pending_switch = False  # the limit no longer applies
            holder.pending_switch_reason = None
    else:
        num.status = "available"
    reconcile_number_in_stack(s, num)
    s.flush()
    fill_waiting(s)
    return num


# ---------------------------------------------------------------- agents
def assign_new_agent(s: Session, bolna_agent_id: str, number_id: int | None, name: str | None) -> Agent:
    """Create (or reuse) an agent and give it a number: the one a person chose, or with number_id=None
    the first suitable pool number. Never a number from another agent. With nothing suitable in the
    pool the agent waits as `pool_empty` and is filled by auto-refill."""
    bolna_agent_id = (bolna_agent_id or "").strip()
    if not bolna_agent_id:
        raise AppError("AGENT_NOT_FOUND", "Enter a Bolna agent ID", 422)
    threshold = get_threshold(s)
    number = _get_number(s, number_id) if number_id is not None else None
    agent = s.scalar(select(Agent).where(Agent.bolna_agent_id == bolna_agent_id).with_for_update())
    if agent is not None and agent.current_number_id is not None:
        raise AppError("AGENT_HAS_NUMBER",
                       f"{agent.name} already has a number. Use Change number on its page.", 409)
    if number is not None:
        _check_assignable(s, number, threshold)
    if agent is None:
        agent = Agent(bolna_agent_id=bolna_agent_id, name=name or f"Agent {bolna_agent_id[:8]}")
        s.add(agent)
        s.flush()
    elif name:
        agent.name = name
    had_before = s.scalar(select(func.count()).select_from(Assignment).where(Assignment.agent_id == agent.id))
    reason = "manual" if had_before else "initial"
    if number is None:
        number = pick_available(s, None, threshold)  # pool order, no provider to match
    if number is not None:
        _assign(s, agent, number, reason)
    else:
        agent.number_taken_at = agent.number_taken_by_agent_id = agent.number_taken_reason = None
        agent.pool_empty = True
        agent.waiting_since = utcnow()
        agent.pool_provider = None
        s.flush()
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
    if agent.current_number_id is None and agent.number_taken_at is not None:
        raise number_taken_error(s, agent)
    if agent.pool_empty:
        raise AppError("POOL_EMPTY", f"No number is available for {agent.name}. Add or reset a number first.", 409)
    if agent.current_number_id is None:
        raise AppError("AGENT_HAS_NO_NUMBER", f"{agent.name} has no number assigned.", 409)
    if agent.current_number is not None and agent.current_number.missing_on_bolna:
        raise _missing_error(agent.current_number)  # blocked, but never switched automatically
    if agent.pending_switch and agent.pending_switch_reason == "spam":
        raise AppError("AGENT_SWITCHING",
                       f"{agent.name}'s number was marked as spam and will be swapped when its current "
                       "calls end. New calls are blocked until then.", 409)
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
    if call.purpose == "lead" and number is not None and number.total_calls > 0:
        number.total_calls -= 1
    agent_id = call.agent_id
    s.delete(call)
    s.flush()
    agent = s.get(Agent, agent_id)
    if agent is not None and agent.pending_switch and agent.pending_switch_reason == "spam":
        # this may have been the last active call a spam swap was waiting for
        _after_settle(s, lock_agent(s, agent_id), get_threshold(s))


def _after_settle(s: Session, agent: Agent, threshold: int) -> None:
    """Threshold / spam check + switch (or pending switch) once a call has ended."""
    active = active_call_count(s, agent.id)
    num = agent.current_number
    if num is not None and num.spam_flag:
        # a flagged number is never kept in service: swap once the agent is idle
        if active > 0:
            agent.pending_switch = True
            agent.pending_switch_reason = "spam"
        else:
            _switch(s, agent, threshold, "auto_spam")
        return
    if num is not None and num.connected_count >= threshold:
        num.status = "limit_reached"
        reconcile_number_in_stack(s, num)
    if num is not None and num.status == "limit_reached":
        if active > 0:
            agent.pending_switch = True  # never change a number mid-call
            agent.pending_switch_reason = "limit"
        else:
            _switch(s, agent, threshold)
    elif agent.pending_switch and active == 0:
        agent.pending_switch = False
        agent.pending_switch_reason = None


def _switch(s: Session, agent: Agent, threshold: int, reason: str = "auto_threshold") -> None:
    """An automatic swap (threshold or spam). Idle-first: take the number of an idle agent, else the
    pool, else wait as pool_empty. Runs when the swap actually happens, so a deferred swap judges the
    lenders at that moment."""
    old = agent.current_number
    provider = old.provider if old is not None else agent.pool_provider
    _release(s, agent, reason, threshold)
    if old is not None and reason in ("auto_threshold", "auto_spam") and _rules(s).idle_first:
        got = take_from_idle_agent(s, agent, provider, threshold,
                                   "auto_idle_swap" if reason == "auto_threshold" else "auto_spam")
        if got is not None:
            return
    new = pick_available(s, provider, threshold)
    if new is not None:
        _assign(s, agent, new, reason)
    else:
        agent.pool_empty = True
        agent.pending_switch = False
        agent.pending_switch_reason = None
        agent.waiting_since = utcnow()
        agent.pool_provider = provider
        s.flush()


# ---------------------------------------------------------------- idle-first lending
def last_activity(s: Session, agent: Agent) -> object:
    """When the agent last placed a call (lead or test); if never, when its current assignment
    started, so a freshly assigned agent does not count as idle."""
    last = s.scalar(select(func.max(Call.placed_at)).where(Call.agent_id == agent.id))
    if last is not None:
        return last
    return s.scalar(select(func.max(Assignment.started_at)).where(
        Assignment.agent_id == agent.id, Assignment.ended_at.is_(None)))


def lender_block(s: Session, lender: Agent, receiver_id: int, provider: str | None, threshold: int) -> str | None:
    """Why `lender` may not lend its number right now, or None when it may."""
    rules = _rules(s)
    now = utcnow()
    num = lender.current_number
    if num is None:
        return "has no number"
    if lender.id == receiver_id:
        return "is the agent that needs a number"
    if lender.never_lend:
        return "is marked Never take this agent's number"
    if lender.pending_switch:
        return "has a pending switch"
    if active_call_count(s, lender.id) > 0:
        return "has a call in progress"
    if s.scalar(select(func.count()).select_from(SpamTestRun).where(
            SpamTestRun.test_agent_id == lender.id, SpamTestRun.status == "running")):
        return "is the test agent of a running test run"
    last = last_activity(s, lender)
    if last is not None and now - last < timedelta(hours=rules.idle_hours):
        return "was active recently"
    if num.connected_count >= threshold or num.status == "limit_reached":
        return "number is at its limit"
    if num.spam_flag:
        return "number is marked as spam"
    if num.missing_on_bolna:
        return "number is not on Bolna"
    if not num.allow_auto_switch:
        return "number is not allowed for auto-switching"
    if num.linked_in_bolna:
        return "number is linked in Bolna"
    if provider and num.provider != provider:
        return "number is from another provider"
    if s.scalar(select(func.count()).select_from(SpamTestItem).join(SpamTestRun, SpamTestRun.id == SpamTestItem.run_id)
                .where(SpamTestItem.number_id == num.id, SpamTestItem.status.in_(("queued", "calling")),
                       SpamTestRun.status == "running")):
        return "number is in a test run"
    if is_awaiting(num, rules.test_expiry_hours):
        return "number is awaiting a test result"
    if rules.cooldown_hours > 0 and s.scalar(select(func.count()).select_from(Assignment).where(
            Assignment.number_id == num.id, Assignment.end_reason == "taken_idle",
            Assignment.ended_at >= now - timedelta(hours=rules.cooldown_hours))):
        return "number was moved recently"
    return None


def lender_candidates(s: Session, receiver: Agent, provider: str | None, threshold: int) -> list[Agent]:
    """Eligible lenders, best first: fewest connected calls on the number, then the number most
    recently taken from the pool, then the lowest agent id."""
    holders = s.scalars(select(Agent).where(Agent.current_number_id.is_not(None), Agent.id != receiver.id)).all()
    ok = [a for a in holders if lender_block(s, a, receiver.id, provider, threshold) is None]

    def rank(a: Agent):
        taken = a.current_number.taken_from_pool_at
        return (a.current_number.connected_count, 0 if taken else 1, -(taken.timestamp() if taken else 0), a.id)
    return sorted(ok, key=rank)


def _lock_pair(s: Session, a_id: int, b_id: int) -> None:
    for agent_id in sorted((a_id, b_id)):  # a fixed order, so two moves can never deadlock
        lock_agent(s, agent_id)


def take_from_idle_agent(s: Session, receiver: Agent, provider: str | None, threshold: int, reason: str) -> Number | None:
    """Move the best idle agent's number to `receiver`, in this one transaction. Every candidate is
    re-checked under both agents' locks; one that changed is skipped. Returns the number, or None."""
    for lender in lender_candidates(s, receiver, provider, threshold):
        _lock_pair(s, receiver.id, lender.id)
        s.expire(lender)
        if active_call_count(s, receiver.id) > 0:
            return None  # the receiver itself got busy: never move a number to an agent on a call
        if lender_block(s, lender, receiver.id, provider, threshold) is not None:
            continue
        number = lender.current_number
        now = utcnow()
        a = s.scalar(select(Assignment).where(
            Assignment.agent_id == lender.id, Assignment.number_id == number.id, Assignment.ended_at.is_(None)))
        if a is not None:
            a.ended_at, a.end_reason = now, "taken_idle"
        lender.current_number_id = None
        lender.number_taken_at = now
        lender.number_taken_by_agent_id = receiver.id
        lender.number_taken_reason = f"moved to {receiver.name} because this agent was idle"
        lender.pool_empty = False
        lender.waiting_since = None
        s.flush()
        s.expire(lender, ["current_number"])
        _assign(s, receiver, number, reason, from_pool=False)  # counts on the number stay as they are
        why = "threshold" if reason == "auto_idle_swap" else "spam"
        add_event(s, number.id, "moved_idle",
                  f"moved from {lender.name} (agent #{lender.id}) to {receiver.name} (agent #{receiver.id}): {why}")
        log.info("idle swap: number %s moved from agent %s to agent %s (%s)", number.id, lender.id, receiver.id, why)
        return number
    return None


def number_taken_error(s: Session, agent: Agent) -> AppError:
    to = s.get(Agent, agent.number_taken_by_agent_id) if agent.number_taken_by_agent_id else None
    when = agent.number_taken_at.strftime("%d %b %Y %H:%M UTC") if agent.number_taken_at else "an earlier date"
    return AppError("NUMBER_TAKEN",
                    f"No number. Its number was moved to {to.name if to else 'another agent'} on {when} "
                    "because this agent was idle.", 409,
                    taken_by_agent_id=agent.number_taken_by_agent_id)


def set_never_lend(s: Session, agent_id: int, value: bool) -> Agent:
    agent = lock_agent(s, agent_id)
    agent.never_lend = bool(value)
    s.flush()
    return agent


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
    if call.purpose == "test":
        # a test call is only a look at how the number appears: it never moves a counter
        n = s.get(Number, call.number_id)
        if n is not None:
            n.last_tested_at = utcnow()
            n.test_awaiting_since = utcnow()  # a person must now say OK or spam
        s.flush()
    elif connected:
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
    if call.purpose == "test":
        n = s.get(Number, call.number_id)
        if n is not None:
            n.last_tested_at = utcnow()
            n.test_awaiting_since = utcnow()
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


# ---------------------------------------------------------------- test stack
# An ordered list of the pool's numbers (ascending stack_order = top first). Numbers join at the
# top, rotate to the bottom after Mark OK, and leave when marked as spam. Positions are computed
# inside the caller's transaction (SQLite serialises writers; ties break by id).
def is_awaiting(num: Number, expiry_hours: float) -> bool:
    """A test call ended and nobody has marked the number yet (and it has not expired)."""
    since = num.test_awaiting_since
    return since is not None and utcnow() - since < timedelta(hours=expiry_hours)


def stack_blockers(s: Session, numbers: list[Number], expiry_hours: float) -> dict[int, tuple[str, str]]:
    """Why a number cannot be tested right now: number id -> (code, short reason)."""
    ids = [n.id for n in numbers]
    busy = set(s.scalars(select(Call.number_id).where(Call.number_id.in_(ids), Call.settled_at.is_(None))))
    reserved = {nid: st for nid, st in s.execute(
        select(SpamTestItem.number_id, SpamTestItem.status).join(SpamTestRun, SpamTestRun.id == SpamTestItem.run_id)
        .where(SpamTestItem.number_id.in_(ids), SpamTestItem.status.in_(("queued", "calling")),
               SpamTestRun.status == "running"))}
    out: dict[int, tuple[str, str]] = {}
    for n in numbers:
        if n.id in busy:
            out[n.id] = ("NUMBER_BUSY", "On a call")
        elif n.id in reserved:
            out[n.id] = ("NUMBER_IN_TEST", "Calling" if reserved[n.id] == "calling" else "Queued")
        elif is_awaiting(n, expiry_hours):
            out[n.id] = ("AWAITING_RESULT", "Awaiting result")
        elif n.missing_on_bolna:
            out[n.id] = ("NUMBER_MISSING_ON_BOLNA", "Not on Bolna")
    return out


def stack_numbers(s: Session) -> list[Number]:
    """The whole stack, top first. A number without a position yet counts as new, so it is on top."""
    return list(s.scalars(select(Number).where(Number.status.in_(POOL_STATUSES), Number.spam_flag.is_(False))
                          .order_by(Number.stack_order.is_(None).desc(), Number.stack_order, Number.id)))


def next_testable(nums: list[Number], blockers: dict[int, tuple[str, str]]) -> Number | None:
    """The first number from the top of the stack that can be tested right now. Auto-select ticks this
    one and the stack list tags it "Next", so the two can never disagree."""
    return next((n for n in nums if n.id not in blockers), None)


def select_next(s: Session, expiry_hours: float) -> dict:
    """Suggest the one number to test next. Reserves nothing, changes nothing."""
    nums = stack_numbers(s)
    blockers = stack_blockers(s, nums, expiry_hours)
    pick = next_testable(nums, blockers)
    skipped = []
    for n in nums:  # the numbers above it that cannot be tested right now, and why
        if pick is not None and n.id == pick.id:
            break
        skipped.append({"number_id": n.id, "code": blockers[n.id][0], "message": blockers[n.id][1]})
    return {"ids": [pick.id] if pick else [], "skipped": skipped}


def clear_test(s: Session, number_id: int) -> Number:
    """Dismiss an awaiting result without marking anything. The number keeps its position."""
    num = _get_number(s, number_id)
    num.test_awaiting_since = None
    s.flush()
    return num


# ---------------------------------------------------------------- spam check
def begin_test_call(s: Session, agent_id: int, number_id: int, reviewer_phone: str) -> CallPlan:
    """Register a test call from `number_id` made by the test agent. It is an active call for
    that agent (the call lock applies) but never touches connected_count or total_calls."""
    reviewer_phone = validate_phone(reviewer_phone)
    agent = lock_agent(s, agent_id)
    if agent.current_number_id is None and agent.number_taken_at is not None:
        raise number_taken_error(s, agent)
    if agent.pending_switch and agent.pending_switch_reason == "spam":
        raise AppError("AGENT_SWITCHING", f"{agent.name} is waiting to swap a spam number.", 409)
    number = _get_number(s, number_id)
    if number_active_calls(s, number.id) > 0:
        raise AppError("NUMBER_BUSY", f"{number.phone_number} has a call in progress right now.", 409)
    call = Call(agent_id=agent.id, number_id=number.id, recipient=reviewer_phone, status="placing",
                purpose="test")
    s.add(call)
    s.flush()
    add_event(s, number.id, "test_call_placed", f"test call #{call.id} placed")
    return CallPlan(call.id, agent.bolna_agent_id, number.phone_number, reviewer_phone)


def mark_spam(s: Session, number_id: int, note: str | None = None) -> Number:
    """A person flags a number. It stays in the pool but is never allocated again. An agent that
    holds it is swapped at once if idle, otherwise as soon as its last call ends."""
    holder = holder_of(s, number_id)
    if holder is not None:
        lock_agent(s, holder.id)  # same lock order as begin_call: agent first, then number
    num = _get_number(s, number_id)
    note = (note or "").strip()[:500] or None
    if num.spam_flag:
        if note:
            num.spam_note = note
        s.flush()
        return num
    num.spam_flag = True
    num.spam_marked_at = utcnow()
    num.spam_note = note
    num.test_awaiting_since = None
    stack_leave(num)  # it now lives in the spam list
    add_event(s, num.id, "marked_spam", "marked as spam" + (" with a note" if note else ""))
    s.flush()
    holder = holder_of(s, num.id)
    if holder is not None:
        # check-and-swap in ONE transaction: a call cannot start between the two
        if active_call_count(s, holder.id) > 0:
            holder.pending_switch = True
            holder.pending_switch_reason = "spam"
        else:
            _switch(s, holder, get_threshold(s), "auto_spam")
    s.flush()
    return num


def mark_not_spam(s: Session, number_id: int) -> Number:
    holder = holder_of(s, number_id)
    if holder is not None:
        lock_agent(s, holder.id)
    num = _get_number(s, number_id)
    if not num.spam_flag:
        return num
    num.spam_flag = False
    num.spam_marked_at = None
    num.spam_note = None
    num.last_test_result = None  # tested again before it is treated as fine
    num.test_awaiting_since = None
    stack_top(s, num)
    add_event(s, num.id, "marked_not_spam", "marked as not spam")
    holder = holder_of(s, num.id)
    if holder is not None and holder.pending_switch_reason == "spam":
        holder.pending_switch = False  # the swap that was waiting is no longer needed
        holder.pending_switch_reason = None
    s.flush()
    return num


def record_test_result(s: Session, number_id: int, result: str, note: str | None = None) -> Number:
    """A person's verdict after looking at the test call. 'spam' is the same as mark_spam."""
    if result not in ("ok", "spam"):
        raise AppError("VALIDATION_ERROR", "Invalid input: result", 422)
    if result == "spam":
        num = mark_spam(s, number_id, note)
    else:
        num = _get_number(s, number_id)
    num.last_test_result = result
    num.test_awaiting_since = None
    if result == "ok":
        stack_bottom(s, num)  # rotates round again later; no cooldown
    add_event(s, num.id, "test_result", f"result: {result}")
    s.flush()
    return num
