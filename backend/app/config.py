"""Settings, read from the environment / .env. The API key never leaves the server."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _flag(name: str, default: bool) -> bool:
    """true/false (also 1/0, yes/no, on/off); anything else stops startup with a clear message."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise SystemExit(f"{name} must be true or false (got {raw!r})")


def _hours(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number of hours, {minimum:g} or more (got {raw!r})")
    if not value >= minimum:  # also rejects nan
        raise SystemExit(f"{name} must be a number of hours, {minimum:g} or more (got {raw!r})")
    return value


def _f(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _action_log_path() -> str:
    """Where the record of changing requests goes. ACTION_LOG_FILE overrides it ("" switches it off)."""
    from pathlib import Path
    raw = os.getenv("ACTION_LOG_FILE")
    if raw is not None:
        return raw.strip()
    return str(Path(__file__).resolve().parent.parent.parent / ".share" / "actions.log")


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
    allow_bolna_number_delete: bool = False  # the release flow may DELETE numbers in Bolna
    idle_first_enabled: bool = True  # automatic swaps take an idle agent's number before the pool's
    idle_hours: float = 24.0  # an agent with no call for this long may lend its number
    number_move_cooldown_hours: float = 24.0  # a number moved by an idle swap stays put this long
    action_log_path: str = ""  # "" = off. Normal startup turns it on (see from_env).
    spam_test_max_per_run: int = 0  # 0 = no cap on how many numbers one run can test
    test_result_expiry_hours: float = 24.0  # an unmarked test result is dismissed after this long
    spam_test_delay_seconds: float = 5.0  # pause between two test calls of a run
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
            allow_bolna_number_delete=os.getenv("ALLOW_BOLNA_NUMBER_DELETE", "").strip().lower()
            in ("1", "true", "yes", "on"),
            spam_test_max_per_run=int(_f("SPAM_TEST_MAX_PER_RUN", 0)),
            test_result_expiry_hours=_f("TEST_RESULT_EXPIRY_HOURS", 24.0),
            action_log_path=_action_log_path(),
            idle_first_enabled=_flag("IDLE_FIRST_ENABLED", True),
            idle_hours=_hours("IDLE_HOURS", 24.0, 1.0),
            number_move_cooldown_hours=_hours("NUMBER_MOVE_COOLDOWN_HOURS", 24.0, 0.0),
            host=os.getenv("HOST", "").strip() or "127.0.0.1",
            port=int(_f("PORT", 8000)),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.bolna_mode not in ("fake", "real"):
            raise SystemExit("BOLNA_MODE must be 'fake' or 'real'")
        if not self.idle_hours >= 1:
            raise SystemExit("IDLE_HOURS must be a number of hours, 1 or more")
        if not self.number_move_cooldown_hours >= 0:
            raise SystemExit("NUMBER_MOVE_COOLDOWN_HOURS must be a number of hours, 0 or more")
        if self.spam_test_max_per_run < 0:
            raise SystemExit("SPAM_TEST_MAX_PER_RUN must be 0 (no cap) or more")
        if self.test_result_expiry_hours <= 0:
            raise SystemExit("TEST_RESULT_EXPIRY_HOURS must be more than 0")
        if self.sync_interval_minutes < 0:
            raise SystemExit("SYNC_INTERVAL_MINUTES must be 0 or more")
        if self.bolna_mode == "real" and not self.bolna_api_key:
            raise SystemExit("BOLNA_MODE=real needs BOLNA_API_KEY in .env")
