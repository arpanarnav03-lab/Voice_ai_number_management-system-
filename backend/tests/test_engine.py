"""Switching, call lock, eligibility, idempotency, concurrency, pool-empty, multi-trigger."""
import threading

from app import services
from app.models import Agent, Call
from sqlalchemy import select
from tests.conftest import RECIPIENT


def place(h, agent_id):
    res = h.trigger((agent_id, RECIPIENT))[0]
    assert res["ok"], res
    return res["execution_id"]


# ---------------------------------------------------------------- 2. switching
def test_fourth_connected_call_lands_on_second_number(h):
    h.set_threshold(3)
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    used = []
    for _ in range(4):
        eid = place(h, a)
        h.bolna.complete(eid, 18)
        h.settle()
        used.append(h.agent(a)["calls"][0]["number_id"])
    assert used == [n1, n1, n1, n2]
    nums = h.numbers()
    assert nums[n1]["status"] == "limit_reached" and nums[n1]["connected_count"] == 3
    assert nums[n2]["status"] == "in_use" and nums[n2]["connected_count"] == 1
    assert h.agent(a)["agent"]["number"]["id"] == n2
    assert nums[n3]["status"] == "available"


def test_voicemail_and_no_answer_never_advance_the_counter(h):
    h.set_threshold(2)
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    for rec in [
        {"status": "completed", "conversation_duration": 10, "answered_by_voice_mail": True,
         "telephony_data": {"hangup_reason": "voicemail_detected"}},
        {"status": "no-answer"}, {"status": "busy"}, {"status": "failed"},
        {"status": "completed", "conversation_duration": 0},
    ]:
        eid = place(h, a)
        h.bolna.set_execution(eid, rec)
        h.settle()
    nums = h.numbers()
    assert nums[n1]["connected_count"] == 0 and nums[n1]["total_calls"] == 5
    assert h.agent(a)["agent"]["number"]["id"] == n1
    assert h.agent(a)["summary"] == {"calls_placed": 5, "connected_calls": 0, "number_switches": 0}


def test_unclear_call_is_flagged_not_counted(h):
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    eid = place(h, a)
    h.bolna.set_execution(eid, {"status": "completed"})
    h.settle()
    call = h.agent(a)["calls"][0]
    assert call["result"] == "unclear" and call["needs_review"] is True
    assert h.numbers()[n1]["connected_count"] == 0
    assert h.agent(a)["agent"]["on_call"] is False  # settled, so the lock is released


def test_switch_only_to_same_provider(h):
    h.bolna.numbers.append({"phone_number": "+918000000050", "telephony_provider": "plivo", "agent_id": None})
    h.set_threshold(1)
    n1 = h.add_number()                       # vobiz
    n_plivo = h.add_number("+918000000050")   # plivo, added second
    n3 = h.add_number()                       # vobiz
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a))
    h.settle()
    assert h.agent(a)["agent"]["number"]["id"] == n3  # skipped the plivo number
    assert h.numbers()[n_plivo]["status"] == "available"


