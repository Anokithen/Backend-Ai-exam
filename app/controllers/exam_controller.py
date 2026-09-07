import json

from flask import Response, current_app, request, send_file, stream_with_context
from flask_jwt_extended import get_current_user

from app.extensions import db
from app.models.exam_model import Exam, ExamStatus, Question, QuestionType
from app.models.material_model import Material
from app.models.submission_model import Answer
from app.pdf.exam_pdf import build_exam_pdf
from app.services.ai_service import (
    AIServiceError,
    extract_material_texts,
    generate_questions,
)
from app.utils.responses import error_response, success_response

MAX_TITLE_LENGTH = 150
MAX_LANGUAGE_LENGTH = 50
QUESTION_TYPES = {t.value for t in QuestionType}

WRITE_STAGE = "write"
SAVE_STAGE = "save"


def _get_owned_exam(teacher, public_id):
    return db.session.query(Exam).filter_by(public_id=public_id, teacher_id=teacher.id).first()


def _read_stage_id(material) -> str:
    return f"read:{material.public_id}"


def _plan(materials) -> list[dict]:
    """The checklist the teacher watches: one line per file, then writing, then saving."""
    stages = [
        {
            "id": _read_stage_id(material),
            "label": f"Read text from {material.original_filename}",
            "status": "pending",
        }
        for material in materials
    ]
    stages.append({"id": WRITE_STAGE, "label": "Write exam questions from the text", "status": "pending"})
    stages.append({"id": SAVE_STAGE, "label": "Save the exam", "status": "pending"})
    return stages


def _prepare(teacher, payload):
    """Validate the request up front, so a bad one fails as an error instead of a stream."""
    material_title = (payload.get("material_title") or "").strip()
    if not material_title:
        return None, error_response("Select a material.", code="VALIDATION_ERROR", status=400)

    # All materials uploaded under the same title are treated as one group and combined.
    materials = (
        db.session.query(Material)
        .filter_by(teacher_id=teacher.id, title=material_title)
        .order_by(Material.created_at.asc())
        .all()
    )
    if not materials:
        return None, error_response("Material not found.", code="NOT_FOUND", status=404)

    counts = payload.get("question_counts") or {}
    if not isinstance(counts, dict) or not any(counts.get(t) for t in QUESTION_TYPES):
        return None, error_response(
            "Select at least one question to generate.", code="VALIDATION_ERROR", status=400
        )

    title = (payload.get("title") or material_title).strip()[:MAX_TITLE_LENGTH]
    if not title:
        return None, error_response("A title is required.", code="VALIDATION_ERROR", status=400)

    # Text the teacher has already corrected, keyed by material, so a fixed-up reading is not
    # thrown away and read again.
    edits = payload.get("texts")
    edits = {k: v for k, v in edits.items() if isinstance(v, str) and v.strip()} if isinstance(edits, dict) else {}

    context = {
        "teacher": teacher,
        "materials": materials,
        "counts": counts,
        "title": title,
        "language": (payload.get("language") or "English").strip()[:MAX_LANGUAGE_LENGTH] or "English",
        "time_limit_minutes": max(int(payload.get("time_limit_minutes") or 60), 1),
        "edits": edits,
        "combined_text": (payload.get("combined_text") or "").strip(),
    }
    return context, None


