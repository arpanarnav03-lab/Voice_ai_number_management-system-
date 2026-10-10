"""backfill the test stack so it holds every non-spam pool number

Revision ID: 0005
Revises: 0004

Plain SQL on purpose (no ORM models), so it keeps working whatever the models become later.
Mirrors app.stack.reconcile_stack and is safe to run twice."""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

POOL = ('available', 'in_use', 'limit_reached')


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, status, spam_flag, stack_order, last_tested_at, last_test_result, added_at FROM numbers"
    )).fetchall()
    new_pos = {r.id: r.stack_order for r in rows}
    members = [r for r in rows if r.status in POOL and not r.spam_flag]
    # 1. numbers that do not belong lose their position
    for r in rows:
        if not (r.status in POOL and not r.spam_flag):
            new_pos[r.id] = None
    # 2. duplicates among valid positions: re-rank in the current order
    positioned = sorted((r for r in members if r.stack_order is not None), key=lambda r: (r.stack_order, r.id))
    if len({r.stack_order for r in positioned}) != len(positioned):
        base = positioned[0].stack_order
        for i, r in enumerate(positioned):
            new_pos[r.id] = base + i
    cur = [p for p in (new_pos[r.id] for r in positioned)]
    lo, hi = (min(cur), max(cur)) if cur else (0, 0)
    # 3. qualifying numbers without a position: never tested -> top, tested -> bottom
    missing = [r for r in members if r.stack_order is None]
    never = sorted((r for r in missing if r.last_tested_at is None and r.last_test_result is None),
                   key=lambda r: (str(r.added_at), r.id))
    tested = sorted((r for r in missing if not (r.last_tested_at is None and r.last_test_result is None)),
                    key=lambda r: (str(r.last_tested_at or r.added_at), r.id))
    top = (lo if cur else 0) - len(never)
    for i, r in enumerate(never):
        new_pos[r.id] = top + i
    bottom = (hi + 1) if cur else (top + len(never))
    for i, r in enumerate(tested):
        new_pos[r.id] = bottom + i
    for r in rows:
        if new_pos[r.id] != r.stack_order:
            conn.execute(sa.text("UPDATE numbers SET stack_order = :p WHERE id = :i"), {"p": new_pos[r.id], "i": r.id})


def downgrade() -> None:
    pass  # a repaired stack is still a valid stack
