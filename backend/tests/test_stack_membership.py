"""The stack holds exactly the non-spam pool numbers, whatever happens to them."""
import random
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import services
from app.main import create_app
from app.models import Number
from tests.conftest import RECIPIENT

ROOT = Path(__file__).resolve().parent.parent
POOL = ("available", "in_use", "limit_reached")


def ph(i):
    return f"+91800{i:07d}"


def bnum(i, **kw):
    return {"id": f"{i:032x}", "phone_number": ph(i), "telephony_provider": "vobiz", "agent_id": None,
            "rented": True, **kw}


def stack(h):
    return [r["id"] for r in h.client.get("/api/spam/stack").json()["items"]]


def pool_not_spam(h):
    return {n["id"] for n in h.client.get("/api/numbers").json()["items"] if not n["spam_flag"]}


def raw(h, sql, params=()):
    with h.db.engine.begin() as c:  # raw SQL: bypasses the ORM safety net on purpose
        c.exec_driver_sql(sql, params)


def positions(h):
    with h.db.session() as s:
        return {n.id: n.stack_order for n in s.scalars(select(Number))}


def assert_invariant(h, where=""):
    """stack set == non-spam pool set, positions unique, non-members have none, health all zero."""
    st = stack(h)
    assert set(st) == pool_not_spam(h), f"stack and pool differ {where}"
    assert len(st) == len(set(st))
    with h.db.session() as s:
        rows = list(s.scalars(select(Number)))
        pos = [n.stack_order for n in rows if n.stack_order is not None]
        assert len(pos) == len(set(pos)), f"duplicate positions {where}"
        for n in rows:
            member = n.status in POOL and not n.spam_flag
            assert (n.stack_order is not None) == member, f"{n.status}/spam={n.spam_flag} {where}"
    assert h.client.get("/api/spam/stack/health").json() == {
        "missing_from_stack": 0, "unexpected_in_stack": 0, "duplicate_positions": 0}, where


def states(h):
    """(status, spam) pairs currently present, to prove a scripted run reached every kind of number."""
    with h.db.session() as s:
        return {(n.status, bool(n.spam_flag)) for n in s.scalars(select(Number))}


def sync(h, expect=200):
    r = h.client.post("/api/sync/run")
    assert r.status_code == expect, r.text
    return r.json()


def discovered(h):
    return {n["phone_number"]: n["id"] for n in h.client.get("/api/numbers", params={"status": "discovered"}).json()["items"]}


def place(h, agent_id):
    res = h.trigger((agent_id, RECIPIENT))[0]
    assert res["ok"], res
    return res["execution_id"]


# ---- migration helpers ------------------------------------------------------------------
def migrate_to(tmp_path, rev):
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path}/m.db")
    command.upgrade(cfg, rev)
    return cfg


def insert(c, i, status="available", added="2026-01-01 00:00:00", spam=0, tested=None, result=None, pos=None):
    c.execute("INSERT INTO numbers (phone_number, provider, status, connected_count, total_calls, added_at, spam_flag,"
              " last_tested_at, last_test_result, stack_order) VALUES (?,?,?,0,0,?,?,?,?,?)",
              (ph(i), "vobiz", status, added, spam, tested, result, pos))


def read_positions(tmp_path):
    c = sqlite3.connect(tmp_path / "m.db")
    return {p: o for p, o in c.execute("SELECT phone_number, stack_order FROM numbers")}


# 1 ------------------------------------------------------------------ backfill: every status
def test_backfill_positions_exactly_the_qualifying_numbers(tmp_path):
    cfg = migrate_to(tmp_path, "0004")
    c = sqlite3.connect(tmp_path / "m.db")
    kinds = ["available", "in_use", "limit_reached"]
    for i, st in enumerate(kinds, start=1):
        insert(c, i, st)
    insert(c, 4, "available", spam=1)
    insert(c, 5, "in_use", spam=1)
    for i, st in enumerate(["discovered", "ignored", "releasing", "released"], start=6):
        insert(c, i, st)
    c.commit()
    c.close()
    command.upgrade(cfg, "0005")
    pos = read_positions(tmp_path)
    assert all(pos[ph(i)] is not None for i in (1, 2, 3))
    assert all(pos[ph(i)] is None for i in range(4, 10))
    vals = [pos[ph(i)] for i in (1, 2, 3)]
    assert len(set(vals)) == 3
    assert vals == sorted(vals)  # never-tested, oldest added first (equal dates: by id)


