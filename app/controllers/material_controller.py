import cloudinary.utils
from flask import current_app, redirect, request
from flask_jwt_extended import get_current_user
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.material_model import Material
from app.services.ai_service import AIServiceError
from app.services.ai_service import extract_material_text as ai_extract_material_text
from app.services.material_service import (
    MaterialServiceError,
    UnsupportedFileType,
    build_upload_signature,
    delete_material_file,
    resolve_file_type,
    verify_uploaded_asset,
)
from app.utils.responses import error_response, success_response

MAX_TITLE_LENGTH = 150


def request_upload_signature():
    """Params the browser needs to upload straight to Cloudinary, bypassing this server."""
    teacher = get_current_user()
    return success_response(build_upload_signature(teacher.public_id))


def upload_material():
    """Record a material after the browser has already uploaded it directly to Cloudinary."""
    teacher = get_current_user()
    payload = request.get_json(silent=True) or {}

    public_id = (payload.get("public_id") or "").strip()
    resource_type = (payload.get("resource_type") or "").strip()
    original_filename = (payload.get("original_filename") or "").strip()
    title = (payload.get("title") or original_filename).strip()[:MAX_TITLE_LENGTH]

    if not public_id or not resource_type or not original_filename:
        return error_response("Upload reference is incomplete.", code="VALIDATION_ERROR", status=400)
    if not title:
        return error_response("A title is required.", code="VALIDATION_ERROR", status=400)

    try:
        asset = verify_uploaded_asset(public_id, resource_type, teacher.public_id)
    except MaterialServiceError as exc:
        return error_response(str(exc), code="VALIDATION_ERROR", status=422)

    try:
        file_type = resolve_file_type(original_filename)
    except UnsupportedFileType as exc:
        delete_material_file(public_id, resource_type)
        return error_response(str(exc), code="UNSUPPORTED_FILE_TYPE", status=422)

    try:
        material = Material(
            teacher_id=teacher.id,
            title=title,
            file_type=file_type,
            original_filename=original_filename,
            cloudinary_public_id=public_id,
            cloudinary_resource_type=resource_type,
            secure_url=asset["secure_url"],
            mime_type=payload.get("mime_type") or "application/octet-stream",
            file_size=asset.get("bytes", 0),
        )
        db.session.add(material)
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_material_file(public_id, resource_type)
        current_app.logger.exception("Failed to save material")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response(material.to_dict(), message="Material uploaded.", status=201)


def list_materials():
    teacher = get_current_user()
    materials = (
        db.session.query(Material)
        .filter_by(teacher_id=teacher.id)
        .order_by(Material.created_at.desc())
        .all()
    )
    return success_response({"materials": [material.to_dict() for material in materials]})


def _get_owned_material(teacher, public_id):
    return db.session.query(Material).filter_by(public_id=public_id, teacher_id=teacher.id).first()


def extract_text(public_id):
    """Read one material's text and store it, so exam generation never re-runs the slow OCR pass."""
    teacher = get_current_user()
    material = _get_owned_material(teacher, public_id)
    if material is None:
        return error_response("Material not found.", code="NOT_FOUND", status=404)

    refresh = request.args.get("refresh") == "1"
    if material.extracted_text and not refresh:
        return success_response({"text": material.extracted_text, "cached": True})

    try:
        text = ai_extract_material_text(material)
    except AIServiceError as exc:
        return error_response(str(exc), code="AI_GENERATION_FAILED", status=422)

    try:
        material.extracted_text = text
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to store extracted text")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response({"text": text, "cached": False}, message="Text extracted.")


def update_text(public_id):
    """Save teacher-corrected text, so OCR mistakes only have to be fixed once."""
    teacher = get_current_user()
    material = _get_owned_material(teacher, public_id)
    if material is None:
        return error_response("Material not found.", code="NOT_FOUND", status=404)

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return error_response("Text cannot be empty.", code="VALIDATION_ERROR", status=400)

    try:
        material.extracted_text = text
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to update extracted text")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response(material.to_dict(include_text=True), message="Text saved.")


def download_material(public_id):
    teacher = get_current_user()
    material = _get_owned_material(teacher, public_id)
    if material is None:
        return error_response("Material not found.", code="NOT_FOUND", status=404)

    base_name = secure_filename(material.original_filename.rsplit(".", 1)[0]) or "download"
    url, _ = cloudinary.utils.cloudinary_url(
        material.cloudinary_public_id,
        resource_type=material.cloudinary_resource_type,
        type="upload",
        secure=True,
        flags=f"attachment:{base_name}",
    )
    return redirect(url)


def delete_material(public_id):
    teacher = get_current_user()
    material = _get_owned_material(teacher, public_id)
    if material is None:
        return error_response("Material not found.", code="NOT_FOUND", status=404)

    try:
        db.session.delete(material)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to delete material")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    delete_material_file(material.cloudinary_public_id, material.cloudinary_resource_type)
    return success_response(message="Material deleted.")
