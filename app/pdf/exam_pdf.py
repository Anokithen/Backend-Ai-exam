import io
import os
import re
import string
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFontFile
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from app.models.exam_model import QuestionType
from app.pdf.bamini import unicode_to_bamini

_LETTERS = string.ascii_uppercase

# The Tamil font only has Tamil-script glyphs (no Latin), so mixed Tamil+English
# text (marks, numbers, English terms) needs per-run font switching rather than
# a single font for the whole document — ReportLab has no automatic font fallback.
_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
_FONT_TAMIL = "TamilScript"
_FONT_TAMIL_BOLD = "TamilScript-Bold"
_TAMIL_RUN = re.compile(r"[\u0b80-\u0bff]+")
# Set by _register_fonts(): True when the resolved face is a legacy 8-bit font.
_TAMIL_IS_LEGACY = False

# Preferred first: Bamini, then Latha (Microsoft's Tamil UI font). Neither is
# redistributable, so they are only used when someone drops the .ttf into fonts/
# (or installs it system-wide); otherwise we fall back to Noto Sans Tamil.
# Bamini is a legacy 8-bit font - see _TAMIL_IS_LEGACY below.
_TAMIL_FACES = (
    ("bamini.ttf", "baminib.ttf"),
    ("latha.ttf", "lathab.ttf"),
    ("NotoSansTamil-Regular.ttf", "NotoSansTamil-Bold.ttf"),
)
# Project-root fonts/ dir, so a TTF can be dropped in without digging into the
# package: <repo>/fonts (exam_pdf.py lives at <repo>/Backend/app/pdf/).
_PROJECT_FONT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "fonts")
)
_FONT_SEARCH_DIRS = (
    _FONT_DIR,
    _PROJECT_FONT_DIR,
    "/usr/share/fonts/truetype/msttcorefonts",
    "/usr/share/fonts/truetype/tamil",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    "C:\\Windows\\Fonts",
)


def _find_font_file(filename: str):
    """Locate a font file case-insensitively across the known font directories."""
    target = filename.lower()
    for directory in _FONT_SEARCH_DIRS:
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            if entry.lower() == target:
                return os.path.join(directory, entry)
    return None


def _resolve_tamil_faces():
    """Return (regular_path, bold_path) for the first available Tamil face."""
    for regular, bold in _TAMIL_FACES:
        regular_path = _find_font_file(regular)
        if not regular_path:
            continue
        # Latha ships without a separate bold on some systems; reuse the regular.
        return regular_path, _find_font_file(bold) or regular_path
    raise FileNotFoundError(
        "No Tamil font found; drop bamini.ttf, latha.ttf or NotoSansTamil-Regular.ttf "
        f"into {_PROJECT_FONT_DIR} or {_FONT_DIR}"
    )


def _is_legacy_face(path: str) -> bool:
    """True for pre-Unicode fonts (Bamini): glyphs on ASCII, nothing in U+0B80-U+0BFF."""
    cmap = TTFontFile(path).charToGlyph
    return not any(code in cmap for code in range(0x0B80, 0x0C00))


def _register_fonts():
    global _TAMIL_IS_LEGACY
    if _FONT_TAMIL in pdfmetrics.getRegisteredFontNames():
        return
    regular_path, bold_path = _resolve_tamil_faces()
    _TAMIL_IS_LEGACY = _is_legacy_face(regular_path)
    pdfmetrics.registerFont(TTFont(_FONT_TAMIL, regular_path))
    pdfmetrics.registerFont(TTFont(_FONT_TAMIL_BOLD, bold_path))
    pdfmetrics.registerFontFamily(
        _FONT_TAMIL,
        normal=_FONT_TAMIL,
        bold=_FONT_TAMIL_BOLD,
        italic=_FONT_TAMIL,
        boldItalic=_FONT_TAMIL_BOLD,
    )


def _markup(text: str, bold: bool = False) -> str:
    """Escape for ReportLab's mini-XML and wrap Tamil-script runs in the Tamil font.

    For a legacy face the Tamil run is transliterated to Bamini's ASCII encoding
    first. That output contains XML-significant bytes (``<`` draws ஈ, ``&`` draws
    ரூ, ``>`` draws a comma), so each run is escaped *after* conversion rather than
    escaping the whole string up front.
    """
    _register_fonts()
    text = text or ""
    tamil_font = _FONT_TAMIL_BOLD if bold else _FONT_TAMIL

    parts = []
    cursor = 0
    for match in _TAMIL_RUN.finditer(text):
        parts.append(escape(text[cursor : match.start()]))
        run = match.group(0)
        if _TAMIL_IS_LEGACY:
            run = unicode_to_bamini(run)
        parts.append(f'<font face="{tamil_font}">{escape(run)}</font>')
        cursor = match.end()
    parts.append(escape(text[cursor:]))
    return "".join(parts)


def build_exam_pdf(exam, include_answer_key: bool) -> io.BytesIO:
    _register_fonts()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
    )

    # Base font stays Helvetica (built-in, full Latin/punctuation coverage); Tamil
    # runs are switched to NotoSansTamil inline via _markup().
    title_style = ParagraphStyle("ExamTitle", fontName="Helvetica-Bold", fontSize=18, leading=22, spaceAfter=6)
    normal_style = ParagraphStyle("ExamNormal", fontName="Helvetica", fontSize=10.5, leading=16)
    question_style = ParagraphStyle(
        "Question", fontName="Helvetica", fontSize=10.5, leading=16, spaceBefore=12, spaceAfter=4
    )
    option_style = ParagraphStyle(
        "Option", fontName="Helvetica", fontSize=10.5, leading=16, leftIndent=18, spaceAfter=2
    )

    story = [
        Paragraph(_markup(exam.title, bold=True), title_style),
        Paragraph(
            f"Total marks: {exam.total_marks} &nbsp;&bull;&nbsp; Time limit: {exam.time_limit_minutes} minutes",
            normal_style,
        ),
    ]
    if exam.instructions:
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<b>Instructions:</b> {_markup(exam.instructions)}", normal_style))
    story.append(Spacer(1, 16))

    for index, question in enumerate(exam.questions, start=1):
        story.append(
            Paragraph(f"{index}. {_markup(question.prompt)} <i>({question.marks} marks)</i>", question_style)
        )
        if question.type == QuestionType.MCQ:
            for opt_index, option in enumerate(question.options or []):
                story.append(Paragraph(f"{_LETTERS[opt_index]}. {_markup(option)}", option_style))
        else:
            blank_lines = 3 if question.type == QuestionType.STRUCTURED else 8
            for _ in range(blank_lines):
                story.append(Spacer(1, 18))
                story.append(Paragraph("_" * 90, option_style))

    if include_answer_key:
        story.append(PageBreak())
        story.append(Paragraph("Answer Key", title_style))
        for index, question in enumerate(exam.questions, start=1):
            if question.type == QuestionType.MCQ and question.correct_option_index is not None:
                answer = _LETTERS[question.correct_option_index]
                story.append(Paragraph(f"{index}. {answer}", normal_style))
            else:
                story.append(Paragraph(f"{index}. (manual grading — {question.type.value})", normal_style))

    doc.build(story)
    buffer.seek(0)
    return buffer