# 2 ------------------------------------------------------------------ backfill: order, twice
def test_backfill_keeps_existing_order_puts_new_ones_top_and_tested_ones_bottom(tmp_path):
    cfg = migrate_to(tmp_path, "0004")
    c = sqlite3.connect(tmp_path / "m.db")
    insert(c, 1, pos=20)  # already positioned: B
    insert(c, 2, pos=10)  # already positioned: A (above B)
    insert(c, 3, added="2026-01-02 00:00:00")                                   # missing, never tested, newer
    insert(c, 4, added="2026-01-01 00:00:00")                                   # missing, never tested, older
    insert(c, 5, tested="2026-03-02 00:00:00", result="ok")                     # missing, tested later
    insert(c, 6, tested="2026-03-01 00:00:00", result="ok")                     # missing, tested earlier
    c.commit()
    c.close()
    command.upgrade(cfg, "0005")
    pos = read_positions(tmp_path)
    order = [p for p, _ in sorted(pos.items(), key=lambda kv: kv[1])]
    assert order == [ph(4), ph(3), ph(2), ph(1), ph(6), ph(5)]
    first = dict(pos)
    command.downgrade(cfg, "0004")  # a no-op downgrade, then upgrade again: nothing changes
    command.upgrade(cfg, "0005")
    assert read_positions(tmp_path) == first


def test_backfill_fixes_duplicates_and_strips_numbers_that_do_not_qualify(tmp_path):
    cfg = migrate_to(tmp_path, "0004")
    c = sqlite3.connect(tmp_path / "m.db")
    insert(c, 1, pos=5)
    insert(c, 2, pos=5)  # duplicate position
    insert(c, 3, pos=7)
    insert(c, 4, spam=1, pos=1)
    insert(c, 5, status="released", pos=2)
    insert(c, 6, status="discovered", pos=3)
    insert(c, 7, status="ignored", pos=4)
    insert(c, 8, status="releasing", pos=6)
    c.commit()
    c.close()
    command.upgrade(cfg, "0005")
    pos = read_positions(tmp_path)
    assert [pos[ph(i)] for i in range(4, 9)] == [None] * 5
    kept = [pos[ph(i)] for i in (1, 2, 3)]
    assert len(set(kept)) == 3 and kept == sorted(kept)  # unique, original relative order kept


def test_migration_and_reconcile_stack_agree(tmp_path, h):
    """The SQL backfill and the service function apply the same rule."""
    cfg = migrate_to(tmp_path, "0004")
    c = sqlite3.connect(tmp_path / "m.db")
    insert(c, 1, pos=3); insert(c, 2, added="2026-01-05 00:00:00"); insert(c, 3, tested="2026-02-01 00:00:00", result="ok")  # noqa: E702
    insert(c, 4, spam=1, pos=1); insert(c, 5, added="2026-01-03 00:00:00")  # noqa: E702
    c.commit()
    c.close()
    command.upgrade(cfg, "0005")
    sql_order = [p for p, o in sorted(read_positions(tmp_path).items(), key=lambda kv: (kv[1] is None, kv[1])) if o is not None]
    # same data through the service
    for i, kw in {1: dict(pos=3), 2: dict(added="2026-01-05 00:00:00"), 3: dict(tested="2026-02-01 00:00:00", result="ok"),
                  4: dict(spam=1, pos=1), 5: dict(added="2026-01-03 00:00:00")}.items():
        raw(h, "INSERT INTO numbers (phone_number, provider, status, connected_count, total_calls, added_at, spam_flag,"
               " last_tested_at, last_test_result, stack_order, linked_in_bolna, allow_auto_switch, missing_on_bolna)"
               " VALUES (?,?,?,0,0,?,?,?,?,?,0,1,0)",
            (ph(i), "vobiz", "available", kw.get("added", "2026-01-01 00:00:00"), kw.get("spam", 0),
             kw.get("tested"), kw.get("result"), kw.get("pos")))
    with h.db.session() as s:
        services.reconcile_stack(s)
        svc_order = [n.phone_number for n in sorted((n for n in s.scalars(select(Number)) if n.stack_order is not None),
                                                    key=lambda n: n.stack_order)]
    assert svc_order == sql_order


