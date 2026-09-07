import enum
import uuid

from sqlalchemy.dialects.mysql import LONGTEXT

from app.extensions import db
from app.models.base import TimestampMixin
from app.utils.time import iso_utc


class MaterialType(str, enum.Enum):
    PDF = "pdf"
    IMAGE = "image"


class Material(db.Model, TimestampMixin):
    __tablename__ = "materials"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    title = db.Column(db.String(150), nullable=False)
    file_type = db.Column(db.Enum(MaterialType), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    cloudinary_public_id = db.Column(db.String(255), nullable=False)
    cloudinary_resource_type = db.Column(db.String(20), nullable=False)
    secure_url = db.Column(db.String(500), nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)

    # Extracted once after upload and reused for every exam, so generation never
    # re-runs the slow OCR/vision pass. NULL means "not read yet".
    #
    # LONGTEXT on MySQL, not TEXT: TEXT holds 65,535 bytes and a textbook chapter runs well
    # past that (a 48-page one came to 77KB), so the read would finish and then fail on the
    # write with "Data too long for column". Other backends keep plain TEXT, which is unbounded.
    extracted_text = db.Column(db.Text().with_variant(LONGTEXT, "mysql"), nullable=True)

    teacher = db.relationship("User", backref=db.backref("materials", cascade="all, delete-orphan"))

    def to_dict(self, include_text=False):
        data = {
            "id": self.public_id,
            "title": self.title,
            "file_type": self.file_type.value,
            "original_filename": self.original_filename,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "has_text": bool(self.extracted_text),
            "text_length": len(self.extracted_text or ""),
            "created_at": iso_utc(self.created_at),
        }
        if include_text:
            data["extracted_text"] = self.extracted_text
        return data
