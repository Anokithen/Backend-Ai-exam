"""add language column to exams

Revision ID: 7c2f5a8e9b1d
Revises: 9b4d2e7f1a6c
Create Date: 2026-09-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7c2f5a8e9b1d'
down_revision = '9b4d2e7f1a6c'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('exams', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('language', sa.String(length=50), nullable=False, server_default='English')
        )
    with op.batch_alter_table('exams', schema=None) as batch_op:
        batch_op.alter_column('language', server_default=None)


def downgrade():
    with op.batch_alter_table('exams', schema=None) as batch_op:
        batch_op.drop_column('language')
