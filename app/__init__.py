from flask import Flask, jsonify
from flask_cors import CORS

from app.config import Config
from app.extensions import db
from app.routes import register_blueprints


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    CORS(app, resources={r"/api/*": {"origins": app.config["FRONTEND_ORIGIN"]}})
    db.init_app(app)

    with app.app_context():
        from app import models  # noqa: F401  (registers SQLAlchemy tables)

    register_blueprints(app)
    register_error_handlers(app)

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    return app


def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "Resource not found", "error_code": "NOT_FOUND"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": "Method not allowed", "error_code": "METHOD_NOT_ALLOWED"}), 405

    @app.errorhandler(500)
    def internal_error(_error):
        return jsonify({"error": "An internal server error occurred.", "error_code": "INTERNAL_ERROR"}), 500
