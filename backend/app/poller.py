"""Background settlement of calls. Runs inside the app process as an asyncio task."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from . import services
from .models import Call, utcnow

log = logging.getLogger("bnm.poller")


def _list_active(db):
    with db.session() as s:
        rows = s.execute(select(Call.id, Call.execution_id, Call.placed_at).where(Call.settled_at.is_(None))).all()
        return [(r.id, r.execution_id, r.placed_at) for r in rows]


def _settle(db, call_id, record, min_seconds):
    with db.session() as s:
        return services.settle_call(s, call_id, record, min_seconds)


def _unknown(db, call_id, why):
    with db.session() as s:
        return services.mark_unknown(s, call_id, why)


async def poll_once(db, bolna, settings) -> int:
    """One pass over every active call. Returns how many calls became final."""
    from fastapi.concurrency import run_in_threadpool

    settled = 0
    now = utcnow()
    for call_id, execution_id, placed_at in await run_in_threadpool(_list_active, db):
        age = now - placed_at
        if execution_id is None:
            # Locked locally but Bolna never confirmed (e.g. a crash mid-request).
            if age > timedelta(minutes=settings.unconfirmed_call_minutes):
                await run_in_threadpool(_unknown, db, call_id, "Bolna never confirmed this call")
                settled += 1
            continue
        try:
            record = await bolna.get_execution(execution_id)
        except Exception as e:
            log.warning("poll failed for call %s (%s); will retry", call_id, type(e).__name__)
            record = None
        if record is not None:
            if await run_in_threadpool(_settle, db, call_id, record, settings.min_connected_seconds):
                settled += 1
                continue
        if age > timedelta(minutes=settings.call_timeout_minutes):
            await run_in_threadpool(_unknown, db, call_id, "No final status from Bolna - please review")
            settled += 1
    return settled


async def poll_loop(app) -> None:
    st = app.state
    while True:
        try:
            await poll_once(st.db, st.bolna, st.settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poller pass failed")
        await asyncio.sleep(st.settings.poll_seconds)
