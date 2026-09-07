"""Dev-only seed data. Never run against production.

Usage: python seed.py
"""

from app import create_app
from app.extensions import db
from app.models.user_model import StudentProfile, TeacherProfile, User, UserRole
from app.utils.security import hash_password

app = create_app()

SEED_USERS = [
    {
        "full_name": "Demo Teacher",
        "email": "teacher@demo.com",
        "password": "password123",
        "role": UserRole.TEACHER,
    },
    {
        "full_name": "Demo Student",
        "email": "student@demo.com",
        "password": "password123",
        "role": UserRole.STUDENT,
    },
]

with app.app_context():
    db.create_all()

    for entry in SEED_USERS:
        if db.session.query(User.id).filter_by(email=entry["email"]).first():
            print(f"Skipping existing user: {entry['email']}")
            continue

        user = User(
            full_name=entry["full_name"],
            email=entry["email"],
            password_hash=hash_password(entry["password"]),
            role=entry["role"],
        )
        db.session.add(user)
        db.session.flush()

        if user.role == UserRole.TEACHER:
            db.session.add(TeacherProfile(user_id=user.id))
        else:
            db.session.add(StudentProfile(user_id=user.id))

        print(f"Created {entry['role'].value}: {entry['email']} / {entry['password']}")

    db.session.commit()

print("Seed complete.")