def _run_generation(context):
    """Run the pipeline, yielding an event per stage as it starts and finishes.

    Images go to the AI first and come back as text; that text — everything read, plus anything
    the teacher corrected — is what the exam is then written from.
    """
    materials = context["materials"]
    edits = context["edits"]

    yield {"type": "stages", "stages": _plan(materials)}

    texts: dict[int, str] = {}
    pending = []
    for material in materials:
        stored = edits.get(material.public_id) or material.extracted_text
        if stored and stored.strip():
            texts[material.id] = stored.strip()
            yield {
                "type": "stage",
                "id": _read_stage_id(material),
                "status": "done",
                "detail": f"already read - {len(stored.strip()):,} characters",
            }
        else:
            pending.append(material)

    if pending:
        for material in pending:
            yield {"type": "stage", "id": _read_stage_id(material), "status": "running"}

        # The exam language is also the language of the pages, so it decides which model is
        # trusted to read them.
        for material, result in extract_material_texts(pending, context["language"]):
            if isinstance(result, AIServiceError):
                current_app.logger.warning("Could not read %s: %s", material.original_filename, result)
                yield {
                    "type": "stage",
                    "id": _read_stage_id(material),
                    "status": "failed",
                    "detail": str(result),
                }
                continue
            texts[material.id] = result
            # Stored so a second exam from the same material skips the reading entirely.
            material.extracted_text = result
            yield {
                "type": "stage",
                "id": _read_stage_id(material),
                "status": "done",
                "detail": f"{len(result):,} characters",
            }

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to store extracted text")

    # Kept in upload order so the exam follows the material, not whichever file finished first.
    combined = "\n\n".join(texts[m.id] for m in materials if texts.get(m.id)).strip()
    combined = combined or context["combined_text"]
    if not combined:
        yield {
            "type": "error",
            "code": "AI_GENERATION_FAILED",
            "message": "No text could be read from this material, so there is nothing to write questions from.",
        }
        return

    yield {"type": "stage", "id": WRITE_STAGE, "status": "running"}
    try:
        questions_data = generate_questions(
            combined, context["counts"], context["title"], context["language"]
        )
    except AIServiceError as exc:
        yield {"type": "stage", "id": WRITE_STAGE, "status": "failed", "detail": str(exc)}
        yield {"type": "error", "code": "AI_GENERATION_FAILED", "message": str(exc)}
        return
    yield {
        "type": "stage",
        "id": WRITE_STAGE,
        "status": "done",
        "detail": f"{len(questions_data)} questions",
    }

    yield {"type": "stage", "id": SAVE_STAGE, "status": "running"}
    try:
        exam = _save_exam(context, questions_data)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to save generated exam")
        yield {"type": "stage", "id": SAVE_STAGE, "status": "failed"}
        yield {
            "type": "error",
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
        }
        return

    yield {"type": "stage", "id": SAVE_STAGE, "status": "done"}
    yield {"type": "done", "exam": exam.to_dict(include_questions=True)}


def _save_exam(context, questions_data):
    exam = Exam(
        teacher_id=context["teacher"].id,
        material_id=context["materials"][0].id,
        title=context["title"],
        time_limit_minutes=context["time_limit_minutes"],
        status=ExamStatus.DRAFT,
        language=context["language"],
    )
    for index, q in enumerate(questions_data):
        exam.questions.append(
            Question(
                order_index=index,
                type=QuestionType(q["type"]),
                prompt=q["prompt"],
                options=q.get("options"),
                correct_option_index=q.get("correct_option_index"),
                marks=q["marks"],
            )
        )
    exam.recompute_total_marks()
    db.session.add(exam)
    db.session.commit()
    return exam


def generate_exam():
    """Run the whole pipeline and answer once, for callers that don't follow the stages."""
    context, failure = _prepare(get_current_user(), request.get_json(silent=True) or {})
    if failure:
        return failure

    exam = None
    error = None
    for event in _run_generation(context):
        if event["type"] == "done":
            exam = event["exam"]
        elif event["type"] == "error":
            error = event

    if error:
        status = 500 if error["code"] == "INTERNAL_ERROR" else 422
        return error_response(error["message"], code=error["code"], status=status)
    return success_response(exam, message="Exam generated.", status=201)


def generate_exam_stream():
    """Same pipeline, but each stage is reported as it happens so the teacher can watch it."""
    context, failure = _prepare(get_current_user(), request.get_json(silent=True) or {})
    if failure:
        return failure

    def events():
        try:
            for event in _run_generation(context):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            current_app.logger.exception("Exam generation stream failed")
            yield "data: " + json.dumps(
                {
                    "type": "error",
                    "code": "INTERNAL_ERROR",
                    "message": "An internal server error occurred.",
                }
            ) + "\n\n"

    return Response(
        stream_with_context(events()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stops nginx buffering the stages up and delivering them all at the end.
            "X-Accel-Buffering": "no",
        },
    )


def list_exams():
    teacher = get_current_user()
    exams = (
        db.session.query(Exam)
        .filter_by(teacher_id=teacher.id)
        .order_by(Exam.created_at.desc())
        .all()
    )
    return success_response({"exams": [exam.to_dict() for exam in exams]})


