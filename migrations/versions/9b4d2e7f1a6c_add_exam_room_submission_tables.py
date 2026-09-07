"""add exam, question, exam_room, room_participant, submission, answer tables

Revision ID: 9b4d2e7f1a6c
Revises: 3a7e9c1d4f2b
Create Date: 2026-09-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9b4d2e7f1a6c'
down_revision = '3a7e9c1d4f2b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'exams',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('teacher_id', sa.Integer(), nullable=False),
        sa.Column('material_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=150), nullable=False),
        sa.Column('instructions', sa.Text(), nullable=True),
        sa.Column('status', sa.Enum('DRAFT', 'PUBLISHED', name='examstatus'), nullable=False),
        sa.Column('time_limit_minutes', sa.Integer(), nullable=False),
        sa.Column('total_marks', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['teacher_id'], ['users.id']),
        sa.ForeignKeyConstraint(['material_id'], ['materials.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
    )
    with op.batch_alter_table('exams', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exams_teacher_id'), ['teacher_id'], unique=False)

    op.create_table(
        'questions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('exam_id', sa.Integer(), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=False),
        sa.Column('type', sa.Enum('MCQ', 'STRUCTURED', 'ESSAY', name='questiontype'), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('options', sa.JSON(), nullable=True),
        sa.Column('correct_option_index', sa.Integer(), nullable=True),
        sa.Column('marks', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['exam_id'], ['exams.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
    )
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_questions_exam_id'), ['exam_id'], unique=False)

    op.create_table(
        'exam_rooms',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('exam_id', sa.Integer(), nullable=False),
        sa.Column('teacher_id', sa.Integer(), nullable=False),
        sa.Column('invite_code', sa.String(length=8), nullable=False),
        sa.Column('time_limit_minutes', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('OPEN', 'CLOSED', name='roomstatus'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['exam_id'], ['exams.id']),
        sa.ForeignKeyConstraint(['teacher_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
        sa.UniqueConstraint('invite_code'),
    )
    with op.batch_alter_table('exam_rooms', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exam_rooms_exam_id'), ['exam_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exam_rooms_teacher_id'), ['teacher_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exam_rooms_invite_code'), ['invite_code'], unique=False)

    op.create_table(
        'room_participants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('room_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['room_id'], ['exam_rooms.id']),
        sa.ForeignKeyConstraint(['student_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('room_id', 'student_id', name='uq_room_student'),
    )
    with op.batch_alter_table('room_participants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_room_participants_room_id'), ['room_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_room_participants_student_id'), ['student_id'], unique=False)

    op.create_table(
        'submissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('room_id', sa.Integer(), nullable=False),
        sa.Column('participant_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('exam_id', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Enum('IN_PROGRESS', 'SUBMITTED', 'GRADED', name='submissionstatus'), nullable=False),
        sa.Column('total_score', sa.Integer(), nullable=True),
        sa.Column('max_score', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['room_id'], ['exam_rooms.id']),
        sa.ForeignKeyConstraint(['participant_id'], ['room_participants.id']),
        sa.ForeignKeyConstraint(['student_id'], ['users.id']),
        sa.ForeignKeyConstraint(['exam_id'], ['exams.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
        sa.UniqueConstraint('participant_id'),
    )
    with op.batch_alter_table('submissions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_submissions_room_id'), ['room_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_submissions_student_id'), ['student_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_submissions_exam_id'), ['exam_id'], unique=False)

    op.create_table(
        'answers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('submission_id', sa.Integer(), nullable=False),
        sa.Column('question_id', sa.Integer(), nullable=False),
        sa.Column('selected_option_index', sa.Integer(), nullable=True),
        sa.Column('response_text', sa.Text(), nullable=True),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('feedback', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['submission_id'], ['submissions.id']),
        sa.ForeignKeyConstraint(['question_id'], ['questions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('submission_id', 'question_id', name='uq_submission_question'),
    )
    with op.batch_alter_table('answers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_answers_submission_id'), ['submission_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_answers_question_id'), ['question_id'], unique=False)


def downgrade():
    op.drop_table('answers')
    op.drop_table('submissions')
    op.drop_table('room_participants')
    op.drop_table('exam_rooms')
    op.drop_table('questions')
    op.drop_table('exams')
