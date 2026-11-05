from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ATTACHMENT_DIR = BASE_DIR / "Attachment"
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONSUMABLE_PROFILE = os.environ.get("CONSUMABLE_PROFILE", "simulation").strip().lower()
if CONSUMABLE_PROFILE not in {"simulation", "production"}:
    raise ValueError("CONSUMABLE_PROFILE must be simulation or production")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env.local")
load_env_file(BASE_DIR / ".env")

DB_PATH = Path(
    os.environ.get(
        "CONSUMABLE_DB_PATH", DATA_DIR / f"{CONSUMABLE_PROFILE}.db"
    )
)
AUTO_SEED = os.environ.get(
    "CONSUMABLE_AUTO_SEED", "1" if CONSUMABLE_PROFILE == "simulation" else "0"
) != "0"
CHAT_UPLOAD_DIR = Path(
    os.environ.get("CONSUMABLE_CHAT_UPLOAD_DIR", DB_PATH.parent / f"{DB_PATH.stem}-uploads")
)
PHOTO_DEFAULT = DB_PATH.parent / "photos" if DB_PATH.name == "consumables.db" else DB_PATH.parent / f"{DB_PATH.stem}-photos"
PHOTO_DIR = Path(os.environ.get("CONSUMABLE_PHOTO_DIR", PHOTO_DEFAULT))
GENAI_API_KEY = os.environ.get("GENAI_API_KEY", "")
GENAI_MODEL = os.environ.get("GENAI_MODEL", "deepseek-pro")
GENAI_MODE = os.environ.get("GENAI_MODE", "completion")
GENAI_RESPONSE_URL = os.environ.get(
    "GENAI_RESPONSE_URL", "https://genaiapi.shanghaitech.edu.cn/api/v1/response"
)
GENAI_COMPLETION_URL = os.environ.get(
    "GENAI_COMPLETION_URL", "https://genaiapi.shanghaitech.edu.cn/api/v1/start"
)
GENAI_BASE_URL = os.environ.get(
    "GENAI_BASE_URL",
    GENAI_COMPLETION_URL if GENAI_MODE == "completion" else GENAI_RESPONSE_URL,
)
GENAI_TIMEOUT = float(os.environ.get("GENAI_TIMEOUT", "18"))
GENAI_TEMPERATURE = float(os.environ.get("GENAI_TEMPERATURE", "0.2"))
GENAI_MAX_OUTPUT_TOKENS = int(os.environ.get("GENAI_MAX_OUTPUT_TOKENS", "450"))
GENAI_TOTAL_TIMEOUT = float(os.environ.get("GENAI_TOTAL_TIMEOUT", "75"))
GENAI_PROVIDER_TIMEOUT = float(os.environ.get("GENAI_PROVIDER_TIMEOUT", "10"))


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
