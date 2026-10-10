"""Bolna API client behind a small interface (so tests and fake mode never touch the network)."""
from __future__ import annotations

import asyncio
from typing import Protocol

import httpx


class BolnaError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class AgentNotFoundOnBolna(BolnaError):
    pass


class BolnaClient(Protocol):
    async def get_me(self) -> dict: ...
    async def list_phone_numbers(self) -> list[dict]: ...
    async def list_all_phone_numbers(self) -> list[dict]: ...
    async def delete_phone_number(self, phone_number_id: str) -> dict: ...
    async def place_call(self, agent_id: str, recipient: str, from_number: str,
                         user_data: dict | None = None) -> dict: ...
    async def get_execution(self, execution_id: str) -> dict: ...
    async def get_agent(self, agent_id: str) -> dict | None: ...
    async def aclose(self) -> None: ...


class RealBolnaClient:
    """Talks to https://api.bolna.ai. GETs are retried (idempotent); POST /call never is."""

    def __init__(self, api_key: str, base_url: str = "https://api.bolna.ai", timeout: float = 20.0):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout),
        )

    @staticmethod
    def _message(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict):
                msg = data.get("message") or data.get("detail") or data.get("error")
                if msg:
                    return str(msg)[:300]
        except ValueError:
            pass
        return (resp.text or f"HTTP {resp.status_code}")[:300]

    async def _get(self, path: str, retries: int = 3):
        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await self._http.get(path)
            except httpx.HTTPError as e:  # network / timeout
                last = BolnaError(f"Could not reach Bolna: {type(e).__name__}")
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    last = BolnaError(self._message(resp), resp.status_code)
                else:
                    raise BolnaError(self._message(resp), resp.status_code)
            await asyncio.sleep(0.5 * (2 ** attempt))
        raise last or BolnaError("Bolna request failed")

    async def get_me(self) -> dict:
        return await self._get("/user/me")

    async def list_phone_numbers(self) -> list[dict]:
        data = await self._get("/phone-numbers/all")
        return data if isinstance(data, list) else data.get("data", [])

    async def list_all_phone_numbers(self) -> list[dict]:
        """Every number on the account, for the sync. The endpoint is documented as one plain
        array with no pagination; anything else is an error, never an empty list."""
        data = await self._get("/phone-numbers/all")
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data = data["data"]
        if not isinstance(data, list):
            raise BolnaError("Bolna returned an unexpected response for the phone number list")
        return data

    async def delete_phone_number(self, phone_number_id: str) -> dict:
        """DELETE /phone-numbers/{id}. Permanent. Only the release flow calls this, never retried."""
        try:
            resp = await self._http.delete(f"/phone-numbers/{phone_number_id}")
        except httpx.HTTPError as e:
            raise BolnaError(f"Could not reach Bolna: {type(e).__name__}")
        if resp.status_code >= 400:
            raise BolnaError(self._message(resp), resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def place_call(self, agent_id, recipient, from_number, user_data=None) -> dict:
        body: dict = {
            "agent_id": agent_id,
            "recipient_phone_number": recipient,
            "from_phone_number": from_number,
        }
        if user_data:
            body["user_data"] = user_data
        try:
            resp = await self._http.post("/call", json=body)  # not retried: not idempotent
        except httpx.HTTPError as e:
            raise BolnaError(f"Could not reach Bolna: {type(e).__name__}")
        if resp.status_code >= 400:
            raise BolnaError(self._message(resp), resp.status_code)
        return resp.json()

    async def get_execution(self, execution_id: str) -> dict:
        return await self._get(f"/executions/{execution_id}")

    async def get_agent(self, agent_id: str) -> dict | None:
        """Documented as GET /v2/agent/{id}. Returns {"name": ...}; None if it cannot be
        verified (network/5xx); raises AgentNotFoundOnBolna when Bolna says it does not exist."""
        try:
            data = await self._get(f"/v2/agent/{agent_id}", retries=2)
        except BolnaError as e:
            if e.status in (400, 404, 422):
                raise AgentNotFoundOnBolna(e.message, e.status)
            if e.status in (401, 403):
                raise
            return None
        name = data.get("agent_name") or data.get("name") if isinstance(data, dict) else None
        return {"name": name}

    async def aclose(self) -> None:
        await self._http.aclose()
