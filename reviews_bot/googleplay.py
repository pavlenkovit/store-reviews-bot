"""Google Play: отзывы с текстом — через Play Developer API (он отдаёт только последние 7 дней
и только отзывы с текстом), оценки без текста — из ежемесячной выгрузки отзывов в бакете
отчётов Play (`reviews/reviews_<package>_<YYYYMM>.csv`), она отстаёт примерно на сутки.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from .telegram import esc, quote, stars

SCOPES = [
    "https://www.googleapis.com/auth/androidpublisher",
    "https://www.googleapis.com/auth/devstorage.read_only",
]
REVIEWS = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{package}/reviews"
OBJECT = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}?alt=media"
REVIEW_TTL = 30 * 86400
RATING_TTL = 75 * 86400


def session(service_account_info: dict) -> AuthorizedSession:
    creds = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return AuthorizedSession(creds)


def fetch_reviews(http: AuthorizedSession, package: str, translate_to: str) -> list[dict]:
    reviews, token = [], None
    for _ in range(20):
        params = {"maxResults": 100, "translationLanguage": translate_to}
        if token:
            params["token"] = token
        r = http.get(REVIEWS.format(package=package), params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Play reviews {package}: HTTP {r.status_code} {r.text[:300]}")
        body = r.json()
        reviews.extend(body.get("reviews", []))
        token = body.get("tokenPagination", {}).get("nextPageToken")
        if not token:
            break
    return reviews


def device_name(device: str | None) -> str | None:
    """«RMX3231 (realme C11 2021)» → «realme C11 2021»."""
    if device and device.endswith(")") and "(" in device:
        return device[device.rindex("(") + 1 : -1]
    return device


def review_text(app: dict, review: dict, edited: bool) -> str:
    c = review["comments"][0]["userComment"]
    modified = datetime.fromtimestamp(int(c["lastModified"]["seconds"]), timezone.utc)
    title = "Отзыв изменён в Google Play" if edited else "Новый отзыв в Google Play"
    lines = [f"<b>{title}</b>", esc(app["name"]), stars(c.get("starRating", 0)), ""]
    text = (c.get("text") or "").strip()
    original = (c.get("originalText") or "").strip()
    if original and original != text:
        lines.append(quote(original))
        lines.append(f"<i>Перевод:</i> {esc(text)}")
    elif text:
        lines.append(quote(text))
    device = device_name((c.get("deviceMetadata") or {}).get("productName") or c.get("device"))
    meta = [review.get("authorName") or "без имени", c.get("reviewerLanguage"), device]
    meta += [c.get("appVersionName") and f"v{c['appVersionName']}", f"{modified:%d.%m.%Y}"]
    lines.append(" · ".join(esc(m) for m in meta if m))
    return "\n".join(lines)


def check_reviews(state: dict, apps: list[dict], http: AuthorizedSession, translate_to: str) -> list[dict]:
    events = []
    seen_all = state.setdefault("play_reviews", {})
    now = int(time.time())
    for app in apps:
        first_run = app["package"] not in seen_all
        seen = seen_all.setdefault(app["package"], {})
        for review in fetch_reviews(http, app["package"], translate_to):
            comment = review["comments"][0]["userComment"]
            modified = int(comment["lastModified"]["seconds"])
            previous = seen.get(review["reviewId"])
            seen[review["reviewId"]] = modified
            if first_run or (previous is not None and previous >= modified):
                continue
            events.append({"time": modified, "text": review_text(app, review, edited=previous is not None)})
        for review_id, modified in list(seen.items()):
            if now - modified > REVIEW_TTL:
                del seen[review_id]
    return events


def month_keys(now: datetime) -> list[str]:
    prev = now.replace(day=1) - timedelta(days=1)
    return [f"{prev:%Y%m}", f"{now:%Y%m}"]


def fetch_report(http: AuthorizedSession, bucket: str, package: str, month: str) -> list[dict]:
    name = quote(f"reviews/reviews_{package}_{month}.csv", safe="")
    r = http.get(OBJECT.format(bucket=bucket, name=name), timeout=60)
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise RuntimeError(f"Play reviews report {package} {month}: HTTP {r.status_code} {r.text[:300]}")
    raw = r.content
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def rating_text(app: dict, row: dict, rating: int, old_rating: int | None) -> str:
    submitted = datetime.fromtimestamp(int(row["Review Submit Millis Since Epoch"]) / 1000, timezone.utc)
    if old_rating is None:
        lines = ["<b>Новая оценка в Google Play</b>", esc(app["name"]), stars(rating)]
    else:
        lines = ["<b>Оценка изменена в Google Play</b>", esc(app["name"]), f"{stars(rating)} (было {old_rating})"]
    meta = [row.get("Reviewer Language"), device_name(row.get("Device"))]
    meta += [row.get("App Version Name") and f"v{row['App Version Name']}", f"{submitted:%d.%m.%Y}"]
    lines.append(" · ".join(esc(m) for m in meta if m))
    return "\n".join(lines)


def check_ratings(state: dict, apps: list[dict], http: AuthorizedSession, bucket: str) -> list[dict]:
    events = []
    seen_all = state.setdefault("play_ratings", {})
    now = datetime.now(timezone.utc)
    for app in apps:
        first_run = app["package"] not in seen_all
        seen = seen_all.setdefault(app["package"], {})
        for month in month_keys(now):
            for row in fetch_report(http, bucket, app["package"], month):
                if (row.get("Review Text") or "").strip() or (row.get("Review Title") or "").strip():
                    continue  # отзывы с текстом приходят из API
                submitted = row.get("Review Submit Millis Since Epoch")
                rating = row.get("Star Rating")
                if not submitted or not rating:
                    continue
                key = f"{submitted}|{row.get('Device', '')}|{row.get('Reviewer Language', '')}"
                rating = int(rating)
                previous = seen.get(key)
                seen[key] = rating
                if first_run or previous == rating:
                    continue
                updated = row.get("Review Last Update Millis Since Epoch") or submitted
                events.append({"time": int(updated) / 1000, "text": rating_text(app, row, rating, previous)})
        cutoff = (now.timestamp() - RATING_TTL) * 1000
        for key in list(seen):
            if int(key.split("|", 1)[0]) < cutoff:
                del seen[key]
    return events
