import enum
import uuid

from app.extensions import db
from app.models.base import TimestampMixin
from app.utils.time import iso_utc


class RoomStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class ExamRoom(db.Model, TimestampMixin):
    __tablename__ = "exam_rooms"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False, index=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    invite_code = db.Column(db.String(8), unique=True, nullable=False, index=True)
    time_limit_minutes = db.Column(db.Integer, nullable=False)
    status = db.Column(db.Enum(RoomStatus), nullable=False, default=RoomStatus.OPEN)

    exam = db.relationship("Exam", back_populates="rooms")
    teacher = db.relationship("User", backref=db.backref("exam_rooms", cascade="all, delete-orphan"))
    participants = db.relationship(
        "RoomParticipant", back_populates="room", cascade="all, delete-orphan"
    )

    def to_dict(self, include_exam=False):
        data = {
            "id": self.public_id,
            "invite_code": self.invite_code,
            "time_limit_minutes": self.time_limit_minutes,
            "status": self.status.value,
            "participant_count": len(self.participants),
            "created_at": iso_utc(self.created_at),
        }
        if include_exam:
            data["exam"] = self.exam.to_dict()
        else:
            data["exam_id"] = self.exam.public_id
            data["exam_title"] = self.exam.title
        return data


class RoomParticipant(db.Model, TimestampMixin):
    __tablename__ = "room_participants"
    __table_args__ = (db.UniqueConstraint("room_id", "student_id", name="uq_room_student"),)

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("exam_rooms.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    room = db.relationship("ExamRoom", back_populates="participants")
    student = db.relationship("User", backref=db.backref("room_participations", cascade="all, delete-orphan"))
    submission = db.relationship(
        "Submission", back_populates="participant", uselist=False, cascade="all, delete-orphan"
    )

    def to_dict(self):
        return {
            "student_name": self.student.full_name,
            "student_email": self.student.email,
            "joined_at": iso_utc(self.created_at),
            "submission": self.submission.to_dict() if self.submission else None,
        }