# ---------------------------------------------------------------- 3. call lock
def test_manual_change_during_a_call_is_refused(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    place(h, a)
    r = h.client.post(f"/api/agents/{a}/change-number", json={"number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "AGENT_ON_CALL"
    assert h.agent(a)["agent"]["number"]["id"] == n1
    assert h.agent(a)["agent"]["status"] == "on_call"


def test_threshold_mid_call_sets_pending_and_switches_after_last_call(h):
    h.set_threshold(1)
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    e1, e2 = place(h, a), place(h, a)           # two active calls
    h.bolna.complete(e1, 18)
    h.settle()
    ag = h.agent(a)["agent"]
    assert ag["number"]["id"] == n1 and ag["pending_switch"] is True and ag["on_call"] is True
    assert h.numbers()[n1]["status"] == "limit_reached"
    # still locked, and no new call can start on the over-limit number
    r = h.client.post(f"/api/agents/{a}/change-number", json={"number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "AGENT_ON_CALL"
    assert h.trigger((a, RECIPIENT))[0]["code"] == "SWITCH_PENDING"
    h.bolna.set_execution(e2, {"status": "busy"})   # the last call ends (not connected)
    h.settle()
    ag = h.agent(a)["agent"]
    assert ag["number"]["id"] == n2 and ag["pending_switch"] is False and ag["on_call"] is False


def test_two_active_calls_need_both_to_end(h):
    h.set_threshold(2)
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    e1, e2, e3 = place(h, a), place(h, a), place(h, a)
    h.bolna.complete(e1); h.bolna.complete(e2)
    h.settle()
    assert h.agent(a)["agent"]["number"]["id"] == n1      # e3 still active
    h.bolna.set_execution(e3, {"status": "no-answer"})
    h.settle()
    assert h.agent(a)["agent"]["number"]["id"] == n2


def test_stuck_call_times_out_and_unlocks(h):
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    place(h, a)
    h.settings.call_timeout_minutes = 0   # everything is "too old"
    h.settle()
    call = h.agent(a)["calls"][0]
    assert call["status"] == "unknown" and call["needs_review"] is True
    assert h.agent(a)["agent"]["on_call"] is False


# ---------------------------------------------------------------- 4. eligibility
def test_eligibility_rules(h):
    h.set_threshold(1)
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    b = h.add_agent("agent-2", n2)
    # in use by another agent
    r = h.client.post("/api/agents", json={"bolna_agent_id": "agent-3", "number_id": n1})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_IN_USE"
    assert r.json()["agent_name"] and r.json()["on_call"] is False
    place(h, b)
    r = h.client.post(f"/api/agents/{a}/change-number", json={"number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_IN_USE" and r.json()["on_call"] is True
    # limit reached
    h.bolna.complete("exec-1"); h.bolna.complete(h.bolna.placed[0]["execution_id"])
    h.settle()
    # agent b's call completes -> n2 hits the limit (1) and b moves to n3
    assert h.numbers()[n2]["status"] == "limit_reached"
    r = h.client.post("/api/agents", json={"bolna_agent_id": "agent-4", "number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_LIMIT_REACHED"
    # a free, under-limit number succeeds, and the old number returns to the pool
    h.set_threshold(5)
    h.client.post(f"/api/numbers/{n2}/reset")
    r = h.client.post(f"/api/agents/{a}/change-number", json={"number_id": n2})
    assert r.status_code == 200
    nums = h.numbers()
    assert nums[n1]["status"] == "available" and nums[n1]["used_by"] is None
    assert nums[n2]["status"] == "in_use"


def test_manual_release_keeps_count(h):
    n1, n2 = h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a)); h.settle()
    h.client.post(f"/api/agents/{a}/change-number", json={"number_id": n2})
    nums = h.numbers()
    assert nums[n1]["status"] == "available" and nums[n1]["connected_count"] == 1
    hist = h.agent(a)["number_history"]
    assert [x["reason"] for x in hist] == ["manual", "initial"]
    assert hist[1]["connected_calls"] == 1


def test_unknown_bolna_agent_is_rejected(h):
    n1 = h.add_number()
    h.bolna.missing_agents.add("ghost")
    r = h.client.post("/api/agents", json={"bolna_agent_id": "ghost", "number_id": n1})
    assert r.status_code == 404 and r.json()["code"] == "AGENT_NOT_FOUND"


# ---------------------------------------------------------------- 5. idempotency
def test_settling_twice_counts_once(h):
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    eid = place(h, a)
    h.bolna.complete(eid)
    h.settle()
    with h.db.session() as s:
        call_id = s.scalar(select(Call.id).where(Call.execution_id == eid))
        assert services.settle_call(s, call_id, {"status": "completed", "conversation_duration": 18}) is False
    h.settle()
    assert h.numbers()[n1]["connected_count"] == 1


# ---------------------------------------------------------------- 6. concurrency
def test_two_agents_cannot_take_the_same_number(h):
    n1 = h.add_number()
    results, barrier = [], threading.Barrier(2)

    def worker(name):
        barrier.wait()
        try:
            with h.db.session() as s:
                services.assign_new_agent(s, name, n1, name)
            results.append("ok")
        except Exception as e:
            results.append(getattr(e, "code", type(e).__name__))

    ts = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(results) == ["NUMBER_IN_USE", "ok"]


def test_call_start_and_switch_never_interleave(h):
    for i in range(15):
        n_old, n_new = h.add_number(), h.add_number()
        a = h.add_agent(f"agent-loop-{i}", n_old)
        out, barrier = {}, threading.Barrier(2)

        def start():
            barrier.wait()
            with h.db.session() as s:
                out["plan"] = services.begin_call(s, a, RECIPIENT)

        def switch():
            barrier.wait()
            try:
                with h.db.session() as s:
                    services.change_number(s, a, n_new)
                out["switched"] = True
            except Exception as e:
                out["switched"] = getattr(e, "code", "error")

        ts = [threading.Thread(target=start), threading.Thread(target=switch)]
        [t.start() for t in ts]; [t.join() for t in ts]
        with h.db.session() as s:
            call = s.scalar(select(Call).where(Call.agent_id == a))
            agent = s.get(Agent, a)
            if out["switched"] is True:
                assert call.number_id == n_new == agent.current_number_id   # call started after the switch
            else:
                assert out["switched"] == "AGENT_ON_CALL"                    # call started first -> locked
                assert call.number_id == n_old == agent.current_number_id


# ---------------------------------------------------------------- 7. pool empty
def test_pool_empty_blocks_calls_and_refills(h):
    h.set_threshold(1)
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    h.bolna.complete(place(h, a)); h.settle()
    ag = h.agent(a)["agent"]
    assert ag["status"] == "pool_empty" and ag["number"] is None
    res = h.trigger((a, RECIPIENT))[0]
    assert res["ok"] is False and res["code"] == "POOL_EMPTY"
    n2 = h.add_number()                                    # adding a number refills
    ag = h.agent(a)["agent"]
    assert ag["number"]["id"] == n2 and ag["status"] in ("ready", "near_limit")
    assert h.agent(a)["number_history"][0]["reason"] == "auto_refill"
    # resetting the exhausted number also refills a waiting agent
    h.bolna.complete(place(h, a)); h.settle()
    assert h.agent(a)["agent"]["status"] == "pool_empty"
    h.client.post(f"/api/numbers/{n1}/reset")
    assert h.agent(a)["agent"]["number"]["id"] == n1


def test_waiting_agents_are_refilled_oldest_first(h):
    h.set_threshold(1)
    n1, n2 = h.add_number(), h.add_number()
    a, b = h.add_agent("agent-a", n1), h.add_agent("agent-b", n2)
    h.bolna.complete(place(h, a)); h.settle()   # a waits first
    h.bolna.complete(place(h, b)); h.settle()   # b waits second
    h.add_number()                              # only one number arrives
    assert h.agent(a)["agent"]["number"] is not None
    assert h.agent(b)["agent"]["number"] is None


# ---------------------------------------------------------------- 9. multi-trigger
def test_multi_trigger_one_failure_does_not_stop_others(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    a, b, c = h.add_agent("agent-a", n1), h.add_agent("agent-b", n2), h.add_agent("agent-c", n3)
    h.bolna.fail_agents.add("agent-b")
    res = h.trigger((a, "+919999999901"), (b, "+919999999902"), (c, "+919999999903"))
    assert [r["ok"] for r in res] == [True, False, True]
    assert res[1]["code"] == "BOLNA_ERROR" and "concurrency" in res[1]["message"]
    assert h.agent(b)["agent"]["on_call"] is False           # failed call left no lock behind
    assert h.numbers()[n2]["total_calls"] == 0
    assert h.agent(a)["agent"]["on_call"] and h.agent(c)["agent"]["on_call"]
    assert h.numbers()[n1]["total_calls"] == 1


def test_trigger_blocked_without_number(h):
    n1 = h.add_number()
    a = h.add_agent("agent-1", n1)
    with h.db.session() as s:
        ag = s.get(Agent, a)
        services._release(s, ag, "released", 5)
    assert h.trigger((a, RECIPIENT))[0]["code"] == "AGENT_HAS_NO_NUMBER"


def test_threshold_validation_and_recount(h):
    r = h.client.put("/api/settings/threshold", json={"threshold": 0})
    assert r.status_code == 422 and r.json()["code"] == "INVALID_THRESHOLD"
    assert h.client.get("/api/settings").json() == {"threshold": 5}
