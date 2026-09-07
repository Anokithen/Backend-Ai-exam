from app.extensions import db
from app.utils.time import utc_now


class TokenBlocklist(db.Model):
    __tablename__ = "token_blocklist"

    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(36), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
