# Predatory Beavers backend

Backend-каркас сайта баскетбольного клуба «Хищные Бобры». Он собран по локальному
FastAPI-шаблону и migration blueprint, но не использует шаблонные пакеты авторизации
и observability.

## Что уже есть

- FastAPI + Pydantic v2, API с префиксом `/api/v1`;
- async SQLAlchemy 2, PostgreSQL и Alembic;
- Dishka DI и разделение `router → service → repository`;
- сущности `teams`, `players`, `media_assets`;
- публичная выдача команд и игроков, admin CRUD игроков;
- собственная авторизация: Argon2, непрозрачная server-side session, HttpOnly cookie,
  CSRF и роли `USER`/`EDITOR`/`ADMIN`;
- собственные JSON-логи с `request_id`, HTTP-метриками и безопасными заголовками;
- отдельный процесс worker и порты будущих интеграций ASB, Telegram и S3;
- Docker Compose, миграция, тесты, Ruff, mypy и GitHub Actions.

В репозитории намеренно нет `backend-auth` и `fastapi-observability-logging`.

## Быстрый старт

Требуются Python 3.12+ и PostgreSQL. Рекомендуемый менеджер — `uv`:

```powershell
uv sync --extra dev
Copy-Item .env.example .env
docker compose up -d db
uv run alembic upgrade head
uv run python -m predatory_beavers.cli seed-club
uv run python -m predatory_beavers.cli create-admin --username admin --email admin@example.com
uv run python run.py
```

Если `uv` ещё не установлен, для первоначального запуска подходит обычный virtualenv:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

API: `http://localhost:8000`; Swagger: `http://localhost:8000/docs`.

Полный локальный стек:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Миграции выполняются отдельным одноразовым сервисом `migrate`; API и worker стартуют
после его успешного завершения.

## Основные endpoints

```text
GET  /health/live
GET  /health/ready

POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/auth/me

GET  /api/v1/teams
GET  /api/v1/players?team=men&category=men&page=1&page_size=24
GET  /api/v1/players/{player_id}

POST   /api/v1/admin/players
PATCH  /api/v1/admin/players/{player_id}
DELETE /api/v1/admin/players/{player_id}
```

После login браузер получает только HttpOnly cookie. Значение `csrf_token` из JSON-ответа
нужно передавать в `X-CSRF-Token` для logout и admin-изменений. В БД хранятся только
SHA-256 отпечатки session/CSRF токенов. В production обязательны
`APP_COOKIE_SECURE=true` и cookie name с префиксом `__Host-`, например
`APP_SESSION_COOKIE_NAME=__Host-pb_session`; настройки валидируются при старте.
Встроенный limiter ограничивает частоту и параллелизм дорогих Argon2-проверок в одном
процессе. На reverse proxy всё равно нужен распределённый rate limit для login/admin API.

## Структура

```text
src/predatory_beavers/
├─ api/                 # composition root, HTTP, ошибки, middleware, /api/v1
├─ db/                  # declarative base и async session factory
├─ modules/
│  ├─ auth/             # самостоятельные sessions/roles/CSRF
│  └─ club/             # teams/players/media
├─ integrations/        # ASB, Telegram, object storage ports
├─ observability/       # самостоятельный logger
└─ worker/              # отдельная точка запуска фонового процесса
```

Секреты не должны попадать в Git. Production-настройки задаются environment/secret store;
список локальных ключей находится в `.env.example`.

## Проверки

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Начальная миграция должна применяться к пустой БД:

```powershell
uv run alembic upgrade head
uv run alembic current
```

Текущая реализация — первый backend milestone. ASB-клиент, загрузка файлов в S3/MinIO,
матчи, турнирные таблицы и Telegram delivery/outbox пока представлены границами модулей
и добавляются следующими вертикальными срезами.
