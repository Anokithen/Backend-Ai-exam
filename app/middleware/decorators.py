from functools import wraps

from flask_jwt_extended import get_current_user, verify_jwt_in_request

from app.utils.responses import error_response


def role_required(*allowed_roles):
    """Restrict a route to users whose role is in allowed_roles (e.g. "teacher", "student")."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            user = get_current_user()
            if user is None or not user.is_active:
                return error_response("Account is inactive or not found.", code="ACCOUNT_INACTIVE", status=403)
            if user.role.value not in allowed_roles:
                return error_response("You do not have permission to access this resource.", code="FORBIDDEN", status=403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def teacher_required(fn):
    return role_required("teacher")(fn)


def student_required(fn):
    return role_required("student")(fn)


def admin_required(fn):
    return role_required("admin")(fn)
