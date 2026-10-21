"""Настройки: список приложений из apps.json, секреты из окружения (или .env)."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Поля файла ASC_CREDENTIALS_FILE ({"key_id", "issuer_id", "key_path"}) → секреты
ASC_CREDENTIALS = {"ASC_KEY_ID": "key_id", "ASC_ISSUER_ID": "issuer_id", "ASC_PRIVATE_KEY": "key_path"}


def secret(name: str) -> str | None:
    """NAME — само значение, NAME_FILE — путь к файлу с ним (удобно локально)."""
    value = os.environ.get(name)
    if value:
        return value
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).expanduser().read_text()
    credentials = os.environ.get("ASC_CREDENTIALS_FILE")
    if name in ASC_CREDENTIALS and credentials:
        value = json.loads(Path(credentials).expanduser().read_text())[ASC_CREDENTIALS[name]]
        return Path(value).expanduser().read_text() if name == "ASC_PRIVATE_KEY" else value
    return None


def apps() -> dict:
    return json.loads((ROOT / "apps.json").read_text())


def state_path() -> Path:
    return Path(os.environ.get("STATE_FILE", ROOT / "state.json"))


def translate_to() -> str:
    return os.environ.get("TRANSLATE_TO", "ru")
