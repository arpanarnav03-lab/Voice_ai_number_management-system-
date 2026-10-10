"""Auto-select suggests exactly one number, and the stack tags that same number "Next"."""
import logging
from pathlib import Path

from sqlalchemy import func, select

from app import config
from app.config import Settings
from app.main import create_app
from app.models import Call, Number, SpamTestItem, SpamTestRun, utcnow
from tests.conftest import RECIPIENT

ROOT = Path(__file__).resolve().parent.parent.parent
OLD_NAME = "AUTO_" + "SELECT_COUNT"  # spelled in two parts so this file does not match its own search


def stack_rows(h):
    return h.client.get("/api/spam/stack").json()["items"]


def stack_ids(h):
    return [r["id"] for r in stack_rows(h)]


def suggest(h, **body):
    r = h.client.post("/api/spam/stack/select", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def next_rows(h):
    return [r["id"] for r in stack_rows(h) if r["is_next"]]


def snapshot(h):
    with h.db.session() as s:
        nums = [(n.id, n.stack_order, n.last_test_result, n.last_tested_at, n.test_awaiting_since, n.spam_flag)
                for n in s.scalars(select(Number).order_by(Number.id))]
        return (nums, s.scalar(select(func.count()).select_from(SpamTestRun)),
                s.scalar(select(func.count()).select_from(SpamTestItem)), s.scalar(select(func.count()).select_from(Call)))


def make_busy(h, nid, agent_name):
    a = h.add_agent(agent_name, nid)
    assert h.trigger((a, RECIPIENT))[0]["ok"]  # a lead call is now running from this number
    return a


def test_it_suggests_exactly_one_number_the_first_from_the_top(h):
    ns = [h.add_number() for _ in range(8)]
    out = suggest(h)
    assert out["ids"] == [ns[7]] == stack_ids(h)[:1]
    assert out["skipped"] == []


def test_it_skips_ineligible_numbers_and_takes_the_next_eligible_one(h):
    ns = [h.add_number() for _ in range(7)]  # stack top-first: ns[6] ... ns[0]
    make_busy(h, ns[6], "agent-1")
    with h.db.session() as s:
        s.get(Number, ns[5]).test_awaiting_since = utcnow()  # awaiting a result
        s.get(Number, ns[4]).missing_on_bolna = True
    out = suggest(h)
    assert out["ids"] == [ns[3]]
    assert {s["number_id"]: s["code"] for s in out["skipped"]} == {
        ns[6]: "NUMBER_BUSY", ns[5]: "AWAITING_RESULT", ns[4]: "NUMBER_MISSING_ON_BOLNA"}
    rows = {r["id"]: r for r in stack_rows(h)}
    assert stack_ids(h)[:4] == [ns[6], ns[5], ns[4], ns[3]]  # they stay where they are
    assert [rows[i]["eligible"] for i in (ns[6], ns[5], ns[4], ns[3])] == [False, False, False, True]
    assert [rows[i]["reason"]["message"] for i in (ns[6], ns[5], ns[4])] == ["On a call", "Awaiting result", "Not on Bolna"]


def test_with_no_eligible_number_it_suggests_nothing(h):
    ns = [h.add_number() for _ in range(3)]
    with h.db.session() as s:
        for n in ns:
            s.get(Number, n).missing_on_bolna = True
    out = suggest(h)
    assert out["ids"] == [] and len(out["skipped"]) == 3
    assert next_rows(h) == []  # and nothing is tagged Next


def test_asking_twice_gives_the_same_answer_and_a_count_is_ignored(h):
    for _ in range(9):
        h.add_number()
    first = suggest(h)
    assert len(first["ids"]) == 1 and suggest(h) == first
    for body in ({"count": 1}, {"count": 0}, {"count": 99}, {"count": "x"}):
        assert suggest(h, **body) == first
    assert h.client.post("/api/spam/stack/select").status_code == 200  # no body at all


def test_the_route_places_no_call_and_changes_no_state(h):
    for _ in range(6):
        h.add_number()
    before = snapshot(h)
    suggest(h)
    suggest(h, count=99)
    for _ in range(3):
        h.settle()
    assert snapshot(h) == before and h.bolna.placed == []


def test_exactly_one_row_is_next_and_it_is_the_row_auto_select_picks(h):
    ns = [h.add_number() for _ in range(6)]
    assert next_rows(h) == [ns[5]] == suggest(h)["ids"]
    a = make_busy(h, ns[5], "agent-1")  # a call starts on the top number
    assert next_rows(h) == [ns[4]] == suggest(h)["ids"]  # both move down together
    rows = {r["id"]: r for r in stack_rows(h)}
    assert rows[ns[5]]["is_next"] is False  # an ineligible row above it is not tagged
    assert sum(r["is_next"] for r in rows.values()) == 1
    with h.db.session() as s:
        s.get(Number, ns[4]).test_awaiting_since = utcnow()
    assert next_rows(h) == [ns[3]] == suggest(h)["ids"]
    h.bolna.complete(h.bolna.placed[0]["execution_id"], 18)
    h.settle()  # the call ends: the top number is testable again, and is Next again
    assert next_rows(h) == [ns[5]] == suggest(h)["ids"]
    assert h.agent(a)["agent"]["on_call"] is False


def test_no_row_is_next_when_nothing_can_be_tested(h):
    ns = [h.add_number() for _ in range(2)]
    assert next_rows(h) == [ns[1]]
    with h.db.session() as s:
        for n in ns:
            s.get(Number, n).missing_on_bolna = True
    assert next_rows(h) == [] and suggest(h)["ids"] == []


def test_the_server_still_refuses_a_run_for_an_ineligible_number(h):
    n1 = h.add_number()
    tester = h.add_agent("tester", h.add_number())
    make_busy(h, n1, "agent-1")  # a call starts on n1 after it was ticked
    r = h.client.post("/api/spam/tests", json={"number_ids": [n1], "test_agent_id": tester, "reviewer_phone": "+919999999902"})
    assert r.status_code == 409 and r.json()["code"] == "NUMBER_BUSY"


def test_the_old_setting_is_gone_everywhere_and_a_leftover_env_value_is_harmless(h, monkeypatch, caplog):
    where = [ROOT / "backend" / "app", ROOT / "backend" / "migrations", ROOT / "backend" / ".env.example",
             ROOT / "backend" / "run.py", ROOT / "README.md", ROOT / "frontend" / "src", ROOT / "frontend" / "index.html"]
    hits = []
    for base in where:
        files = [base] if base.is_file() else [f for f in base.rglob("*") if f.is_file() and f.suffix in (".py", ".ts", ".tsx", ".md", ".html", ".example", ".css")]
        hits += [str(f) for f in files if OLD_NAME in f.read_text(errors="ignore") or "auto_select_count" in f.read_text(errors="ignore")]
    assert hits == []
    assert "auto_select_count" not in h.client.get("/api/settings").json()

    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # never read a real .env
    monkeypatch.setenv("BOLNA_MODE", "fake")
    for leftover in ("3", "0", "not-a-number", "2.5"):
        monkeypatch.setenv(OLD_NAME, leftover)
        with caplog.at_level(logging.DEBUG):
            Settings.from_env()  # ignored silently: no crash
    assert "not-a-number" not in caplog.text and "2.5" not in caplog.text
    assert not hasattr(Settings(), "auto_select_count")
