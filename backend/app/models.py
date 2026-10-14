from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC (SQLite drops tzinfo anyway)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class Number(Base):
    __tablename__ = "numbers"
    __table_args__ = (
        Index("ix_numbers_stack", "stack_order", "id"),
        CheckConstraint("status IN ('available','in_use','limit_reached','discovered','ignored','releasing','released')",
        name="ck_number_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String(16), unique=True)
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="available")
    connected_count: Mapped[int] = mapped_column(Integer, default=0)
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    bolna_linked_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Sync from Bolna. 'discovered' / 'ignored' rows are NOT part of the pool.
    linked_in_bolna: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_auto_switch: Mapped[bool] = mapped_column(Boolean, default=True)
    missing_on_bolna: Mapped[bool] = mapped_column(Boolean, default=False)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Spam check. A person decides; a flagged number stays in the pool but is never allocated.
    spam_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    spam_marked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    spam_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_result: Mapped[str | None] = mapped_column(String(8), nullable=True)  # ok | spam
    bolna_phone_number_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bolna_rented: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # bought through Bolna
    # Test stack: ascending = top first. NULL = not in the stack (spam, not in the pool).
    stack_order: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # a test call ended and nobody has marked the number yet
    test_awaiting_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # when the number last went from the pool to an agent (kept by an idle move, cleared on return)
    taken_from_pool_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


POOL_STATUSES = ("available", "in_use", "limit_reached")


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    bolna_agent_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    # Invariant 1: a number belongs to at most one agent (UNIQUE), an agent has one number.
    current_number_id: Mapped[int | None] = mapped_column(
        ForeignKey("numbers.id"), unique=True, nullable=True
    )
    pending_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_switch_reason: Mapped[str | None] = mapped_column(String(8), nullable=True)  # limit | spam
    pool_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pool_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # idle-first swapping: a person can protect an agent, and an agent whose number was lent away
    # stays without one ("Number taken") until a person gives it a number
    never_lend: Mapped[bool] = mapped_column(Boolean, default=False)
    number_taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    number_taken_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    number_taken_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    current_number: Mapped[Number | None] = relationship(foreign_keys=[current_number_id])
    number_taken_by: Mapped["Agent | None"] = relationship(foreign_keys=[number_taken_by_agent_id], remote_side=[id])


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        # Only one open assignment per number and per agent, enforced by the database.
        Index("uq_open_assignment_number", "number_id", unique=True,
              sqlite_where=text("ended_at IS NULL"), postgresql_where=text("ended_at IS NULL")),
        Index("uq_open_assignment_agent", "agent_id", unique=True,
              sqlite_where=text("ended_at IS NULL"), postgresql_where=text("ended_at IS NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    number_id: Mapped[int] = mapped_column(ForeignKey("numbers.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # why it started: initial | manual | auto_threshold | auto_refill | auto_spam | auto_idle_swap
    reason: Mapped[str] = mapped_column(String(24))
    # why it ended: manual | auto_threshold | auto_spam | taken_idle | released (NULL while open)
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    connected_calls: Mapped[int] = mapped_column(Integer, default=0)

    agent: Mapped[Agent] = relationship()
    number: Mapped[Number] = relationship()


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_agent_placed", "agent_id", "placed_at"),
        Index("ix_calls_number_placed", "number_id", "placed_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    # Invariant 4: an execution is counted at most once. NULL until Bolna accepts the call.
    execution_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    number_id: Mapped[int] = mapped_column(ForeignKey("numbers.id"))
    recipient: Mapped[str] = mapped_column(String(16))
    placed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(32), default="placing")
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    connected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    purpose: Mapped[str] = mapped_column(String(8), default="lead")  # lead | test

    agent: Mapped[Agent] = relationship()
    number: Mapped[Number] = relationship()


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # success | failed
    total_on_bolna: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_invalid_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # JSON list of the raw values that were skipped (shown in the UI, never logged)
    skipped_numbers: Mapped[str | None] = mapped_column(Text, nullable=True)
    # released numbers that Bolna still lists (the delete may lag); never put back in the pool
    released_listed_count: Mapped[int] = mapped_column(Integer, default=0)


class NumberEvent(Base):
    __tablename__ = "number_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    number_id: Mapped[int] = mapped_column(ForeignKey("numbers.id"), index=True)
    # marked_spam | marked_not_spam | test_call_placed | test_result | release_started |
    # release_succeeded | release_failed
    event: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(String(500), default="")  # never a full phone number
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpamTestRun(Base):
    __tablename__ = "spam_test_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    test_agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    reviewer_phone: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | done


class SpamTestItem(Base):
    __tablename__ = "spam_test_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("spam_test_runs.id"), index=True)
    number_id: Mapped[int] = mapped_column(ForeignKey("numbers.id"))
    execution_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"), nullable=True)
    # queued | calling | done | failed
    status: Mapped[str] = mapped_column(String(12), default="queued")
    placed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
