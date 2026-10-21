"""Загрузить секреты из .env в GitHub Actions репозитория.

    uv run --with pynacl --with requests python scripts/setup_secrets.py [owner/repo]

Токен GitHub берётся из git credential helper (keychain), значения — как у бота (.env).
"""
from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import requests
from nacl import encoding, public

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reviews_bot import config  # noqa: E402

NAMES = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON"]


def github_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)["password"]


def main() -> int:
    config.load_dotenv()
    repo = sys.argv[1] if len(sys.argv) > 1 else "pavlenkovit/store-reviews-bot"
    gh = requests.Session()
    gh.headers.update({"Authorization": f"Bearer {github_token()}", "Accept": "application/vnd.github+json"})
    key = gh.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", timeout=30)
    key.raise_for_status()
    box = public.SealedBox(public.PublicKey(key.json()["key"].encode(), encoding.Base64Encoder()))

    missing = []
    for name in NAMES:
        value = config.secret(name)
        if not value:
            missing.append(name)
            continue
        encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
        r = gh.put(
            f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
            json={"encrypted_value": encrypted, "key_id": key.json()["key_id"]}, timeout=30,
        )
        r.raise_for_status()
        print(f"✓ {name}")
    if missing:
        print("Не заданы в .env: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
