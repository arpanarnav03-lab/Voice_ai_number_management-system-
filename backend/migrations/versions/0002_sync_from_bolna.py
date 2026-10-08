"""sync from bolna: discovered/ignored numbers, missing flags, sync_runs

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

OLD = "status IN ('available','in_use','limit_reached')"
NEW = "status IN ('available','in_use','limit_reached','discovered','ignored')"


def upgrade() -> None:
    with op.batch_alter_table('numbers', schema=None, recreate='always') as b:
        b.add_column(sa.Column('linked_in_bolna', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('allow_auto_switch', sa.Boolean(), nullable=False, server_default=sa.true()))
        b.add_column(sa.Column('missing_on_bolna', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('missing_since', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('discovered_at', sa.DateTime(), nullable=True))
        b.drop_constraint('ck_number_status', type_='check')
        b.create_check_constraint('ck_number_status', NEW)
    # Numbers already linked to an agent inside Bolna stay out of auto-switching, as before.
    op.execute("UPDATE numbers SET linked_in_bolna = 1, allow_auto_switch = 0 "
               "WHERE bolna_linked_agent_id IS NOT NULL")
    op.create_table(
        'sync_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('total_on_bolna', sa.Integer(), nullable=False),
        sa.Column('new_count', sa.Integer(), nullable=False),
        sa.Column('missing_count', sa.Integer(), nullable=False),
        sa.Column('skipped_invalid_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('skipped_numbers', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('sync_runs')
    op.execute("DELETE FROM numbers WHERE status IN ('discovered','ignored')")
    with op.batch_alter_table('numbers', schema=None, recreate='always') as b:
        b.drop_constraint('ck_number_status', type_='check')
        b.create_check_constraint('ck_number_status', OLD)
        b.drop_column('discovered_at')
        b.drop_column('missing_since')
        b.drop_column('missing_on_bolna')
        b.drop_column('allow_auto_switch')
        b.drop_column('linked_in_bolna')
