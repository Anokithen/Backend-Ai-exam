import base64
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import current_app
from PIL import Image
from pypdf import PdfReader

from app.models.material_model import MaterialType
from app.services.material_service import authenticated_download_url

MIN_EXTRACTED_CHARS = 40

# Every model call is streamed, so this is a stall detector rather than a cap on the whole
# response: a reasoning model thinks for minutes before its first token, but once it is
# working it should never go this long without sending anything.
STREAM_CONNECT_TIMEOUT_SECONDS = 30
STREAM_STALL_TIMEOUT_SECONDS = 300
# Backstop so a model that dribbles chunks forever still ends the teacher's wait.
STREAM_TOTAL_TIMEOUT_SECONDS = 900

VISION_MAX_TOKENS = 4096

# NIM rejects an inline base64 image much over 180KB, so oversized photos are re-encoded
# smaller until they fit rather than failing the whole generation.
MAX_IMAGE_BASE64_BYTES = 175_000

# Text on a page of notes is still legible at this size, and anything larger only buys the
# model more image tokens to chew through before it starts writing.
MAX_IMAGE_LONG_SIDE = 1600

# The pages are read concurrently; the ceiling keeps a big material from firing enough
# requests at once to get the whole exam rate limited.
MAX_CONCURRENT_IMAGE_CALLS = 4

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Languages the fast vision models were measured reading correctly. Everything else prefers
# the multilingual model, because a model that cannot read a script does not say so: given a
# Sinhala page, one transliterated it into Tamil letters and the other returned well-formed
# Sinhala gibberish. Both produce a confident, wrong exam. Add a language here only after
# checking a page of it comes back verbatim.
_FAST_MODEL_LANGUAGES = {"english", "tamil"}


class AIServiceError(Exception):
    pass


class AIServiceUnavailable(AIServiceError):
    """Transport-level failure (timeout, rate limit, upstream error) that was already retried."""


def _retry_delay(response, attempt: int) -> float:
    retry_after = (response.headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        return min(int(retry_after), 30)
    return 2**attempt


def _raise_for_nim_error(response) -> None:
    current_app.logger.warning(
        "NIM request failed [%s]: %s", response.status_code, response.text[:300]
    )
    if response.status_code == 429:
        raise AIServiceUnavailable(
            "The AI service is rate limited right now. Please try again in a minute."
        )
    raise AIServiceUnavailable("The AI service rejected the request. Please try again.")


def _stream_nim(payload: dict, attempts: int = 2, api_key: str | None = None) -> str:
    """Stream a chat completion and return the assembled content.

    Reasoning models such as Kimi think for minutes before the first token and then write
    for minutes more. Streaming turns the read timeout into a "the model went quiet" check
    instead of a cap on how long a legitimate answer may take.
    """
    config = current_app.config
    url = f"{config['NVIDIA_NIM_BASE_URL']}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key or config['NVIDIA_NIM_API_KEY']}"}
    body = {**payload, "stream": True}

    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            with requests.post(
                url,
                headers=headers,
                json=body,
                stream=True,
                timeout=(STREAM_CONNECT_TIMEOUT_SECONDS, STREAM_STALL_TIMEOUT_SECONDS),
            ) as response:
                if response.status_code in _RETRYABLE_STATUS and not is_last:
                    time.sleep(_retry_delay(response, attempt))
                    continue
                if not response.ok:
                    _raise_for_nim_error(response)
                return _collect_stream(response)
        except (requests.Timeout, requests.ConnectionError):
            if is_last:
                raise AIServiceUnavailable(
                    "The AI service took too long to respond. Please try again."
                )
            time.sleep(2**attempt)
        except requests.RequestException as exc:
            raise AIServiceUnavailable("Failed to reach the AI service. Please try again.") from exc

    raise AIServiceUnavailable("Failed to reach the AI service. Please try again.")


