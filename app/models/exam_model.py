import enum
import uuid

from app.extensions import db
from app.models.base import TimestampMixin
from app.utils.time import iso_utc


class ExamStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class QuestionType(str, enum.Enum):
    MCQ = "mcq"
    STRUCTURED = "structured"
    ESSAY = "essay"


class Exam(db.Model, TimestampMixin):
    __tablename__ = "exams"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    material_id = db.Column(db.Integer, db.ForeignKey("materials.id"), nullable=True)

    title = db.Column(db.String(150), nullable=False)
    instructions = db.Column(db.Text, nullable=True)
    status = db.Column(db.Enum(ExamStatus), nullable=False, default=ExamStatus.DRAFT)
    time_limit_minutes = db.Column(db.Integer, nullable=False, default=60)
    total_marks = db.Column(db.Integer, nullable=False, default=0)
    language = db.Column(db.String(50), nullable=False, default="English")

    teacher = db.relationship("User", backref=db.backref("exams", cascade="all, delete-orphan"))
    material = db.relationship("Material")
    questions = db.relationship(
        "Question",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="Question.order_index",
    )
    rooms = db.relationship("ExamRoom", back_populates="exam", cascade="all, delete-orphan")

    def recompute_total_marks(self):
        self.total_marks = sum(question.marks for question in self.questions)

    def to_dict(self, include_questions=False, reveal_answers=True):
        data = {
            "id": self.public_id,
            "title": self.title,
            "instructions": self.instructions,
            "status": self.status.value,
            "time_limit_minutes": self.time_limit_minutes,
            "total_marks": self.total_marks,
            "language": self.language,
            "material_id": self.material.public_id if self.material else None,
            "question_count": len(self.questions),
            "created_at": iso_utc(self.created_at),
        }
        if include_questions:
            data["questions"] = [q.to_dict(reveal_answer=reveal_answers) for q in self.questions]
        return data


class Question(db.Model, TimestampMixin):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False, index=True)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    type = db.Column(db.Enum(QuestionType), nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    options = db.Column(db.JSON, nullable=True)
    correct_option_index = db.Column(db.Integer, nullable=True)
    marks = db.Column(db.Integer, nullable=False, default=1)

    exam = db.relationship("Exam", back_populates="questions")

    def to_dict(self, reveal_answer=True):
        data = {
            "id": self.public_id,
            "order_index": self.order_index,
            "type": self.type.value,
            "prompt": self.prompt,
            "options": self.options,
            "marks": self.marks,
        }
        if reveal_answer:
            data["correct_option_index"] = self.correct_option_index
        return data
