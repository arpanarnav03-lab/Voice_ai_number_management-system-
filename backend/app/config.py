"""Settings, read from the environment / .env. The API key never leaves the server."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _f(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


@dataclass
class Settings:
    bolna_api_key: str = ""
    app_password: str = ""
    database_url: str = "sqlite:///./bolna_manager.db"
    poll_seconds: float = 5.0
    bolna_mode: str = "fake"  # "fake" (default) or "real"
    min_connected_seconds: float = 1.0
    call_timeout_minutes: float = 30.0
    unconfirmed_call_minutes: float = 2.0  # placed locally but Bolna never answered
    default_threshold: int = 5
    sync_interval_minutes: float = 60.0  # 0 turns the scheduled sync (and the startup sync) off
    sync_auto_approve: bool = False  # unlinked discovered numbers go straight to 'available'
    host: str = "127.0.0.1"
    port: int = 8000
    start_poller: bool = True
    run_migrations: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        s = cls(
            bolna_api_key=os.getenv("BOLNA_API_KEY", "").strip(),
            app_password=os.getenv("APP_PASSWORD", ""),
            database_url=os.getenv("DATABASE_URL", "").strip() or cls.database_url,
            poll_seconds=_f("POLL_SECONDS", 5.0),
            bolna_mode=(os.getenv("BOLNA_MODE", "").strip().lower() or "fake"),
            min_connected_seconds=_f("MIN_CONNECTED_SECONDS", 1.0),
            call_timeout_minutes=_f("CALL_TIMEOUT_MINUTES", 30.0),
            sync_interval_minutes=_f("SYNC_INTERVAL_MINUTES", 60.0),
            sync_auto_approve=os.getenv("SYNC_AUTO_APPROVE", "").strip().lower() in ("1", "true", "yes", "on"),
            host=os.getenv("HOST", "").strip() or "127.0.0.1",
            port=int(_f("PORT", 8000)),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.bolna_mode not in ("fake", "real"):
            raise SystemExit("BOLNA_MODE must be 'fake' or 'real'")
        if self.sync_interval_minutes < 0:
            raise SystemExit("SYNC_INTERVAL_MINUTES must be 0 or more")
        if self.bolna_mode == "real" and not self.bolna_api_key:
            raise SystemExit("BOLNA_MODE=real needs BOLNA_API_KEY in .env")
