import logging

from flask import Flask

from app.config import Config
from app.extensions import db, jwt, migrate
from app.routes import register_blueprints
from app.utils.responses import error_response, success_response


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    _configure_logging(app)
    _init_extensions(app)

    with app.app_context():
        from app import models  # noqa: F401  (registers SQLAlchemy tables)
        from app.schema_sync import ensure_schema

        ensure_schema(app)

    from app.auth.jwt_callbacks import register_jwt_callbacks

    register_jwt_callbacks()

    from app.cli import register_cli

    register_cli(app)
    register_blueprints(app)
    register_error_handlers(app)

    @app.route("/health")
    def health():
        return success_response({"status": "ok"})

    return app


def _configure_logging(app):
    """Route Flask's logs through gunicorn's handlers.

    Without this the app logger keeps its own defaults under gunicorn and everything below
    WARNING - including the startup lines and the 500 handler's traceback - never reaches
    the platform log stream, which is the only view of a deployed service.
    """
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)


def _init_extensions(app):
    import cloudinary
    from flask_cors import CORS

    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}}, supports_credentials=True)
    # A browser only reports "blocked by CORS policy" on its own side, so state the allowed
    # set here - it is the one place a deployed service can show what it will accept.
    app.logger.info(
        "CORS allows: %s",
        ", ".join(o.pattern if hasattr(o, "pattern") else o for o in app.config["CORS_ORIGINS"]),
    )
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)

    cloudinary.config(
        cloud_name=app.config["CLOUDINARY_CLOUD_NAME"],
        api_key=app.config["CLOUDINARY_API_KEY"],
        api_secret=app.config["CLOUDINARY_API_SECRET"],
        secure=True,
    )
    # Every material is fetched from Cloudinary with a signed URL, so a missing secret breaks
    # reading and downloading files while everything else looks healthy. Said once at boot,
    # where the platform log will show it, rather than only when a teacher hits it.
    missing = [
        name
        for name in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
        if not app.config[name]
    ]
    if missing:
        app.logger.error(
            "Cloudinary is not configured (%s not set): uploading, reading and downloading "
            "study materials will all fail until it is.",
            ", ".join(missing),
        )
    else:
        app.logger.info(
            "Cloudinary configured: cloud=%s key=...%s",
            app.config["CLOUDINARY_CLOUD_NAME"],
            str(app.config["CLOUDINARY_API_KEY"])[-4:],
        )


def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(_error):
        return error_response("Resource not found.", code="NOT_FOUND", status=404)

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return error_response("Method not allowed.", code="METHOD_NOT_ALLOWED", status=405)

    @app.errorhandler(413)
    def payload_too_large(_error):
        return error_response("Uploaded file is too large.", code="PAYLOAD_TOO_LARGE", status=413)

    @app.errorhandler(500)
    def internal_error(_error):
        app.logger.exception("Unhandled server error")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)
