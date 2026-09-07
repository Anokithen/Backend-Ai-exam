import enum
import uuid
from datetime import timezone

from app.extensions import db
from app.models.base import TimestampMixin
from app.models.exam_model import QuestionType
from app.utils.time import iso_utc, utc_now


class SubmissionStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    GRADED = "graded"


class Submission(db.Model, TimestampMixin):
    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    room_id = db.Column(db.Integer, db.ForeignKey("exam_rooms.id"), nullable=False, index=True)
    participant_id = db.Column(
        db.Integer, db.ForeignKey("room_participants.id"), unique=True, nullable=False
    )
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False, index=True)

    started_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.Enum(SubmissionStatus), nullable=False, default=SubmissionStatus.IN_PROGRESS)
    total_score = db.Column(db.Integer, nullable=True)
    max_score = db.Column(db.Integer, nullable=False)

    room = db.relationship("ExamRoom")
    participant = db.relationship("RoomParticipant", back_populates="submission")
    student = db.relationship("User", backref=db.backref("submissions", cascade="all, delete-orphan"))
    exam = db.relationship("Exam")
    answers = db.relationship("Answer", back_populates="submission", cascade="all, delete-orphan")

    def is_expired(self) -> bool:
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            # MySQL DATETIME columns drop tzinfo on round-trip; the value was always stored as UTC.
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return utc_now() >= expires_at

    def expire_if_stale(self) -> bool:
        """Force-submit a still-open submission whose time limit has passed. Returns True if it changed state."""
        if self.status == SubmissionStatus.IN_PROGRESS and self.is_expired():
            self.finalize()
            return True
        return False

    def finalize(self):
        """Auto-grade MCQ answers and move the submission out of in_progress."""
        answers_by_question = {answer.question_id: answer for answer in self.answers}
        for question in self.exam.questions:
            if question.type != QuestionType.MCQ:
                continue
            answer = answers_by_question.get(question.id)
            if answer is None:
                continue
            answer.score = (
                question.marks
                if answer.selected_option_index == question.correct_option_index
                else 0
            )

        self.submitted_at = utc_now()
        self.recompute_score()

    def recompute_score(self):
        scored = [answer.score for answer in self.answers if answer.score is not None]
        self.total_score = sum(scored) if scored else 0
        all_graded = all(answer.score is not None for answer in self.answers) and len(self.answers) == len(
            self.exam.questions
        )
        self.status = SubmissionStatus.GRADED if all_graded else SubmissionStatus.SUBMITTED

    def to_dict(self, include_answers=False):
        data = {
            "id": self.public_id,
            "student_name": self.student.full_name,
            "started_at": iso_utc(self.started_at),
            "expires_at": iso_utc(self.expires_at),
            "submitted_at": iso_utc(self.submitted_at),
            "status": self.status.value,
            "total_score": self.total_score,
            "max_score": self.max_score,
        }
        if include_answers:
            data["answers"] = [answer.to_dict() for answer in self.answers]
        return data


class Answer(db.Model, TimestampMixin):
    __tablename__ = "answers"
    __table_args__ = (db.UniqueConstraint("submission_id", "question_id", name="uq_submission_question"),)

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submissions.id"), nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False, index=True)

    selected_option_index = db.Column(db.Integer, nullable=True)
    response_text = db.Column(db.Text, nullable=True)
    score = db.Column(db.Integer, nullable=True)
    feedback = db.Column(db.Text, nullable=True)

    submission = db.relationship("Submission", back_populates="answers")
    question = db.relationship("Question")

    def to_dict(self):
        return {
            "question_id": self.question.public_id,
            "question_prompt": self.question.prompt,
            "question_type": self.question.type.value,
            "marks": self.question.marks,
            "selected_option_index": self.selected_option_index,
            "response_text": self.response_text,
            "score": self.score,
            "feedback": self.feedback,
        }
