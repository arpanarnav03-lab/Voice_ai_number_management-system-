"""Sync from Bolna: discovery, approval, missing numbers, failure guard, serialisation."""
import threading
import time

from sqlalchemy import select

from app import services
from app.bolna_client import BolnaError
from app.models import Agent, Assignment, Number, SyncRun, utcnow
from tests.conftest import RECIPIENT


def bn(i, provider="vobiz", agent=None):
    return {"phone_number": f"+91800{i:07d}", "telephony_provider": provider, "agent_id": agent}


def ph(i):
    return f"+91800{i:07d}"


def sync(h, expect=200):
    r = h.client.post("/api/sync/run")
    assert r.status_code == expect, r.text
    return r.json()


def listing(h, status, **params):
    r = h.client.get("/api/numbers", params={"status": status, **params})
    assert r.status_code == 200, r.text
    return r.json()


def by_phone(h, status):
    return {n["phone_number"]: n for n in listing(h, status)["items"]}


def snapshot(h):
    with h.db.session() as s:
        nums = [(n.phone_number, n.status, n.connected_count, n.total_calls, n.allow_auto_switch)
                for n in s.scalars(select(Number).order_by(Number.id))]
        agents = [(a.id, a.current_number_id, a.pool_empty, a.pending_switch)
                  for a in s.scalars(select(Agent).order_by(Agent.id))]
        assigns = s.scalars(select(Assignment)).all()
        return nums, agents, len(assigns)


def place(h, agent_id):
    res = h.trigger((agent_id, RECIPIENT))[0]
    assert res["ok"], res
    return res["execution_id"]


# 1 ------------------------------------------------------------------ discovery
def test_first_sync_creates_discovered_numbers_outside_the_pool(h):
    h.set_threshold(1)
    n1 = h.add_number()  # pool number (+9180000000001) so the agent has somewhere to start
    a = h.add_agent("agent-1", n1)
    h.bolna.numbers = [bn(1), bn(2), bn(3)]
    run = sync(h)
    assert run["total_on_bolna"] == 3 and run["new_count"] == 2
    found = by_phone(h, "discovered")
    assert set(found) == {ph(2), ph(3)}
    assert all(n["connected_count"] == 0 and n["total_calls"] == 0 for n in found.values())
    assert all(n["discovered_at"] for n in found.values())
    assert set(h.numbers()) == {n1}  # none appear in the pool
    # a discovered number cannot be given to an agent by hand or by auto-switch
    d_id = found[ph(2)]["id"]
    r = h.client.post(f"/api/agents/{a}/change-number", json={"number_id": d_id})
    assert r.status_code == 404
    h.bolna.complete(place(h, a))
    h.settle()
    assert h.agent(a)["agent"]["status"] == "pool_empty"


# 2 ------------------------------------------------------------------ idempotent
def test_second_identical_sync_changes_nothing(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a))
    h.settle()
    r = h.client.put(f"/api/numbers/{n2}/auto-switch", json={"allow": False, "confirm": True})
    assert r.status_code == 200
    h.bolna.numbers = [bn(1), bn(2), bn(3), bn(4, agent="inbound-1")]
    sync(h)
    first = snapshot(h)
    run2 = sync(h)
    assert run2["new_count"] == 0
    assert snapshot(h) == first
    # existing pool numbers kept their counts / assignment / allow_auto_switch through both syncs
    nums = h.numbers()
    assert nums[n1]["connected_count"] == 1 and nums[n1]["status"] == "in_use"
    assert nums[n2]["allow_auto_switch"] is False


# 3 ------------------------------------------------------------------ invalid
def test_invalid_numbers_are_skipped_and_reported_never_stored(h):
    h.bolna.numbers = [bn(1), {"phone_number": "+9180000", "telephony_provider": "vobiz", "agent_id": None},
                       {"phone_number": "+15551234567", "telephony_provider": "x", "agent_id": None},
                       {"phone_number": None}, "junk"]
    run = sync(h)
    assert run["skipped_invalid_count"] == 4 and run["new_count"] == 1
    assert "+9180000" in run["skipped_numbers"]
    with h.db.session() as s:
        assert [n.phone_number for n in s.scalars(select(Number))] == [ph(1)]
    st = h.client.get("/api/sync/status").json()
    assert st["last_run"]["skipped_invalid_count"] == 4 and st["discovered_count"] == 1


