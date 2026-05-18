"""add section_availability table

Revision ID: 70093d163eca
Revises: e7cc3027e9bc
Create Date: 2026-05-18 12:38:25.108348

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '70093d163eca'
down_revision: Union[str, None] = 'e7cc3027e9bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'section_availability',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('section_name', sa.String(), nullable=False),
        sa.Column('is_available', sa.Boolean(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['events.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('section_availability')
