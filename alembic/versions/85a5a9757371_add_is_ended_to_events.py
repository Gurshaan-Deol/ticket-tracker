"""add_is_ended_to_events

Revision ID: 85a5a9757371
Revises: 5cf13f9f4e5a
Create Date: 2026-05-27 14:05:08.162812

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '85a5a9757371'
down_revision: Union[str, None] = '5cf13f9f4e5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('events', sa.Column('is_ended', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('events', 'is_ended')