# 3 ------------------------------------------------------------------ non-qualifying lose position
def test_reconcile_strips_positions_from_numbers_that_no_longer_qualify(h):
    ns = [h.add_number() for _ in range(5)]
    for nid, st in zip(ns, ["released", "discovered", "ignored", "releasing", "available"]):
        raw(h, "UPDATE numbers SET status=?, stack_order=? WHERE id=?", (st, nid, nid))
    raw(h, "UPDATE numbers SET spam_flag=1, stack_order=99 WHERE id=?", (ns[4],))
    with h.db.session() as s:
        out = services.reconcile_stack(s)
    assert out["removed"] == 5 and out["added"] == 0
    assert all(v is None for v in positions(h).values())


# 4 ------------------------------------------------------------------ every entry path
def test_every_way_into_the_pool_joins_the_stack_at_the_top(h):
    n1 = h.add_number()
    assert stack(h) == [n1]
    n2 = h.add_number()  # by hand
    assert stack(h)[0] == n2

    h.bolna.numbers = [bnum(i) for i in range(1, 11)] + [bnum(11, agent_id="inbound-1")]
    sync(h)
    disc = discovered(h)
    assert not set(disc.values()) & set(stack(h))  # discovered numbers are not members

    assert h.client.post(f"/api/numbers/{disc[ph(5)]}/approve").status_code == 200  # single
    assert stack(h)[0] == disc[ph(5)]
    r = h.client.post("/api/numbers/approve", json={"ids": [disc[ph(6)], disc[ph(7)]]})  # bulk
    assert r.json()["approved"] == 2
    assert stack(h)[:2] == [disc[ph(6)], disc[ph(7)]]
    r = h.client.post("/api/numbers/approve", json={"all_unlinked": True})  # add all unlinked
    got = r.json()["approved"]
    assert got == len(disc) - 3 - 1  # minus the three approved and the linked one
    assert set(stack(h)[:got]) == {v for k, v in disc.items() if k not in (ph(5), ph(6), ph(7), ph(11))}
    assert disc[ph(11)] not in stack(h)  # the linked one still waits
    assert_invariant(h)


def test_sync_auto_approval_joins_at_the_top(h):
    n1 = h.add_number()
    h.app.state.settings.sync_auto_approve = True
    h.bolna.numbers = [bnum(1), bnum(61), bnum(62), bnum(63, agent_id="inbound-1")]
    sync(h)
    order = stack(h)
    assert order[-1] == n1 and len(order) == 3
    with h.db.session() as s:
        assert [s.get(Number, i).phone_number for i in order[:2]] == [ph(61), ph(62)]
    assert_invariant(h)


# 5 ------------------------------------------------------------------ spam, not spam, reset
def test_spam_leaves_not_spam_returns_on_top_and_reset_keeps_the_place(h):
    h.set_threshold(1)
    n1, n2, n3 = (h.add_number() for _ in range(3))
    a = h.add_agent("agent-1", n1)
    h.client.post(f"/api/numbers/{n2}/mark-spam", json={})
    assert n2 not in stack(h) and set(stack(h)) == {n1, n3}
    h.client.post(f"/api/numbers/{n2}/not-spam")
    assert stack(h)[0] == n2
    assert_invariant(h)

    h.bolna.complete(place(h, a))
    h.settle()  # n1 hits the limit and the agent moves on
    assert h.numbers()[n1]["status"] == "limit_reached"
    before = positions(h)
    assert h.client.post(f"/api/numbers/{n1}/reset").status_code == 200
    assert h.numbers()[n1]["status"] == "available"
    assert positions(h) == before  # still a member, same place
    assert_invariant(h)


# 6 ------------------------------------------------------------------ status moves do not move it
def test_status_changes_never_remove_or_move_a_number(h):
    h.set_threshold(1)
    ns = [h.add_number() for _ in range(5)]
    before = positions(h)
    a = h.add_agent("agent-1", ns[0])  # available -> in_use
    b = h.add_agent("agent-2", ns[1])
    assert positions(h) == before
    h.client.post(f"/api/agents/{a}/change-number", json={"number_id": ns[2]})  # release + assign
    assert positions(h) == before
    h.bolna.complete(place(h, a))
    h.settle()  # auto-switch: in_use -> limit_reached, next number in_use
    assert positions(h) == before
    h.bolna.complete(place(h, b))
    h.settle()
    h.client.post(f"/api/numbers/{ns[4]}/mark-spam", json={})  # automatic swap path for a holder
    for nid in ns:
        if nid != ns[4]:
            assert positions(h)[nid] == before[nid]
    assert_invariant(h)
    # auto-refill: a waiting agent is filled when a number is added; membership still exact
    c = h.add_agent("agent-3", h.add_number())
    h.bolna.complete(place(h, c))
    h.settle()
    h.add_number()
    assert_invariant(h)


