import random
import string
from datetime import timedelta

from flask import request
from flask_jwt_extended import get_current_user

from app.extensions import db
from app.models.exam_model import Exam, ExamStatus, QuestionType
from app.models.exam_room_model import ExamRoom, RoomParticipant, RoomStatus
from app.models.submission_model import Answer, Submission, SubmissionStatus
from app.utils.responses import error_response, success_response
from app.utils.time import utc_now

INVITE_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _generate_invite_code() -> str:
    for _ in range(10):
        code = "".join(random.choices(INVITE_CODE_ALPHABET, k=6))
        if not db.session.query(ExamRoom).filter_by(invite_code=code).first():
            return code
    raise RuntimeError("Could not generate a unique invite code.")


def _get_owned_exam(teacher, exam_public_id):
    return db.session.query(Exam).filter_by(public_id=exam_public_id, teacher_id=teacher.id).first()


def _get_owned_room(teacher, room_public_id):
    return db.session.query(ExamRoom).filter_by(public_id=room_public_id, teacher_id=teacher.id).first()


# --- Teacher: room management ---


def create_room(exam_public_id):
    teacher = get_current_user()
    exam = _get_owned_exam(teacher, exam_public_id)
    if exam is None:
        return error_response("Exam not found.", code="NOT_FOUND", status=404)
    if exam.status != ExamStatus.PUBLISHED:
        return error_response("Publish the exam before creating a room.", code="VALIDATION_ERROR", status=400)

    payload = request.get_json(silent=True) or {}
    time_limit_minutes = int(payload.get("time_limit_minutes") or exam.time_limit_minutes)

    room = ExamRoom(
        exam_id=exam.id,
        teacher_id=teacher.id,
        invite_code=_generate_invite_code(),
        time_limit_minutes=max(time_limit_minutes, 1),
        status=RoomStatus.OPEN,
    )
    db.session.add(room)
    db.session.commit()
    return success_response(room.to_dict(), message="Room created.", status=201)


def list_rooms(exam_public_id):
    teacher = get_current_user()
    exam = _get_owned_exam(teacher, exam_public_id)
    if exam is None:
        return error_response("Exam not found.", code="NOT_FOUND", status=404)
    return success_response({"rooms": [room.to_dict() for room in exam.rooms]})


def get_room(room_public_id):
    teacher = get_current_user()
    room = _get_owned_room(teacher, room_public_id)
    if room is None:
        return error_response("Room not found.", code="NOT_FOUND", status=404)

    data = room.to_dict()
    data["participants"] = [p.to_dict() for p in room.participants]
    return success_response(data)


def close_room(room_public_id):
    teacher = get_current_user()
    room = _get_owned_room(teacher, room_public_id)
    if room is None:
        return error_response("Room not found.", code="NOT_FOUND", status=404)

    room.status = RoomStatus.CLOSED
    db.session.commit()
    return success_response(room.to_dict(), message="Room closed.")


def list_submissions(room_public_id):
    teacher = get_current_user()
    room = _get_owned_room(teacher, room_public_id)
    if room is None:
        return error_response("Room not found.", code="NOT_FOUND", status=404)

    submissions = (
        db.session.query(Submission)
        .filter_by(room_id=room.id)
        .order_by(Submission.created_at.desc())
        .all()
    )
    for submission in submissions:
        submission.expire_if_stale()
    db.session.commit()
    return success_response({"submissions": [s.to_dict() for s in submissions]})


def _get_owned_submission(teacher, submission_public_id):
    return (
        db.session.query(Submission)
        .join(ExamRoom, Submission.room_id == ExamRoom.id)
        .filter(Submission.public_id == submission_public_id, ExamRoom.teacher_id == teacher.id)
        .first()
    )


def get_submission(submission_public_id):
    teacher = get_current_user()
    submission = _get_owned_submission(teacher, submission_public_id)
    if submission is None:
        return error_response("Submission not found.", code="NOT_FOUND", status=404)

    submission.expire_if_stale()
    db.session.commit()
    return success_response(submission.to_dict(include_answers=True))


def grade_answer(submission_public_id, question_public_id):
    teacher = get_current_user()
    submission = _get_owned_submission(teacher, submission_public_id)
    if submission is None:
        return error_response("Submission not found.", code="NOT_FOUND", status=404)

    answer = next(
        (a for a in submission.answers if a.question.public_id == question_public_id), None
    )
    if answer is None:
        return error_response("Answer not found.", code="NOT_FOUND", status=404)

    payload = request.get_json(silent=True) or {}
    score = payload.get("score")
    if not isinstance(score, int) or score < 0 or score > answer.question.marks:
        return error_response(
            f"Score must be an integer between 0 and {answer.question.marks}.",
            code="VALIDATION_ERROR",
            status=400,
        )

    answer.score = score
    answer.feedback = payload.get("feedback", answer.feedback)
    submission.recompute_score()

    db.session.commit()
    return success_response(submission.to_dict(include_answers=True), message="Answer graded.")


