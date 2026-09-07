import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")

    # --- Database ---
    MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
    MYSQL_PORT = os.environ.get("MYSQL_PORT", "3306")
    MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "ai_exam_platform")
    MYSQL_USERNAME = os.environ.get("MYSQL_USERNAME", "root")
    MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")

    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{MYSQL_USERNAME}:{MYSQL_PASSWORD}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
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
    CORS_ORIGINS = [
        origin.strip()
        for origin in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    ]

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