# 7 ------------------------------------------------------------------ missing stays
def test_a_missing_number_stays_in_the_stack_and_is_not_testable(h):
    n1, n2 = h.add_number(), h.add_number()
    h.bolna.numbers = [bnum(2)]
    sync(h)
    assert h.numbers()[n1]["missing_on_bolna"]
    rows = {r["id"]: r for r in h.client.get("/api/spam/stack").json()["items"]}
    assert n1 in rows and rows[n1]["eligible"] is False
    assert rows[n1]["reason"]["code"] == "NUMBER_MISSING_ON_BOLNA"
    assert rows[n2]["eligible"] is True
    h.bolna.numbers = [bnum(1), bnum(2)]
    sync(h)
    assert_invariant(h)


# 8 ------------------------------------------------------------------ the long invariant check
class Driver:
    """Applies one random operation, whichever paths it takes, and never trusts it to succeed."""

    def __init__(self, h, rng):
        self.h, self.rng = h, rng
        self.universe = [bnum(i) for i in range(1, 21)] + [bnum(i) for i in range(21, 31)]
        self.universe[25] = bnum(26, agent_id="inbound-x")
        h.bolna.numbers = [dict(n) for n in self.universe]
        h.app.state.settings.allow_bolna_number_delete = True
        h.set_threshold(2)
        self.agents = 0

    def api(self, method, url, **kw):
        r = getattr(self.h.client, method)(url, **kw)
        assert r.status_code < 500, (url, r.text)
        return r

    def pool(self):
        return self.api("get", "/api/numbers").json()["items"]

    def pick(self, items):
        return self.rng.choice(items) if items else None

    # -- operations
    def op_add_manual(self):
        have = {n["phone_number"] for n in self.pool()}
        todo = [n for n in self.h.bolna.numbers if n["phone_number"] not in have]
        n = self.pick(todo)
        if n:
            self.api("post", "/api/numbers", json={"phone_number": n["phone_number"]})

    def op_sync(self):
        self.api("post", "/api/sync/run")

    def op_sync_hide_some(self):  # some pool numbers vanish from Bolna, then come back
        live = [n for n in self.universe if n["phone_number"] in {x["phone_number"] for x in self.h.bolna.numbers}]
        keep = [n for n in live if self.rng.random() > 0.3] or live[:1]
        saved = self.h.bolna.numbers
        self.h.bolna.numbers = [dict(n) for n in keep]
        self.api("post", "/api/sync/run")
        self.h.bolna.numbers = saved
        self.api("post", "/api/sync/run")

    def op_approve_one(self):
        d = self.api("get", "/api/numbers", params={"status": "discovered", "page_size": 200}).json()["items"]
        n = self.pick(d)
        if n:
            self.api("post", f"/api/numbers/{n['id']}/approve", json={"confirm_linked": True})

    def op_approve_bulk(self):
        d = self.api("get", "/api/numbers", params={"status": "discovered", "page_size": 200}).json()["items"]
        ids = [n["id"] for n in d if self.rng.random() < 0.3]
        if ids:
            self.api("post", "/api/numbers/approve", json={"ids": ids})

    def op_approve_all_unlinked(self):
        self.api("post", "/api/numbers/approve", json={"all_unlinked": True})

    def op_auto_approve_sync(self):
        s = self.h.app.state.settings
        s.sync_auto_approve = True
        self.api("post", "/api/sync/run")
        s.sync_auto_approve = False

    def op_new_on_bolna(self):  # fresh numbers appear in the Bolna account
        base = 200 + len(self.universe)
        for i in range(base, base + 3):
            num = bnum(i)
            self.universe.append(num)
            self.h.bolna.numbers.append(dict(num))
        self.api("post", "/api/sync/run")

    def op_ignore(self):
        d = self.api("get", "/api/numbers", params={"status": "discovered", "page_size": 200}).json()["items"]
        n = self.pick(d)
        if n:
            self.api("post", f"/api/numbers/{n['id']}/ignore")

    def op_ignore_restore(self):
        d = self.api("get", "/api/numbers", params={"status": "discovered", "page_size": 200}).json()["items"]
        n = self.pick(d)
        if n:
            self.api("post", f"/api/numbers/{n['id']}/ignore")
            if self.rng.random() < 0.5:
                self.api("post", f"/api/numbers/{n['id']}/restore")

    def op_mark_spam(self):
        n = self.pick([x for x in self.pool() if not x["spam_flag"]])
        if n:
            self.api("post", f"/api/numbers/{n['id']}/mark-spam", json={})

    def op_not_spam(self):
        n = self.pick([x for x in self.pool() if x["spam_flag"]])
        if n:
            self.api("post", f"/api/numbers/{n['id']}/not-spam")

    def op_result(self):
        n = self.pick(self.pool())
        if n:
            self.api("post", f"/api/numbers/{n['id']}/spam-result", json={"result": self.rng.choice(["ok", "ok", "spam"])})

    def op_assign_agent(self):
        free = [n for n in self.pool() if n["status"] == "available" and not n["spam_flag"] and not n["used_by"]]
        n = self.pick(free)
        if n:
            self.agents += 1
            self.api("post", "/api/agents", json={"bolna_agent_id": f"agent-{self.agents}", "number_id": n["id"]})

    def op_change_number(self):
        ags = self.api("get", "/api/agents").json()["items"]
        a, n = self.pick(ags), self.pick(self.pool())
        if a and n:
            self.api("post", f"/api/agents/{a['id']}/change-number", json={"number_id": n["id"]})

    def op_call(self):  # connected calls drive auto-switch, refill and the spam swap
        ags = [a for a in self.api("get", "/api/agents").json()["items"] if a["number"]]
        a = self.pick(ags)
        if not a:
            return
        res = self.api("post", "/api/calls/trigger", json={"items": [{"agent_id": a["id"], "recipient": RECIPIENT}]}).json()["results"][0]
        if res["ok"]:
            if self.rng.random() < 0.8:
                self.h.bolna.complete(res["execution_id"], 18)
            self.h.settle()

    def op_settle(self):
        self.h.settle()

    def op_reset(self):
        n = self.pick([x for x in self.pool() if x["status"] == "limit_reached"])
        if n:
            self.api("post", f"/api/numbers/{n['id']}/reset")

    def op_release(self):
        n = [x for x in self.pool() if x["spam_flag"]]
        if n:
            self.api("post", "/api/spam/release", json={"ids": [x["id"] for x in n]})

    def op_threshold(self):
        self.h.set_threshold(self.rng.choice([1, 2, 3]))

    def op_clear_test(self):
        n = self.pick(self.pool())
        if n:
            self.api("post", f"/api/numbers/{n['id']}/clear-test")

    def ops(self):
        return [getattr(self, m) for m in dir(self) if m.startswith("op_")]


