"""add_alert_logs

Revision ID: 812d1b19887c
Revises: 85a5a9757371
Create Date: 2026-05-27 17:28:51.500959

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '812d1b19887c'
down_revision: Union[str, None] = '85a5a9757371'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'alert_logs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id'), nullable=False),
        sa.Column('event_name', sa.String(), nullable=False),
        sa.Column('section_name', sa.String(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('price_at_alert', sa.Float(), nullable=False),
        sa.Column('target_price', sa.Float(), nullable=True),
        sa.Column('alerted_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_alert_logs_event_alerted', 'alert_logs', ['event_id', 'alerted_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_alert_logs_event_alerted', table_name='alert_logs')
    op.drop_table('alert_logs')
