"""add materials table for teacher pdf/image uploads

Revision ID: 8f1c2b4a9d3e
Revises: 6276a00be5e0
Create Date: 2026-09-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8f1c2b4a9d3e'
down_revision = '6276a00be5e0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'materials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('teacher_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=150), nullable=False),
        sa.Column('file_type', sa.Enum('PDF', 'IMAGE', name='materialtype'), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('stored_filename', sa.String(length=255), nullable=False),
        sa.Column('relative_path', sa.String(length=400), nullable=False),
        sa.Column('mime_type', sa.String(length=100), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['teacher_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
    )
    with op.batch_alter_table('materials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_materials_teacher_id'), ['teacher_id'], unique=False)


def downgrade():
    with op.batch_alter_table('materials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_materials_teacher_id'))

    op.drop_table('materials')
