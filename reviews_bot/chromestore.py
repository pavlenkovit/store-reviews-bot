"""Chrome Web Store: API для отзывов нет, поэтому читаем страницу отзывов расширения.

В ней лежат данные для отрисовки (AF_initDataCallback): ds:0 — карточка расширения
со средней оценкой и числом оценок, ds:1 — свежие отзывы, но только на языке страницы (hl).
Поэтому отзывы собираем, обходя все языки витрины. Оценки без текста, как и в App Store,
замечаем по приросту общего счётчика; звёзды уже показанных отзывов лежат в pending
и вычитаются, когда счётчик до них дойдёт. Тогда же в сообщение об отзыве дописываем,
как изменилась средняя.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from .telegram import esc, pending_review, quote, ratings_word, settle_reviews, stars

PAGE = "https://chromewebstore.google.com/detail/{id}/reviews"
# Языки интерфейса Chrome Web Store
LANGUAGES = [
    "am", "ar", "bg", "bn", "ca", "cs", "da", "de", "el", "en", "es", "et", "fa", "fi", "fil", "fr",
    "gu", "he", "hi", "hr", "hu", "id", "it", "ja", "kn", "ko", "lt", "lv", "ml", "mr", "ms", "nl",
    "no", "pl", "pt", "ro", "ru", "sk", "sl", "sr", "sv", "sw", "ta", "te", "th", "tr", "uk", "vi",
    "zh-CN", "zh-TW",
]
PENDING_TTL = 7 * 86400
DROP_TTL = 86400
SEEN_LIMIT = 1000


def page_data(html: str, key: str):
    m = re.search(r"AF_initDataCallback\(\{key: '" + re.escape(key) + r"'.*?data:(.*?), sideChannel:", html, re.S)
    if not m:
        raise RuntimeError(f"нет {key} на странице")
    return json.loads(m.group(1))


def fetch_page(ext_id: str, hl: str) -> tuple[tuple[int, float], list[list]]:
    """(число оценок, средняя) и отзывы на языке hl, свежие сначала."""
    for attempt in range(3):
        r = requests.get(PAGE.format(id=ext_id), params={"hl": hl}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        if r.status_code == 200:
            card = page_data(r.text, "ds:0")[0]
            reviews = page_data(r.text, "ds:1")
            return (int(card[4] or 0), float(card[3] or 0)), (reviews[1] if len(reviews) > 1 else None) or []
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Chrome Web Store {ext_id} {hl}: HTTP {r.status_code}")


def review_text(ext: dict, review: list, edited: bool) -> str:
    author, rating, text = review[1][0], review[2], review[3]
    modified = datetime.fromtimestamp((review[5] or review[4])[0], timezone.utc)
    title = "Отзыв изменён в Chrome Web Store" if edited else "Новый отзыв в Chrome Web Store"
    lines = [f"<b>{title}</b>", esc(ext["name"]), stars(rating), ""]
    if text:
        lines.append(quote(text))
    meta = [author or "без имени", review[13], review[11] and f"v{review[11]}", f"{modified:%d.%m.%Y}"]
    lines.append(" · ".join(esc(m) for m in meta if m))
    lines.append(f'<a href="{PAGE.format(id=ext["id"])}">Открыть в Chrome Web Store</a>')
    return "\n".join(lines)


def check(state: dict, extensions: list[dict]) -> list[dict]:
    events = []
    seen_all = state.setdefault("chrome_reviews", {})
    ratings_all = state.setdefault("chrome_ratings", {})
    pending_all = state.setdefault("chrome_pending", {})
    now = int(time.time())

    for ext in extensions:
        def safe_fetch(hl: str):
            try:
                return fetch_page(ext["id"], hl)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(zip(LANGUAGES, pool.map(safe_fetch, LANGUAGES)))
        failed = [hl for hl, result in results.items() if result is None]
        if len(failed) > len(LANGUAGES) // 2:
            raise RuntimeError(f"{ext['name']}: страница не ответила на {len(failed)} языках из {len(LANGUAGES)}")

        # Отзывы. Один и тот же отзыв может попасть на страницы разных языков
        first_run = ext["id"] not in seen_all
        seen = seen_all.setdefault(ext["id"], {})
        pending = pending_all.setdefault(ext["id"], [])
        reviews = {}
        for result in results.values():
            for review in (result or (None, []))[1]:
                reviews[review[0]] = review
        for review in sorted(reviews.values(), key=lambda r: (r[5] or r[4])[0]):
            modified = (review[5] or review[4])[0]
            previous = seen.get(review[0])
            seen[review[0]] = modified
            if first_run or (previous is not None and previous >= modified):
                continue
            text = review_text(ext, review, edited=previous is not None)
            if previous is None:
                pending.append(pending_review(review[2], now, text))
                events.append({"time": modified, "review": pending[-1]})
            else:
                events.append({"time": modified, "text": text})
        for review_id in sorted(seen, key=seen.get)[:-SEEN_LIMIT]:
            del seen[review_id]

        # Оценки без текста. Если какой-то язык не ответил, его новый отзыв можно принять
        # за оценку без текста — счётчик тогда не трогаем до следующей проверки
        pending[:] = [item for item in pending if now - item[1] < PENDING_TTL]
        if failed:
            print(f"Chrome Web Store {ext['name']}: не ответили языки {', '.join(failed)}")
            continue
        new_count, new_avg = next(result[0] for result in results.values() if result)
        old = ratings_all.get(ext["id"])
        if old is None:
            ratings_all[ext["id"]] = [new_count, new_avg]
            continue
        old_count, old_avg, *rest = old
        if new_count < old_count:
            # Как и в App Store: устаревший ответ с меньшим счётчиком не записываем,
            # иначе следующий свежий пришлёт ту же оценку ещё раз. Уменьшение
            # (удалённую оценку) принимаем, только если оно держится сутки.
            since = rest[0] if rest else now
            if now - since < DROP_TTL:
                ratings_all[ext["id"]] = [old_count, old_avg, since]
                continue
        ratings_all[ext["id"]] = [new_count, new_avg]
        added = new_count - old_count
        if added <= 0:
            continue
        stars_sum = new_avg * new_count - old_avg * old_count
        taken = pending[:added]
        del pending[:added]
        stars_sum -= sum(item[0] for item in taken)
        added -= len(taken)
        summary = f"Средняя {old_avg:.2f} → {new_avg:.2f} ({ratings_word(new_count)})"
        events.extend(settle_reviews(taken, summary))
        if added <= 0:
            continue

        if added == 1:
            rating = max(1, min(5, round(stars_sum)))
            text = f"<b>Новая оценка в Chrome Web Store</b>\n{esc(ext['name'])}\n{stars(rating)}\n{summary}"
        else:
            head = f"<b>+{ratings_word(added)} в Chrome Web Store</b>"
            text = f"{head}\n{esc(ext['name'])}\nВ среднем {stars_sum / added:.1f}\n{summary}"
        events.append({"time": now, "text": text})
    return events
