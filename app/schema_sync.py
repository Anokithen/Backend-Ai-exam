import sqlalchemy as sa

from app.extensions import db


def ensure_schema(app):
    """Auto-create any missing tables and reconcile columns that create_all() can't fix.

    db.create_all() only creates tables that don't exist yet - it never alters an
    existing table. So a column like users.role (a MySQL ENUM) needs an explicit
    ALTER when new enum values (e.g. a new role) are added to the model.
    """
    db.create_all()
    _ensure_users_role_enum(app)
    _ensure_materials_cloudinary_columns(app)
    _ensure_materials_extracted_text_column(app)
    _ensure_exams_language_column(app)


def _ensure_users_role_enum(app):
    from app.models.user_model import UserRole

    inspector = sa.inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return

    columns = {col["name"]: col for col in inspector.get_columns("users")}
    role_col = columns.get("role")
    if role_col is None:
        return

    existing_values = getattr(role_col["type"], "enums", None)
    if existing_values is None:
        return

    required_values = [role.name for role in UserRole]
    missing = [value for value in required_values if value not in existing_values]
    if not missing:
        return

    all_values = list(existing_values) + missing
    quoted = ", ".join(f"'{value}'" for value in all_values)
    with db.engine.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE users MODIFY COLUMN role ENUM({quoted}) NOT NULL"))

    app.logger.info("users.role enum updated to include: %s", ", ".join(missing))


def _ensure_materials_cloudinary_columns(app):
    """Migrate a pre-Cloudinary materials table (local-disk columns) in place."""
    inspector = sa.inspect(db.engine)
    if "materials" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("materials")}
    changed = False

    with db.engine.begin() as conn:
        if "cloudinary_public_id" not in columns:
            conn.execute(sa.text(
                "ALTER TABLE materials ADD COLUMN cloudinary_public_id VARCHAR(255) NOT NULL DEFAULT ''"
            ))
            changed = True
        if "cloudinary_resource_type" not in columns:
            conn.execute(sa.text(
                "ALTER TABLE materials ADD COLUMN cloudinary_resource_type VARCHAR(20) NOT NULL DEFAULT ''"
            ))
            changed = True
        if "secure_url" not in columns:
            conn.execute(sa.text(
                "ALTER TABLE materials ADD COLUMN secure_url VARCHAR(500) NOT NULL DEFAULT ''"
            ))
            changed = True
        if "stored_filename" in columns:
            conn.execute(sa.text("ALTER TABLE materials DROP COLUMN stored_filename"))
            changed = True
        if "relative_path" in columns:
            conn.execute(sa.text("ALTER TABLE materials DROP COLUMN relative_path"))
            changed = True

    if changed:
        app.logger.info("materials table reconciled for Cloudinary storage")


def _ensure_materials_extracted_text_column(app):
    inspector = sa.inspect(db.engine)
    if "materials" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("materials")}
    if "extracted_text" in columns:
        return

    with db.engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE materials ADD COLUMN extracted_text LONGTEXT NULL"))

    app.logger.info("materials table reconciled with extracted_text column")


def _ensure_exams_language_column(app):
    inspector = sa.inspect(db.engine)
    if "exams" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("exams")}
    if "language" in columns:
        return

    with db.engine.begin() as conn:
        conn.execute(sa.text(
            "ALTER TABLE exams ADD COLUMN language VARCHAR(50) NOT NULL DEFAULT 'English'"
        ))

    app.logger.info("exams table reconciled with language column")