# 4 ------------------------------------------------------------------ linked
def test_linked_number_needs_confirmation_and_stays_out_of_auto_switch(h):
    h.set_threshold(1)
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    h.bolna.numbers = [bn(1), bn(2, agent="inbound-agent-1")]
    sync(h)
    d = by_phone(h, "discovered")[ph(2)]
    assert d["linked_in_bolna"] is True and d["bolna_linked_agent_id"] == "inbound-agent-1"
    assert d["allow_auto_switch"] is False

    r = h.client.post(f"/api/numbers/{d['id']}/approve")
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_LINKED_IN_BOLNA"
    assert r.json()["linked_agent_id"] == "inbound-agent-1"
    assert by_phone(h, "discovered")[ph(2)]["status"] == "discovered"

    r = h.client.post(f"/api/numbers/{d['id']}/approve", json={"confirm_linked": True})
    assert r.status_code == 200 and r.json()["status"] == "available"
    assert r.json()["allow_auto_switch"] is False

    h.bolna.complete(place(h, a))  # n1 reaches the limit; the only free number is the linked one
    h.settle()
    assert h.agent(a)["agent"]["status"] == "pool_empty"

    # turning it on needs a confirmation
    assert h.client.put(f"/api/numbers/{d['id']}/auto-switch", json={"allow": True}).status_code == 422
    assert h.numbers()[d["id"]]["allow_auto_switch"] is False
    r = h.client.put(f"/api/numbers/{d['id']}/auto-switch", json={"allow": True, "confirm": True})
    assert r.status_code == 200 and r.json()["allow_auto_switch"] is True
    with h.db.session() as s:
        assert services.pick_available(s, "vobiz", 1).id == d["id"]


