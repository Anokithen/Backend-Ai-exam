from flask import Blueprint

from app.controllers import admin_controller as ctrl
from app.middleware.decorators import admin_required

bp = Blueprint("admin", __name__, url_prefix="/api/admin")

bp.route("/stats", methods=["GET"])(admin_required(ctrl.stats))
bp.route("/users", methods=["GET"])(admin_required(ctrl.list_users))
bp.route("/users", methods=["POST"])(admin_required(ctrl.create_user))
bp.route("/users/<public_id>", methods=["GET"])(admin_required(ctrl.get_user))
bp.route("/users/<public_id>", methods=["PATCH"])(admin_required(ctrl.update_user))
bp.route("/users/<public_id>", methods=["DELETE"])(admin_required(ctrl.delete_user))
