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


def _stream_nim(
    payload: dict,
    attempts: int = 2,
    api_key: str | None = None,
    on_piece=None,
    on_attempt=None,
) -> str:
    """Stream a chat completion and return the assembled content.

    Reasoning models such as Kimi think for minutes before the first token and then write
    for minutes more. Streaming turns the read timeout into a "the model went quiet" check
    instead of a cap on how long a legitimate answer may take.

    ``on_piece`` is handed each fragment of content as it arrives, so a caller can act on the
    answer before it is complete. ``on_attempt`` fires as each attempt starts; a retry begins
    the answer again from nothing, so a caller's partial reading of it must start over too.
    """
    config = current_app.config
    url = f"{config['NVIDIA_NIM_BASE_URL']}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key or config['NVIDIA_NIM_API_KEY']}"}
    body = {**payload, "stream": True}

    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        if on_attempt:
            on_attempt()
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
                return _collect_stream(response, on_piece)
        except (requests.Timeout, requests.ConnectionError):
            if is_last:
                raise AIServiceUnavailable(
                    "The AI service took too long to respond. Please try again."
                )
            time.sleep(2**attempt)
        except requests.RequestException as exc:
            raise AIServiceUnavailable("Failed to reach the AI service. Please try again.") from exc

    raise AIServiceUnavailable("Failed to reach the AI service. Please try again.")


def _collect_stream(response, on_piece=None) -> str:
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
            if on_piece:
                on_piece(piece)

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


_SYSTEM_PROMPT = """You are an expert exam writer. You write exam questions from study material in \
small sets, strictly as JSON, and you are shown every question already written so far.

Output ONLY a JSON object of this exact shape, no markdown, no commentary:
{
  "questions": [
    {"type": "mcq", "prompt": "...", "options": ["...", "...", "...", "..."], "correct_option_index": 0, "marks": 1},
    {"type": "structured", "prompt": "...", "marks": 5},
    {"type": "essay", "prompt": "...", "marks": 10}
  ]
}
If the material has no remaining distinct fact for a requested question type, leave that type
out; if it has none for any of them, return {"questions": []}.

Accuracy rules (most important):
- Every question, option, and correct answer must be directly and verifiably supported by the
  material below. Do not invent facts, names, dates, numbers, formulas, or details that are not
  present in the material.
- For "mcq" questions, the correct option must be explicitly stated or directly derivable from the
  material, and the three incorrect options must be plausible but clearly wrong per the material —
  never ambiguous or arguably also correct.
- Never pad with generic or off-topic questions. Returning fewer questions is always better than
  inventing content.

No repetition (just as important as accuracy):
- The questions already written for this exam are listed below. Every new question must test a
  DIFFERENT fact from all of them and from each other: no shared correct answer, no re-checking
  the same detail, whatever the question type.
- Never re-ask a fact by rewording it. "What is X?", "X is made of what?", "Which of these is
  NOT part of X?" and "X was discovered by whom?" all test the same fact — use at most ONE.
- Before writing each question, re-read the already-written list and the questions you have
  written in this set, and confirm the new one covers a fact none of them touch.
- Pick facts from parts of the material the earlier questions have not touched, working through
  the material from beginning to end so the exam covers every distinct topic evenly.
- Returning FEWER questions than requested is always better than repeating a fact.

Other rules:
- Write only the question types and counts requested, in the order requested.
- "mcq" questions must have 4 "options" and a valid 0-based "correct_option_index".
- "structured" and "essay" questions must NOT include "options" or "correct_option_index".
- Set marks sensibly: mcq typically 1-2, structured 4-8, essay 8-15.
- Write every "prompt" and "options" value in the requested language below. Keep the JSON keys
  ("questions", "type", "prompt", "options", "correct_option_index", "marks") in English exactly
  as shown.
"""


MAX_MATERIAL_CHARS = 24000

_RETRY_NUDGE = (
    "{problem} Respond with JSON only: {{\"questions\": [...]}} holding the requested questions, "
    "each testing a fact none of the already-written questions test, or {{\"questions\": []}} if "
    "the material has none left."
)

# The order the paper reads in: quick recall first, then longer answers.
_TYPE_ORDER = ("mcq", "structured", "essay")

