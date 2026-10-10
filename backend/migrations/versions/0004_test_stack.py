"""test stack: stack_order, awaiting-result marker

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('numbers', schema=None) as b:
        b.add_column(sa.Column('stack_order', sa.BigInteger(), nullable=True))
        b.add_column(sa.Column('test_awaiting_since', sa.DateTime(), nullable=True))
        b.create_index('ix_numbers_stack', ['stack_order', 'id'], unique=False)

    # Give every eligible pool number a position: never tested first (oldest added first),
    # then tested numbers, oldest test first.
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, last_tested_at, last_test_result, added_at FROM numbers "
        "WHERE status IN ('available','in_use','limit_reached') AND spam_flag = 0"
    )).fetchall()
    never = sorted((r for r in rows if r.last_tested_at is None and r.last_test_result is None),
                   key=lambda r: (str(r.added_at), r.id))
    tested = sorted((r for r in rows if not (r.last_tested_at is None and r.last_test_result is None)),
                    key=lambda r: (str(r.last_tested_at or ''), r.id))
    for pos, r in enumerate(never + tested, start=1):
        conn.execute(sa.text("UPDATE numbers SET stack_order = :p WHERE id = :i"), {"p": pos, "i": r.id})


def downgrade() -> None:
    with op.batch_alter_table('numbers', schema=None) as b:
        b.drop_index('ix_numbers_stack')
        b.drop_column('test_awaiting_since')
        b.drop_column('stack_order')
