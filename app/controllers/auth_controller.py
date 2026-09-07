from flask import current_app, request
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    get_jwt,
    jwt_required,
)
from pydantic import ValidationError

from app.extensions import db
from app.models.token_blocklist_model import TokenBlocklist
from app.models.user_model import StudentProfile, TeacherProfile, User, UserRole
from app.schemas.auth_schema import LoginSchema, RegisterSchema
from app.utils.responses import error_response, success_response
from app.utils.security import hash_password, verify_password


def _issue_tokens(user: User):
    return {
        "access_token": create_access_token(identity=user),
        "refresh_token": create_refresh_token(identity=user),
        "user": user.to_dict(),
    }


def register():
    payload = request.get_json(silent=True) or {}
    try:
        data = RegisterSchema(**payload)
    except ValidationError as exc:
        return error_response("Invalid registration data.", code="VALIDATION_ERROR", status=400, details=exc.errors())

    if db.session.query(User.id).filter_by(email=data.email.lower()).first():
        return error_response("An account with this email already exists.", code="EMAIL_TAKEN", status=409)

    try:
        user = User(
            full_name=data.full_name.strip(),
            email=data.email.lower(),
            password_hash=hash_password(data.password),
            role=UserRole(data.role),
        )
        db.session.add(user)
        db.session.flush()  # assign user.id before creating the profile

        if user.role == UserRole.TEACHER:
            db.session.add(TeacherProfile(user_id=user.id))
        else:
            db.session.add(StudentProfile(user_id=user.id))

        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to register user")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response(_issue_tokens(user), message="Registration successful.", status=201)


def login():
    payload = request.get_json(silent=True) or {}
    try:
        data = LoginSchema(**payload)
    except ValidationError as exc:
        return error_response("Invalid login data.", code="VALIDATION_ERROR", status=400, details=exc.errors())

    user = db.session.query(User).filter_by(email=data.email.lower()).first()
    if user is None or not verify_password(data.password, user.password_hash):
        return error_response("Invalid email or password.", code="INVALID_CREDENTIALS", status=401)

    if not user.is_active:
        return error_response("This account has been deactivated.", code="ACCOUNT_INACTIVE", status=403)

    return success_response(_issue_tokens(user), message="Login successful.")


@jwt_required(refresh=True)
def refresh():
    user = get_current_user()
    if user is None or not user.is_active:
        return error_response("Account is inactive or not found.", code="ACCOUNT_INACTIVE", status=403)
    return success_response({"access_token": create_access_token(identity=user)}, message="Token refreshed.")


@jwt_required()
def logout():
    jti = get_jwt()["jti"]
    db.session.add(TokenBlocklist(jti=jti))
    db.session.commit()
    return success_response(message="Logged out successfully.")


@jwt_required()
def me():
    user = get_current_user()
    if user is None:
        return error_response("User not found.", code="NOT_FOUND", status=404)
    return success_response(user.to_dict())
