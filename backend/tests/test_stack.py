"""The test stack: ordering, rotation, suggestions, no automatic calls, no cap."""
import sqlite3
import threading
from datetime import timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app import services, spam
from app.config import Settings
from app.models import Agent, Call, Number, SpamTestItem, SpamTestRun, utcnow
from tests.conftest import RECIPIENT

REVIEWER = "+919999999902"


def ph(i):
    return f"+91800{i:07d}"


def bnum(i, **kw):
    return {"id": f"{i:032x}", "phone_number": ph(i), "telephony_provider": "vobiz", "agent_id": None,
            "rented": True, **kw}


def stack(h):
    return [r["id"] for r in h.client.get("/api/spam/stack").json()["items"]]


def stack_rows(h):
    return {r["id"]: r for r in h.client.get("/api/spam/stack").json()["items"]}


def verdict(h, nid, result, expect=200):
    r = h.client.post(f"/api/numbers/{nid}/spam-result", json={"result": result})
    assert r.status_code == expect, r.text
    return r.json()


def start_run(h, ids, agent, expect=201):
    r = h.client.post("/api/spam/tests", json={"number_ids": ids, "test_agent_id": agent, "reviewer_phone": REVIEWER})
    assert r.status_code == expect, r.text
    return r.json()


def pick_next(h):
    r = h.client.post("/api/spam/stack/select")
    assert r.status_code == 200, r.text
    return r.json()


def place(h, agent_id):
    res = h.trigger((agent_id, RECIPIENT))[0]
    assert res["ok"], res
    return res["execution_id"]


def db_set(h, nid, **fields):
    with h.db.session() as s:
        for k, v in fields.items():
            setattr(s.get(Number, nid), k, v)


def sync(h):
    r = h.client.post("/api/sync/run")
    assert r.status_code == 200, r.text
    return r.json()


def finish_test_call(h, k=0):
    h.settle()  # places the next queued test call
    h.bolna.complete(h.bolna.placed[k]["execution_id"], 18)
    h.settle()


def counts(h):
    with h.db.session() as s:
        return (s.scalar(select(func.count()).select_from(SpamTestRun)),
                s.scalar(select(func.count()).select_from(SpamTestItem)),
                s.scalar(select(func.count()).select_from(Call)))


# 1 ------------------------------------------------------------------ joining
def test_numbers_enter_the_stack_at_the_top_however_they_join(h):
    n1, n2 = h.add_number(), h.add_number()
    assert stack(h) == [n2, n1]  # added by hand: newest on top

    h.bolna.numbers = [bnum(1), bnum(2), bnum(50), bnum(51), bnum(52, agent_id="inbound-1")]
    sync(h)
    assert stack(h) == [n2, n1]  # discovered numbers are not in the stack
    disc = {n["phone_number"]: n["id"] for n in h.client.get("/api/numbers", params={"status": "discovered"}).json()["items"]}
    h.client.post(f"/api/numbers/{disc[ph(50)]}/ignore")
    assert stack(h) == [n2, n1]  # nor are ignored ones

    r = h.client.post(f"/api/numbers/{disc[ph(51)]}/approve")  # approved from Discovered
    assert r.status_code == 200
    assert stack(h) == [disc[ph(51)], n2, n1]
    h.client.post(f"/api/numbers/{disc[ph(50)]}/restore")
    h.client.post("/api/numbers/approve", json={"ids": [disc[ph(50)], disc[ph(52)]], "confirm_linked": True})
    top = stack(h)[:2]
    assert set(top) == {disc[ph(50)], disc[ph(52)]} and stack(h)[2:] == [disc[ph(51)], n2, n1]


def test_auto_approved_numbers_enter_at_the_top(h):
    n1 = h.add_number()
    h.app.state.settings.sync_auto_approve = True
    h.bolna.numbers = [bnum(1), bnum(60), bnum(61)]
    sync(h)
    order = stack(h)
    assert order[2] == n1 and len(order) == 3
    with h.db.session() as s:
        first_two = [s.get(Number, i).phone_number for i in order[:2]]
    assert first_two == [ph(60), ph(61)]  # kept in the order Bolna listed them


