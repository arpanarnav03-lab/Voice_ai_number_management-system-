from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    """Engine + session factory. SQLite gets WAL, FK enforcement and BEGIN IMMEDIATE
    on every transaction, so 'start a call' and 'switch number' can never interleave."""

    def __init__(self, url: str):
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        kwargs = {}
        if self.is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, future=True, **kwargs)
        if self.is_sqlite:
            self._configure_sqlite()
        self._factory = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self.session_info: dict = {}  # copied into every session (e.g. the allocation rules)

    def _configure_sqlite(self) -> None:
        @event.listens_for(self.engine, "connect")
        def _on_connect(dbapi_conn, _):
            dbapi_conn.isolation_level = None  # we emit BEGIN ourselves
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(self.engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def session(self):
        s: Session = self._factory()
        s.info.update(self.session_info)
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def migrate(self) -> None:
        """Apply Alembic migrations (used at app start-up)."""
        from alembic import command
        from alembic.config import Config

        root = Path(__file__).resolve().parent.parent
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        command.upgrade(cfg, "head")

    def dispose(self) -> None:
        self.engine.dispose()
