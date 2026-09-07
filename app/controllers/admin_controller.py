from flask import current_app, request
from flask_jwt_extended import get_current_user
from pydantic import ValidationError

from app.extensions import db
from app.models.user_model import StudentProfile, TeacherProfile, User, UserRole
from app.schemas.admin_schema import AdminCreateUserSchema, AdminUpdateUserSchema
from app.utils.responses import error_response, success_response
from app.utils.security import hash_password


def _get_user_or_404(public_id):
    return db.session.query(User).filter_by(public_id=public_id).first()


def stats():
    total_users = db.session.query(User.id).count()
    total_teachers = db.session.query(User.id).filter_by(role=UserRole.TEACHER).count()
    total_students = db.session.query(User.id).filter_by(role=UserRole.STUDENT).count()
    total_admins = db.session.query(User.id).filter_by(role=UserRole.ADMIN).count()
    active_users = db.session.query(User.id).filter_by(is_active=True).count()

    return success_response(
        {
            "total_users": total_users,
            "total_teachers": total_teachers,
            "total_students": total_students,
            "total_admins": total_admins,
            "active_users": active_users,
            "inactive_users": total_users - active_users,
        }
    )


def list_users():
    query = db.session.query(User)

    role = request.args.get("role")
    if role:
        try:
            query = query.filter_by(role=UserRole(role))
        except ValueError:
            return error_response("Invalid role filter.", code="VALIDATION_ERROR", status=400)

    is_active = request.args.get("is_active")
    if is_active is not None:
        query = query.filter_by(is_active=is_active.lower() in ("true", "1"))

    search = request.args.get("search", "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(User.full_name.ilike(like), User.email.ilike(like)))

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 20, type=int), 1), 100)

    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)

    return success_response(
        {
            "users": [user.to_dict() for user in pagination.items],
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "total_pages": pagination.pages,
        }
    )


def get_user(public_id):
    user = _get_user_or_404(public_id)
    if user is None:
        return error_response("User not found.", code="NOT_FOUND", status=404)
    return success_response(user.to_dict())


def create_user():
    payload = request.get_json(silent=True) or {}
    try:
        data = AdminCreateUserSchema(**payload)
    except ValidationError as exc:
        return error_response("Invalid user data.", code="VALIDATION_ERROR", status=400, details=exc.errors())

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
        db.session.flush()

        if user.role == UserRole.TEACHER:
            db.session.add(TeacherProfile(user_id=user.id))
        elif user.role == UserRole.STUDENT:
            db.session.add(StudentProfile(user_id=user.id))

        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to create user")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response(user.to_dict(), message="User created.", status=201)


def update_user(public_id):
    user = _get_user_or_404(public_id)
    if user is None:
        return error_response("User not found.", code="NOT_FOUND", status=404)

    payload = request.get_json(silent=True) or {}
    try:
        data = AdminUpdateUserSchema(**payload)
    except ValidationError as exc:
        return error_response("Invalid update data.", code="VALIDATION_ERROR", status=400, details=exc.errors())

    current_admin = get_current_user()
    if user.id == current_admin.id:
        if data.is_active is False:
            return error_response("You cannot deactivate your own account.", code="SELF_ACTION_FORBIDDEN", status=400)
        if data.role is not None and data.role != user.role.value:
            return error_response("You cannot change your own role.", code="SELF_ACTION_FORBIDDEN", status=400)

    try:
        if data.is_active is not None:
            user.is_active = data.is_active

        if data.role is not None and data.role != user.role.value:
            old_role = user.role
            user.role = UserRole(data.role)

            if old_role == UserRole.TEACHER and user.teacher_profile:
                db.session.delete(user.teacher_profile)
            elif old_role == UserRole.STUDENT and user.student_profile:
                db.session.delete(user.student_profile)

            db.session.flush()
            if user.role == UserRole.TEACHER and not user.teacher_profile:
                db.session.add(TeacherProfile(user_id=user.id))
            elif user.role == UserRole.STUDENT and not user.student_profile:
                db.session.add(StudentProfile(user_id=user.id))

        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to update user")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response(user.to_dict(), message="User updated.")


def delete_user(public_id):
    user = _get_user_or_404(public_id)
    if user is None:
        return error_response("User not found.", code="NOT_FOUND", status=404)

    current_admin = get_current_user()
    if user.id == current_admin.id:
        return error_response("You cannot delete your own account.", code="SELF_ACTION_FORBIDDEN", status=400)

    try:
        db.session.delete(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to delete user")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response(message="User deleted.")
