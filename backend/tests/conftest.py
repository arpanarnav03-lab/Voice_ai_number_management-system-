import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.fake_bolna import FakeBolnaClient
from app.main import create_app
from app.poller import poll_once


class Harness:
    def __init__(self, tmp_path, password=""):
        self.settings = Settings(
            database_url=f"sqlite:///{tmp_path}/test.db", run_migrations=False,
            start_poller=False, app_password=password,
        )
        self.bolna = FakeBolnaClient()
        self.app = create_app(self.settings, self.bolna)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.db = self.app.state.db
        self._n = 0

    def close(self):
        self.client.__exit__(None, None, None)

    # --- api helpers
    def set_threshold(self, n):
        r = self.client.put("/api/settings/threshold", json={"threshold": n})
        assert r.status_code == 200, r.text

    def add_number(self, phone=None):
        self._n += 1
        phone = phone or f"+91800{self._n:07d}"
        r = self.client.post("/api/numbers", json={"phone_number": phone})
        assert r.status_code == 201, r.text
        return r.json()["number"]["id"]

    def add_agent(self, bolna_id, number_id):
        r = self.client.post("/api/agents", json={"bolna_agent_id": bolna_id, "number_id": number_id})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def trigger(self, *items):
        r = self.client.post("/api/calls/trigger", json={"items": [
            {"agent_id": a, "recipient": rec} for a, rec in items]})
        assert r.status_code == 200, r.text
        return r.json()["results"]

    def agent(self, agent_id):
        return self.client.get(f"/api/agents/{agent_id}").json()

    def numbers(self):
        return {n["id"]: n for n in self.client.get("/api/numbers").json()["items"]}

    def settle(self):
        return asyncio.run(poll_once(self.db, self.bolna, self.settings))


@pytest.fixture
def h(tmp_path):
    hh = Harness(tmp_path)
    yield hh
    hh.close()


@pytest.fixture
def make_harness(tmp_path):
    made = []

    def _make(password=""):
        hh = Harness(tmp_path, password)
        made.append(hh)
        return hh
    yield _make
    for hh in made:
        hh.close()


RECIPIENT = "+919999999901"
