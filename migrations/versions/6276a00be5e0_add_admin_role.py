"""add admin role to users.role enum

Revision ID: 6276a00be5e0
Revises: cf6cf2834c1b
Create Date: 2026-09-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6276a00be5e0'
down_revision = 'cf6cf2834c1b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column(
            'role',
            existing_type=sa.Enum('TEACHER', 'STUDENT', name='userrole'),
            type_=sa.Enum('TEACHER', 'STUDENT', 'ADMIN', name='userrole'),
            existing_nullable=False,
        )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column(
            'role',
            existing_type=sa.Enum('TEACHER', 'STUDENT', 'ADMIN', name='userrole'),
            type_=sa.Enum('TEACHER', 'STUDENT', name='userrole'),
            existing_nullable=False,
        )
