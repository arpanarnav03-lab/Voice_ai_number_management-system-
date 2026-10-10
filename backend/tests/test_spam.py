"""Spam Check: marking, the swap that never happens mid-call, test calls, and release."""
import threading

from sqlalchemy import select

from app import services
from app.bolna_client import BolnaError
from app.models import Agent, Call, Number, NumberEvent, SyncRun
from tests.conftest import RECIPIENT

REVIEWER = "+919999999902"


def ph(i):
    return f"+91800{i:07d}"


def bid(i):  # the fake Bolna's id for number i
    return f"{i:032x}"


def place(h, agent_id):
    res = h.trigger((agent_id, RECIPIENT))[0]
    assert res["ok"], res
    return res["execution_id"]


def mark(h, nid, note=None):
    r = h.client.post(f"/api/numbers/{nid}/mark-spam", json={"note": note} if note else {})
    assert r.status_code == 200, r.text
    return r.json()


def history(h, agent_id):
    return h.agent(agent_id)["number_history"]


def spam_ids(h):
    return {n["id"] for n in h.client.get("/api/spam/list").json()["items"]}


def start_run(h, number_ids, agent_id, reviewer=REVIEWER, expect=201):
    r = h.client.post("/api/spam/tests", json={"number_ids": number_ids, "test_agent_id": agent_id,
                                               "reviewer_phone": reviewer})
    assert r.status_code == expect, r.text
    return r.json()


def release(h, ids, expect=200):
    r = h.client.post("/api/spam/release", json={"ids": ids})
    assert r.status_code == expect, r.text
    return r.json()


def by_id(results):
    return {r["id"]: r for r in results["results"]}


def number(h, nid):
    return h.client.get(f"/api/numbers/{nid}").json()["number"]


