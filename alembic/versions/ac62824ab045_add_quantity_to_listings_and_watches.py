"""add_quantity_to_listings_and_watches

Revision ID: ac62824ab045
Revises: 3286c160d642
Create Date: 2026-05-23 13:32:27.454005

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ac62824ab045'
down_revision: Union[str, None] = '3286c160d642'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('listings', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('quantity', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint(
            'uq_listing_event_name_qty', ['event_id', 'name', 'quantity']
        )

    with op.batch_alter_table('user_watches', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('quantity', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user_watches', recreate='always') as batch_op:
        batch_op.drop_column('quantity')

    with op.batch_alter_table('listings', recreate='always') as batch_op:
        batch_op.drop_constraint('uq_listing_event_name_qty', type_='unique')
        batch_op.drop_column('quantity')
