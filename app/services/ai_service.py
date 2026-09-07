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

MIN_EXTRACTED_CHARS = 40

TEXT_TIMEOUT_SECONDS = 120

# Image calls are streamed, so this is a stall detector rather than a cap on the whole
# response: the vision model reasons for minutes before its first token, but once it is
# working it should never go this long without sending anything.
VISION_CONNECT_TIMEOUT_SECONDS = 30
VISION_STALL_TIMEOUT_SECONDS = 300
# Backstop so a model that dribbles chunks forever still ends the teacher's wait.
VISION_TOTAL_TIMEOUT_SECONDS = 900

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

# A PDF page holding this little text is taken to be a scan rather than a page of writing,
# and is read with the vision model instead. Kept low so a genuinely sparse page - a title
# page, a section divider - is trusted as-is rather than costing a model call.
MIN_PAGE_TEXT_CHARS = 20

# Reading a scanned page costs a model round trip, so a long scan is cut off here instead of
# running for an hour. The teacher is told in the text which pages were left out.
MAX_OCR_PAGES = 12

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


def _post_nim(payload: dict, timeout: int, attempts: int = 2, api_key: str | None = None) -> dict:
    """POST to the NIM chat endpoint, retrying transient failures (timeouts, 429s, 5xx).

    ``api_key`` overrides the default text-model key (vision calls use their own).
    """
    config = current_app.config
    url = f"{config['NVIDIA_NIM_BASE_URL']}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key or config['NVIDIA_NIM_API_KEY']}"}

    timed_out = False
    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            timed_out = isinstance(exc, requests.Timeout)
            if is_last:
                break
            time.sleep(2**attempt)
            continue
        except requests.RequestException as exc:
            raise AIServiceUnavailable("Failed to reach the AI service. Please try again.") from exc

        if response.status_code in _RETRYABLE_STATUS and not is_last:
            time.sleep(_retry_delay(response, attempt))
            continue

        if not response.ok:
            _raise_for_nim_error(response)

        try:
            return response.json()
        except ValueError as exc:
            raise AIServiceError("The AI service returned an unexpected response.") from exc

    if timed_out:
        raise AIServiceUnavailable("The AI service took too long to respond. Please try again.")
    raise AIServiceUnavailable("Failed to reach the AI service. Please try again.")


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
                timeout=(VISION_CONNECT_TIMEOUT_SECONDS, VISION_STALL_TIMEOUT_SECONDS),
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
        if time.monotonic() - started > VISION_TOTAL_TIMEOUT_SECONDS:
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
    try:
        response = requests.get(material.secure_url, timeout=30)
        response.raise_for_status()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            raise AIServiceError(
                "Cloudinary is blocking delivery of this file. In the Cloudinary console, go to "
                "Settings → Security and enable delivery of PDF/raw files, then try again."
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
    """Read a PDF's text, falling back to the vision model for pages that were scanned.

    pypdf only ever returns a text layer, and a scan has none: every page comes back empty and
    the material was rejected as unreadable, which is the whole of "try a text-based PDF". A
    scanned page does carry its image, and reading an image of a page is what the vision path
    already does - so those pages go through it. Mixed files are handled a page at a time, so
    a typed report with a photographed appendix reads correctly throughout.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = list(reader.pages)
    except Exception as exc:
        raise AIServiceError("Failed to read the PDF file.") from exc

    texts = []
    scanned = []
    for index, page in enumerate(pages):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            # One malformed page should not cost the whole document.
            text = ""
        texts.append(text)
        if len(text) < MIN_PAGE_TEXT_CHARS:
            scanned.append(index)

    if not scanned:
        return "\n".join(texts)

    unread = scanned[MAX_OCR_PAGES:]
    for index, page_text in _read_scanned_pages(pages, scanned[:MAX_OCR_PAGES], language):
        texts[index] = page_text

    if unread:
        # Said in the returned text, which the teacher reviews and edits, so a part-read file
        # can never look like a complete one.
        texts.append(
            f"[{len(unread)} scanned page(s) were not read: at most {MAX_OCR_PAGES} per file. "
            f"Split the PDF to read the rest.]"
        )

    return "\n".join(text for text in texts if text)


def _read_scanned_pages(pages, indexes, language):
    """Read the given pages with the vision model, yielding ``(index, text)`` as each finishes."""
    if not indexes:
        return

    app = current_app._get_current_object()

    def read(index):
        # Flask's context is thread-local, so each worker pushes its own.
        with app.app_context():
            image = _page_image_bytes(pages[index])
            if image is None:
                return index, ""
            try:
                return index, _extract_image_text(image, "image/jpeg", language).strip()
            except AIServiceError:
                # A page the model cannot read is left empty rather than failing the file;
                # the length check on the whole document still catches a total failure.
                current_app.logger.warning("Could not read scanned page %s", index + 1)
                return index, ""

    with ThreadPoolExecutor(max_workers=min(len(indexes), MAX_CONCURRENT_IMAGE_CALLS)) as pool:
        futures = [pool.submit(read, index) for index in indexes]
        for future in as_completed(futures):
            yield future.result()


def _page_image_bytes(page):
    """The page's scan as JPEG bytes, or None if the page carries no usable image.

    A scanned page is normally a single full-page image; where a scanner has split one into
    strips, the largest is the one carrying the body text.
    """
    try:
        images = list(page.images)
    except Exception:
        return None
    if not images:
        return None

    try:
        image = max(images, key=lambda item: item.image.width * item.image.height).image
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90, optimize=True)
        return buffer.getvalue()
    except Exception:
        return None


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


_SYSTEM_PROMPT = """You are an expert exam writer. Given study material and a requested \
question breakdown, generate exam questions strictly as JSON.

Output ONLY a JSON object of this exact shape, no markdown, no commentary:
{
  "questions": [
    {"type": "mcq", "prompt": "...", "options": ["...", "...", "...", "..."], "correct_option_index": 0, "marks": 1},
    {"type": "structured", "prompt": "...", "marks": 5},
    {"type": "essay", "prompt": "...", "marks": 10}
  ]
}

Accuracy rules (most important):
- Every question, option, and correct answer must be directly and verifiably supported by the
  material below. Do not invent facts, names, dates, numbers, formulas, or details that are not
  present in the material.
- For "mcq" questions, the correct option must be explicitly stated or directly derivable from the
  material, and the three incorrect options must be plausible but clearly wrong per the material —
  never ambiguous or arguably also correct.
- If the material does not contain enough distinct content to support the full requested count of
  a question type without repeating or fabricating, generate fewer questions of that type rather
  than inventing content. Never pad with generic or off-topic questions.

No repetition (just as important as accuracy):
- Every question must test a DIFFERENT fact. No two questions may have the same correct answer or
  check the same detail, in any question type.
- Never re-ask a fact by rewording it. "What is X?", "X is made of what?", "Which of these is NOT
  part of X?" and "X was discovered by whom?" all test the same fact — choose only ONE of them.
- Before you write each new question, re-read the questions you have already written and confirm
  the new one covers a fact that none of them touch.
- Work through the whole material from beginning to end and spread the questions evenly across
  every distinct topic in it. Do not ask several questions about one topic while ignoring others.
- Returning FEWER questions than requested is always better than repeating a fact. If the material
  only supports 6 distinct questions, return 6 and stop.

Other rules:
- Produce the requested number of questions of each type, unless the accuracy or no-repetition
  rules above require producing fewer.
- "mcq" questions must have 4 "options" and a valid 0-based "correct_option_index".
- "structured" and "essay" questions must NOT include "options" or "correct_option_index".
- Vary marks sensibly: mcq typically 1-2, structured 4-8, essay 8-15.
- Write every "prompt" and "options" value in the requested language below. Keep the JSON keys
  ("type", "prompt", "options", "correct_option_index", "marks") in English exactly as shown.
"""


MAX_MATERIAL_CHARS = 24000

_RETRY_NUDGE = (
    "That response was empty or not valid JSON matching the schema. If the material "
    "has ANY content relevant to the requested topics, generate at least a few valid "
    "questions from it — only return an empty list if the material truly has no "
    "usable content at all. Respond with JSON only."
)


def _normalize_counts(counts: dict) -> dict:
    counts = {k: v for k, v in counts.items() if v}
    if not counts:
        raise AIServiceError("Select at least one question to generate.")
    return counts


def _brief(counts: dict, title_hint: str, language: str) -> str:
    requested = ", ".join(f"{count} {qtype}" for qtype, count in counts.items())
    return (
        f"Material title: {title_hint}\n\n"
        f"Requested questions: {requested}\n\n"
        f"Write the exam in this language: {language}"
    )


def _material_block(text: str) -> str:
    truncated_note = (
        "\n\n[material truncated for length]" if len(text) > MAX_MATERIAL_CHARS else ""
    )
    return f"Material:\n{text[:MAX_MATERIAL_CHARS]}{truncated_note}"


def generate_questions(text: str, counts: dict, title_hint: str, language: str = "English") -> list[dict]:
    counts = _normalize_counts(counts)
    user_prompt = f"{_brief(counts, title_hint, language)}\n\n{_material_block(text)}"
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    return _generate(messages, counts, _call_nvidia_nim)


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


def _generate(messages: list[dict], counts: dict, call, max_attempts: int = 3) -> list[dict]:
    for attempt in range(max_attempts):
        raw = None
        try:
            raw = call(messages)
            return _parse_questions(raw, counts)
        except AIServiceUnavailable:
            # _post_nim already retried the transport; retrying here just multiplies the wait.
            raise
        except AIServiceError:
            if attempt == max_attempts - 1:
                raise
            if raw is not None:
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": _RETRY_NUDGE},
                ]

    raise AIServiceError("AI failed to generate valid questions.")


def _message_content(data: dict) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIServiceError("The AI service returned an unexpected response.") from exc

    # Reasoning models leave "content" null when they exhaust the budget mid-thought;
    # surfacing it as an error lets the caller retry instead of parsing None.
    if not content:
        raise AIServiceError("The AI service returned an empty response.")
    return content


def _call_nvidia_nim(messages: list[dict]) -> str:
    data = _post_nim(
        {
            "model": current_app.config["NVIDIA_NIM_MODEL"],
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 4096,
        },
        timeout=TEXT_TIMEOUT_SECONDS,
    )
    return _message_content(data)


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


def _extract_question_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("questions"), list):
            return payload["questions"]
        if "type" in payload and "prompt" in payload:
            return [payload]
    return None


def _parse_questions(raw: str, counts: dict) -> list[dict]:
    cleaned = re.sub(r"^```(json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIServiceError("AI response was not valid JSON.") from exc

    questions = _extract_question_list(payload)
    if questions is None:
        raise AIServiceError("AI response did not contain a list of questions.")

    normalized = []
    seen_prompts = set()
    for q in questions:
        if not isinstance(q, dict):
            continue
        qtype = _coerce_type(q.get("type"))
        if qtype is None:
            continue
        prompt = str(q.get("prompt") or "").strip()
        if not prompt:
            continue
        key = _dedupe_key(prompt)
        if key in seen_prompts:
            continue

        item = {"type": qtype, "prompt": prompt, "marks": _coerce_marks(q.get("marks"), qtype)}
        if qtype == "mcq":
            options = q.get("options") or []
            if not isinstance(options, list) or len(options) < 2:
                continue
            answer = q.get("correct_option_index")
            if answer is None:
                answer = q.get("correct_answer", q.get("answer"))
            correct = _coerce_option_index(answer, options)
            if correct is None or not (0 <= correct < len(options)):
                continue
            if len(options) > 4:
                # Keep exactly 4 options; make sure the correct one survives the trim.
                correct_option = options[correct]
                others = [opt for i, opt in enumerate(options) if i != correct][:3]
                options = others + [correct_option]
                correct = len(options) - 1
            item["options"] = [str(opt) for opt in options]
            item["correct_option_index"] = correct
        seen_prompts.add(key)
        normalized.append(item)

    if not normalized:
        if questions:
            raise AIServiceError("AI returned questions in an unexpected format.")
        raise AIServiceError("AI did not return any usable questions.")
    return normalized
