from flask import Blueprint

from app.controllers import auth_controller as ctrl

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

bp.route("/register", methods=["POST"])(ctrl.register)
bp.route("/login", methods=["POST"])(ctrl.login)
bp.route("/refresh", methods=["POST"])(ctrl.refresh)
bp.route("/logout", methods=["POST"])(ctrl.logout)
bp.route("/me", methods=["GET"])(ctrl.me)
