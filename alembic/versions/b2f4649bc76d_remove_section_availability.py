"""remove_section_availability

Revision ID: b2f4649bc76d
Revises: b778af5b417e
Create Date: 2026-05-18 13:43:48.227137

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2f4649bc76d'
down_revision: Union[str, None] = 'b778af5b417e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('section_availability_watches')
    op.drop_table('section_availability')


def downgrade() -> None:
    op.create_table('section_availability',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('event_id', sa.INTEGER(), nullable=False),
    sa.Column('section_name', sa.VARCHAR(), nullable=False),
    sa.Column('is_available', sa.BOOLEAN(), nullable=False),
    sa.Column('recorded_at', sa.DATETIME(), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('section_availability_watches',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('event_id', sa.INTEGER(), nullable=False),
    sa.Column('section_name', sa.VARCHAR(), nullable=False),
    sa.Column('created_at', sa.DATETIME(), nullable=False),
    sa.Column('is_active', sa.BOOLEAN(), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
