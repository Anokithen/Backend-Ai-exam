"""switch materials storage to cloudinary

Revision ID: 3a7e9c1d4f2b
Revises: 8f1c2b4a9d3e
Create Date: 2026-09-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3a7e9c1d4f2b'
down_revision = '8f1c2b4a9d3e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('materials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cloudinary_public_id', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('cloudinary_resource_type', sa.String(length=20), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('secure_url', sa.String(length=500), nullable=False, server_default=''))
        batch_op.drop_column('stored_filename')
        batch_op.drop_column('relative_path')

    with op.batch_alter_table('materials', schema=None) as batch_op:
        batch_op.alter_column('cloudinary_public_id', server_default=None)
        batch_op.alter_column('cloudinary_resource_type', server_default=None)
        batch_op.alter_column('secure_url', server_default=None)


def downgrade():
    with op.batch_alter_table('materials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stored_filename', sa.String(length=255), nullable=False))
        batch_op.add_column(sa.Column('relative_path', sa.String(length=400), nullable=False))
        batch_op.drop_column('secure_url')
        batch_op.drop_column('cloudinary_resource_type')
        batch_op.drop_column('cloudinary_public_id')