def get_exam(public_id):
    teacher = get_current_user()
    exam = _get_owned_exam(teacher, public_id)
    if exam is None:
        return error_response("Exam not found.", code="NOT_FOUND", status=404)
    return success_response(exam.to_dict(include_questions=True))


def update_exam(public_id):
    teacher = get_current_user()
    exam = _get_owned_exam(teacher, public_id)
    if exam is None:
        return error_response("Exam not found.", code="NOT_FOUND", status=404)

    payload = request.get_json(silent=True) or {}
    title = payload.get("title")
    if title is not None:
        title = title.strip()[:MAX_TITLE_LENGTH]
        if not title:
            return error_response("A title is required.", code="VALIDATION_ERROR", status=400)
        exam.title = title

    if "instructions" in payload:
        exam.instructions = payload.get("instructions")
    if "time_limit_minutes" in payload:
        exam.time_limit_minutes = max(int(payload["time_limit_minutes"] or 1), 1)

    questions_payload = payload.get("questions")
    if questions_payload is not None:
        error = _apply_questions(exam, questions_payload)
        if error:
            db.session.rollback()
            return error_response(error, code="VALIDATION_ERROR", status=400)

    exam.recompute_total_marks()

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to update exam")
        return error_response("An internal server error occurred.", code="INTERNAL_ERROR", status=500)

    return success_response(exam.to_dict(include_questions=True), message="Exam updated.")


def _apply_questions(exam, questions_payload):
    if not isinstance(questions_payload, list) or not questions_payload:
        return "An exam needs at least one question."

    existing_by_public_id = {q.public_id: q for q in exam.questions}
    kept_public_ids = set()
    new_questions = []

    for index, item in enumerate(questions_payload):
        qtype = item.get("type")
        if qtype not in QUESTION_TYPES:
            return f"Invalid question type: {qtype}"
        prompt = (item.get("prompt") or "").strip()
        if not prompt:
            return "Every question needs a prompt."
        marks = int(item.get("marks") or 1)

        options = None
        correct_option_index = None
        if qtype == QuestionType.MCQ.value:
            options = item.get("options") or []
            correct_option_index = item.get("correct_option_index")
            if not isinstance(options, list) or len(options) < 2:
                return "MCQ questions need at least 2 options."
            if not isinstance(correct_option_index, int) or not (0 <= correct_option_index < len(options)):
                return "MCQ questions need a valid correct_option_index."

        public_id = item.get("id")
        question = existing_by_public_id.get(public_id) if public_id else None
        if question is None:
            question = Question()
            new_questions.append(question)
        else:
            kept_public_ids.add(public_id)

        question.order_index = index
        question.type = QuestionType(qtype)
        question.prompt = prompt
        question.marks = max(marks, 1)
        question.options = options
        question.correct_option_index = correct_option_index

    for public_id, question in existing_by_public_id.items():
        if public_id not in kept_public_ids:
            if db.session.query(Answer.id).filter_by(question_id=question.id).first():
                return "Can't remove a question that students have already answered."
            exam.questions.remove(question)

    for question in new_questions:
        exam.questions.append(question)

    return None


def publish_exam(public_id):
    teacher = get_current_user()
    exam = _get_owned_exam(teacher, public_id)
    if exam is None:
        return error_response("Exam not found.", code="NOT_FOUND", status=404)
    if not exam.questions:
        return error_response("Add at least one question before publishing.", code="VALIDATION_ERROR", status=400)

    exam.status = ExamStatus.PUBLISHED
    db.session.commit()
    return success_response(exam.to_dict(), message="Exam published.")


def delete_exam(public_id):
    teacher = get_current_user()
    exam = _get_owned_exam(teacher, public_id)
    if exam is None:
        return error_response("Exam not found.", code="NOT_FOUND", status=404)

    db.session.delete(exam)
    db.session.commit()
    return success_response(message="Exam deleted.")


def download_exam_pdf(public_id):
    teacher = get_current_user()
    exam = _get_owned_exam(teacher, public_id)
    if exam is None:
        return error_response("Exam not found.", code="NOT_FOUND", status=404)

    include_answers = request.args.get("with_answers") == "1"
    pdf_buffer = build_exam_pdf(exam, include_answer_key=include_answers)
    filename = f"{exam.title.strip().replace(' ', '_') or 'exam'}.pdf"
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)
