"""Отправка сообщений в Telegram."""
from __future__ import annotations

import html
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


class Telegram:
    def __init__(self, token: str, chat_id: str | None):
        self.token = token
        self.chat_id = chat_id

    def call(self, method: str, **params) -> dict:
        for _ in range(5):
            r = requests.post(API.format(token=self.token, method=method), json=params, timeout=30)
            body = r.json()
            if r.status_code == 429:
                time.sleep(body.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            if not body.get("ok"):
                raise RuntimeError(f"Telegram {method}: {body.get('description')}")
            return body["result"]
        raise RuntimeError(f"Telegram {method}: слишком много запросов")

    def send(self, text: str) -> None:
        if len(text) > LIMIT:
            text = text[: LIMIT - 1] + "…"
        self.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
        # Telegram позволяет ~1 сообщение в секунду в один чат
        time.sleep(1.1)

    def chats(self) -> list[dict]:
        seen = {}
        for update in self.call("getUpdates"):
            for key in ("message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    seen[chat["id"]] = chat
        return list(seen.values())


def stars(rating: int) -> str:
    rating = max(0, min(5, int(rating)))
    return "⭐️" * rating + "☆" * (5 - rating)


def quote(text: str) -> str:
    return f"<blockquote>{esc(text)}</blockquote>"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def ratings_word(n: int) -> str:
    return f"{n} {plural(n, 'оценка', 'оценки', 'оценок')}"
