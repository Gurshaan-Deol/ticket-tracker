"""add_is_any_listing_to_availability_watches

Revision ID: c7d2f1a84b3e
Revises: f3a9c7e2b841
Create Date: 2026-06-01 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c7d2f1a84b3e'
down_revision: Union[str, None] = 'f3a9c7e2b841'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'availability_watches',
        sa.Column('is_any_listing', sa.Boolean(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('availability_watches', 'is_any_listing')
