from app.extensions import db, jwt
from app.models.token_blocklist_model import TokenBlocklist
from app.models.user_model import User


def register_jwt_callbacks():
    @jwt.user_identity_loader
    def user_identity_lookup(user: User):
        return str(user.id)

    @jwt.user_lookup_loader
    def user_lookup_callback(_jwt_header, jwt_data):
        user_id = int(jwt_data["sub"])
        return db.session.get(User, user_id)

    @jwt.additional_claims_loader
    def add_claims(user: User):
        return {"role": user.role.value}

    @jwt.token_in_blocklist_loader
    def check_if_token_revoked(_jwt_header, jwt_data):
        jti = jwt_data["jti"]
        return db.session.query(TokenBlocklist.id).filter_by(jti=jti).first() is not None

    @jwt.expired_token_loader
    def expired_token_callback(_jwt_header, _jwt_data):
        from app.utils.responses import error_response

        return error_response("Token has expired.", code="TOKEN_EXPIRED", status=401)

    @jwt.invalid_token_loader
    def invalid_token_callback(_reason):
        from app.utils.responses import error_response

        return error_response("Invalid token.", code="TOKEN_INVALID", status=401)

    @jwt.unauthorized_loader
    def missing_token_callback(_reason):
        from app.utils.responses import error_response

        return error_response("Authorization token is required.", code="TOKEN_MISSING", status=401)

    @jwt.revoked_token_loader
    def revoked_token_callback(_jwt_header, _jwt_data):
        from app.utils.responses import error_response

        return error_response("Token has been revoked.", code="TOKEN_REVOKED", status=401)
