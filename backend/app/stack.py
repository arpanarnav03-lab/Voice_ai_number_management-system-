"""Who is in the Spam Check test stack, and where.

THE RULE: a number is in the stack if and only if it is in the pool (status available, in_use or
limit_reached) AND is not marked as spam. Membership is stored as `stack_order IS NOT NULL`
(ascending = top first, unique). Releasing / released / discovered / ignored numbers are not members.

Three layers keep the stack and the pool from drifting apart:
  1. `reconcile_number_in_stack` is called by every service path that changes a number's status or
     spam flag;
  2. a `before_flush` hook applies the same rule to every Number the ORM is about to write, in the
     same transaction, so a path that forgets still cannot leave a number in the wrong place;
  3. `reconcile_stack` repairs old data (migration, startup, after every sync, and on request).

Positions follow deque rules: a number that JOINS goes to the TOP; Mark OK moves it to the BOTTOM;
Not spam puts it back on top. Ties are impossible (positions are unique; order breaks by id)."""
from __future__ import annotations

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from .models import POOL_STATUSES, Number


def qualifies(num: Number) -> bool:
    """The rule. Unset attributes on a not-yet-inserted row use their column defaults."""
    status = num.status or "available"
    return status in POOL_STATUSES and not (num.spam_flag or False)


in_stack = qualifies  # older name


def never_tested(num: Number) -> bool:
    return num.last_tested_at is None and num.last_test_result is None


# ---------------------------------------------------------------- positions
def _edge(s: Session, top: bool, flush: bool = True) -> int | None:
    if flush:
        s.flush()
    with s.no_autoflush:
        return s.scalar(select(func.min(Number.stack_order) if top else func.max(Number.stack_order)))


def place_top(s: Session, nums: list[Number], flush: bool = True) -> None:
    """Put `nums` above everyone else, keeping their own order (first one ends up first)."""
    if not nums:
        return
    edge = _edge(s, True, flush)
    first = (0 if edge is None else edge) - len(nums)
    for i, n in enumerate(nums):
        n.stack_order = first + i


def place_bottom(s: Session, nums: list[Number], flush: bool = True) -> None:
    if not nums:
        return
    edge = _edge(s, False, flush)
    first = 1 if edge is None else edge + 1
    for i, n in enumerate(nums):
        n.stack_order = first + i


def stack_top(s: Session, num: Number) -> None:
    """Put one number on top (a restored "Not spam" number, or a number that just joined)."""
    num.stack_order = None
    reconcile_number_in_stack(s, num)


def stack_top_many(s: Session, nums: list[Number]) -> None:
    for n in nums:
        n.stack_order = None
    place_top(s, [n for n in nums if qualifies(n)])
    for n in nums:
        if not qualifies(n):
            n.stack_order = None


def stack_bottom(s: Session, num: Number) -> None:
    """Mark OK: rotate a member to the bottom. A non-member stays out."""
    if qualifies(num):
        num.stack_order = None
        place_bottom(s, [num])
    else:
        num.stack_order = None


def stack_leave(num: Number) -> None:
    num.stack_order = None


# ---------------------------------------------------------------- reconcile
def reconcile_number_in_stack(s: Session, num: Number) -> None:
    """Apply the rule to one number: a non-qualifying number leaves, a qualifying number without
    a position joins at the top, a qualifying number with a position stays exactly where it is."""
    if not qualifies(num):
        num.stack_order = None
    elif num.stack_order is None:
        place_top(s, [num])


def reconcile_stack(s: Session) -> dict:
    """Apply the rule to every number. Existing valid positions keep their order. Qualifying numbers
    with no position: never-tested ones go to the top (oldest added first), tested ones to the bottom
    (oldest test first). Non-qualifying numbers lose their position. Duplicate positions are fixed
    keeping the relative order. Safe to run any number of times."""
    s.flush()
    nums = list(s.scalars(select(Number)))
    removed = 0
    for n in nums:
        if not qualifies(n) and n.stack_order is not None:
            n.stack_order = None
            removed += 1
    members = [n for n in nums if qualifies(n)]
    positioned = sorted((n for n in members if n.stack_order is not None), key=lambda n: (n.stack_order, n.id))
    seen, dup_positions = set(), set()
    for n in positioned:
        (dup_positions if n.stack_order in seen else seen).add(n.stack_order)
    if dup_positions:  # re-rank in the current order so every position is unique
        base = positioned[0].stack_order
        for i, n in enumerate(positioned):
            n.stack_order = base + i
    missing = [n for n in members if n.stack_order is None]
    fresh = sorted((n for n in missing if never_tested(n)), key=lambda n: (n.added_at, n.id))
    tested = sorted((n for n in missing if not never_tested(n)), key=lambda n: (n.last_tested_at or n.added_at, n.id))
    place_top(s, fresh)
    place_bottom(s, tested)
    s.flush()
    return {"added": len(missing), "removed": removed, "duplicates_fixed": len(dup_positions)}


def stack_health(s: Session) -> dict:
    """All zeros on a healthy system."""
    s.flush()
    pool = Number.status.in_(POOL_STATUSES)
    ok = pool & Number.spam_flag.is_(False)
    count = lambda *w: s.scalar(select(func.count()).select_from(Number).where(*w)) or 0  # noqa: E731
    dup = s.scalar(select(func.count()).select_from(
        select(Number.stack_order).where(Number.stack_order.is_not(None))
        .group_by(Number.stack_order).having(func.count() > 1).subquery())) or 0
    return {
        "missing_from_stack": count(ok, Number.stack_order.is_(None)),
        "unexpected_in_stack": count(~ok, Number.stack_order.is_not(None)),
        "duplicate_positions": dup,
    }


# ---------------------------------------------------------------- the safety net
@event.listens_for(Session, "before_flush")
def _keep_stack_consistent(session: Session, flush_context, instances) -> None:
    """Whatever code changes a Number, it is never written in the wrong place in the stack."""
    todo = [o for o in list(session.new) + list(session.dirty) if isinstance(o, Number)]
    if not todo:
        return
    joining = []
    for n in todo:
        if not qualifies(n):
            if n.stack_order is not None:
                n.stack_order = None
        elif n.stack_order is None:
            joining.append(n)
    if joining:
        place_top(session, joining, flush=False)