# --- Student: joining and taking exams ---


def join_room():
    student = get_current_user()
    payload = request.get_json(silent=True) or {}
    invite_code = (payload.get("invite_code") or "").strip().upper()

    room = db.session.query(ExamRoom).filter_by(invite_code=invite_code).first()
    if room is None:
        return error_response("Invalid invite code.", code="NOT_FOUND", status=404)
    if room.status != RoomStatus.OPEN:
        return error_response("This room is closed.", code="VALIDATION_ERROR", status=400)

    participant = (
        db.session.query(RoomParticipant)
        .filter_by(room_id=room.id, student_id=student.id)
        .first()
    )
    if participant is None:
        participant = RoomParticipant(room_id=room.id, student_id=student.id)
        db.session.add(participant)
        db.session.commit()

    return success_response(room.to_dict(include_exam=True), message="Joined room.")


def my_rooms():
    student = get_current_user()
    participants = (
        db.session.query(RoomParticipant)
        .filter_by(student_id=student.id)
        .join(ExamRoom, RoomParticipant.room_id == ExamRoom.id)
        .order_by(ExamRoom.created_at.desc())
        .all()
    )
    rooms = []
    for participant in participants:
        room_data = participant.room.to_dict(include_exam=True)
        room_data["submission"] = participant.submission.to_dict() if participant.submission else None
        rooms.append(room_data)
    return success_response({"rooms": rooms})


def _get_participant(student, room_public_id):
    room = db.session.query(ExamRoom).filter_by(public_id=room_public_id).first()
    if room is None:
        return None, None
    participant = (
        db.session.query(RoomParticipant).filter_by(room_id=room.id, student_id=student.id).first()
    )
    return room, participant


def start_exam(room_public_id):
    student = get_current_user()
    room, participant = _get_participant(student, room_public_id)
    if room is None or participant is None:
        return error_response("Join this room before starting the exam.", code="NOT_FOUND", status=404)

    if participant.submission is not None:
        participant.submission.expire_if_stale()
        db.session.commit()
        return success_response(participant.submission.to_dict())

    if room.status != RoomStatus.OPEN:
        return error_response("This room is closed.", code="VALIDATION_ERROR", status=400)

    now = utc_now()
    submission = Submission(
        room_id=room.id,
        participant_id=participant.id,
        student_id=student.id,
        exam_id=room.exam_id,
        started_at=now,
        expires_at=now + timedelta(minutes=room.time_limit_minutes),
        max_score=room.exam.total_marks,
    )
    db.session.add(submission)
    db.session.commit()
    return success_response(submission.to_dict(), message="Exam started.", status=201)


def _get_active_submission(student, room_public_id):
    room, participant = _get_participant(student, room_public_id)
    if room is None or participant is None or participant.submission is None:
        return None, None
    submission = participant.submission
    if submission.expire_if_stale():
        db.session.commit()
    return room, submission


def get_exam_for_taking(room_public_id):
    student = get_current_user()
    room, submission = _get_active_submission(student, room_public_id)
    if room is None or submission is None:
        return error_response("Start the exam first.", code="NOT_FOUND", status=404)

    data = room.exam.to_dict(include_questions=True, reveal_answers=False)
    data["submission"] = submission.to_dict(include_answers=True)
    return success_response(data)


def save_answer(room_public_id):
    student = get_current_user()
    room, submission = _get_active_submission(student, room_public_id)
    if room is None or submission is None:
        return error_response("Start the exam first.", code="NOT_FOUND", status=404)
    if submission.status != SubmissionStatus.IN_PROGRESS:
        return error_response("This exam has already been submitted.", code="VALIDATION_ERROR", status=400)

    payload = request.get_json(silent=True) or {}
    question_public_id = payload.get("question_id")
    question = next((q for q in room.exam.questions if q.public_id == question_public_id), None)
    if question is None:
        return error_response("Question not found.", code="NOT_FOUND", status=404)

    answer = next((a for a in submission.answers if a.question_id == question.id), None)
    if answer is None:
        answer = Answer(submission_id=submission.id, question_id=question.id)
        db.session.add(answer)

    if question.type == QuestionType.MCQ:
        answer.selected_option_index = payload.get("selected_option_index")
    else:
        answer.response_text = payload.get("response_text")

    db.session.commit()
    return success_response(message="Answer saved.")


def submit_exam(room_public_id):
    student = get_current_user()
    room, submission = _get_active_submission(student, room_public_id)
    if room is None or submission is None:
        return error_response("Start the exam first.", code="NOT_FOUND", status=404)

    if submission.status == SubmissionStatus.IN_PROGRESS:
        submission.finalize()
        db.session.commit()

    return success_response(submission.to_dict(include_answers=True), message="Exam submitted.")
