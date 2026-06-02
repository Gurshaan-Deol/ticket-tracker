"""add_app_settings

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('default_refresh_interval_minutes', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('default_alert_cooldown_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('default_availability_cooldown_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('default_availability_interval_minutes', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('default_any_listing_interval_minutes', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('default_any_listing_cooldown_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