# 1 ------------------------------------------------------------------ mark / not spam
def test_marking_a_free_number_keeps_it_in_the_pool_but_out_of_allocation(h):
    h.set_threshold(1)
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    out = mark(h, n2, "shows as Spam Likely")
    assert out["spam_flag"] is True and out["status"] == "available" and out["spam_note"]
    assert n2 in h.numbers() and h.numbers()[n2]["spam_flag"] is True  # still listed in the pool
    assert spam_ids(h) == {n2}
    # not assignable by hand ...
    r = h.client.post("/api/agents", json={"bolna_agent_id": "agent-2", "number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_MARKED_SPAM"
    # ... nor by auto-switch
    h.bolna.complete(place(h, a))
    h.settle()
    assert h.agent(a)["agent"]["status"] == "pool_empty"
    # not spam makes it eligible again, but does not hand it back to anyone
    out = h.client.post(f"/api/numbers/{n2}/not-spam").json()
    assert out["spam_flag"] is False and out["spam_note"] is None
    assert spam_ids(h) == set() and h.agent(a)["agent"]["status"] == "pool_empty"
    assert h.client.post("/api/agents", json={"bolna_agent_id": "agent-2", "number_id": n2}).status_code == 201
    events = [e["event"] for e in h.client.get(f"/api/numbers/{n2}").json()["events"]]
    assert "marked_spam" in events and "marked_not_spam" in events


# 2 ------------------------------------------------------------------ idle holder
def test_flagging_a_number_held_by_an_idle_agent_swaps_it_at_once(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    mark(h, n1)
    ag = h.agent(a)
    assert ag["agent"]["number"]["id"] == n2 and ag["agent"]["status"] == "ready"
    assert history(h, a)[0]["reason"] == "auto_spam" and history(h, a)[1]["end_reason"] == "auto_spam"
    nums = h.numbers()
    assert nums[n1]["spam_flag"] and nums[n1]["used_by"] is None and nums[n1]["status"] == "available"
    assert nums[n3]["status"] == "available"


# 3 ------------------------------------------------------------------ active call
def test_flagging_during_a_call_waits_blocks_new_calls_then_swaps(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    e1 = place(h, a)
    e2 = place(h, a)  # two active calls
    mark(h, n1)
    ag = h.agent(a)["agent"]
    assert ag["number"]["id"] == n1 and ag["pending_switch"] and ag["pending_switch_reason"] == "spam"
    assert ag["status"] == "switching_spam"
    res = h.trigger((a, RECIPIENT))[0]
    assert res["ok"] is False and res["code"] == "AGENT_SWITCHING"
    h.bolna.complete(e1)
    h.settle()
    assert h.agent(a)["agent"]["number"]["id"] == n1  # the other call is still running
    h.bolna.complete(e2)
    h.settle()
    ag = h.agent(a)
    assert ag["agent"]["number"]["id"] == n2 and not ag["agent"]["pending_switch"]
    assert history(h, a)[0]["reason"] == "auto_spam"
    nums = h.numbers()
    assert nums[n1]["connected_count"] == 2  # calls in progress finished and counted normally
    assert nums[n1]["used_by"] is None and nums[n1]["spam_flag"]


def test_not_spam_cancels_a_waiting_swap(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    e = place(h, a)
    mark(h, n1)
    h.client.post(f"/api/numbers/{n1}/not-spam")
    ag = h.agent(a)["agent"]
    assert not ag["pending_switch"] and ag["status"] == "on_call"
    h.bolna.complete(e)
    h.settle()
    assert h.agent(a)["agent"]["number"]["id"] == n1


# 4 ------------------------------------------------------------------ no interleaving
def test_swap_first_means_the_call_uses_the_new_number(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    mark(h, n1)
    place(h, a)
    assert h.agent(a)["calls"][0]["number_id"] == n2


def test_a_call_and_a_spam_mark_racing_never_end_with_the_flagged_number_in_service(h):
    for round_ in range(4):
        n1, n2 = h.add_number(), h.add_number()
        a = h.add_agent(f"agent-{round_}", n1)
        out = {}
        t1 = threading.Thread(target=lambda: out.update(call=h.trigger((a, RECIPIENT))[0]))
        t2 = threading.Thread(target=lambda: out.update(mark=h.client.post(f"/api/numbers/{n1}/mark-spam", json={})))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert out["mark"].status_code == 200
        if out["call"]["ok"]:
            h.bolna.complete(out["call"]["execution_id"])
            h.settle()
        ag = h.agent(a)
        assert ag["agent"]["number"]["id"] == n2 and not ag["agent"]["pending_switch"]
        used = {c["number_id"] for c in ag["calls"]}
        assert used <= {n1, n2}


# 5 ------------------------------------------------------------------ no replacement
def test_no_replacement_leaves_the_agent_pool_empty_until_a_clean_number_arrives(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    mark(h, n2)  # the only other number is already flagged
    mark(h, n1)
    ag = h.agent(a)["agent"]
    assert ag["status"] == "pool_empty" and ag["number"] is None
    res = h.trigger((a, RECIPIENT))[0]
    assert res["ok"] is False and res["code"] == "POOL_EMPTY"
    n3 = h.add_number()  # a clean number refills the waiting agent
    assert h.agent(a)["agent"]["number"]["id"] == n3
    assert h.numbers()[n1]["used_by"] is None and h.numbers()[n2]["used_by"] is None


def test_no_replacement_and_an_active_call_goes_pool_empty_after_the_call(h):
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    e = place(h, a)
    mark(h, n1)
    assert h.agent(a)["agent"]["number"]["id"] == n1
    h.bolna.complete(e)
    h.settle()
    assert h.agent(a)["agent"]["status"] == "pool_empty"


# 6 ------------------------------------------------------------------ test calls
def test_test_calls_go_one_at_a_time_in_order_and_never_touch_counters(h):
    n1, n2, n3, n4 = (h.add_number() for _ in range(4))
    lead = h.add_agent("agent-1", n1)
    tester = h.add_agent("tester", n4)
    run = start_run(h, [n3, n2, n1], tester)
    assert [i["status"] for i in run["items"]] == ["queued"] * 3
    assert [i["position"] for i in run["items"]] == [1, 2, 3]

    h.settle()
    assert [p["from"] for p in h.bolna.placed] == [ph(3)]  # only the first, the rest wait
    assert h.bolna.placed[0]["recipient"] == REVIEWER and h.bolna.placed[0]["agent_id"] == "tester"
    h.settle()
    assert len(h.bolna.placed) == 1
    assert h.agent(tester)["agent"]["on_call"] is True  # the test call locks the test agent
    r = h.client.post(f"/api/agents/{tester}/change-number", json={"number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "AGENT_ON_CALL"

    for k in range(3):
        h.bolna.complete(h.bolna.placed[k]["execution_id"], 18)
        h.settle()
    assert [p["from"] for p in h.bolna.placed] == [ph(3), ph(2), ph(1)]
    prog = h.client.get(f"/api/spam/tests/{run['id']}").json()
    assert prog["status"] == "done" and all(i["status"] == "done" for i in prog["items"])

    nums = h.numbers()
    for n in (n1, n2, n3):
        assert nums[n]["connected_count"] == 0 and nums[n]["total_calls"] == 0
        assert nums[n]["last_tested_at"]
    assert nums[n4]["connected_count"] == 0 and nums[n4]["total_calls"] == 0
    d = h.agent(tester)
    assert {c["purpose"] for c in d["calls"]} == {"test"}
    assert d["summary"]["calls_placed"] == 0  # tests are not lead calls
    assert h.agent(lead)["agent"]["status"] == "ready"


def test_test_call_rules_busy_cap_and_validation(h):
    n1, n2, n3 = (h.add_number() for _ in range(3))
    lead = h.add_agent("agent-1", n1)
    tester = h.add_agent("tester", n3)
    place(h, lead)  # a lead call is running from n1
    r = h.client.post("/api/spam/tests", json={"number_ids": [n1], "test_agent_id": tester, "reviewer_phone": REVIEWER})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_BUSY"
    run = start_run(h, [n1, n2], tester)  # mixed: n1 refused, n2 accepted
    assert [i["number_id"] for i in run["items"]] == [n2]
    assert run["refused"][0]["number_id"] == n1 and run["refused"][0]["code"] == "NUMBER_BUSY"

    h.app.state.settings.spam_test_max_per_run = 2
    r = h.client.post("/api/spam/tests", json={"number_ids": [n1, n2, n3], "test_agent_id": tester, "reviewer_phone": REVIEWER})
    assert r.status_code == 422 and r.json()["code"] == "TEST_RUN_TOO_LARGE"
    r = h.client.post("/api/spam/tests", json={"number_ids": [n2], "test_agent_id": tester, "reviewer_phone": "12345"})
    assert r.status_code == 422 and r.json()["code"] == "INVALID_PHONE"
    r = h.client.post("/api/spam/tests", json={"number_ids": [], "test_agent_id": tester, "reviewer_phone": REVIEWER})
    assert r.status_code == 422


def test_a_busy_number_is_skipped_when_its_turn_comes(h):
    n1, n2 = h.add_number(), h.add_number()
    lead = h.add_agent("agent-1", n1)
    tester = h.add_agent("tester", n2)
    run = start_run(h, [n1], tester)
    place(h, lead)  # n1 becomes busy after the run was queued
    h.settle()
    prog = h.client.get(f"/api/spam/tests/{run['id']}").json()
    assert prog["items"][0]["status"] == "failed" and "NUMBER_BUSY" in prog["items"][0]["detail"]
    assert prog["status"] == "done"
    assert [p["from"] for p in h.bolna.placed] == [ph(1)]  # only the lead call


def test_pasted_bolna_agent_id_creates_an_agent_and_unknown_ids_are_refused(h):
    n1 = h.add_number()
    r = h.client.post("/api/spam/tests", json={"number_ids": [n1], "bolna_agent_id": "pasted-tester",
                                               "reviewer_phone": REVIEWER})
    assert r.status_code == 201
    agents = h.client.get("/api/agents").json()["items"]
    made = next(a for a in agents if a["bolna_agent_id"] == "pasted-tester")
    assert made["number"] is None
    h.bolna.missing_agents.add("ghost")
    r = h.client.post("/api/spam/tests", json={"number_ids": [n1], "bolna_agent_id": "ghost", "reviewer_phone": REVIEWER})
    assert r.status_code == 404 and r.json()["code"] == "AGENT_NOT_FOUND"
    assert not any(a["bolna_agent_id"] == "ghost" for a in h.client.get("/api/agents").json()["items"])


def test_a_refused_test_call_fails_its_item_and_the_run_moves_on(h):
    n1, n2, n3 = (h.add_number() for _ in range(3))
    tester = h.add_agent("tester", n3)
    h.bolna.fail_agents.add("tester")
    run = start_run(h, [n1, n2], tester)
    h.settle()
    h.settle()
    prog = h.client.get(f"/api/spam/tests/{run['id']}").json()
    assert [i["status"] for i in prog["items"]] == ["failed", "failed"] and prog["status"] == "done"
    assert h.agent(tester)["agent"]["on_call"] is False
    assert h.numbers()[n1]["total_calls"] == 0


# 7 ------------------------------------------------------------------ verdicts
def test_a_spam_verdict_acts_like_mark_spam_and_ok_only_records(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    r = h.client.post(f"/api/numbers/{n2}/spam-result", json={"result": "ok"})
    assert r.status_code == 200 and r.json()["last_test_result"] == "ok" and r.json()["spam_flag"] is False
    r = h.client.post(f"/api/numbers/{n1}/spam-result", json={"result": "spam", "note": "Scam Likely"})
    assert r.status_code == 200 and r.json()["spam_flag"] is True and r.json()["last_test_result"] == "spam"
    assert h.agent(a)["agent"]["number"]["id"] == n2  # swapped automatically
    assert h.client.post(f"/api/numbers/{n1}/spam-result", json={"result": "maybe"}).status_code == 422


# 8 - 12 -------------------------------------------------------------- release
def enable_delete(h):
    h.app.state.settings.allow_bolna_number_delete = True


def set_db(h, nid, **fields):
    with h.db.session() as s:
        for k, v in fields.items():
            setattr(s.get(Number, nid), k, v)


def test_release_refuses_ineligible_numbers_and_still_releases_the_rest(h):
    enable_delete(h)
    n1, n2, n3, n4, n5, n6, n7 = (h.add_number() for _ in range(7))
    tester = h.add_agent("tester", n6)  # n6 stays unflagged
    holder = h.add_agent("agent-1", n1)
    place(h, holder)
    mark(h, n1)  # held: waits for the call to end
    mark(h, n2)
    run = start_run(h, [n2], tester)
    h.settle()  # a test call is now running from n2
    for n in (n3, n4, n5, n7):
        mark(h, n)
    set_db(h, n3, linked_in_bolna=True, bolna_linked_agent_id="inbound-1")
    set_db(h, n4, bolna_phone_number_id=None)
    set_db(h, n5, bolna_rented=False)

    res = by_id(release(h, [n1, n2, n3, n4, n5, n6, n7]))
    assert res[n1]["code"] == "NUMBER_HELD_BY_AGENT"
    assert res[n2]["code"] == "NUMBER_BUSY"
    assert res[n3]["code"] == "NUMBER_LINKED_IN_BOLNA"
    assert res[n4]["code"] == "NUMBER_ID_UNKNOWN"
    assert res[n5]["code"] == "NUMBER_NOT_RENTED"
    assert res[n6]["code"] == "NUMBER_NOT_MARKED_SPAM"
    assert res[n7]["ok"] and res[n7]["outcome"] == "released"
    assert all(not res[n]["ok"] for n in (n1, n2, n3, n4, n5, n6))
    assert h.bolna.deleted == [bid(7)]
    assert number(h, n1)["status"] != "released" and number(h, n6)["status"] != "released"


def test_release_success_goes_through_releasing_and_keeps_history(h):
    enable_delete(h)
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a))
    h.settle()
    mark(h, n1)  # swapped to n2 at once
    seen = {}
    real_delete = h.bolna.delete_phone_number

    async def spy(pid):
        with h.db.session() as s:  # read the database directly: the request is still in flight
            seen["status"] = s.get(Number, n1).status
        return await real_delete(pid)
    h.bolna.delete_phone_number = spy

    res = by_id(release(h, [n1]))[n1]
    assert res["ok"] and seen["status"] == "releasing"
    assert h.bolna.deleted == [bid(1)]
    assert number(h, n1)["status"] == "released"
    assert n1 not in h.numbers() and n1 not in spam_ids(h)  # gone from the pool and the spam list
    released = h.client.get("/api/spam/released").json()["items"]
    assert [r["id"] for r in released] == [n1] and released[0]["released_at"]
    assert h.client.get("/api/spam/list").json()["released_count"] == 1
    # history, counts and calls stay readable
    d = h.client.get(f"/api/numbers/{n1}").json()
    assert d["number"]["connected_count"] == 1 and len(d["calls"]) == 1 and len(d["agents"]) == 1
    ev = {e["event"]: e["detail"] for e in d["events"]}
    assert "release_started" in ev and "connected 1" in ev["release_succeeded"]
    assert ph(1) not in " ".join(ev.values())  # no full phone numbers in event details
    # never a candidate again
    with h.db.session() as s:
        assert services.pick_available(s, "vobiz", 5) is None  # n2 is held, n1 is released
    h.bolna.numbers.append(bnum(1))  # Bolna still lists it (the delete lagged): adding by hand is refused
    r = h.client.post("/api/numbers", json={"phone_number": ph(1)})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_RELEASED"


def test_a_failed_delete_restores_the_number_and_the_others_still_release(h):
    enable_delete(h)
    h.set_threshold(1)
    n1, n2, n3, n4 = (h.add_number() for _ in range(4))
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a))
    h.settle()  # n1 hits the limit, a moves to n2
    for n in (n1, n3, n4):
        mark(h, n)
    assert number(h, n1)["status"] == "limit_reached"
    h.bolna.delete_errors[bid(1)] = BolnaError("boom", 500)
    h.bolna.delete_errors[bid(3)] = BolnaError("Phone number not found", 404)
    res = by_id(release(h, [n1, n3, n4]))
    assert res[n1]["ok"] is False and res[n1]["code"] == "BOLNA_DELETE_FAILED"
    assert number(h, n1)["status"] == "limit_reached" and number(h, n1)["spam_flag"] is True
    assert res[n3]["ok"] and "no longer" in res[n3]["message"]  # not found = already gone
    assert number(h, n3)["status"] == "released"
    assert res[n4]["ok"] and number(h, n4)["status"] == "released"
    events = [e["event"] for e in h.client.get(f"/api/numbers/{n1}").json()["events"]]
    assert "release_failed" in events
    assert n1 in spam_ids(h)


def test_release_is_off_unless_switched_on(h):
    n1 = h.add_number()
    mark(h, n1)
    r = h.client.post("/api/spam/release", json={"ids": [n1]})
    assert r.status_code == 403 and r.json()["code"] == "BOLNA_DELETE_DISABLED"
    assert h.bolna.deleted == [] and number(h, n1)["status"] == "available"
    assert h.client.get("/api/spam/list").json()["delete_enabled"] is False


def test_releasing_twice_is_idempotent_and_a_released_number_never_returns(h):
    enable_delete(h)
    n1, n2 = h.add_number(), h.add_number()
    mark(h, n1)
    assert by_id(release(h, [n1]))[n1]["outcome"] == "released"
    again = by_id(release(h, [n1]))[n1]
    assert again["ok"] and again["outcome"] == "already_released"
    assert h.bolna.deleted == [bid(1)]
    # it cannot be marked, tested or assigned
    assert h.client.post(f"/api/numbers/{n1}/mark-spam", json={}).status_code == 404
    assert h.client.post("/api/agents", json={"bolna_agent_id": "x", "number_id": n1}).status_code == 404
    assert n1 not in {r["id"] for r in h.client.get("/api/spam/stack").json()["items"]}
    assert n2 in {r["id"] for r in h.client.get("/api/spam/stack").json()["items"]}


def test_a_release_interrupted_by_a_crash_is_restored_at_startup(h):
    from app import spam
    n1 = h.add_number()
    set_db(h, n1, status="releasing")
    assert spam.recover_releasing(h.db) == 1
    assert number(h, n1)["status"] == "available"


# 13 ------------------------------------------------------------------ sync interplay
def bnum(i, **kw):
    return {"id": bid(i), "phone_number": ph(i), "telephony_provider": "vobiz", "agent_id": None,
            "rented": True, **kw}


def sync(h):
    r = h.client.post("/api/sync/run")
    assert r.status_code == 200, r.text
    return r.json()


def test_sync_keeps_spam_flags_and_still_syncs_flagged_numbers(h):
    n1, n2 = h.add_number(), h.add_number()
    mark(h, n1, "note")
    h.bolna.numbers = [bnum(1, agent_id="inbound-9"), bnum(3)]  # n1 now linked, n2 gone
    sync(h)
    n = number(h, n1)
    assert n["spam_flag"] and n["spam_note"] == "note" and n["spam_marked_at"]
    assert n["linked_in_bolna"] is True  # synced normally
    assert number(h, n2)["missing_on_bolna"] is True
    h.bolna.numbers = []  # empty list is refused, flags untouched
    assert h.client.post("/api/sync/run").status_code == 502
    assert number(h, n1)["spam_flag"] is True


def test_sync_does_not_resurrect_released_numbers_or_flag_releasing_ones(h):
    enable_delete(h)
    n1, n2, n3 = (h.add_number() for _ in range(3))
    mark(h, n1)
    release(h, [n1])
    set_db(h, n2, status="releasing")  # mid-release
    h.bolna.numbers = [bnum(1), bnum(3)]  # Bolna still lists the released number; n2 is absent
    run = sync(h)
    assert run["released_listed_count"] == 1 and run["new_count"] == 0
    assert number(h, n1)["status"] == "released"
    assert h.client.get("/api/numbers", params={"status": "discovered"}).json()["total"] == 0
    n = number(h, n2)
    assert n["status"] == "releasing" and n["missing_on_bolna"] is False
    assert sync(h)["released_listed_count"] == 1
    assert h.client.get("/api/sync/status").json()["last_run"]["released_listed_count"] == 1
    with h.db.session() as s:
        assert s.scalar(select(Number).where(Number.status == "released")).phone_number == ph(1)


def test_sync_fills_the_bolna_id_for_existing_numbers(h):
    n1 = h.add_number()
    set_db(h, n1, bolna_phone_number_id=None, bolna_rented=None)
    assert h.client.get("/api/spam/list").json()["items"] == []
    h.bolna.numbers = [bnum(1)]
    sync(h)
    with h.db.session() as s:
        n = s.get(Number, n1)
        assert n.bolna_phone_number_id == bid(1) and n.bolna_rented is True


# 14 ------------------------------------------------------------------ eligibility
def test_flagged_and_released_numbers_are_never_auto_switch_candidates(h):
    def row(i, **kw):
        base = dict(phone_number=ph(900 + i), provider="vobiz", status="available", connected_count=0)
        base.update(kw)
        return Number(**base)

    with h.db.session() as s:
        s.add_all([row(1, spam_flag=True), row(2, status="released"), row(3, status="releasing")])
    with h.db.session() as s:
        assert services.pick_available(s, "vobiz", 5) is None
        s.add(row(4))
        s.flush()
        assert services.pick_available(s, "vobiz", 5).phone_number == ph(904)