def _collect_stream(response) -> str:
    started = time.monotonic()
    pieces = []
    # Decoded here rather than by requests: an SSE response carries no charset, so requests
    # would fall back to Latin-1 and turn every non-ASCII character — every Sinhala or Tamil
    # exam, and even an em dash — into mojibake.
    for raw in response.iter_lines():
        if time.monotonic() - started > STREAM_TOTAL_TIMEOUT_SECONDS:
            raise AIServiceUnavailable("The AI service took too long to respond. Please try again.")
        if not raw:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        try:
            # Reasoning arrives in a separate "reasoning_content" field, so taking only
            # "content" drops the model's thinking without any parsing of our own.
            delta = chunk["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError):
            continue
        piece = delta.get("content")
        if piece:
            pieces.append(piece)

    content = "".join(pieces).strip()
    if not content:
        raise AIServiceError("The AI service returned an empty response.")
    return content


def _download_material(material) -> bytes:
    # Not material.secure_url: Cloudinary refuses plain delivery of raw files, so a PDF's own
    # URL answers 401 and the read failed before it had read anything.
    try:
        url = authenticated_download_url(
            material.cloudinary_public_id, material.cloudinary_resource_type
        )
    except Exception as exc:
        # Signing needs the API secret; without it this raises before any request is made,
        # and a config problem would otherwise reach the teacher as a blank 500.
        raise AIServiceError(
            "The server is missing its Cloudinary settings, so it cannot fetch this file. "
            "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET on the "
            "server and restart it."
        ) from exc

    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            raise AIServiceError(
                "Cloudinary refused to deliver this file even for an authenticated download. "
                "Check that the API key and secret in the server's settings belong to the "
                "same Cloudinary account the file was uploaded to."
            ) from exc
        raise AIServiceError("Failed to download the material file.") from exc
    except requests.RequestException as exc:
        raise AIServiceError("Failed to download the material file.") from exc
    return response.content


def _encode_image_data_url(content: bytes, mime_type: str) -> str:
    """Return the image as a data URL, downscaled first if it is bigger than the model needs.

    A phone photo is several thousand pixels wide, and every one of them becomes image tokens
    the model has to read before it can write anything. Capping the long side cuts that prefill
    — and the teacher's wait — without losing text legibility on a page of notes.
    """
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except Exception as exc:
        raise AIServiceError("Failed to read the image file.") from exc

    if max(image.size) <= MAX_IMAGE_LONG_SIDE:
        encoded = base64.b64encode(content).decode()
        if len(encoded) <= MAX_IMAGE_BASE64_BYTES:
            return f"data:{mime_type or 'image/png'};base64,{encoded}"

    return f"data:image/jpeg;base64,{_shrink_to_jpeg_base64(image)}"


def _shrink_to_jpeg_base64(image) -> str:
    """Re-encode an image down until it fits both the size cap and NIM's inline image limit."""
    if image.mode != "RGB":
        image = image.convert("RGB")

    for max_side, quality in (
        (MAX_IMAGE_LONG_SIDE, 80),
        (1200, 75),
        (1000, 70),
        (800, 65),
        (600, 55),
    ):
        candidate = image.copy()
        candidate.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode()
        if len(encoded) <= MAX_IMAGE_BASE64_BYTES:
            return encoded

    raise AIServiceError(
        "This image is too large to send to the AI. Try a smaller or lower-resolution file."
    )


def extract_material_texts(materials, language: str = "English"):
    """Read several materials at once, yielding ``(material, text_or_error)`` as each finishes.

    The reads run concurrently because each one is a round trip to the AI, but they are handed
    back one at a time so the caller can report progress as it happens. ``language`` is the
    language of the page, which decides which model is trusted to read it.
    """
    if not materials:
        return

    app = current_app._get_current_object()

    def read(material):
        # Flask's context is thread-local, so each worker pushes its own.
        with app.app_context():
            return extract_material_text(material, language)

    with ThreadPoolExecutor(max_workers=min(len(materials), MAX_CONCURRENT_IMAGE_CALLS)) as pool:
        futures = {pool.submit(read, material): material for material in materials}
        for future in as_completed(futures):
            material = futures[future]
            try:
                yield material, future.result()
            except AIServiceError as exc:
                yield material, exc


def extract_material_text(material, language: str = "English") -> str:
    content = _download_material(material)

    if material.file_type == MaterialType.PDF:
        text = _extract_pdf_text(content, language)
    else:
        text = _extract_image_text(content, material.mime_type, language)

    text = text.strip()
    if len(text) < MIN_EXTRACTED_CHARS:
        raise AIServiceError(
            "Could not read enough text from this material. Try a clearer scan or a text-based PDF."
        )
    return text


def _extract_pdf_text(content: bytes, language: str = "English") -> str:
    """Read a PDF's text with pypdf.

    A PDF made from a document carries its text, so pypdf reads it directly: no model call, no
    cost, and the words come back exactly as they were written rather than as a model's account
    of them. A PDF made by scanning carries no text at all, only page images - there is nothing
    for pypdf to find, and the teacher is told to upload those pages as images, which is the
    path built for reading a picture of a page.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = list(reader.pages)
    except Exception as exc:
        raise AIServiceError("Failed to read the PDF file.") from exc

    texts = []
    for page in pages:
        try:
            texts.append((page.extract_text() or "").strip())
        except Exception:
            # One malformed page should not cost the whole document.
            texts.append("")

    text = "\n".join(part for part in texts if part)
    if len(text) < MIN_EXTRACTED_CHARS:
        raise AIServiceError(
            "There is no text in this PDF to read - it is a scan, a picture of each page. "
            "Upload the pages as images instead and the AI will read them."
        )
    return text


def _extract_image_text(content: bytes, mime_type: str, language: str = "English") -> str:
    """Read the visible text/content of an image using a vision-capable NIM model.

    ``language`` is the language of the page, not of the request: a model that quietly
    transliterates a script it cannot read produces text that looks fine and is worthless, so
    the choice of model has to follow the material.
    """
    data_url = _encode_image_data_url(content, mime_type)
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Transcribe this image VERBATIM, word for word, in its original language and script "
                        "(do not translate or transliterate). Output only the transcribed text itself — never "
                        "a description of the image such as 'the image shows...' or 'this page contains...'. "
                        "For any diagram, chart, or figure, add a line starting with 'Diagram: ' describing "
                        "only what it depicts. If the image contains no legible text at all, output exactly: "
                        "NO_TEXT_FOUND"
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]

    # Either model returns an empty response now and then, so a page one of them fluffs is
    # retried on the next rather than failing the whole material.
    models = _vision_models(language)
    # With only one model trusted for this script there is nothing to fall back to, so it gets
    # the second try instead.
    attempts = [m for m in models for _ in range(2 if len(models) == 1 else 1)]

    for position, model in enumerate(attempts):
        try:
            return _stream_nim(
                {
                    "model": model,
                    "messages": messages,
                    "max_tokens": VISION_MAX_TOKENS,
                },
                api_key=current_app.config["NVIDIA_NIM_VISION_API_KEY"],
            )
        except AIServiceError:
            if position == len(attempts) - 1:
                raise
            current_app.logger.warning("Vision model %s failed to read an image; retrying", model)

    raise AIServiceError("Failed to read text from the image. Please try again.")


_SYSTEM_PROMPT = """You are an expert exam writer. You write ONE exam question at a time from \
study material, strictly as JSON.

Output ONLY a JSON object of this exact shape, no markdown, no commentary:
- for an "mcq" question:
  {"question": {"type": "mcq", "prompt": "...", "options": ["...", "...", "...", "..."], "correct_option_index": 0, "marks": 1}}
- for a "structured" question:
  {"question": {"type": "structured", "prompt": "...", "marks": 5}}
- for an "essay" question:
  {"question": {"type": "essay", "prompt": "...", "marks": 10}}
- if the material has no remaining distinct fact that a question of the requested type could
  test without repeating or fabricating:
  {"question": null}

Accuracy rules (most important):
- The question, its options, and its correct answer must be directly and verifiably supported
  by the material below. Do not invent facts, names, dates, numbers, formulas, or details that
  are not present in the material.
- For an "mcq" question, the correct option must be explicitly stated or directly derivable
  from the material, and the three incorrect options must be plausible but clearly wrong per
  the material — never ambiguous or arguably also correct.
- Never pad with a generic or off-topic question. Returning {"question": null} is always
  better than inventing content.

No repetition (just as important as accuracy):
- The questions already written for this exam are listed below. The new question must test a
  DIFFERENT fact from every one of them: it may not share a correct answer with any of them or
  check the same detail, whatever its type.
- Never re-ask a fact by rewording it. "What is X?", "X is made of what?", "Which of these is
  NOT part of X?" and "X was discovered by whom?" all test the same fact.
- Pick the fact from a part of the material the earlier questions have not touched, working
  through the material from beginning to end so the exam covers every distinct topic evenly.
- If every distinct fact suitable for this question type is already tested, return
  {"question": null} rather than repeating one.

Other rules:
- Write exactly the question type requested.
- An "mcq" question must have 4 "options" and a valid 0-based "correct_option_index".
- A "structured" or "essay" question must NOT include "options" or "correct_option_index".
- Set marks sensibly: mcq typically 1-2, structured 4-8, essay 8-15.
- Write the "prompt" and "options" values in the requested language below. Keep the JSON keys
  ("question", "type", "prompt", "options", "correct_option_index", "marks") in English
  exactly as shown.
"""


MAX_MATERIAL_CHARS = 24000

_RETRY_NUDGE = (
    "{problem} Respond with JSON only: one \"{qtype}\" question that tests a fact none of the "
    "already-written questions test, or {{\"question\": null}} if the material has none left."
)

# The order the paper reads in: quick recall first, then longer answers.
_TYPE_ORDER = ("mcq", "structured", "essay")


def _normalize_counts(counts: dict) -> dict:
    counts = {k: v for k, v in counts.items() if v}
    if not counts:
        raise AIServiceError("Select at least one question to generate.")
    return counts


def _material_block(text: str) -> str:
    truncated_note = (
        "\n\n[material truncated for length]" if len(text) > MAX_MATERIAL_CHARS else ""
    )
    return f"Material:\n{text[:MAX_MATERIAL_CHARS]}{truncated_note}"


def _written_block(written: list[dict]) -> str:
    """The questions so far, with their answers, so the model can see which facts are taken."""
    if not written:
        return "Already written questions: none yet."
    lines = ["Already written questions (the new question must not test any fact these test):"]
    for number, q in enumerate(written, 1):
        lines.append(f"{number}. [{q['type']}] {q['prompt']}")
        if q["type"] == "mcq":
            lines.append(f"   correct answer: {q['options'][q['correct_option_index']]}")
    return "\n".join(lines)


def _brief(qtype: str, number: int, total: int, title_hint: str, language: str) -> str:
    return (
        f"Material title: {title_hint}\n\n"
        f"Write question {number} of {total}: one \"{qtype}\" question.\n\n"
        f"Write it in this language: {language}"
    )


def generate_questions(
    text: str,
    counts: dict,
    title_hint: str,
    language: str = "English",
    on_progress=None,
) -> list[dict]:
    """Write the exam one question at a time, reporting each as it is finished.

    Each question is its own model call that is shown everything written so far, so the exam
    grows in front of the teacher and no two questions test the same fact. ``on_progress``
    receives ``{"created": n, "writing": m | None, "writing_type": type, "total": t}`` as each
    question starts, and the same plus ``"question": {...}`` as each one lands.

    A question the model cannot produce is skipped rather than failing the exam: if it says
    the material has no more distinct facts for a type, the rest of that type is dropped, and
    if the service drops out part-way the questions already written are kept.
    """
    counts = _normalize_counts(counts)
    order = [qtype for qtype in _TYPE_ORDER for _ in range(int(counts.get(qtype) or 0))]
    total = len(order)
    report = on_progress or (lambda event: None)
    material = _material_block(text)

    written: list[dict] = []
    seen_prompts: set[str] = set()
    exhausted: set[str] = set()

    for qtype in order:
        if qtype in exhausted:
            continue
        number = len(written) + 1
        report({"created": len(written), "writing": number, "writing_type": qtype, "total": total})

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{_brief(qtype, number, total, title_hint, language)}\n\n"
                    f"{_written_block(written)}\n\n{material}"
                ),
            },
        ]
        try:
            question = _generate(
                messages,
                lambda raw: _parse_one_question(raw, qtype, seen_prompts),
                _call_nvidia_nim,
                nudge=lambda problem: _RETRY_NUDGE.format(problem=problem, qtype=qtype),
            )
        except AIServiceUnavailable:
            if not written:
                raise
            current_app.logger.warning("AI service dropped out after %d questions; keeping them", len(written))
            break
        except AIServiceError as exc:
            current_app.logger.warning("Skipping %s question %d: %s", qtype, number, exc)
            continue

        if question is None:
            current_app.logger.info("Material exhausted for %s questions after %d written", qtype, len(written))
            exhausted.add(qtype)
            continue

        written.append(question)
        seen_prompts.add(_dedupe_key(question["prompt"]))
        report({"created": len(written), "writing": None, "total": total, "question": question})

    if not written:
        raise AIServiceError("AI did not return any usable questions.")
    # Settles the display when the last slot was skipped rather than written.
    report({"created": len(written), "writing": None, "total": total})
    return written


def _vision_models(language: str) -> list[str]:
    """The models to try for one page, in order, stopping at the first that works.

    For a language the fast models read correctly, the second one covers the first's flaky
    empty responses. For any other language they are left out of the chain entirely, even as a
    last resort: a model that cannot read a script does not fail, it invents a page that looks
    right, and falling back to that turns a visible failure into a plausible wrong exam. Better
    the teacher sees the read fail and tries again.
    """
    config = current_app.config
    multilingual = config["NVIDIA_NIM_VISION_MULTILINGUAL_MODEL"]

    if multilingual and language.strip().casefold() not in _FAST_MODEL_LANGUAGES:
        return [multilingual]

    chain = [config["NVIDIA_NIM_VISION_MODEL"], config["NVIDIA_NIM_VISION_FALLBACK_MODEL"]]
    # Deduplicated in order, so pointing two settings at one model doesn't retry it twice.
    return list(dict.fromkeys(model for model in chain if model))


def _generate(messages: list[dict], parse, call, nudge, max_attempts: int = 3):
    """Call the model until ``parse`` accepts its answer, telling it what was wrong each time."""
    for attempt in range(max_attempts):
        raw = None
        try:
            raw = call(messages)
            return parse(raw)
        except AIServiceUnavailable:
            # _stream_nim already retried the transport; retrying here just multiplies the wait.
            raise
        except AIServiceError as exc:
            if attempt == max_attempts - 1:
                raise
            if raw is not None:
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": nudge(str(exc))},
                ]

    raise AIServiceError("AI failed to generate a valid question.")


def _call_nvidia_nim(messages: list[dict]) -> str:
    return _stream_nim(
        {
            "model": current_app.config["NVIDIA_NIM_MODEL"],
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 4096,
        }
    )


_OPTION_LETTERS = "ABCDEFGHIJ"

# The model is asked for these three type names but routinely returns a casing or
# wording variant; treating those as unusable throws away a perfectly good question.
_TYPE_ALIASES = {
    "mcq": "mcq",
    "multiple choice": "mcq",
    "multiple_choice": "mcq",
    "multiple-choice": "mcq",
    "multiplechoice": "mcq",
    "structured": "structured",
    "short answer": "structured",
    "short_answer": "structured",
    "short-answer": "structured",
    "short": "structured",
    "essay": "essay",
    "long answer": "essay",
    "long_answer": "essay",
    "long-answer": "essay",
}

_DEFAULT_MARKS = {"mcq": 1, "structured": 5, "essay": 10}


def _coerce_type(value) -> str | None:
    if not isinstance(value, str):
        return None
    return _TYPE_ALIASES.get(value.strip().lower())


def _coerce_marks(value, qtype: str) -> int:
    """Marks come back as ints, floats, or strings like "5", "8-10", or "2 marks"."""
    if isinstance(value, bool):
        number = None
    elif isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        match = re.search(r"\d+", value)
        number = int(match.group(0)) if match else None
    else:
        number = None
    return max(number or _DEFAULT_MARKS[qtype], 1)


def _coerce_option_index(value, options: list) -> int | None:
    """The answer arrives as an index, a numeric string, a letter, or the option's own text."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        if len(text) == 1 and text.upper() in _OPTION_LETTERS:
            return _OPTION_LETTERS.index(text.upper())
        for index, option in enumerate(options):
            if str(option).strip().casefold() == text.casefold():
                return index
    return None


def _dedupe_key(prompt: str) -> str:
    """Match prompts that differ only by case, spacing or punctuation.

    Deliberately exact-after-normalising rather than fuzzy: questions like "Adenine pairs with?"
    and "Cytosine pairs with?" are near-identical as strings but are genuinely different
    questions, so similarity matching would throw away good ones. Rewordings of the same fact
    are the prompt's job to prevent.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", prompt.casefold())).strip()


def _load_json(raw: str):
    cleaned = re.sub(r"^```(json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIServiceError("AI response was not valid JSON.") from exc


def _normalize_question(q) -> dict | None:
    """Coerce one question object into the stored shape, or None if it is unusable."""
    if not isinstance(q, dict):
        return None
    qtype = _coerce_type(q.get("type"))
    if qtype is None:
        return None
    prompt = str(q.get("prompt") or "").strip()
    if not prompt:
        return None

    item = {"type": qtype, "prompt": prompt, "marks": _coerce_marks(q.get("marks"), qtype)}
    if qtype == "mcq":
        options = q.get("options") or []
        if not isinstance(options, list) or len(options) < 2:
            return None
        answer = q.get("correct_option_index")
        if answer is None:
            answer = q.get("correct_answer", q.get("answer"))
        correct = _coerce_option_index(answer, options)
        if correct is None or not (0 <= correct < len(options)):
            return None
        if len(options) > 4:
            # Keep exactly 4 options; make sure the correct one survives the trim.
            correct_option = options[correct]
            others = [opt for i, opt in enumerate(options) if i != correct][:3]
            options = others + [correct_option]
            correct = len(options) - 1
        item["options"] = [str(opt) for opt in options]
        item["correct_option_index"] = correct
    return item


def _parse_one_question(raw: str, qtype: str, seen_prompts: set[str]) -> dict | None:
    """Read the model's single question; None means it said the material has none left.

    Anything else the model might do wrong - the wrong type, a repeat of an earlier question,
    a malformed object - raises with a message the retry can put back to it.
    """
    payload = _load_json(raw)
    if isinstance(payload, dict) and "question" in payload:
        payload = payload["question"]
    if payload is None:
        return None
    # A model that ignores the wrapper and sends a list is forgiven if the list holds one.
    if isinstance(payload, list):
        payload = payload[0] if len(payload) == 1 else None
    if not isinstance(payload, dict):
        raise AIServiceError("AI response did not contain a question object.")

    question = _normalize_question(payload)
    if question is None:
        raise AIServiceError("AI returned a question in an unexpected format.")
    if question["type"] != qtype:
        raise AIServiceError(f"AI returned a {question['type']} question instead of {qtype}.")
    if _dedupe_key(question["prompt"]) in seen_prompts:
        raise AIServiceError("That question repeats one already written.")
    return question
