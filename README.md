# Store Reviews Bot

Telegram-бот, который присылает новые отзывы и оценки из App Store, Google Play и Chrome Web Store.
Работает без сервера: GitHub Actions запускает проверку каждые 10 минут
(`.github/workflows/check.yml`), а что уже отправлено, хранит в ветке `state`.

Приложения перечислены в `apps.json`.

## Откуда что берётся

| | отзыв с текстом | оценка без текста |
|---|---|---|
| App Store | App Store Connect API `customerReviews` | прирост счётчика оценок по стране в iTunes Lookup |
| Google Play | Play Developer API `reviews` (+ перевод) | выгрузка `reviews/…csv` в бакете отчётов Play |
| Chrome Web Store | страница отзывов расширения, по всем языкам витрины | прирост общего счётчика оценок |

Ограничения сторов:

- Apple не отдаёт оценки без текста поштучно. Бот видит, что в стране счётчик вырос
  на 1, и вычисляет звёзды по изменению средней. Если между проверками пришло несколько —
  пишет «+N оценок» со средней. Отзыв тоже увеличивает счётчик, поэтому его звёзды
  вычитаются, чтобы не прислать его дважды. Счётчик в Lookup обновляется с задержкой
  до нескольких часов.
- Play API отдаёт только отзывы с текстом за последние 7 дней. Оценки без текста есть
  только в ежемесячной выгрузке отзывов, она отстаёт примерно на сутки.
- У Chrome Web Store нет API для отзывов. Бот читает данные со страницы отзывов, а она
  показывает только отзывы на своём языке, поэтому обходит ~50 языков. Оценки без текста —
  как в App Store, по приросту счётчика (одного на весь мир, без страны).
- При первом запуске бот запоминает всё, что уже есть, и присылает только новое.

## Секреты (GitHub → Settings → Secrets → Actions)

| секрет | что это |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | чат, куда слать (личка или группа) |
| `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_PRIVATE_KEY` | ключ App Store Connect API (содержимое `.p8`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON сервисного аккаунта с доступом к приложению и отчётам в Play Console |

Загрузить их из локального `.env` (см. `.env.example`):

    uv run --with pynacl --with requests python scripts/setup_secrets.py

## Локально

    cp .env.example .env   # заполнить
    uv run --python 3.12 --with-requirements requirements.txt python -m reviews_bot chats      # узнать chat id
    uv run --python 3.12 --with-requirements requirements.txt python -m reviews_bot test       # тестовое сообщение
    uv run --python 3.12 --with-requirements requirements.txt python -m reviews_bot --dry-run  # проверка без отправки

Локальное состояние лежит в `state.json` и не связано с веткой `state` в GitHub.
