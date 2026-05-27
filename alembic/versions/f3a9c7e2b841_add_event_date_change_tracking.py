"""add_event_date_change_tracking

Revision ID: f3a9c7e2b841
Revises: a1f3c2e8d590
Create Date: 2026-05-27 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c7e2b841'
down_revision: Union[str, None] = 'a1f3c2e8d590'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('events', sa.Column('previous_event_date', sa.String(), nullable=True))
    op.add_column('events', sa.Column('date_changed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('events') as batch_op:
        batch_op.drop_column('date_changed_at')
        batch_op.drop_column('previous_event_date')
