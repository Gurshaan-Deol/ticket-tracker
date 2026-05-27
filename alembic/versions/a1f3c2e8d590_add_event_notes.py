"""add_event_notes

Revision ID: a1f3c2e8d590
Revises: 812d1b19887c
Create Date: 2026-05-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f3c2e8d590'
down_revision: Union[str, None] = '812d1b19887c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite supports ADD COLUMN; existing rows get NULL automatically.
    op.add_column('events', sa.Column('notes', sa.Text(), nullable=True))


def downgrade() -> None:
    # SQLite does not support DROP COLUMN in older versions;
    # recreate the table without the column.
    with op.batch_alter_table('events') as batch_op:
        batch_op.drop_column('notes')
