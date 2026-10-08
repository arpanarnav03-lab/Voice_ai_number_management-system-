"""Two stand-ins for Bolna that never touch the network.

* SimulatedBolna: used when BOLNA_MODE=fake (the default). Stateless: the execution id
  encodes when the call was placed and how it will end, so it survives restarts.
  How a simulated call ends is decided by the LAST DIGIT of the recipient number:
      0-5 picked up (18s)   6 voicemail   7 no answer   8 busy   9 failed
* FakeBolnaClient: scripted, used by the tests.
"""
from __future__ import annotations

import asyncio
import itertools
import time

from .bolna_client import AgentNotFoundOnBolna, BolnaError

# Obviously fake numbers (never real ones).
SIM_NUMBERS = [
    {"phone_number": f"+9180000000{i:02d}", "telephony_provider": "vobiz", "agent_id": None}
    for i in range(1, 7)
] + [
    {"phone_number": "+918000000090", "telephony_provider": "plivo", "agent_id": None},
    {"phone_number": "+918000000099", "telephony_provider": "vobiz", "agent_id": "sim-inbound-agent"},
]


class SimulatedBolna:
    def __init__(self, speed: float = 1.0):
        self.speed = speed  # >1 makes simulated calls finish faster
        self._sync_round = 0

    async def get_me(self) -> dict:
        return {"id": "sim", "name": "Simulated Bolna", "email": "sim@example.invalid",
                "wallet": 0, "concurrency": {"max": 2, "current": 0}}

    async def list_phone_numbers(self) -> list[dict]:
        return [dict(n) for n in SIM_NUMBERS]

    async def list_all_phone_numbers(self) -> list[dict]:
        """Scripted for the sync, one step per call, repeating:
        1. the normal list plus an invalid number and an extra unlinked one
        2. same, but +918000000006 has disappeared
        3. an empty response (the sync must refuse to act on it)
        """
        step = self._sync_round % 3
        self._sync_round += 1
        if step == 2:
            return []
        nums = [dict(n) for n in SIM_NUMBERS] + [
            {"phone_number": "+9180000", "telephony_provider": "vobiz", "agent_id": None},
            {"phone_number": "+918000000100", "telephony_provider": "vobiz", "agent_id": None},
        ]
        if step == 1:
            nums = [n for n in nums if n["phone_number"] != "+918000000006"]
        return nums

    async def get_agent(self, agent_id: str) -> dict | None:
        if agent_id.lower().startswith("missing"):
            raise AgentNotFoundOnBolna("Agent not found", 404)
        return {"name": f"Simulated agent {agent_id[:14]}"}

    async def place_call(self, agent_id, recipient, from_number, user_data=None) -> dict:
        digit = recipient[-1]
        ms = int(time.time() * 1000)
        return {"status": "queued", "message": "done",
                "execution_id": f"sim-{ms}-{digit}", "run_id": f"sim-{ms}-{digit}"}

    async def get_execution(self, execution_id: str) -> dict:
        try:
            _, ms, digit = execution_id.split("-")
            t = (time.time() * 1000 - int(ms)) / 1000.0 * self.speed
        except ValueError:
            raise BolnaError("unknown simulated execution", 404)
        base = {"id": execution_id, "agent_number": "+918000000000", "telephony_data": {}}

        def rec(status, **kw):
            out = {**base, "status": status, **kw}
            return out

        if t < 1.5:
            return rec("queued")
        if t < 3:
            return rec("ringing")
        if digit == "8":
            return rec("busy", conversation_duration=0)
        if digit == "7":
            return rec("no-answer", conversation_duration=0) if t >= 6 else rec("ringing")
        if digit == "9":
            return rec("failed", conversation_duration=0) if t >= 4 else rec("ringing")
        if digit == "6":
            if t < 6:
                return rec("in-progress")
            return rec("completed", conversation_duration=10.0, answered_by_voice_mail=True,
                       telephony_data={"hangup_reason": "voicemail_detected", "duration": 10.0})
        if t < 8:
            return rec("in-progress")
        if t < 9:
            return rec("call-disconnected", conversation_duration=None)
        return rec("completed", conversation_duration=18.0,
                   telephony_data={"hangup_reason": "user_hangup", "duration": 18.0})

    async def aclose(self) -> None:
        pass


class FakeBolnaClient:
    """Scripted Bolna for tests. Set records with set_execution()/complete()."""

    def __init__(self, numbers: list[dict] | None = None):
        self.numbers = numbers if numbers is not None else [
            {"phone_number": f"+91800{i:07d}", "telephony_provider": "vobiz", "agent_id": None}
            for i in range(1, 41)
        ]
        self.executions: dict[str, dict] = {}
        self.placed: list[dict] = []
        self.fail_agents: set[str] = set()
        self.missing_agents: set[str] = set()
        self.concurrency = {"max": 2, "current": 0}
        self._ids = itertools.count(1)
        self.list_error: Exception | None = None  # raised by list_all_phone_numbers
        self.list_delay: float = 0.0  # seconds, to hold a sync open
        self.list_calls = 0

    async def get_me(self) -> dict:
        return {"id": "fake", "concurrency": dict(self.concurrency)}

    async def list_phone_numbers(self) -> list[dict]:
        return [dict(n) for n in self.numbers]

    async def list_all_phone_numbers(self) -> list[dict]:
        self.list_calls += 1
        if self.list_delay:
            await asyncio.sleep(self.list_delay)
        if self.list_error is not None:
            raise self.list_error
        return [dict(n) if isinstance(n, dict) else n for n in self.numbers]

    async def get_agent(self, agent_id: str) -> dict | None:
        if agent_id in self.missing_agents:
            raise AgentNotFoundOnBolna("Agent not found", 404)
        return {"name": f"Agent {agent_id}"}

    async def place_call(self, agent_id, recipient, from_number, user_data=None) -> dict:
        if agent_id in self.fail_agents:
            raise BolnaError("concurrency limit reached", 429)
        eid = f"exec-{next(self._ids)}"
        self.placed.append({"execution_id": eid, "agent_id": agent_id,
                            "recipient": recipient, "from": from_number})
        self.executions[eid] = {"id": eid, "status": "queued"}
        return {"status": "queued", "message": "done", "execution_id": eid, "run_id": eid}

    async def get_execution(self, execution_id: str) -> dict:
        if execution_id not in self.executions:
            raise BolnaError("not found", 404)
        return dict(self.executions[execution_id])

    # ---- test helpers -------------------------------------------------
    def set_execution(self, eid: str, record: dict) -> None:
        self.executions[eid] = {"id": eid, **record}

    def complete(self, eid: str, duration: float = 18.0, **extra) -> None:
        self.set_execution(eid, {"status": "completed", "conversation_duration": duration, **extra})

    async def aclose(self) -> None:
        pass
