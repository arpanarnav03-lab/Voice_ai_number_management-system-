"""idle-first swapping: lendable agents, Number taken state, taken_from_pool_at

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('agents', schema=None) as b:
        b.add_column(sa.Column('never_lend', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('number_taken_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('number_taken_by_agent_id', sa.Integer(), nullable=True))
        b.add_column(sa.Column('number_taken_reason', sa.String(length=255), nullable=True))
    with op.batch_alter_table('numbers', schema=None) as b:
        b.add_column(sa.Column('taken_from_pool_at', sa.DateTime(), nullable=True))
    # numbers held right now were taken from the pool when their current assignment started
    op.execute(
        "UPDATE numbers SET taken_from_pool_at = ("
        " SELECT MAX(a.started_at) FROM assignments a"
        " WHERE a.number_id = numbers.id AND a.ended_at IS NULL)"
        " WHERE id IN (SELECT current_number_id FROM agents WHERE current_number_id IS NOT NULL)"
    )


def downgrade() -> None:
    with op.batch_alter_table('numbers', schema=None) as b:
        b.drop_column('taken_from_pool_at')
    with op.batch_alter_table('agents', schema=None) as b:
        b.drop_column('number_taken_reason')
        b.drop_column('number_taken_by_agent_id')
        b.drop_column('number_taken_at')
        b.drop_column('never_lend')