FIXED = ["op_add_manual", "op_sync", "op_approve_one", "op_approve_bulk", "op_assign_agent", "op_assign_agent",
         "op_call", "op_call", "op_mark_spam", "op_change_number", "op_not_spam", "op_result", "op_sync_hide_some",
         "op_reset", "op_auto_approve_sync", "op_assign_agent", "op_call", "op_mark_spam", "op_call", "op_settle",
         "op_release", "op_new_on_bolna", "op_ignore", "op_approve_all_unlinked", "op_ignore_restore", "op_threshold", "op_result", "op_call",
         "op_mark_spam", "op_release", "op_sync", "op_clear_test", "op_not_spam", "op_add_manual"]


def test_invariant_holds_after_every_step_of_a_fixed_script(h):
    d = Driver(h, random.Random(0))
    sync(h)
    assert_invariant(h, "after first sync")
    seen = set()
    for i, name in enumerate(FIXED):
        getattr(d, name)()
        assert_invariant(h, f"after step {i} ({name})")
        seen |= states(h)
    # the script really visited the interesting kinds of number
    assert {("in_use", False), ("available", True), ("released", True), ("discovered", False),
            ("ignored", False)} <= seen


def test_invariant_holds_after_every_step_of_seeded_random_sequences(h):
    seen = set()
    for seed in (1, 2, 3, 4, 5, 6):
        d = Driver(h, random.Random(seed))
        ops = d.ops()
        for i in range(60):
            op = d.rng.choice(ops)
            op()
            assert_invariant(h, f"seed {seed} step {i} ({op.__name__})")
            seen |= states(h)
    assert {"in_use", "limit_reached", "discovered", "released"} <= {st for st, _ in seen}
    assert any(spam for _, spam in seen)


