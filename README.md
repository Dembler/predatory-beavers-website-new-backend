# Predatory Beavers backend

Backend-каркас сайта баскетбольного клуба «Хищные Бобры». Он собран по локальному
FastAPI-шаблону и migration blueprint, но не использует шаблонные пакеты авторизации
и observability.

## Что уже есть

- FastAPI + Pydantic v2, API с префиксом `/api/v1`;
- async SQLAlchemy 2, SQLite с контролем foreign keys и Alembic;
- Dishka DI и разделение `router → service → repository`;
- сущности `teams`, `players`, `media_assets`, `competitions`, `venues`, `matches`,
  `achievements`;
- публичная выдача команд и игроков, admin CRUD игроков;
- соревнования, залы и матчи с публичными фильтрами и защищённым admin CRUD;
- загрузка изображений с проверкой содержимого, ограничением размеров, нормализацией в WebP
  и локальным файловым хранилищем;
- достижения клуба с публичной выдачей и защищённым admin CRUD;
- нормализованные турнирные таблицы с текущей версией и историей snapshot;
- атомарный append-only журнал административных изменений со снимками до/после;
- allowlisted ASB/InfoBasket import матчей, залов и турнирных таблиц с job-историей;
- агрегированный `/home` для первого экрана: команды, ближайший матч и последние результаты;
- собственная авторизация: Argon2, непрозрачная server-side session, HttpOnly cookie,
  CSRF и роли `USER`/`EDITOR`/`ADMIN`;
- собственные JSON-логи с `request_id`, HTTP-метриками и безопасными заголовками;
- отключённый по умолчанию каркас worker и порты будущих интеграций ASB, Telegram и S3;
- Docker Compose, миграция, тесты, Ruff, mypy и GitHub Actions.

В репозитории намеренно нет `backend-auth`, `backend-shared` и
`fastapi-observability-logging`: нужные контракты остаются локальными и типизированными.

## Быстрый старт

Требуется Python 3.12+. Рекомендуемый менеджер — `uv`:

```powershell
uv sync --extra dev
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data
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

БД создаётся в `data/predatory_beavers.db`, изображения сохраняются в `data/media`.
Текущий локальный снимок БД временно хранится в Git; каталог изображений и служебные
SQLite-файлы по-прежнему игнорируются. Для каждого SQLite-соединения backend включает foreign keys
и busy timeout. Перед развёртыванием нужно настроить резервное копирование общего каталога
`data`; API рассчитан на один пишущий экземпляр приложения.

Полный локальный стек:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Миграции выполняются отдельным одноразовым сервисом `migrate`; API стартует после его
успешного завершения. И БД, и медиа находятся в Docker volume `backend_data`. Telegram
worker не входит в текущий локальный стек.

## Основные endpoints

```text
GET  /health/live
GET  /health/ready

POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/auth/me
GET  /api/v1/auth/csrf

GET  /api/v1/teams
GET  /api/v1/players?team=men&category=men&page=1&page_size=24
GET  /api/v1/players/{player_id}
GET  /api/v1/competitions?season=2026-2027
GET  /api/v1/venues
GET  /api/v1/matches?team=men&status=scheduled&from=&to=
GET  /api/v1/matches/{match_id}
GET  /api/v1/achievements
GET  /api/v1/achievements/{achievement_id}
GET  /api/v1/standings?team=men&season=2026-2027
GET  /media/{media_id}/content
GET  /api/v1/home

GET    /api/v1/admin/players
GET    /api/v1/admin/players/{player_id}
POST   /api/v1/admin/players
PATCH  /api/v1/admin/players/{player_id}
DELETE /api/v1/admin/players/{player_id}

