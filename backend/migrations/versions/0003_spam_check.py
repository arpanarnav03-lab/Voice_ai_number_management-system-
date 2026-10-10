"""spam check: spam flag, test calls, release, number events

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

OLD = "status IN ('available','in_use','limit_reached','discovered','ignored')"
NEW = "status IN ('available','in_use','limit_reached','discovered','ignored','releasing','released')"


def upgrade() -> None:
    with op.batch_alter_table('numbers', schema=None, recreate='always') as b:
        b.add_column(sa.Column('spam_flag', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('spam_marked_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('spam_note', sa.Text(), nullable=True))
        b.add_column(sa.Column('last_tested_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('last_test_result', sa.String(length=8), nullable=True))
        b.add_column(sa.Column('bolna_phone_number_id', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('bolna_rented', sa.Boolean(), nullable=True))
        b.drop_constraint('ck_number_status', type_='check')
        b.create_check_constraint('ck_number_status', NEW)
    with op.batch_alter_table('agents', schema=None) as b:
        b.add_column(sa.Column('pending_switch_reason', sa.String(length=8), nullable=True))
    # A switch that was already waiting was waiting for the limit.
    op.execute("UPDATE agents SET pending_switch_reason = 'limit' WHERE pending_switch = 1")
    with op.batch_alter_table('calls', schema=None) as b:
        b.add_column(sa.Column('purpose', sa.String(length=8), nullable=False, server_default='lead'))
    with op.batch_alter_table('sync_runs', schema=None) as b:
        b.add_column(sa.Column('released_listed_count', sa.Integer(), nullable=False, server_default='0'))

    op.create_table(
        'number_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('number_id', sa.Integer(), nullable=False),
        sa.Column('event', sa.String(length=24), nullable=False),
        sa.Column('detail', sa.String(length=500), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['number_id'], ['numbers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_number_events_number_id', 'number_events', ['number_id'])
    op.create_table(
        'spam_test_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('test_agent_id', sa.Integer(), nullable=False),
        sa.Column('reviewer_phone', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(['test_agent_id'], ['agents.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'spam_test_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('number_id', sa.Integer(), nullable=False),
        sa.Column('execution_id', sa.String(length=80), nullable=True),
        sa.Column('call_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('placed_at', sa.DateTime(), nullable=True),
        sa.Column('detail', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['spam_test_runs.id']),
        sa.ForeignKeyConstraint(['number_id'], ['numbers.id']),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_spam_test_items_run_id', 'spam_test_items', ['run_id'])


def downgrade() -> None:
    op.drop_table('spam_test_items')
    op.drop_table('spam_test_runs')
    op.drop_table('number_events')
    with op.batch_alter_table('sync_runs', schema=None) as b:
        b.drop_column('released_listed_count')
    with op.batch_alter_table('calls', schema=None) as b:
        b.drop_column('purpose')
    with op.batch_alter_table('agents', schema=None) as b:
        b.drop_column('pending_switch_reason')
    op.execute("DELETE FROM numbers WHERE status IN ('releasing','released')")
    with op.batch_alter_table('numbers', schema=None, recreate='always') as b:
        b.drop_constraint('ck_number_status', type_='check')
        b.create_check_constraint('ck_number_status', OLD)
        for c in ('bolna_rented', 'bolna_phone_number_id', 'last_test_result', 'last_tested_at',
                  'spam_note', 'spam_marked_at', 'spam_flag'):
            b.drop_column(c)