# A long exam is written in this many sets, one after another. One call for the whole thing
# runs past every timeout in the chain and truncates at the token limit; one call per question
# takes as many round trips as there are questions. Up to this many, though, one at a time is
# fine and gives the smoothest picture of the exam appearing.
BATCHED_ABOVE_QUESTIONS = 20
BATCH_COUNT = 5


def _normalize_counts(counts: dict) -> dict:
    counts = {k: v for k, v in counts.items() if v}
    if not counts:
        raise AIServiceError("Select at least one question to generate.")
    return counts


def _batch_size(total: int) -> int:
    if total <= BATCHED_ABOVE_QUESTIONS:
        return 1
    return -(-total // BATCH_COUNT)  # ceiling division


def _material_block(text: str) -> str:
    truncated_note = (
        "\n\n[material truncated for length]" if len(text) > MAX_MATERIAL_CHARS else ""
    )
    return f"Material:\n{text[:MAX_MATERIAL_CHARS]}{truncated_note}"


def _written_block(written: list[dict]) -> str:
    """The questions so far, with their answers, so the model can see which facts are taken."""
    if not written:
        return "Already written questions: none yet."
    lines = ["Already written questions (the new questions must not test any fact these test):"]
    for number, q in enumerate(written, 1):
        lines.append(f"{number}. [{q['type']}] {q['prompt']}")
        if q["type"] == "mcq":
            lines.append(f"   correct answer: {q['options'][q['correct_option_index']]}")
    return "\n".join(lines)


def _brief(wanted: dict, first_number: int, total: int, title_hint: str, language: str) -> str:
    count = sum(wanted.values())
    mix = ", ".join(f"{n} {qtype}" for qtype, n in wanted.items())
    if count == 1:
        ask = f"Write question {first_number} of {total}: one \"{next(iter(wanted))}\" question."
    else:
        ask = f"Write questions {first_number}-{first_number + count - 1} of {total}: {mix}."
    return f"Material title: {title_hint}\n\n{ask}\n\nWrite them in this language: {language}"


def generate_questions(
    text: str,
    counts: dict,
    title_hint: str,
    language: str = "English",
    on_progress=None,
) -> list[dict]:
    """Write the exam in sets, one after another, reporting every question as it is finished.

    Each set is its own model call that is shown everything written so far, so no two questions
    test the same fact even across sets. Within a call the questions are read out of the JSON
    as the model streams it, so the exam still appears one question at a time. ``on_progress``
    receives ``{"created": n, "writing": m | None, "total": t}`` as each question starts (plus
    ``"writing_type"`` when the set is a single question) and the same plus ``"question": {...}``
    as each one lands.

    A set the model cannot fill is not the end of the exam: a type it returns none of is
    treated as exhausted and dropped from the remaining sets, a type it returns too few of is
    asked for once more at the end, and if the service drops out part-way the questions already
    written are kept.
    """
    counts = _normalize_counts(counts)
    queue = [qtype for qtype in _TYPE_ORDER for _ in range(int(counts.get(qtype) or 0))]
    total = len(queue)
    batch_size = _batch_size(total)
    report = on_progress or (lambda event: None)
    material = _material_block(text)

    written: list[dict] = []
    seen_prompts: set[str] = set()
    exhausted: set[str] = set()
    topped_up: set[str] = set()

    while queue:
        batch, queue = queue[:batch_size], queue[batch_size:]
        wanted = {qtype: batch.count(qtype) for qtype in _TYPE_ORDER if qtype in batch}
        first_number = len(written) + 1

        def on_start(_wanted=wanted):
            event = {"created": len(written), "writing": len(written) + 1, "total": total}
            if len(_wanted) == 1:
                event["writing_type"] = next(iter(_wanted))
            report(event)

        def on_question(question):
            written.append(question)
            report({"created": len(written), "writing": None, "total": total, "question": question})

        reader = _QuestionStream(wanted, seen_prompts, on_start, on_question)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{_brief(wanted, first_number, total, title_hint, language)}\n\n"
                    f"{_written_block(written)}\n\n{material}"
                ),
            },
        ]
        try:
            got = _generate(
                messages,
                reader.finish,
                lambda msgs: _call_nvidia_nim(msgs, on_piece=reader.feed, on_attempt=reader.reset),
                nudge=lambda problem: _RETRY_NUDGE.format(problem=problem),
            )
        except AIServiceUnavailable:
            if not written:
                raise
            current_app.logger.warning("AI service dropped out after %d questions; keeping them", len(written))
            break
        except AIServiceError as exc:
            current_app.logger.warning("Skipping questions %d-%d: %s", first_number, first_number + len(batch) - 1, exc)
            continue

        for qtype, asked in wanted.items():
            returned = sum(q["type"] == qtype for q in got)
            if returned == 0:
                current_app.logger.info("Material exhausted for %s questions after %d written", qtype, len(written))
                exhausted.add(qtype)
                queue = [t for t in queue if t != qtype]
            elif returned < asked and qtype not in topped_up:
                # Asked for once more at the end; a second shortfall means the material is done.
                topped_up.add(qtype)
                queue.extend([qtype] * (asked - returned))

    if not written:
        raise AIServiceError("AI did not return any usable questions.")
    # Settles the display when the last set came back short rather than full.
    report({"created": len(written), "writing": None, "total": total})
    return written


