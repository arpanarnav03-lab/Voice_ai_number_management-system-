"""Idle-first swapping and automatic numbers for new agents."""
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app import config, services
from app.config import Settings
from app.models import Agent, Assignment, Call, Number, utcnow
from tests.conftest import RECIPIENT

ROOT = Path(__file__).resolve().parent.parent


def ph(i):
    return f"+91800{i:07d}"


def place(h, agent_id):
    res = h.trigger((agent_id, RECIPIENT))[0]
    assert res["ok"], res
    return res["execution_id"]


def make_idle(h, agent_id, hours=48):
    """Pretend the agent's assignment started, and its calls were placed, `hours` ago."""
    past = utcnow() - timedelta(hours=hours)
    with h.db.session() as s:
        for a in s.scalars(select(Assignment).where(Assignment.agent_id == agent_id, Assignment.ended_at.is_(None))):
            a.started_at = past
        for c in s.scalars(select(Call).where(Call.agent_id == agent_id)):
            c.placed_at = past


def number_of(h, agent_id):
    n = h.agent(agent_id)["agent"]["number"]
    return n["id"] if n else None


def num(h, nid):
    with h.db.session() as s:
        n = s.get(Number, nid)
        return {"status": n.status, "connected": n.connected_count, "total": n.total_calls,
                "taken": n.taken_from_pool_at, "stack": n.stack_order, "spam": n.spam_flag}


def set_num(h, nid, **kw):
    with h.db.session() as s:
        for k, v in kw.items():
            setattr(s.get(Number, nid), k, v)


def set_agent(h, aid, **kw):
    with h.db.session() as s:
        for k, v in kw.items():
            setattr(s.get(Agent, aid), k, v)


def history(h, agent_id):
    return h.agent(agent_id)["number_history"]  # newest first


def mark_spam(h, nid):
    r = h.client.post(f"/api/numbers/{nid}/mark-spam", json={})
    assert r.status_code == 200, r.text


def new_agent(h, bolna_id, number_id=None, expect=201):
    body = {"bolna_agent_id": bolna_id} if number_id is None else {"bolna_agent_id": bolna_id, "number_id": number_id}
    r = h.client.post("/api/agents", json=body)
    assert r.status_code == expect, r.text
    return r.json()


# 1 ------------------------------------------------------------------ new agents
def test_a_new_agent_gets_the_first_suitable_pool_number_automatically(h):
    n1, n2 = h.add_number(), h.add_number()
    a = new_agent(h, "agent-1")
    assert a["number"]["id"] == n1 and a["status"] == "ready"
    assert history(h, a["id"])[0]["reason"] == "initial"
    b = new_agent(h, "agent-2")
    assert b["number"]["id"] == n2


def test_with_an_empty_pool_the_new_agent_waits_and_is_filled_later(h):
    n1 = h.add_number()
    lender = h.add_agent("old-agent", n1)
    make_idle(h, lender)
    a = new_agent(h, "agent-1")
    assert a["status"] == "pool_empty" and a["number"] is None
    assert number_of(h, lender) == n1  # never taken from an idle agent for a new agent
    n2 = h.add_number()
    assert number_of(h, a["id"]) == n2  # auto-refill
    assert history(h, a["id"])[0]["reason"] == "auto_refill"


