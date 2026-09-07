from flask import Blueprint

from app.controllers import dashboard_controller as ctrl
from app.middleware.decorators import student_required, teacher_required

bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")

bp.route("/teacher", methods=["GET"])(teacher_required(ctrl.teacher_summary))
bp.route("/student", methods=["GET"])(student_required(ctrl.student_summary))