class _QuestionStream:
    """Pull finished questions out of the model's JSON while it is still arriving.

    The whole answer only parses once it is complete, but each question is a small object of
    its own, so the scanner watches the braces go by, copies out every object that opens
    directly inside an array, and parses each one the moment it closes. Braces inside strings
    are skipped so a prompt like "Explain {x}" is not mistaken for structure.

    Only questions that are wanted, well-formed and new are kept and reported, so what the
    teacher watches arrive is exactly what the exam ends up holding. A reply cut off at the
    token limit still yields every question that finished before the cut.
    """

    def __init__(self, wanted: dict, seen_prompts: set, on_start, on_question):
        self.wanted = wanted
        self.seen_prompts = seen_prompts
        self.on_start = on_start
        self.on_question = on_question
        self.questions: list[dict] = []
        self.reset()

    def reset(self):
        """Start scanning afresh; questions already accepted from an earlier attempt are kept."""
        self._stack: list[str] = []
        self._in_string = False
        self._escaped = False
        self._capture: list[str] | None = None
        self._capture_depth = 0

    def feed(self, piece: str):
        for char in piece:
            if self._capture is not None:
                self._capture.append(char)
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._in_string = False
                continue
            if char == '"':
                self._in_string = True
            elif char in "{[":
                if char == "{" and self._capture is None and self._stack and self._stack[-1] == "[":
                    self._capture = ["{"]
                    self._capture_depth = len(self._stack)
                    self.on_start()
                self._stack.append(char)
            elif char in "}]":
                if not self._stack:
                    continue
                self._stack.pop()
                if self._capture is not None and len(self._stack) == self._capture_depth:
                    text, self._capture = "".join(self._capture), None
                    self._accept(text)

    def _accept(self, text: str):
        try:
            payload = json.loads(text)
        except ValueError:
            return
        self._take(payload)

    def _take(self, payload) -> bool:
        question = _normalize_question(payload)
        if question is None:
            return False
        qtype = question["type"]
        if sum(q["type"] == qtype for q in self.questions) >= self.wanted.get(qtype, 0):
            return False  # a type that was not asked for, or more of one than were
        key = _dedupe_key(question["prompt"])
        if key in self.seen_prompts:
            return False
        self.seen_prompts.add(key)
        self.questions.append(question)
        self.on_question(question)
        return True

    def finish(self, raw: str) -> list[dict]:
        """The set's questions once the reply has ended; raises if the reply was unusable.

        An empty list is the model saying the material has nothing left, which is an answer;
        a reply that was not JSON, or JSON holding no usable question, is put back to it. A
        model that skips the list and answers with a single bare question is forgiven.
        """
        if self.questions:
            return self.questions
        payload = _load_json(raw)
        if isinstance(payload, dict):
            if "questions" in payload:
                payload = payload["questions"]
            elif "question" in payload:
                payload = [payload["question"]] if payload["question"] is not None else []
            else:
                payload = [payload]
        if not isinstance(payload, list):
            raise AIServiceError("AI response did not contain a list of questions.")
        if not payload:
            return []
        for item in payload:
            self._take(item)
        if self.questions:
            return self.questions
        raise AIServiceError("AI returned questions in an unexpected format, or only repeats of earlier ones.")


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


def _call_nvidia_nim(messages: list[dict], on_piece=None, on_attempt=None) -> str:
    return _stream_nim(
        {
            "model": current_app.config["NVIDIA_NIM_MODEL"],
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 4096,
        },
        on_piece=on_piece,
        on_attempt=on_attempt,
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
