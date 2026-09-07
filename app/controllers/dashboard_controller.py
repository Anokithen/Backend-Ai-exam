from flask_jwt_extended import get_current_user

from app.extensions import db
from app.models.exam_model import Exam, ExamStatus
from app.models.exam_room_model import ExamRoom, RoomParticipant, RoomStatus
from app.models.submission_model import Submission, SubmissionStatus
from app.utils.responses import success_response


def teacher_summary():
    user = get_current_user()

    exams = db.session.query(Exam).filter_by(teacher_id=user.id).all()
    exam_ids = [exam.id for exam in exams]

    active_exams = sum(
        1 for exam in exams if any(room.status == RoomStatus.OPEN for room in exam.rooms)
    )
    total_students = (
        db.session.query(RoomParticipant.student_id)
        .join(ExamRoom, RoomParticipant.room_id == ExamRoom.id)
        .filter(ExamRoom.teacher_id == user.id)
        .distinct()
        .count()
    )
    pending_grading = (
        db.session.query(Submission)
        .filter(Submission.exam_id.in_(exam_ids), Submission.status == SubmissionStatus.SUBMITTED)
        .count()
        if exam_ids
        else 0
    )
    recent_exams = (
        db.session.query(Exam)
        .filter_by(teacher_id=user.id)
        .order_by(Exam.created_at.desc())
        .limit(5)
        .all()
    )

    return success_response(
        {
            "teacher": user.to_dict(),
            "total_exams": len(exams),
            "active_exams": active_exams,
            "completed_exams": sum(1 for exam in exams if exam.status == ExamStatus.PUBLISHED) - active_exams,
            "total_students": total_students,
            "pending_grading": pending_grading,
            "recent_exams": [exam.to_dict() for exam in recent_exams],
        }
    )


def student_summary():
    user = get_current_user()

    participants = (
        db.session.query(RoomParticipant)
        .filter_by(student_id=user.id)
        .join(ExamRoom, RoomParticipant.room_id == ExamRoom.id)
        .all()
    )

    upcoming_exams = []
    active_exams = []
    completed_exams = []
    scores = []

    for participant in participants:
        room = participant.room
        submission = participant.submission
        room_data = room.to_dict(include_exam=True)
        if submission is None:
            upcoming_exams.append(room_data)
        elif submission.status == SubmissionStatus.IN_PROGRESS:
            active_exams.append(room_data)
        else:
            room_data["submission"] = submission.to_dict()
            completed_exams.append(room_data)
            if submission.total_score is not None and submission.max_score:
                scores.append(submission.total_score / submission.max_score * 100)

    return success_response(
        {
            "student": user.to_dict(),
            "upcoming_exams": upcoming_exams,
            "active_exams": active_exams,
            "completed_exams": completed_exams,
            "average_percentage": round(sum(scores) / len(scores), 1) if scores else None,
            "best_score": round(max(scores), 1) if scores else None,
        }
    )