# 5 ------------------------------------------------------------------ missing
def test_missing_pool_number_is_flagged_blocked_and_never_auto_chosen(h):
    h.set_threshold(1)
    n1, n2, n3, n4, n5 = (h.add_number() for _ in range(5))
    a1, a2 = h.add_agent("agent-1", n1), h.add_agent("agent-2", n3)
    h.bolna.numbers = [bn(3), bn(4), bn(5)]  # n1 and n2 are gone from Bolna
    run = sync(h)
    assert run["missing_count"] == 2
    nums = h.numbers()
    assert nums[n1]["missing_on_bolna"] and nums[n2]["missing_on_bolna"] and nums[n2]["missing_since"]
    assert not nums[n3]["missing_on_bolna"]

    # the holder is blocked, not switched
    ag = h.agent(a1)["agent"]
    assert ag["status"] == "number_missing" and ag["number"]["id"] == n1
    res = h.trigger((a1, RECIPIENT))[0]
    assert res["ok"] is False and res["code"] == "NUMBER_MISSING_ON_BOLNA"
    assert h.agent(a1)["agent"]["number"]["id"] == n1
    # by hand: a missing number cannot be picked, a present one can (no active call)
    r = h.client.post(f"/api/agents/{a1}/change-number", json={"number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_MISSING_ON_BOLNA"

    # auto-switch skips n2 even though it is first in pool order
    h.bolna.complete(place(h, a2))
    h.settle()
    assert h.agent(a2)["agent"]["number"]["id"] == n4

    r = h.client.post(f"/api/agents/{a1}/change-number", json={"number_id": n5})
    assert r.status_code == 200 and r.json()["number"]["id"] == n5

    # listed again by Bolna: back to normal
    h.bolna.numbers = [bn(i) for i in range(1, 6)]
    sync(h)
    assert not any(n["missing_on_bolna"] for n in h.numbers().values())
    assert h.numbers()[n2]["missing_since"] is None


def test_missing_since_keeps_the_first_timestamp(h):
    n1, _ = h.add_number(), h.add_number()
    h.bolna.numbers = [bn(2)]
    sync(h)
    first = h.numbers()[n1]["missing_since"]
    time.sleep(0.01)
    sync(h)
    assert h.numbers()[n1]["missing_since"] == first


# 6 ------------------------------------------------------------------ cleanup
def test_gone_discovered_and_ignored_numbers_are_deleted_ignored_stay_ignored(h):
    h.bolna.numbers = [bn(1), bn(2), bn(3)]
    sync(h)
    ids = {p: n["id"] for p, n in by_phone(h, "discovered").items()}
    assert h.client.post(f"/api/numbers/{ids[ph(2)]}/ignore").status_code == 200
    sync(h)
    assert set(by_phone(h, "ignored")) == {ph(2)}  # a later sync does not bring it back
    assert set(by_phone(h, "discovered")) == {ph(1), ph(3)}
    assert h.client.post(f"/api/numbers/{ids[ph(2)]}/restore").json()["status"] == "discovered"
    assert h.client.post(f"/api/numbers/{ids[ph(2)]}/ignore").status_code == 200

    h.bolna.numbers = [bn(1)]
    sync(h)
    assert set(by_phone(h, "discovered")) == {ph(1)} and by_phone(h, "ignored") == {}
    with h.db.session() as s:
        assert [n.phone_number for n in s.scalars(select(Number))] == [ph(1)]


# 7 ------------------------------------------------------------------ failure guard
def test_empty_error_or_timeout_fails_the_run_and_changes_nothing(h):
    n1 = h.add_number()
    h.bolna.numbers = [bn(1), bn(2)]
    sync(h)
    before = snapshot(h)

    h.bolna.numbers = []
    r = h.client.post("/api/sync/run")
    assert r.status_code == 502 and r.json()["code"] == "SYNC_FAILED"
    assert snapshot(h) == before and not h.numbers()[n1]["missing_on_bolna"]

    h.bolna.numbers = [bn(1), bn(2)]
    for err in (BolnaError("Bolna is down", 503), TimeoutError("timed out")):
        h.bolna.list_error = err
        r = h.client.post("/api/sync/run")
        assert r.status_code == 502 and r.json()["code"] == "SYNC_FAILED"
        assert snapshot(h) == before
    h.bolna.list_error = None

    st = h.client.get("/api/sync/status").json()
    assert st["last_run"]["status"] == "failed" and "timed out" in st["last_run"]["error_message"]
    assert st["last_success"]["status"] == "success"  # the earlier good run is still reported
    with h.db.session() as s:
        assert s.scalar(select(SyncRun).where(SyncRun.status == "failed").limit(1)) is not None


def test_empty_response_with_an_empty_pool_is_fine(h):
    h.bolna.numbers = []
    assert sync(h)["total_on_bolna"] == 0


# 8 ------------------------------------------------------------------ serialised
def test_two_syncs_at_once_run_only_one(h):
    h.bolna.numbers = [bn(1)]
    h.bolna.list_delay = 0.6
    out = {}
    t = threading.Thread(target=lambda: out.update(first=h.client.post("/api/sync/run")))
    t.start()
    time.sleep(0.2)
    assert h.client.get("/api/sync/status").json()["running"] is True
    second = h.client.post("/api/sync/run")
    t.join()
    assert second.status_code == 409 and second.json()["code"] == "SYNC_ALREADY_RUNNING"
    assert out["first"].status_code == 200
    assert h.bolna.list_calls == 1
    assert h.client.get("/api/sync/status").json()["running"] is False


# 9 ------------------------------------------------------------------ approve
def test_approving_refills_a_waiting_agent_and_add_all_skips_linked(h):
    h.set_threshold(1)
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a))
    h.settle()
    assert h.agent(a)["agent"]["status"] == "pool_empty"

    h.bolna.numbers = [bn(1), bn(2), bn(3, agent="inbound-1"), bn(4)]
    sync(h)
    disc = by_phone(h, "discovered")
    d2 = disc[ph(2)]["id"]
    r = h.client.post(f"/api/numbers/{d2}/approve")
    assert r.status_code == 200 and r.json()["connected_count"] == 0
    ag = h.agent(a)["agent"]
    assert ag["number"]["id"] == d2 and ag["status"] != "pool_empty"  # refilled
    assert h.client.post(f"/api/numbers/{d2}/approve").status_code == 409  # already in the pool

    body = listing(h, "discovered")
    assert body["total"] == 2 and body["unlinked_total"] == 1
    res = h.client.post("/api/numbers/approve", json={"all_unlinked": True}).json()
    assert res["approved"] == 1
    assert set(by_phone(h, "discovered")) == {ph(3)}  # the linked one still waits
    assert h.numbers()[disc[ph(4)]["id"]]["status"] == "available"

    # selected ids: a linked one is skipped without confirm_linked
    d3 = by_phone(h, "discovered")[ph(3)]["id"]
    res = h.client.post("/api/numbers/approve", json={"ids": [d3]}).json()
    assert res == {"approved": 0, "skipped_linked": [d3]}
    res = h.client.post("/api/numbers/approve", json={"ids": [d3], "confirm_linked": True}).json()
    assert res["approved"] == 1


