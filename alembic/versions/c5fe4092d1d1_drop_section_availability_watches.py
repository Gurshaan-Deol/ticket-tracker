"""drop_section_availability_watches

Revision ID: c5fe4092d1d1
Revises: b2f4649bc76d
Create Date: 2026-05-18 14:01:19.968262

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5fe4092d1d1'
down_revision: Union[str, None] = 'b2f4649bc76d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('section_availability_watches')


def downgrade() -> None:
    op.create_table('section_availability_watches',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('event_id', sa.INTEGER(), nullable=False),
    sa.Column('section_name', sa.VARCHAR(), nullable=False),
    sa.Column('created_at', sa.DATETIME(), nullable=False),
    sa.Column('is_active', sa.BOOLEAN(), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
