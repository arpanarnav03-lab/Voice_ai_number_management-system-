"""Sync the numbers on the Bolna account into the database (read-only towards Bolna).

New numbers are stored as 'discovered' and need a person's approval before they join the pool.
A sync never changes a pool number's status, counts, assignments or allow_auto_switch, and it
never reads or touches calls or agents, so it cannot break the call lock."""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .errors import AppError
from .models import POOL_STATUSES, Number, SyncRun, utcnow
from .validation import is_valid_phone

log = logging.getLogger("bnm.sync")

_running = threading.Lock()  # one sync at a time; held while Bolna is being asked too
MAX_SKIPPED_STORED = 200


def is_running() -> bool:
    return _running.locked()


def _provider(item: dict) -> str:
    return str(item.get("telephony_provider") or "unknown").lower()


def _bolna_id(item: dict) -> str | None:
    """Bolna's own id for the number (needed by the release flow's DELETE)."""
    raw = item.get("id")
    return str(raw)[:64] if raw else None


def _rented(item: dict) -> bool | None:
    raw = item.get("rented")
    return raw if isinstance(raw, bool) else None


def apply_sync(s: Session, items: list, auto_approve: bool) -> dict:
    """Merge Bolna's list into the database (one transaction). Returns the run counts."""
    skipped: list[str] = []
    seen: dict[str, dict] = {}
    for item in items:
        phone = item.get("phone_number") if isinstance(item, dict) else None
        if not is_valid_phone(phone):
            skipped.append(str(phone)[:40])
            continue
        seen.setdefault(phone, item)

    existing = {n.phone_number: n for n in s.scalars(select(Number))}
    now = utcnow()
    new_count = approved = released_listed = 0
    joined: list[Number] = []
    for phone, item in seen.items():
        agent_id = item.get("agent_id") or None
        linked = agent_id is not None
        num = existing.get(phone)
        if num is None:
            auto = auto_approve and not linked
            fresh = Number(
                phone_number=phone, provider=_provider(item), status="available" if auto else "discovered",
                connected_count=0, total_calls=0, linked_in_bolna=linked,
                bolna_linked_agent_id=str(agent_id) if linked else None,
                allow_auto_switch=not linked, discovered_at=now,
                bolna_phone_number_id=_bolna_id(item), bolna_rented=_rented(item),
            )
            s.add(fresh)
            joined.append(fresh)
            new_count += 1
            approved += auto
        elif num.status in ("releasing", "released"):
            # Never resurrected and never touched. A released number Bolna still lists is reported.
            released_listed += num.status == "released"
        else:
            # status, counts, assignments, allow_auto_switch and the spam fields are never touched here
            num.provider = _provider(item)
            num.bolna_phone_number_id = _bolna_id(item) or num.bolna_phone_number_id
            num.bolna_rented = _rented(item) if _rented(item) is not None else num.bolna_rented
            num.linked_in_bolna = linked
            num.bolna_linked_agent_id = str(agent_id) if linked else None
            num.missing_on_bolna = False
            num.missing_since = None
            services.reconcile_number_in_stack(s, num)  # membership is unchanged by syncing
    s.flush()
    services.stack_top_many(s, joined)  # auto-approved numbers enter the test stack at the top

    for phone, num in existing.items():
        if phone in seen:
            continue
        if num.status in POOL_STATUSES:
            if not num.missing_on_bolna:
                num.missing_on_bolna = True
                num.missing_since = num.missing_since or now
            services.reconcile_number_in_stack(s, num)  # still a member (shown as not testable)
        elif num.status in ("discovered", "ignored"):  # gone from Bolna
            s.delete(num)
        # releasing / released numbers are expected to be absent: left alone, never flagged missing
    s.flush()
    missing = s.scalar(select(func.count()).select_from(Number).where(
        Number.status.in_(POOL_STATUSES), Number.missing_on_bolna.is_(True))) or 0
    if approved:
        services.fill_waiting(s)
    fixed = services.reconcile_stack(s)  # self-heal after every sync run
    if fixed["added"] or fixed["removed"] or fixed["duplicates_fixed"]:
        log.warning("sync repaired the test stack: %d added, %d removed, %d duplicate positions",
                    fixed["added"], fixed["removed"], fixed["duplicates_fixed"])
    return {"total_on_bolna": len(items), "new_count": new_count, "missing_count": missing,
            "skipped": skipped, "released_listed": released_listed}


