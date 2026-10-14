"""A minimal record of who changed what, for sessions shared through a tunnel (there is no login).

One line per request that changes something (POST, PUT, PATCH, DELETE):
    2026-10-12T10:15:03Z POST /api/numbers/12/mark-spam 200 ip=203.0.113.9
It never records request bodies, query strings, headers other than the client address, or full phone
numbers (any long digit run in the path is masked to its last four digits). Writes go through a queue,
so a request never waits for the disk, and the middleware never touches a response."""
from __future__ import annotations

import logging
import logging.handlers
import queue
import re
from datetime import datetime, timezone
from pathlib import Path

CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_LONG_DIGITS = re.compile(r"\+?\d{7,}")
_UNSAFE = re.compile(r"[^\x21-\x7e]")  # anything but visible ASCII: blocks log-line injection


def mask_digits(text: str) -> str:
    """+918000000001 -> +********0001 (all but the last four digits)."""
    def mask(m: re.Match) -> str:
        raw = m.group(0)
        plus = "+" if raw.startswith("+") else ""
        digits = raw.lstrip("+")
        return plus + "*" * (len(digits) - 4) + digits[-4:]
    return _LONG_DIGITS.sub(mask, text)


def clean(text: str, limit: int = 200) -> str:
    return _UNSAFE.sub("?", text)[:limit]


def format_action_line(when: datetime, method: str, path: str, status: int, ip: str) -> str:
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} {method} {clean(mask_digits(path))} {status} ip={clean(ip, 64) or '-'}"


def client_ip(scope: dict) -> str:
    """CF-Connecting-IP when the request came through the tunnel, otherwise the direct client."""
    for name, value in scope.get("headers", []):
        if name == b"cf-connecting-ip":
            return value.decode("latin-1").strip()
    client = scope.get("client")
    return client[0] if client else "-"


class ActionLog:
    """Owns the file and the queue that feeds it."""

    def __init__(self, path: str):
        self.logger = logging.getLogger(f"bnm.actions.{id(self)}")
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue = queue.Queue()
        self._listener = logging.handlers.QueueListener(
            self._queue, _file_handler(path), respect_handler_level=True)
        self.logger.addHandler(logging.handlers.QueueHandler(self._queue))
        self._listener.start()

    def record(self, line: str) -> None:
        self.logger.info(line)

    def close(self) -> None:
        self._listener.stop()  # flushes what is queued


def _file_handler(path: str) -> logging.Handler:
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


class ActionLogMiddleware:
    """Pure ASGI: it only watches the response status go by."""

    def __init__(self, app, log: ActionLog):
        self.app, self.log = app, log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in CHANGING:
            return await self.app(scope, receive, send)
        status = {"code": 500}

        async def watching_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, watching_send)
        finally:
            try:
                self.log.record(format_action_line(
                    datetime.now(timezone.utc), scope["method"], scope["path"], status["code"], client_ip(scope)))
            except Exception:  # logging must never change a response
                pass