def test_approve_never_touches_pool_numbers_and_ignore_only_discovered(h):
    n1 = h.add_number()
    h.bolna.numbers = [bn(1), bn(2)]
    sync(h)
    res = h.client.post("/api/numbers/approve", json={"ids": [n1]}).json()
    assert res["approved"] == 0
    assert h.client.post(f"/api/numbers/{n1}/ignore").status_code == 409
    assert h.client.post(f"/api/numbers/{n1}/restore").status_code == 409
    assert h.client.put(f"/api/numbers/{n1}/auto-switch", json={"allow": False, "confirm": True}).status_code == 200
    d = by_phone(h, "discovered")[ph(2)]["id"]
    assert h.client.put(f"/api/numbers/{d}/auto-switch", json={"allow": True, "confirm": True}).status_code == 404
    assert h.client.post(f"/api/numbers/{d}/reset").status_code == 404  # not a pool number


def test_manual_add_approves_a_discovered_number(h):
    h.bolna.numbers = [bn(1)]
    sync(h)
    assert by_phone(h, "discovered")
    h.bolna.numbers = [bn(1)]
    r = h.client.post("/api/numbers", json={"phone_number": ph(1)})
    assert r.status_code == 201 and r.json()["number"]["status"] == "available"
    assert listing(h, "discovered")["total"] == 0 and len(h.numbers()) == 1


def test_discovered_list_is_paginated_and_searchable(h):
    h.bolna.numbers = [bn(i) for i in range(1, 8)]
    sync(h)
    p1 = listing(h, "discovered", page_size=3)
    assert p1["total"] == 7 and len(p1["items"]) == 3
    assert len(listing(h, "discovered", page_size=3, page=3)["items"]) == 1
    hit = listing(h, "discovered", q="0000005")
    assert [n["phone_number"] for n in hit["items"]] == [ph(5)]
    assert h.client.get("/api/numbers", params={"status": "bogus"}).status_code == 422


# 10 ----------------------------------------------------------------- auto approve
def test_auto_approve_adds_unlinked_numbers_and_leaves_linked_ones(h):
    h.set_threshold(1)
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a))
    h.settle()
    h.app.state.settings.sync_auto_approve = True
    h.bolna.numbers = [bn(1), bn(2), bn(3, agent="inbound-1")]
    sync(h)
    nums = h.numbers()
    new = next(n for n in nums.values() if n["phone_number"] == ph(2))
    assert new["status"] in ("available", "in_use") and new["connected_count"] == 0
    assert h.agent(a)["agent"]["number"]["id"] == new["id"]  # pool_empty agent refilled
    assert set(by_phone(h, "discovered")) == {ph(3)}


# 11 ----------------------------------------------------------------- call lock
def test_sync_during_an_active_call_leaves_the_agent_alone(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    place(h, a)  # call in progress
    h.bolna.numbers = [bn(2), bn(5)]  # n1 vanishes from Bolna mid-call
    sync(h)
    ag = h.agent(a)["agent"]
    assert ag["number"]["id"] == n1 and ag["on_call"] is True and ag["active_calls"] == 1
    assert ag["pending_switch"] is False
    r = h.client.post(f"/api/agents/{a}/change-number", json={"number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "AGENT_ON_CALL"


# 12 ----------------------------------------------------------------- eligibility
def test_auto_switch_never_picks_an_ineligible_number(h):
    def row(i, **kw):
        base = dict(phone_number=ph(i), provider="vobiz", status="available", connected_count=0)
        base.update(kw)
        return Number(**base)

    bad = [
        row(1, status="discovered"), row(2, status="ignored"), row(3, missing_on_bolna=True),
        row(4, allow_auto_switch=False), row(5, provider="plivo"), row(6, connected_count=5),
        row(7, status="limit_reached", connected_count=5), row(8, status="in_use"),
    ]
    with h.db.session() as s:
        s.add_all(bad)
    with h.db.session() as s:
        assert services.pick_available(s, "vobiz", 5) is None
        s.add(row(9))
        s.flush()
        assert services.pick_available(s, "vobiz", 5).phone_number == ph(9)