GET/POST/PATCH/DELETE /api/v1/admin/competitions[/{competition_id}]
GET/POST/PATCH/DELETE /api/v1/admin/venues[/{venue_id}]
GET/POST/PATCH/DELETE /api/v1/admin/matches[/{match_id}]
GET/POST/PATCH/DELETE /api/v1/admin/achievements[/{achievement_id}]
GET    /api/v1/admin/standings[/{snapshot_id}]
POST   /api/v1/admin/standings
DELETE /api/v1/admin/standings/{snapshot_id}
GET    /api/v1/admin/audit-log?action=&entity_type=&entity_id=&from=&to=
GET    /api/v1/admin/imports?status=&team=
GET    /api/v1/admin/imports/{job_id}
POST   /api/v1/admin/imports/asb
GET  /api/v1/admin/media/{media_id}
POST /api/v1/admin/media  # multipart/form-data: file, alt_text
```

Ответы с изображениями содержат стабильный `content_url`, независимый от версии JSON API,
но не раскрывают внутренний путь хранения. Старый путь `/api/v1/media/{id}/content`
сохранён как deprecated-совместимость.
Загрузка принимает JPEG, PNG и WebP размером до `APP_MEDIA_MAX_UPLOAD_BYTES`; файл
декодируется Pillow, поворачивается по EXIF, уменьшается до `APP_MEDIA_MAX_DIMENSION` и
сохраняется как WebP. Одинаковые изображения дедуплицируются по SHA-256.

Турнирная таблица публикуется целиком через `POST /api/v1/admin/standings`. Каждая
публикация создаёт новый snapshot и атомарно архивирует предыдущий для той же пары
команда/соревнование. Строки проверяются на уникальные последовательные места, уникальные
названия команд и согласованность количества игр с победами, поражениями и ничьими.

Каждая успешная admin-мутация сохраняет в той же транзакции автора, роль, действие,
тип/ID сущности, безопасные снимки `before`/`after` и `request_id`. Журнал доступен только
пользователям с ролью `ADMIN`; endpoints изменения и удаления журнала отсутствуют.

ASB import по умолчанию отключён. После заполнения
`APP_ASB_ALLOWED_COMPETITION_IDS`/`APP_ASB_ALLOWED_TEAM_IDS` и включения
`APP_ASB_ENABLED` endpoint принимает только числовые ID, но никогда не принимает URL.
Клиент обращается к фиксированному HTTPS-host, запрещает redirects, проверяет content type,
таймаут, размер и структуру ответа. Повторный импорт обновляет изменившиеся матчи и не
дублирует неизменившиеся матчи, залы или standings snapshot. История запусков и ошибок
доступна только `ADMIN`.

После login браузер получает только HttpOnly cookie. Значение `csrf_token` из JSON-ответа
нужно передавать в `X-CSRF-Token` для logout и admin-изменений. В БД хранятся только
SHA-256 отпечатки session/CSRF токенов. В production обязательны
`APP_COOKIE_SECURE=true` и cookie name с префиксом `__Host-`, например
`APP_SESSION_COOKIE_NAME=__Host-pb_session`; настройки валидируются при старте.
После перезагрузки SPA новый CSRF-токен можно безопасно получить через `GET /api/v1/auth/csrf`;
ответ не кешируется, а ранее выданный CSRF-токен становится недействительным.
Встроенный limiter ограничивает частоту и параллелизм дорогих Argon2-проверок в одном
процессе. На reverse proxy всё равно нужен распределённый rate limit для login/admin API.

## Структура

```text
src/predatory_beavers/
├─ api/                 # composition root, HTTP, ошибки, middleware, /api/v1
├─ db/                  # declarative base и async session factory
├─ modules/
│  ├─ auth/             # самостоятельные sessions/roles/CSRF
│  ├─ audit/            # append-only журнал административных действий
│  ├─ club/             # teams/players и описание media assets
│  ├─ matches/          # competitions/venues/matches, сервис на отдельный aggregate
│  ├─ media/            # безопасная загрузка и выдача изображений
│  ├─ achievements/     # достижения клуба
│  ├─ standings/        # текущие турнирные таблицы и история snapshot
│  ├─ imports/          # import jobs, orchestration и отдельный ASB applier
│  └─ home/             # агрегат первого экрана
├─ integrations/        # безопасный ASB client, Telegram port, object storage adapters
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

Текущая реализация покрывает авторизацию, составы, расписание, результаты, изображения,
достижения, турнирные таблицы, аудит, ASB import и данные первого экрана. Следующие крупные
срезы — при необходимости S3/MinIO, миграция legacy-данных и backup/restore runbook для
SQLite и media-каталога.
Telegram не входит в текущий объём работ.