# 9 ------------------------------------------------------------------ repair
def corrupt(h):
    ns = [h.add_number() for _ in range(5)]
    raw(h, "UPDATE numbers SET stack_order=NULL WHERE id=?", (ns[0],))  # pool number missing from the stack
    raw(h, "UPDATE numbers SET spam_flag=1, status='available' WHERE id=?", (ns[1],))  # spam number keeps its position
    raw(h, "UPDATE numbers SET stack_order=(SELECT stack_order FROM numbers WHERE id=?) WHERE id=?", (ns[2], ns[3]))  # duplicate
    health = h.client.get("/api/spam/stack/health").json()
    assert health["missing_from_stack"] >= 1 and health["unexpected_in_stack"] >= 1 and health["duplicate_positions"] >= 1
    return ns


ZERO = {"missing_from_stack": 0, "unexpected_in_stack": 0, "duplicate_positions": 0}


def test_reconcile_endpoint_repairs_a_corrupted_stack(h):
    corrupt(h)
    out = h.client.post("/api/spam/stack/reconcile").json()
    assert out["added"] == 1 and out["removed"] == 1 and out["duplicates_fixed"] == 1
    assert h.client.get("/api/spam/stack/health").json() == ZERO
    assert_invariant(h)
    assert h.client.post("/api/spam/stack/reconcile").json() == {"added": 0, "removed": 0, "duplicates_fixed": 0}


def test_startup_repairs_a_corrupted_stack(h):
    corrupt(h)
    with TestClient(create_app(h.settings, h.bolna)) as second:  # a fresh start on the same database
        assert second.get("/api/spam/stack/health").json() == ZERO
    assert_invariant(h)


def test_a_sync_run_repairs_a_corrupted_stack(h):
    corrupt(h)
    h.bolna.numbers = [bnum(i) for i in range(1, 11)]
    sync(h)
    assert_invariant(h)


def test_the_orm_safety_net_places_numbers_even_if_a_path_forgets(h):
    with h.db.session() as s:  # a path that never calls the stack helpers
        s.add(Number(phone_number=ph(900), provider="vobiz", status="available"))
    n = h.add_number()
    assert_invariant(h)
    with h.db.session() as s:
        s.get(Number, n).spam_flag = True
    assert_invariant(h)
    with h.db.session() as s:
        s.get(Number, n).spam_flag = False
        s.get(Number, n).status = "released"
    assert_invariant(h)


# 10 ----------------------------------------------------------------- API agreement
def test_the_stack_api_lists_exactly_the_non_spam_pool_numbers(h):
    ns = [h.add_number() for _ in range(6)]
    h.client.post(f"/api/numbers/{ns[1]}/mark-spam", json={})
    h.bolna.numbers = [bnum(i) for i in range(1, 9)] + [bnum(70)]
    sync(h)
    raw(h, "UPDATE numbers SET status='in_use' WHERE id=?", (ns[2],))
    raw(h, "UPDATE numbers SET status='limit_reached' WHERE id=?", (ns[3],))
    body = h.client.get("/api/spam/stack").json()
    assert {r["id"] for r in body["items"]} == pool_not_spam(h)
    assert body["total"] == len(body["items"]) == len(pool_not_spam(h))
    assert [r["position"] for r in body["items"]] == list(range(1, body["total"] + 1))
    assert body["never_tested"] == body["total"]  # nothing tested yet


# 11 ----------------------------------------------------------------- allocation unchanged
def test_allocation_still_follows_pool_order_not_stack_order(h):
    h.set_threshold(1)
    n1, n2, n3 = (h.add_number() for _ in range(3))
    a = h.add_agent("agent-1", n1)
    h.client.post(f"/api/numbers/{n2}/spam-result", json={"result": "ok"})  # n2 sinks to the bottom
    assert stack(h)[-1] == n2
    h.bolna.complete(place(h, a))
    h.settle()
    assert h.agent(a)["agent"]["number"]["id"] == n2  # first available by date added, untouched by the stack
    assert_invariant(h)
