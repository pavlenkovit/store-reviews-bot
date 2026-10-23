"""Проверка новых отзывов и оценок в сторах.

    python -m reviews_bot            — проверить и отправить новое в Telegram
    python -m reviews_bot --dry-run  — то же, но печатать сообщения, а не отправлять
    python -m reviews_bot chats      — показать чаты, где боту писали (узнать TELEGRAM_CHAT_ID)
    python -m reviews_bot test       — отправить тестовое сообщение
"""
from __future__ import annotations

import json
import sys
import traceback

from . import appstore, chromestore, config, googleplay
from .telegram import Telegram, esc


def load_state() -> dict:
    path = config.state_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(state: dict) -> None:
    config.state_path().write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def run(dry_run: bool) -> int:
    apps = config.apps()
    state = load_state()
    fresh = not state
    telegram = Telegram(config.secret("TELEGRAM_BOT_TOKEN") or "", config.secret("TELEGRAM_CHAT_ID"))
    events, errors = [], []

    def step(name, fn):
        try:
            result = fn()
            print(f"{name}: новых событий {len(result)}")
            events.extend(result)
        except Exception as e:
            traceback.print_exc()
            errors.append(f"{name}: {e}")

    key_id, issuer_id, private_key = (config.secret(n) for n in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"))
    if apps.get("appstore"):
        if key_id and issuer_id and private_key:
            token = appstore.asc_token(key_id, issuer_id, private_key)
            step("App Store отзывы", lambda: appstore.check_reviews(state, apps["appstore"], token))
        else:
            errors.append("App Store отзывы: не заданы ASC_KEY_ID / ASC_ISSUER_ID / ASC_PRIVATE_KEY")

        def ratings():
            found, failed = appstore.check_ratings(state, apps["appstore"])
            if failed:
                print(f"iTunes lookup не ответил по странам: {', '.join(failed)}")
            return found

        step("App Store оценки", ratings)

    service_account = config.secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    if apps.get("googleplay"):
        if service_account:
            http = googleplay.session(json.loads(service_account))
            step("Google Play отзывы", lambda: googleplay.check_reviews(state, apps["googleplay"], http, config.translate_to()))
            if apps.get("play_reports_bucket"):
                step("Google Play оценки", lambda: googleplay.check_ratings(state, apps["googleplay"], http, apps["play_reports_bucket"]))
        else:
            errors.append("Google Play: не задан GOOGLE_SERVICE_ACCOUNT_JSON")

    if apps.get("chromestore"):
        step("Chrome Web Store", lambda: chromestore.check(state, apps["chromestore"]))

    # Сообщение: (текст, отзыв в pending или None, message_id для правки или None).
    # Текст отзыва берём из pending только сейчас: проверка оценок могла дописать в него среднюю
    messages = []
    for e in sorted(events, key=lambda e: e["time"]):
        if "edit" in e:
            messages.append((e["edit"][2], None, e["edit"][3]))
        elif "review" in e:
            messages.append((e["review"][2], e["review"], None))
        else:
            messages.append((e["text"], None, None))
    if fresh:
        names = [f"{a['name']} (App Store)" for a in apps.get("appstore", [])]
        names += [f"{a['name']} (Google Play)" for a in apps.get("googleplay", [])]
        names += [f"{a['name']} (Chrome Web Store)" for a in apps.get("chromestore", [])]
        messages.insert(0, ("👋 Бот отзывов запущен. Слежу за: " + ", ".join(esc(n) for n in names)
                            + ".\nУже существующие отзывы и оценки пропускаю, пришлю только новые.", None, None))

    error_text = "\n".join(errors)
    if error_text != state.get("last_error", ""):
        if error_text:
            messages.append(("⚠️ <b>Ошибка проверки отзывов</b>\n" + esc(error_text), None, None))
        elif state.get("last_error"):
            messages.append(("✅ Проверка отзывов снова работает", None, None))
    state["last_error"] = error_text

    if dry_run:
        for text, _, message_id in messages:
            print("-" * 40 + (f" правка {message_id}" if message_id else "") + "\n" + text)
        return 1 if errors else 0

    sent = 0
    try:
        for text, review, message_id in messages:
            if message_id:
                try:
                    telegram.send(text, message_id)
                except RuntimeError as e:
                    print(f"Не удалось дописать среднюю в сообщение {message_id}: {e}")
            else:
                message_id = telegram.send(text)
                if review:
                    review[3] = message_id
            sent += 1
    finally:
        if sent < len(messages):
            print(f"Отправлено {sent} из {len(messages)} — состояние не сохраняю, повторю в следующий раз")
        else:
            save_state(state)
    print(f"Отправлено сообщений: {sent}")
    return 1 if errors else 0


def main() -> int:
    config.load_dotenv()
    args = sys.argv[1:]
    if args[:1] == ["chats"]:
        for chat in Telegram(config.secret("TELEGRAM_BOT_TOKEN") or "", None).chats():
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            print(f"{chat['id']}\t{chat['type']}\t{name}")
        return 0
    if args[:1] == ["test"]:
        Telegram(config.secret("TELEGRAM_BOT_TOKEN") or "", config.secret("TELEGRAM_CHAT_ID")).send("✅ Бот отзывов на связи")
        return 0
    return run(dry_run="--dry-run" in args)


if __name__ == "__main__":
    sys.exit(main())
