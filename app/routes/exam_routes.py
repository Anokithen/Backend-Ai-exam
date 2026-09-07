from flask import Blueprint

from app.controllers import exam_controller as ctrl
from app.controllers import room_controller as room_ctrl
from app.middleware.decorators import teacher_required

bp = Blueprint("exams", __name__, url_prefix="/api/exams")

bp.route("/generate", methods=["POST"])(teacher_required(ctrl.generate_exam))
bp.route("/generate/stream", methods=["POST"])(teacher_required(ctrl.generate_exam_stream))
bp.route("", methods=["GET"])(teacher_required(ctrl.list_exams))
bp.route("/<public_id>", methods=["GET"])(teacher_required(ctrl.get_exam))
bp.route("/<public_id>", methods=["PATCH"])(teacher_required(ctrl.update_exam))
bp.route("/<public_id>", methods=["DELETE"])(teacher_required(ctrl.delete_exam))
bp.route("/<public_id>/publish", methods=["POST"])(teacher_required(ctrl.publish_exam))
bp.route("/<public_id>/pdf", methods=["GET"])(teacher_required(ctrl.download_exam_pdf))

bp.route("/<exam_public_id>/rooms", methods=["POST"])(teacher_required(room_ctrl.create_room))
bp.route("/<exam_public_id>/rooms", methods=["GET"])(teacher_required(room_ctrl.list_rooms))
