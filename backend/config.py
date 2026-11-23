import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


class SettingsError(RuntimeError):
    """Raised at startup for an unusable .env value, so the server fails fast with a clear message."""


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"Invalid value for {name} in .env: {raw!r} is not a whole number.")
    if value < minimum:
        raise SettingsError(f"Invalid value for {name} in .env: {value} (must be >= {minimum}).")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


PROVIDERS = {"ocr_api", "openai", "mock"}


class Settings:
    # --- model -------------------------------------------------------------
    MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "mock").strip().lower()  # ocr_api | openai | mock
    MODEL_API_URL = os.getenv("MODEL_API_URL", "").strip()
    MODEL_API_KEY = os.getenv("MODEL_API_KEY", "").strip()
    MODEL_NAME = os.getenv("MODEL_NAME", "gemma-4-12b-it").strip()
    MODEL_TIMEOUT = _int("MODEL_TIMEOUT_SECONDS", 120, 1)
    MODEL_MAX_TOKENS = _int("MODEL_MAX_TOKENS", 3000, 1)
    MODEL_PROMPT_MAX_CHARS = _int("MODEL_PROMPT_MAX_CHARS", 0)  # 0 = no limit

    # --- documents ---------------------------------------------------------
    MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 15, 1)
    MAX_PDF_PAGES = _int("MAX_PDF_PAGES", 3, 1)
    IMAGE_MAX_SIDE = _int("IMAGE_MAX_SIDE", 2000, 256)

    # --- server ------------------------------------------------------------
    HOST = os.getenv("HOST", "10.120.14.218")
    PORT = _int("PORT", 5000, 1)
    DEBUG = _bool("DEBUG", True)

    # --- matching ----------------------------------------------------------
    # A key whose match score is at least this (but which is not a full match) is "PARTIALLY_MATCHED".
    PARTIAL_MATCH_MIN_SCORE = _int("PARTIAL_MATCH_MIN_SCORE", 65, 1)
    MAX_JSON_KB = 256   # largest accepted JSON file

    # --- paths -------------------------------------------------------------
    DOCUMENT_TYPES_FILE = ROOT_DIR / "backend" / "document_types.json"
    FRONTEND_DIR = ROOT_DIR / "frontend"

    def problems(self) -> list:
        """Configuration mistakes worth warning about at startup (the server still starts)."""
        out = []
        if self.MODEL_PROVIDER not in PROVIDERS:
            out.append(f"MODEL_PROVIDER={self.MODEL_PROVIDER!r} is not one of {sorted(PROVIDERS)}.")
        elif self.MODEL_PROVIDER != "mock" and not self.MODEL_API_URL:
            out.append(f"MODEL_PROVIDER={self.MODEL_PROVIDER} needs MODEL_API_URL to be set.")
        return out


settings = Settings()
