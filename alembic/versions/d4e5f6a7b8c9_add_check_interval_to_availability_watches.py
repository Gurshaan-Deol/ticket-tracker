"""add_check_interval_to_availability_watches

Revision ID: d4e5f6a7b8c9
Revises: c7d2f1a84b3e
Create Date: 2026-06-01 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c7d2f1a84b3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'availability_watches',
        sa.Column('check_interval_minutes', sa.Integer(), nullable=False, server_default='30'),
    )


def downgrade() -> None:
    op.drop_column('availability_watches', 'check_interval_minutes')