def _pool_size(db) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(Number).where(Number.status.in_(POOL_STATUSES))) or 0


def _record(db, started, status: str, counts: dict | None = None, error: str | None = None) -> dict:
    counts = counts or {}
    skipped = counts.get("skipped", [])
    with db.session() as s:
        run = SyncRun(
            started_at=started, finished_at=utcnow(), status=status,
            total_on_bolna=counts.get("total_on_bolna", 0), new_count=counts.get("new_count", 0),
            missing_count=counts.get("missing_count", 0), skipped_invalid_count=len(skipped),
            released_listed_count=counts.get("released_listed", 0),
            error_message=(error or None) and error[:500],
            skipped_numbers=json.dumps(skipped[:MAX_SKIPPED_STORED]) if skipped else None,
        )
        s.add(run)
        s.flush()
        return run_summary(run)


def _apply_and_record(db, started, items, auto_approve) -> dict:
    with db.session() as s:
        counts = apply_sync(s, items, auto_approve)
    return _record(db, started, "success", counts)


def run_summary(run: SyncRun) -> dict:
    def iso(dt):
        return dt.isoformat() + "Z" if dt else None
    return {
        "id": run.id, "status": run.status, "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at), "total_on_bolna": run.total_on_bolna,
        "new_count": run.new_count, "missing_count": run.missing_count,
        "skipped_invalid_count": run.skipped_invalid_count, "error_message": run.error_message,
        "released_listed_count": run.released_listed_count,
        "skipped_numbers": json.loads(run.skipped_numbers) if run.skipped_numbers else [],
    }


async def run_sync(st) -> dict:
    """Run one sync. Raises SYNC_ALREADY_RUNNING if one is in progress and SYNC_FAILED when the
    run failed (a failed run is recorded and changes nothing)."""
    if not _running.acquire(blocking=False):
        raise AppError("SYNC_ALREADY_RUNNING", "A sync is already running.", 409)
    try:
        started = utcnow()
        try:
            items = await st.bolna.list_all_phone_numbers()
            if not isinstance(items, list):
                raise ValueError("Bolna returned an unexpected response")
            if not items and await run_in_threadpool(_pool_size, st.db) > 0:
                raise ValueError("Bolna returned an empty number list, so nothing was changed")
        except Exception as e:
            msg = getattr(e, "message", None) or str(e) or type(e).__name__
            run = await run_in_threadpool(_record, st.db, started, "failed", None, str(msg))
            log.warning("sync run %s failed (%s)", run["id"], type(e).__name__)
            raise AppError("SYNC_FAILED", f"Sync failed: {msg}", 502, run=run)
        run = await run_in_threadpool(_apply_and_record, st.db, started, items, st.settings.sync_auto_approve)
        log.info("sync run %s: %d on Bolna, %d new, %d missing, %d skipped, %d released still listed",
                 run["id"], run["total_on_bolna"], run["new_count"], run["missing_count"],
                 run["skipped_invalid_count"], run["released_listed_count"])
        return run
    finally:
        _running.release()


def status(db) -> dict:
    with db.session() as s:
        last = s.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
        ok = s.scalar(select(SyncRun).where(SyncRun.status == "success").order_by(SyncRun.id.desc()).limit(1))
        count = lambda st_: s.scalar(select(func.count()).select_from(Number).where(Number.status == st_)) or 0
        return {
            "running": is_running(),
            "last_run": run_summary(last) if last else None,
            "last_success": run_summary(ok) if ok else None,
            "discovered_count": count("discovered"), "ignored_count": count("ignored"),
        }


async def sync_loop(app) -> None:
    """Once at startup, then every SYNC_INTERVAL_MINUTES. Interval 0 = never."""
    st = app.state
    if st.settings.sync_interval_minutes <= 0:
        return
    while True:
        try:
            await run_sync(st)
        except asyncio.CancelledError:
            raise
        except AppError:
            pass  # already logged / recorded
        except Exception:
            log.exception("scheduled sync crashed")
        await asyncio.sleep(st.settings.sync_interval_minutes * 60)
