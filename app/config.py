import os
import re
from datetime import timedelta
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()


def _database_uri(username, password, host, port, database):
    """Build the SQLAlchemy URL, preferring a full URL when the platform supplies one.

    Railway's MySQL service exposes MYSQL_URL/DATABASE_URL rather than the five separate
    settings, and writes it with a bare "mysql://" scheme that SQLAlchemy cannot resolve to
    a driver on its own. The credentials are percent-encoded because a generated password
    routinely contains characters that would otherwise end the URL early.
    """
    url = os.environ.get("DATABASE_URL") or os.environ.get("MYSQL_URL") or ""
    if url:
        if url.startswith("mysql://"):
            url = "mysql+pymysql://" + url[len("mysql://") :]
        return url

    return (
        f"mysql+pymysql://{quote_plus(username)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}"
    )


def _cors_origins():
    """Origins allowed to call the API, as exact strings or compiled patterns.

    Two things bite here. A trailing slash never matches, because the browser's Origin
    header has none - so it is stripped rather than silently failing. And Vercel gives every
    preview deployment its own hostname, which no fixed list can cover, so a `*` in an entry
    is treated as a wildcard: `https://my-app-*.vercel.app` admits them all.
    """
    raw = os.environ.get("CORS_ORIGINS", "http://localhost:3000")
    origins = []
    for entry in raw.split(","):
        entry = entry.strip().rstrip("/")
        if not entry:
            continue
        if "*" in entry:
            pattern = ".*".join(re.escape(part) for part in entry.split("*"))
            origins.append(re.compile(f"^{pattern}$"))
        else:
            origins.append(entry)
    return origins


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")

    # --- Database ---
    MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
    MYSQL_PORT = os.environ.get("MYSQL_PORT", "3306")
    MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "ai_exam_platform")
    MYSQL_USERNAME = os.environ.get("MYSQL_USERNAME", "root")
    MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")

    SQLALCHEMY_DATABASE_URI = _database_uri(
        MYSQL_USERNAME, MYSQL_PASSWORD, MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
        # Sized against gunicorn.conf.py: each worker process gets its own pool, so the
        # ceiling on the database is workers x (pool_size + max_overflow).
        "pool_size": int(os.environ.get("DB_POOL_SIZE", 5)),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", 5)),
    }

    # --- JWT ---
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "change-me-too")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(
        seconds=int(os.environ.get("JWT_ACCESS_TOKEN_EXPIRES", 900))
    )
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(
        seconds=int(os.environ.get("JWT_REFRESH_TOKEN_EXPIRES", 2592000))
    )
    JWT_TOKEN_LOCATION = ["headers"]
    JWT_ERROR_MESSAGE_KEY = "message"

    # --- CORS ---
    CORS_ORIGINS = _cors_origins()

    # --- NVIDIA NIM ---
    NVIDIA_NIM_API_KEY = os.environ.get("NVIDIA_NIM_API_KEY", "")
    # Image work can be pointed at its own key so it doesn't eat the text model's rate
    # limit; blank (the normal setup) means it runs on NVIDIA_NIM_API_KEY.
    NVIDIA_NIM_VISION_API_KEY = (
        os.environ.get("NVIDIA_NIM_VISION_API_KEY", "") or NVIDIA_NIM_API_KEY
    )
    NVIDIA_NIM_BASE_URL = os.environ.get(
        "NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"
    )
    NVIDIA_NIM_MODEL = os.environ.get("NVIDIA_NIM_MODEL", "openai/gpt-oss-20b")
    # Image materials are sent to these as images and they write the exam from what they see.
    # The fallback is tried when the first model fails on a page, so one flaky response
    # doesn't cost the teacher the exam.
    NVIDIA_NIM_VISION_MODEL = os.environ.get(
        "NVIDIA_NIM_VISION_MODEL", "meta/llama-3.2-11b-vision-instruct"
    )
    NVIDIA_NIM_VISION_FALLBACK_MODEL = os.environ.get(
        "NVIDIA_NIM_VISION_FALLBACK_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    )
    # Reads pages in scripts the fast models cannot: given a Sinhala page they returned Tamil
    # letters and Sinhala-shaped gibberish respectively, while this one transcribed it. It is
    # much slower, so it is used only for languages not listed as safe for the fast models.
    NVIDIA_NIM_VISION_MULTILINGUAL_MODEL = os.environ.get(
        "NVIDIA_NIM_VISION_MULTILINGUAL_MODEL", "moonshotai/kimi-k3"
    )

    # --- File storage ---
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_MB", 25)) * 1024 * 1024

    # --- Cloudinary ---
    CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")