# 2 ------------------------------------------------------------------ rotation
def test_mark_ok_rotates_to_the_bottom_in_a_stable_order_without_collisions(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    assert stack(h) == [n3, n2, n1]
    verdict(h, n3, "ok")
    assert stack(h) == [n2, n1, n3]
    assert stack_rows(h)[n3]["last_test_result"] == "ok"
    for _ in range(3):  # rotate everything round several times
        for nid in list(stack(h)):
            verdict(h, nid, "ok")
    assert stack(h) == [n2, n1, n3]
    with h.db.session() as s:
        pos = [n.stack_order for n in s.scalars(select(Number))]
    assert len(pos) == len(set(pos)) == 3 and None not in pos


# 3 ------------------------------------------------------------------ spam leaves
def test_marking_spam_removes_the_number_but_keeps_swap_and_lock_behaviour(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    a = h.add_agent("agent-1", n1)
    verdict(h, n1, "spam")
    assert n1 not in stack(h) and set(stack(h)) == {n2, n3}
    assert n1 in {r["id"] for r in h.client.get("/api/spam/list").json()["items"]}
    assert h.agent(a)["agent"]["number"]["id"] in (n2, n3)  # idle agent swapped at once
    # with an active call the swap still waits
    a2 = h.add_agent("agent-2", n2) if h.agent(a)["agent"]["number"]["id"] != n2 else h.add_agent("agent-2", n3)
    held = h.agent(a2)["agent"]["number"]["id"]
    place(h, a2)
    verdict(h, held, "spam")
    assert h.agent(a2)["agent"]["number"]["id"] == held and h.agent(a2)["agent"]["pending_switch_reason"] == "spam"
    assert held not in stack(h)


# 4 ------------------------------------------------------------------ not spam
def test_not_spam_returns_to_the_top_with_the_result_cleared(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    verdict(h, n1, "ok")
    verdict(h, n1, "spam")
    assert stack_rows(h).get(n1) is None
    h.client.post(f"/api/numbers/{n1}/not-spam")
    assert stack(h)[0] == n1
    row = stack_rows(h)[n1]
    assert row["last_test_result"] is None and row["spam_flag"] is False


# 5 ------------------------------------------------------------------ select next
def test_select_next_skips_unavailable_numbers_reports_why_and_changes_nothing(h):
    ns = [h.add_number() for _ in range(6)]  # stack top-first: ns[5] ... ns[0]
    n1, n2, n3, n4, n5, n6 = ns
    a = h.add_agent("agent-1", n6)
    place(h, a)  # n6 (top) is on a call
    db_set(h, n5, test_awaiting_since=utcnow())  # awaiting a result
    db_set(h, n4, missing_on_bolna=True)
    before = (stack(h), counts(h))
    out = pick_next(h)
    assert out["ids"] == [n3]  # one number: the first that can be tested
    assert {(s["number_id"], s["code"]) for s in out["skipped"]} == {
        (n6, "NUMBER_BUSY"), (n5, "AWAITING_RESULT"), (n4, "NUMBER_MISSING_ON_BOLNA")}
    assert all(s["message"] for s in out["skipped"])
    assert (stack(h), counts(h)) == before and h.bolna.placed == [h.bolna.placed[0]]  # only the lead call
    rows = stack_rows(h)
    assert rows[n3]["eligible"] and not rows[n6]["eligible"] and rows[n6]["reason"]["code"] == "NUMBER_BUSY"
    assert h.client.post("/api/spam/stack/select", json={"count": 0}).status_code == 200  # a count is ignored


# 6 ------------------------------------------------------------------ never automatic
def test_nothing_creates_a_test_run_except_an_explicit_request(h):
    n1, n2 = h.add_number(), h.add_number()
    h.add_agent("agent-1", n1)
    h.bolna.numbers = [bnum(1), bnum(2), bnum(70)]
    h.app.state.settings.sync_auto_approve = True
    pick_next(h)
    sync(h)
    sync(h)
    db_set(h, n2, test_awaiting_since=utcnow() - timedelta(hours=48))
    spam.expire_awaiting(h.db, h.settings)
    for _ in range(3):
        h.settle()  # the poller pass: advances queued tests only, there are none
    verdict(h, n1, "ok")
    h.client.post(f"/api/numbers/{n2}/clear-test")
    assert counts(h)[:2] == (0, 0) and h.bolna.placed == []


# 7 ------------------------------------------------------------------ no cap
def test_a_run_of_25_numbers_places_25_sequential_calls(h):
    assert Settings().spam_test_max_per_run == 0  # no cap by default
    ids = [h.add_number() for _ in range(25)]
    tester = h.add_agent("tester", h.add_number())
    run = start_run(h, ids, tester)
    assert len(run["items"]) == 25 and run["refused"] == []
    for k in range(25):
        h.settle()
        assert len(h.bolna.placed) == k + 1  # exactly one at a time
        h.bolna.complete(h.bolna.placed[k]["execution_id"], 18)
    h.settle()
    assert [p["from"] for p in h.bolna.placed] == [ph(i) for i in range(1, 26)]
    prog = h.client.get(f"/api/spam/tests/{run['id']}").json()
    assert prog["status"] == "done" and all(i["status"] == "done" for i in prog["items"])
    assert all(r["awaiting_result"] for r in h.client.get("/api/spam/stack").json()["items"] if r["id"] in ids)


# 8 ------------------------------------------------------------------ allocation unaffected
def test_allocation_ignores_test_results_but_never_picks_spam(h):
    h.set_threshold(1)
    n1, n2, n3, n4 = (h.add_number() for _ in range(4))
    a = h.add_agent("agent-1", n1)
    verdict(h, n3, "ok")  # tested OK: no preference
    db_set(h, n2, test_awaiting_since=utcnow())  # untested, awaiting: still allocatable
    verdict(h, n4, "spam")
    h.bolna.complete(place(h, a))
    h.settle()
    assert h.agent(a)["agent"]["number"]["id"] == n2  # first available by date added, as before
    a2 = h.add_agent("agent-2", n3)  # an OK-tested number is a normal number
    h.bolna.complete(place(h, a2))
    h.settle()
    assert h.agent(a2)["agent"]["status"] == "pool_empty"  # n4 is spam; nothing else is free


# 9 ------------------------------------------------------------------ awaiting
def test_awaiting_result_is_cleared_by_clear_test_or_expires_and_keeps_position(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    tester = h.add_agent("tester", h.add_number())
    order = stack(h)
    start_run(h, [n2], tester)
    finish_test_call(h)
    rows = stack_rows(h)
    assert rows[n2]["awaiting_result"] and not rows[n2]["eligible"]
    assert rows[n2]["reason"]["code"] == "AWAITING_RESULT"
    assert stack(h) == order  # position unchanged
    refused = h.client.post("/api/spam/tests", json={"number_ids": [n2], "test_agent_id": tester, "reviewer_phone": REVIEWER})
    assert refused.status_code == 409 and refused.json()["code"] == "AWAITING_RESULT"

    assert h.client.post(f"/api/numbers/{n2}/clear-test").status_code == 200
    assert stack_rows(h)[n2]["eligible"] and stack(h) == order

    start_run(h, [n2], tester)
    finish_test_call(h, 1)
    assert not stack_rows(h)[n2]["eligible"]
    db_set(h, n2, test_awaiting_since=utcnow() - timedelta(hours=h.settings.test_result_expiry_hours + 1))
    assert stack_rows(h)[n2]["eligible"] and stack(h) == order  # expired: eligible again, same place
    assert spam.expire_awaiting(h.db, h.settings) == 1
    with h.db.session() as s:
        assert s.get(Number, n2).test_awaiting_since is None


def test_marking_clears_the_awaiting_result(h):
    n1 = h.add_number()
    tester = h.add_agent("tester", h.add_number())
    start_run(h, [n1], tester)
    finish_test_call(h)
    assert stack_rows(h)[n1]["awaiting_result"]
    verdict(h, n1, "ok")
    row = stack_rows(h)[n1]
    assert not row["awaiting_result"] and row["eligible"] and row["last_test_result"] == "ok"


# 10 ----------------------------------------------------------------- reservation
def test_two_runs_created_together_never_test_the_same_number(h):
    ids = [h.add_number() for _ in range(3)]
    t1, t2 = h.add_agent("tester-1", h.add_number()), h.add_agent("tester-2", h.add_number())
    out = {}

    def go(key, agent):
        out[key] = h.client.post("/api/spam/tests", json={"number_ids": ids, "test_agent_id": agent,
                                                          "reviewer_phone": REVIEWER})
    ts = [threading.Thread(target=go, args=("a", t1)), threading.Thread(target=go, args=("b", t2))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r.status_code for r in out.values()) == [201, 409]
    assert {r.json().get("code") for r in out.values() if r.status_code == 409} == {"NUMBER_IN_TEST"}
    with h.db.session() as s:
        per_number = [c for _, c in s.execute(select(SpamTestItem.number_id, func.count()).group_by(SpamTestItem.number_id))]
    assert per_number == [1, 1, 1]
    rows = stack_rows(h)
    assert all(rows[i]["test"] and rows[i]["test"]["status"] == "queued" for i in ids)


def test_a_run_needs_numbers_an_agent_and_a_valid_reviewer_phone(h):
    n1 = h.add_number()
    tester = h.add_agent("tester", h.add_number())
    bad = lambda **kw: h.client.post("/api/spam/tests", json={"number_ids": [n1], "test_agent_id": tester,  # noqa: E731
                                                              "reviewer_phone": REVIEWER, **kw})
    assert bad(number_ids=[]).status_code == 422
    assert bad(reviewer_phone="12345").status_code == 422
    assert h.client.post("/api/spam/tests", json={"number_ids": [n1], "reviewer_phone": REVIEWER}).status_code == 422
    assert counts(h)[0] == 0


# 11 ----------------------------------------------------------------- migration
def test_migration_orders_untested_numbers_first_then_oldest_test(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    cfg = Config()
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0003")
    c = sqlite3.connect(tmp_path / "m.db")
    base = "(phone_number, provider, status, connected_count, total_calls, added_at, spam_flag, last_tested_at, last_test_result)"

    def add(i, status="available", added="2026-01-01 00:00:00", spam=0, tested=None, result=None):
        c.execute(f"INSERT INTO numbers {base} VALUES (?,?,?,0,0,?,?,?,?)", (ph(i), "vobiz", status, added, spam, tested, result))
    add(1, added="2026-01-03 00:00:00")                                       # never tested, newer
    add(2, added="2026-01-02 00:00:00")                                       # never tested, older
    add(3, tested="2026-02-05 00:00:00", result="ok")                         # tested later
    add(4, tested="2026-02-01 00:00:00", result="ok")                         # tested earlier
    add(5, spam=1)
    add(6, status="discovered")
    add(7, status="released")
    c.commit()
    c.close()
    command.upgrade(cfg, "head")
    c = sqlite3.connect(tmp_path / "m.db")
    pos = {p: o for p, o in c.execute("SELECT phone_number, stack_order FROM numbers")}
    order = sorted((o, p) for p, o in pos.items() if o is not None)
    assert [p for _, p in order] == [ph(2), ph(1), ph(4), ph(3)]
    assert pos[ph(5)] is None and pos[ph(6)] is None and pos[ph(7)] is None


# 12 ----------------------------------------------------------------- held numbers
def test_a_held_number_is_testable_unless_its_agent_is_on_a_call(h):
    n1, n2, n3 = h.add_number(), h.add_number(), h.add_number()
    idle, busy = h.add_agent("idle", n1), h.add_agent("busy", n2)
    tester = h.add_agent("tester", n3)
    place(h, busy)
    out = pick_next(h)
    assert out["ids"] == [n3]  # the tester's own idle number is the first that can be tested
    rows = stack_rows(h)
    assert rows[n1]["eligible"] and not rows[n2]["eligible"] and rows[n2]["reason"]["code"] == "NUMBER_BUSY"
    assert stack_rows(h)[n1]["used_by"]["name"] == h.agent(idle)["agent"]["name"]
    start_run(h, [n1], tester)
    finish_test_call(h, 1)  # placed[0] is the lead call
    assert h.agent(idle)["agent"]["number"]["id"] == n1  # stays held
    r = h.client.post("/api/spam/tests", json={"number_ids": [n2], "test_agent_id": tester, "reviewer_phone": REVIEWER})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_BUSY"
