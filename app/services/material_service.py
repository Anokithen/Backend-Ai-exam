import time
import uuid

import cloudinary
import cloudinary.api
import cloudinary.exceptions
import cloudinary.uploader
import cloudinary.utils
from flask import current_app

from app.models.material_model import MaterialType

ALLOWED_EXTENSIONS = {
    "pdf": MaterialType.PDF,
    "png": MaterialType.IMAGE,
    "jpg": MaterialType.IMAGE,
    "jpeg": MaterialType.IMAGE,
    "webp": MaterialType.IMAGE,
}

class MaterialServiceError(Exception):
    pass


class UnsupportedFileType(MaterialServiceError):
    pass


def resolve_file_type(filename: str) -> MaterialType:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_type = ALLOWED_EXTENSIONS.get(ext)
    if file_type is None:
        raise UnsupportedFileType("Only PDF and image files (png, jpg, jpeg, webp) are allowed.")
    return file_type


def build_upload_signature(teacher_public_id: str) -> dict:
    """Params a browser needs to upload straight to Cloudinary, bypassing our server."""
    timestamp = int(time.time())
    folder = f"materials/{teacher_public_id}"
    public_id = uuid.uuid4().hex

    params_to_sign = {
        "timestamp": timestamp,
        "folder": folder,
        "public_id": public_id,
    }
    signature = cloudinary.utils.api_sign_request(
        params_to_sign, current_app.config["CLOUDINARY_API_SECRET"]
    )

    return {
        **params_to_sign,
        "signature": signature,
        "api_key": current_app.config["CLOUDINARY_API_KEY"],
        "cloud_name": current_app.config["CLOUDINARY_CLOUD_NAME"],
    }


def verify_uploaded_asset(public_id: str, resource_type: str, teacher_public_id: str) -> dict:
    """Confirm an asset a client claims to have uploaded really exists and belongs to them."""
    expected_prefix = f"materials/{teacher_public_id}/"
    if not public_id.startswith(expected_prefix):
        raise MaterialServiceError("Invalid upload reference.")

    try:
        return cloudinary.api.resource(public_id, resource_type=resource_type)
    except cloudinary.exceptions.NotFound as exc:
        raise MaterialServiceError("Uploaded file could not be verified.") from exc


def delete_material_file(public_id: str, resource_type: str) -> None:
    cloudinary.uploader.destroy(public_id, resource_type=resource_type)
