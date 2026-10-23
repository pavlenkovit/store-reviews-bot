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

    def send(self, text: str, message_id: int | None = None) -> int:
        """Отправить сообщение или, если задан message_id, заменить текст уже отправленного."""
        if len(text) > LIMIT:
            text = text[: LIMIT - 1] + "…"
        params = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if message_id:
            self.call("editMessageText", message_id=message_id, **params)
        else:
            message_id = self.call("sendMessage", **params)["message_id"]
        # Telegram позволяет ~1 сообщение в секунду в один чат
        time.sleep(1.1)
        return message_id

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


def pending_review(rating: int, now: int, text: str) -> list:
    """Отзыв, который ещё не дошёл до счётчика оценок: [звёзды, когда, текст сообщения, message_id]."""
    return [rating, now, text, None]


def settle_reviews(taken: list[list], summary: str) -> list[dict]:
    """Счётчик дошёл до отзывов: дописать под звёздами, как изменилась средняя.
    Если сообщение ещё не отправлено, строка попадёт в него сразу, иначе его отредактируем."""
    events = []
    for item in taken:
        if len(item) < 3:
            continue  # из состояния до того, как начали хранить текст
        head, tail = item[2].split("\n\n", 1)
        item[2] = f"{head}\n{summary}\n\n{tail}"
        if item[3]:
            events.append({"time": item[1], "edit": item})
    return events
