from flask import Blueprint

from app.controllers import room_controller as ctrl
from app.middleware.decorators import student_required, teacher_required

bp = Blueprint("rooms", __name__, url_prefix="/api/rooms")

# Student
bp.route("/join", methods=["POST"])(student_required(ctrl.join_room))
bp.route("/mine", methods=["GET"])(student_required(ctrl.my_rooms))
bp.route("/<room_public_id>/start", methods=["POST"])(student_required(ctrl.start_exam))
bp.route("/<room_public_id>/exam", methods=["GET"])(student_required(ctrl.get_exam_for_taking))
bp.route("/<room_public_id>/answers", methods=["PATCH"])(student_required(ctrl.save_answer))
bp.route("/<room_public_id>/submit", methods=["POST"])(student_required(ctrl.submit_exam))

# Teacher
bp.route("/<room_public_id>", methods=["GET"])(teacher_required(ctrl.get_room))
bp.route("/<room_public_id>/close", methods=["POST"])(teacher_required(ctrl.close_room))
bp.route("/<room_public_id>/submissions", methods=["GET"])(teacher_required(ctrl.list_submissions))
bp.route("/submissions/<submission_public_id>", methods=["GET"])(teacher_required(ctrl.get_submission))
bp.route(
    "/submissions/<submission_public_id>/answers/<question_public_id>", methods=["PATCH"]
)(teacher_required(ctrl.grade_answer))