def test_the_automatic_choice_follows_the_normal_pool_rules(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    mark_spam(h, n1)
    set_num(h, n2, missing_on_bolna=True)
    assert new_agent(h, "agent-1")["number"]["id"] == n3


# 2 ------------------------------------------------------------------ chosen number
def test_choosing_a_specific_number_works_as_before(h):
    h.set_threshold(1)
    n1, n2, n3, n4 = (h.add_number() for _ in range(4))
    assert new_agent(h, "agent-1", n3)["number"]["id"] == n3
    r = h.client.post("/api/agents", json={"bolna_agent_id": "agent-2", "number_id": n3})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_IN_USE"
    set_num(h, n2, connected_count=1, status="limit_reached")
    r = h.client.post("/api/agents", json={"bolna_agent_id": "agent-2", "number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_LIMIT_REACHED"
    mark_spam(h, n4)
    r = h.client.post("/api/agents", json={"bolna_agent_id": "agent-2", "number_id": n4})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_MARKED_SPAM"
    assert new_agent(h, "agent-2", n1)["number"]["id"] == n1


# 3 ------------------------------------------------------------------ threshold swap
def test_a_threshold_swap_takes_an_idle_agents_number_before_the_pool(h):
    h.set_threshold(1)
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver = h.add_agent("busy-agent", n1)
    lender = h.add_agent("idle-agent", n2)
    make_idle(h, lender)
    before = num(h, n2)
    h.bolna.complete(place(h, receiver))
    h.settle()
    assert number_of(h, receiver) == n2
    assert number_of(h, lender) is None
    assert num(h, n1)["status"] == "limit_reached"
    assert num(h, n3)["status"] == "available" and h.numbers()[n3]["used_by"] is None  # pool untouched
    after = num(h, n2)
    assert (after["connected"], after["total"]) == (before["connected"], before["total"])
    assert after["status"] == "in_use" and after["taken"] == before["taken"]
    rh, lh = history(h, receiver), history(h, lender)
    assert rh[0]["reason"] == "auto_idle_swap" and rh[1]["end_reason"] == "auto_threshold"
    assert lh[0]["end_reason"] == "taken_idle"
    d = h.client.get(f"/api/numbers/{n2}").json()
    assert len(d["moves"]) == 1 and "idle-agent" in d["moves"][0]["detail"] and "busy-agent" in d["moves"][0]["detail"]
    assert "+91" not in d["moves"][0]["detail"]  # no phone numbers in the event


# 4 ------------------------------------------------------------------ spam swap
def test_a_spam_swap_takes_an_idle_agents_number_before_the_pool(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver = h.add_agent("agent-1", n1)
    lender = h.add_agent("agent-2", n2)
    make_idle(h, lender)
    mark_spam(h, n1)
    assert number_of(h, receiver) == n2 and number_of(h, lender) is None
    assert history(h, receiver)[0]["reason"] == "auto_spam"
    assert history(h, lender)[0]["end_reason"] == "taken_idle"
    assert h.numbers()[n3]["used_by"] is None


# 5 ------------------------------------------------------------------ fallbacks
def test_without_a_lender_the_pool_is_used_and_without_either_the_agent_waits(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver = h.add_agent("agent-1", n1)
    h.add_agent("agent-2", n2)  # freshly assigned: not idle
    mark_spam(h, n1)
    assert number_of(h, receiver) == n3
    mark_spam(h, n3)
    assert h.agent(receiver)["agent"]["status"] == "pool_empty"


# 6 ------------------------------------------------------------------ ranking
def test_the_lender_with_the_fewest_connected_calls_wins_then_the_latest_taken_then_the_lowest_id(h):
    ns = [h.add_number() for _ in range(5)]
    receiver = h.add_agent("receiver", ns[0])
    l1, l2, l3, l4 = (h.add_agent(f"lender-{i}", ns[i]) for i in range(1, 5))
    for a in (l1, l2, l3, l4):
        make_idle(h, a)
    now = utcnow()
    set_num(h, ns[1], connected_count=1, taken_from_pool_at=now - timedelta(hours=1))   # most calls: loses
    set_num(h, ns[2], connected_count=0, taken_from_pool_at=now - timedelta(hours=30))  # taken earlier
    set_num(h, ns[3], connected_count=0, taken_from_pool_at=now - timedelta(hours=10))  # taken latest: wins
    set_num(h, ns[4], connected_count=0, taken_from_pool_at=now - timedelta(hours=10))  # same time, higher id
    mark_spam(h, ns[0])
    assert number_of(h, receiver) == ns[3]


def test_a_full_tie_goes_to_the_lowest_agent_id(h):
    ns = [h.add_number() for _ in range(3)]
    receiver = h.add_agent("receiver", ns[0])
    l1, l2 = h.add_agent("lender-1", ns[1]), h.add_agent("lender-2", ns[2])
    same = utcnow() - timedelta(hours=40)
    for a, n in ((l1, ns[1]), (l2, ns[2])):
        make_idle(h, a)
        set_num(h, n, taken_from_pool_at=same)
    mark_spam(h, ns[0])
    assert number_of(h, receiver) == ns[1] and number_of(h, l1) is None and number_of(h, l2) == ns[2]


# 7 ------------------------------------------------------------------ eligibility
def _active_old_call(h, agent_id, number_id):
    with h.db.session() as s:
        s.add(Call(agent_id=agent_id, number_id=number_id, recipient=RECIPIENT, status="ringing",
                   placed_at=utcnow() - timedelta(hours=48)))


def _recent_call(h, agent_id, number_id):
    with h.db.session() as s:
        s.add(Call(agent_id=agent_id, number_id=number_id, recipient=RECIPIENT, status="completed",
                   placed_at=utcnow() - timedelta(hours=1), settled_at=utcnow(), connected=False))


def _test_run(h, agent_id, number_id):
    tester = h.add_agent("tester", h.add_number())
    r = h.client.post("/api/spam/tests", json={"number_ids": [number_id], "test_agent_id": tester,
                                               "reviewer_phone": "+919999999902"})
    assert r.status_code == 201, r.text


def _cooldown(h, agent_id, number_id):
    with h.db.session() as s:
        s.add(Assignment(agent_id=agent_id, number_id=number_id, reason="manual", end_reason="taken_idle",
                         started_at=utcnow() - timedelta(hours=50), ended_at=utcnow() - timedelta(hours=1)))


EXCLUDERS = {
    "active call": _active_old_call,
    "pending switch": lambda h, a, n: set_agent(h, a, pending_switch=True, pending_switch_reason="limit"),
    "recent call": _recent_call,
    "never lend": lambda h, a, n: set_agent(h, a, never_lend=True),
    "flagged number": lambda h, a, n: set_num(h, n, spam_flag=True),
    "over threshold": lambda h, a, n: set_num(h, n, connected_count=5),
    "missing number": lambda h, a, n: set_num(h, n, missing_on_bolna=True),
    "linked in bolna": lambda h, a, n: set_num(h, n, linked_in_bolna=True, bolna_linked_agent_id="inbound-1"),
    "no auto-switch": lambda h, a, n: set_num(h, n, allow_auto_switch=False),
    "other provider": lambda h, a, n: set_num(h, n, provider="plivo"),
    "in a test run": _test_run,
    "awaiting result": lambda h, a, n: set_num(h, n, test_awaiting_since=utcnow()),
    "recently moved": _cooldown,
}


@pytest.mark.parametrize("case", list(EXCLUDERS))
def test_each_rule_keeps_a_lender_out_and_the_pool_is_used_instead(h, case):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver = h.add_agent("receiver", n1)
    lender = h.add_agent("lender", n2)
    make_idle(h, lender)
    EXCLUDERS[case](h, lender, n2)
    mark_spam(h, n1)
    assert number_of(h, lender) == n2, case
    assert number_of(h, receiver) == n3, case


def test_a_never_called_agent_assigned_recently_is_not_idle(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver = h.add_agent("receiver", n1)
    lender = h.add_agent("lender", n2)
    make_idle(h, lender, hours=2)  # assigned 2 hours ago, never called
    mark_spam(h, n1)
    assert number_of(h, lender) == n2 and number_of(h, receiver) == n3


def test_the_receiver_is_never_its_own_lender(h):
    n1 = h.add_number()
    receiver = h.add_agent("receiver", n1)
    make_idle(h, receiver)
    mark_spam(h, n1)
    assert h.agent(receiver)["agent"]["status"] == "pool_empty"


# 8 ------------------------------------------------------------------ the lender afterwards
def test_the_lender_waits_as_number_taken_until_a_person_assigns_a_number(h):
    n1, n2 = h.add_number(), h.add_number()
    receiver = h.add_agent("receiver-agent", n1)
    lender = h.add_agent("lender", n2)
    make_idle(h, lender)
    mark_spam(h, n1)
    row = h.agent(lender)["agent"]
    assert row["status"] == "number_taken" and row["number"] is None
    assert row["number_taken"]["by"] == {"id": receiver, "name": "Agent receiver-agent"}
    assert "because this agent was idle" in row["number_taken"]["reason"]

    n3 = h.add_number()  # a number added to the pool does not refill it
    h.client.post(f"/api/numbers/{n1}/not-spam")
    assert number_of(h, lender) is None
    res = h.trigger((lender, RECIPIENT))[0]
    assert res["ok"] is False and res["code"] == "NUMBER_TAKEN"
    assert "moved to Agent receiver-agent" in res["message"] and "because this agent was idle" in res["message"]
    r = h.client.post("/api/spam/tests", json={"number_ids": [n3], "test_agent_id": lender, "reviewer_phone": "+919999999902"})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_TAKEN"

    r = h.client.post(f"/api/agents/{lender}/change-number", json={"number_id": n3})  # a person assigns
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["number_taken"] is None


def test_a_reset_or_a_sync_never_refills_a_number_taken_agent(h):
    h.set_threshold(1)
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver, lender = h.add_agent("receiver", n1), h.add_agent("lender", n2)
    make_idle(h, lender)
    h.bolna.complete(place(h, receiver))
    h.settle()
    assert h.agent(lender)["agent"]["status"] == "number_taken"
    h.client.post(f"/api/numbers/{n1}/reset")
    h.client.post("/api/sync/run")
    assert number_of(h, lender) is None


def test_the_automatic_option_by_a_person_also_gives_a_number_taken_agent_a_number(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver, lender = h.add_agent("receiver", n1), h.add_agent("lender", n2)
    make_idle(h, lender)
    mark_spam(h, n1)
    assert number_of(h, receiver) == n2
    out = new_agent(h, "lender")  # the Agents-tab form with "Assign automatically from the pool"
    assert out["number"]["id"] == n3 and out["number_taken"] is None


# 9 ------------------------------------------------------------------ atomicity
def test_a_call_that_starts_on_the_lender_after_selection_stops_the_move(h, monkeypatch):
    ns = [h.add_number() for _ in range(4)]
    receiver = h.add_agent("receiver", ns[0])
    l1, l2 = h.add_agent("lender-1", ns[1]), h.add_agent("lender-2", ns[2])
    for a in (l1, l2):
        make_idle(h, a)
    real = services.lender_candidates
    order = {}

    def racing(s, rec, provider, threshold):
        picked = real(s, rec, provider, threshold)
        assert {a.id for a in picked} == {l1, l2}
        first = picked[0]
        order["first"], order["second"] = first.id, picked[1].id
        # a call starts on the first choice between selection and the move
        s.add(Call(agent_id=first.id, number_id=first.current_number_id, recipient=RECIPIENT, status="placing"))
        s.flush()
        return picked
    monkeypatch.setattr(services, "lender_candidates", racing)
    first_num = number_of(h, l2)  # l2 was taken from the pool last, so it ranks first
    second_num = number_of(h, l1)
    mark_spam(h, ns[0])
    assert order == {"first": l2, "second": l1}
    assert number_of(h, l2) == first_num  # the busy one kept its number
    assert number_of(h, receiver) == second_num and number_of(h, l1) is None  # the next candidate lent


def test_two_receivers_never_take_the_same_lender(h):
    ns = [h.add_number() for _ in range(3)]
    r1, r2 = h.add_agent("receiver-1", ns[0]), h.add_agent("receiver-2", ns[1])
    lender = h.add_agent("lender", ns[2])
    make_idle(h, lender)
    out = {}
    ts = [threading.Thread(target=lambda n=n: out.update({n: h.client.post(f"/api/numbers/{n}/mark-spam", json={})}))
          for n in (ns[0], ns[1])]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(r.status_code == 200 for r in out.values())
    got = [number_of(h, r1), number_of(h, r2)]
    assert got.count(ns[2]) == 1  # exactly one receiver got the lender's number
    assert number_of(h, lender) is None
    with h.db.session() as s:
        open_rows = s.scalars(select(Assignment).where(Assignment.number_id == ns[2], Assignment.ended_at.is_(None))).all()
        assert len(open_rows) == 1


def test_locking_a_pair_in_opposite_directions_does_not_deadlock(h):
    a, b = h.add_agent("a", h.add_number()), h.add_agent("b", h.add_number())
    done, errors = [], []

    def lock(x, y):
        try:
            for _ in range(10):
                with h.db.session() as s:
                    services._lock_pair(s, x, y)
            done.append((x, y))
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=lock, args=(a, b)), threading.Thread(target=lock, args=(b, a))]
    [t.start() for t in ts]
    [t.join(timeout=20) for t in ts]
    assert not errors and len(done) == 2 and not any(t.is_alive() for t in ts)


# 10 ----------------------------------------------------------------- manual changes
def test_manual_changes_never_take_from_another_agent(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    me, idle = h.add_agent("me", n1), h.add_agent("idle", n2)
    make_idle(h, idle)
    r = h.client.post(f"/api/agents/{me}/change-number", json={"number_id": n2})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_IN_USE"
    assert number_of(h, idle) == n2
    r = h.client.post(f"/api/agents/{me}/change-number", json={"number_id": n3})
    assert r.status_code == 200 and number_of(h, idle) == n2
    assert history(h, me)[0]["reason"] == "manual"


# 11 ----------------------------------------------------------------- deferred swaps
def test_a_deferred_swap_judges_the_lenders_when_it_finally_happens(h):
    h.set_threshold(1)
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver = h.add_agent("receiver", n1)
    lender = h.add_agent("lender", n2)  # not idle yet
    e1, e2 = place(h, receiver), place(h, receiver)
    h.bolna.complete(e1)
    h.settle()
    assert h.agent(receiver)["agent"]["pending_switch"] is True and number_of(h, receiver) == n1
    make_idle(h, lender)  # becomes idle while the swap waits
    h.bolna.complete(e2)
    h.settle()
    assert number_of(h, receiver) == n2 and number_of(h, lender) is None
    assert h.numbers()[n3]["used_by"] is None


# 12 ----------------------------------------------------------------- taken_from_pool_at
def test_taken_from_pool_at_is_set_kept_and_cleared(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    assert num(h, n1)["taken"] is None
    receiver = h.add_agent("receiver", n1)
    assert num(h, n1)["taken"] is not None
    lender = h.add_agent("lender", n2)
    make_idle(h, lender)
    kept = num(h, n2)["taken"]
    mark_spam(h, n1)  # idle move
    assert num(h, n2)["taken"] == kept  # unchanged by the move
    assert num(h, n1)["taken"] is None  # back in the pool
    r = h.client.post(f"/api/agents/{receiver}/change-number", json={"number_id": n3})
    assert r.status_code == 200
    assert num(h, n2)["taken"] is None and num(h, n3)["taken"] is not None
    assert h.client.get(f"/api/numbers/{n3}").json()["number"]["taken_from_pool_at"]


def test_the_migration_backfills_taken_from_pool_at(tmp_path):
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path}/m.db")
    command.upgrade(cfg, "0005")
    c = sqlite3.connect(tmp_path / "m.db")
    c.execute("INSERT INTO numbers (id, phone_number, provider, status, connected_count, total_calls, added_at) "
              "VALUES (1, ?, 'vobiz', 'in_use', 0, 0, '2026-01-01 00:00:00')", (ph(1),))
    c.execute("INSERT INTO numbers (id, phone_number, provider, status, connected_count, total_calls, added_at) "
              "VALUES (2, ?, 'vobiz', 'available', 0, 0, '2026-01-01 00:00:00')", (ph(2),))
    c.execute("INSERT INTO agents (id, bolna_agent_id, name, current_number_id, pending_switch, pool_empty, created_at) "
              "VALUES (1, 'a1', 'A', 1, 0, 0, '2026-01-01 00:00:00')")
    c.execute("INSERT INTO assignments (agent_id, number_id, started_at, ended_at, reason, connected_calls) "
              "VALUES (1, 1, '2026-02-01 00:00:00', '2026-02-02 00:00:00', 'initial', 0)")
    c.execute("INSERT INTO assignments (agent_id, number_id, started_at, reason, connected_calls) "
              "VALUES (1, 1, '2026-03-05 10:00:00', 'manual', 0)")
    c.commit()
    c.close()
    command.upgrade(cfg, "head")
    c = sqlite3.connect(tmp_path / "m.db")
    vals = dict(c.execute("SELECT id, taken_from_pool_at FROM numbers"))
    assert str(vals[1]).startswith("2026-03-05 10:00:00") and vals[2] is None
    assert c.execute("SELECT never_lend, number_taken_at FROM agents").fetchone() == (0, None)


# 13 ----------------------------------------------------------------- the setting
def test_idle_first_off_gives_the_old_pool_only_behaviour(h):
    h.app.state.db.session_info["alloc"].idle_first = False
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver, lender = h.add_agent("receiver", n1), h.add_agent("lender", n2)
    make_idle(h, lender)
    mark_spam(h, n1)
    assert number_of(h, receiver) == n3 and number_of(h, lender) == n2


@pytest.mark.parametrize("name,raw", [
    ("IDLE_FIRST_ENABLED", "maybe"), ("IDLE_FIRST_ENABLED", "2"),
    ("IDLE_HOURS", "0.5"), ("IDLE_HOURS", "abc"), ("IDLE_HOURS", "0"), ("IDLE_HOURS", "nan"),
    ("NUMBER_MOVE_COOLDOWN_HOURS", "-1"), ("NUMBER_MOVE_COOLDOWN_HOURS", "soon"),
])
def test_invalid_settings_stop_startup_with_a_clear_message(monkeypatch, name, raw):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOLNA_MODE", "fake")
    monkeypatch.setenv(name, raw)
    with pytest.raises(SystemExit) as e:
        Settings.from_env()
    assert name in str(e.value)


def test_valid_settings_and_defaults(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOLNA_MODE", "fake")
    for n in ("IDLE_FIRST_ENABLED", "IDLE_HOURS", "NUMBER_MOVE_COOLDOWN_HOURS"):
        monkeypatch.delenv(n, raising=False)
    s = Settings.from_env()
    assert (s.idle_first_enabled, s.idle_hours, s.number_move_cooldown_hours) == (True, 24.0, 24.0)
    monkeypatch.setenv("IDLE_FIRST_ENABLED", "false")
    monkeypatch.setenv("IDLE_HOURS", "6")
    monkeypatch.setenv("NUMBER_MOVE_COOLDOWN_HOURS", "0")
    s = Settings.from_env()
    assert (s.idle_first_enabled, s.idle_hours, s.number_move_cooldown_hours) == (False, 6.0, 0.0)


# 14 ----------------------------------------------------------------- nothing else changes
def test_a_moved_number_stays_in_the_pool_and_in_the_stack_where_it_was(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    receiver, lender = h.add_agent("receiver", n1), h.add_agent("lender", n2)
    make_idle(h, lender)
    before = num(h, n2)["stack"]
    mark_spam(h, n1)
    assert number_of(h, receiver) == n2
    assert num(h, n2)["stack"] == before and num(h, n2)["status"] == "in_use"
    stack = {r["id"] for r in h.client.get("/api/spam/stack").json()["items"]}
    pool = {n["id"] for n in h.client.get("/api/numbers").json()["items"] if not n["spam_flag"]}
    assert stack == pool and n2 in stack
    assert h.client.get("/api/spam/stack/health").json() == {
        "missing_from_stack": 0, "unexpected_in_stack": 0, "duplicate_positions": 0}
    assert {n["id"] for n in h.client.get("/api/spam/list").json()["items"]} == {n1}


def test_never_lend_toggle_and_last_call_are_on_the_agent_row(h):
    a = h.add_agent("agent-1", h.add_number())
    row = h.agent(a)["agent"]
    assert row["never_lend"] is False and row["last_call_at"] is None
    r = h.client.put(f"/api/agents/{a}/never-lend", json={"never_lend": True})
    assert r.status_code == 200 and r.json()["never_lend"] is True
    place(h, a)
    assert h.agent(a)["agent"]["last_call_at"] is not None
