from flask import Blueprint

from app.controllers import material_controller as ctrl
from app.middleware.decorators import teacher_required

bp = Blueprint("materials", __name__, url_prefix="/api/materials")

bp.route("", methods=["GET"])(teacher_required(ctrl.list_materials))
bp.route("/upload-signature", methods=["POST"])(teacher_required(ctrl.request_upload_signature))
bp.route("", methods=["POST"])(teacher_required(ctrl.upload_material))
bp.route("/<public_id>/download", methods=["GET"])(teacher_required(ctrl.download_material))
bp.route("/<public_id>/extract-text", methods=["POST"])(teacher_required(ctrl.extract_text))
bp.route("/<public_id>/text", methods=["PATCH"])(teacher_required(ctrl.update_text))
bp.route("/<public_id>", methods=["DELETE"])(teacher_required(ctrl.delete_material))
