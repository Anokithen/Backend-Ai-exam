import click

from app.extensions import db
from app.models.user_model import User, UserRole
from app.utils.security import hash_password


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--email", required=True)
    @click.option("--password", required=True)
    @click.option("--full-name", "full_name", required=True)
    def create_admin(email, password, full_name):
        """Create an admin user (bootstrap, since /auth/register only allows teacher/student)."""
        email = email.lower()
        if db.session.query(User.id).filter_by(email=email).first():
            click.echo(f"A user with email {email} already exists.")
            return

        user = User(
            full_name=full_name,
            email=email,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
        )
        db.session.add(user)
        db.session.commit()
        click.echo(f"Admin user created: {email}")
