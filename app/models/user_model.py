import enum
import uuid

from app.extensions import db
from app.models.base import TimestampMixin
from app.utils.time import iso_utc


class UserRole(str, enum.Enum):
    TEACHER = "teacher"
    STUDENT = "student"
    ADMIN = "admin"


class User(db.Model, TimestampMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.Enum(UserRole), nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    teacher_profile = db.relationship(
        "TeacherProfile", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    student_profile = db.relationship(
        "StudentProfile", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    def to_dict(self):
        data = {
            "id": self.public_id,
            "full_name": self.full_name,
            "email": self.email,
            "role": self.role.value,
            "is_active": self.is_active,
            "created_at": iso_utc(self.created_at),
        }
        if self.role == UserRole.TEACHER and self.teacher_profile:
            data["profile"] = self.teacher_profile.to_dict()
        elif self.role == UserRole.STUDENT and self.student_profile:
            data["profile"] = self.student_profile.to_dict()
        return data


class TeacherProfile(db.Model, TimestampMixin):
    __tablename__ = "teacher_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    institution = db.Column(db.String(150))
    subject_specialization = db.Column(db.String(150))
    bio = db.Column(db.Text)

    user = db.relationship("User", back_populates="teacher_profile")

    def to_dict(self):
        return {
            "institution": self.institution,
            "subject_specialization": self.subject_specialization,
            "bio": self.bio,
        }


class StudentProfile(db.Model, TimestampMixin):
    __tablename__ = "student_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    institution = db.Column(db.String(150))
    grade_level = db.Column(db.String(50))
    bio = db.Column(db.Text)

    user = db.relationship("User", back_populates="student_profile")

    def to_dict(self):
        return {
            "institution": self.institution,
            "grade_level": self.grade_level,
            "bio": self.bio,
        }
