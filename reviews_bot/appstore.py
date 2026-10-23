"""App Store: отзывы — через App Store Connect API, оценки без текста — по счётчику iTunes Lookup.

Apple не отдаёт оценки без текста поштучно, только количество и среднюю по витрине.
Поэтому оценку мы замечаем как прирост счётчика в стране, а её звёзды восстанавливаем
по изменению суммы (средняя × количество). Отзыв тоже прибавляет к счётчику, поэтому
звёзды уже показанных отзывов лежат в pending и вычитаются, когда счётчик до них дойдёт.
Тогда же в сообщение об отзыве дописываем, как изменилась средняя.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import jwt
import requests

from .countries import ALPHA3_TO_2, STOREFRONTS, label
from .telegram import esc, pending_review, quote, ratings_word, settle_reviews, stars

ASC = "https://api.appstoreconnect.apple.com/v1"
LOOKUP = "https://itunes.apple.com/lookup"
PENDING_TTL = 7 * 86400
DROP_TTL = 86400
SEEN_LIMIT = 1000


def asc_token(key_id: str, issuer_id: str, private_key: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": issuer_id, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )


def reply_link(app_id: str) -> str:
    return f"https://appstoreconnect.apple.com/apps/{app_id}/distribution/activity/ios/ratingsResponses"


def fetch_reviews(token: str, app_id: str, seen: set[str]) -> list[dict]:
    """Свежие отзывы сначала; листаем, пока не встретим уже виденный (не больше 5 страниц)."""
    url = f"{ASC}/apps/{app_id}/customerReviews"
    params = {"sort": "-createdDate", "limit": 200}
    reviews = []
    for _ in range(5):
        r = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"ASC customerReviews {app_id}: HTTP {r.status_code} {r.text[:300]}")
        body = r.json()
        page = body.get("data", [])
        reviews.extend(page)
        if not page or any(item["id"] in seen for item in page):
            break
        url, params = body.get("links", {}).get("next"), None
        if not url:
            break
    return reviews


def review_text(app: dict, review: dict) -> str:
    a = review["attributes"]
    country = ALPHA3_TO_2.get(a.get("territory", ""), a.get("territory", "")[:2].lower())
    created = datetime.fromisoformat(a["createdDate"].replace("Z", "+00:00"))
    lines = ["<b>Новый отзыв в App Store</b>", f"{esc(app['name'])} · {label(country)}", stars(a["rating"]), ""]
    if a.get("title"):
        lines.append(f"<b>{esc(a['title'])}</b>")
    if a.get("body"):
        lines.append(quote(a["body"]))
    lines.append(f"{esc(a.get('reviewerNickname') or 'без имени')} · {created:%d.%m.%Y}")
    lines.append(f'<a href="{reply_link(app["id"])}">Ответить в App Store Connect</a>')
    return "\n".join(lines)


def check_reviews(state: dict, apps: list[dict], token: str) -> list[dict]:
    events = []
    seen_all = state.setdefault("appstore_reviews", {})
    pending_all = state.setdefault("appstore_pending", {})
    for app in apps:
        first_run = app["id"] not in seen_all
        seen = seen_all.setdefault(app["id"], [])
        seen_set = set(seen)
        reviews = fetch_reviews(token, app["id"], seen_set)
        new = [r for r in reviews if r["id"] not in seen_set]
        for review in reversed(new):
            seen.append(review["id"])
            if first_run:
                continue
            a = review["attributes"]
            created = datetime.fromisoformat(a["createdDate"].replace("Z", "+00:00")).timestamp()
            country = ALPHA3_TO_2.get(a.get("territory", ""))
            item = pending_review(a["rating"], int(time.time()), review_text(app, review))
            if country:
                pending_all.setdefault(app["id"], {}).setdefault(country, []).append(item)
            events.append({"time": created, "review": item})
        del seen[:-SEEN_LIMIT]
    return events


def lookup(app_ids: list[str], country: str) -> dict[str, tuple[int, float]]:
    for attempt in range(3):
        r = requests.get(LOOKUP, params={"id": ",".join(app_ids), "country": country}, timeout=20)
        if r.status_code == 200:
            return {
                str(item["trackId"]): (int(item.get("userRatingCount") or 0), float(item.get("averageUserRating") or 0))
                for item in r.json().get("results", [])
            }
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"iTunes lookup {country}: HTTP {r.status_code}")


def check_ratings(state: dict, apps: list[dict]) -> tuple[list[dict], list[str]]:
    events, failed = [], []
    ratings_all = state.setdefault("appstore_ratings", {})
    pending_all = state.setdefault("appstore_pending", {})
    first_run = {app["id"]: app["id"] not in ratings_all for app in apps}
    now = int(time.time())

    def safe_lookup(country: str) -> dict | None:
        try:
            return lookup([app["id"] for app in apps], country)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(zip(STOREFRONTS, pool.map(safe_lookup, STOREFRONTS)))

    for country, current in results.items():
        if current is None:
            failed.append(country)
            continue
        for app in apps:
            if app["id"] not in current:
                continue
            new_count, new_avg = current[app["id"]]
            ratings = ratings_all.setdefault(app["id"], {})
            old_count, old_avg, *rest = ratings.get(country, (0, 0.0))
            if new_count < old_count:
                # Lookup отвечает из разных кэшей, и старый ответ с меньшим счётчиком
                # приходит вперемешку со свежим. Если его записать, следующий свежий ответ
                # пришлёт ту же оценку ещё раз. Уменьшение (удалённую оценку) принимаем,
                # только если оно держится сутки.
                since = rest[0] if rest else now
                if now - since < DROP_TTL:
                    ratings[country] = [old_count, old_avg, since]
                    continue
            ratings[country] = [new_count, new_avg]
            added = new_count - old_count
            if first_run[app["id"]] or added <= 0:
                continue

            stars_sum = new_avg * new_count - old_avg * old_count
            pending = pending_all.get(app["id"], {}).get(country, [])
            taken = pending[:added]
            del pending[:added]
            stars_sum -= sum(item[0] for item in taken)
            added -= len(taken)

            if old_count:
                summary = f"Средняя {old_avg:.2f} → {new_avg:.2f} ({ratings_word(new_count)})"
            else:
                summary = f"Первая оценка в этой стране, средняя {new_avg:.2f}"
            events.extend(settle_reviews(taken, summary))
            if added <= 0:
                continue

            where = f"{esc(app['name'])} · {label(country)}"
            if added == 1:
                rating = max(1, min(5, round(stars_sum)))
                text = f"<b>Новая оценка в App Store</b>\n{where}\n{stars(rating)}\n{summary}"
            else:
                head = f"<b>+{ratings_word(added)} в App Store</b>"
                text = f"{head}\n{where}\nВ среднем {stars_sum / added:.1f}\n{summary}"
            events.append({"time": now, "text": text})

    for by_country in pending_all.values():
        for country, items in list(by_country.items()):
            items[:] = [item for item in items if now - item[1] < PENDING_TTL]
            if not items:
                del by_country[country]

    if len(failed) > len(STOREFRONTS) // 2:
        raise RuntimeError(f"iTunes lookup не ответил по {len(failed)} странам из {len(STOREFRONTS)}")
    return events, failed
