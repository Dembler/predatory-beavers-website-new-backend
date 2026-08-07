# «Хищные Бобры»: план переноса на Python + Vue

Дата анализа: 7 августа 2026 года

Исходный репозиторий: [Dembler/Web_AppSite_Predatory_Beavers](https://github.com/Dembler/Web_AppSite_Predatory_Beavers)

Зафиксированный коммит анализа: [`d106836`](https://github.com/Dembler/Web_AppSite_Predatory_Beavers/commit/d106836c7b34cd3779edd78d4b5a33ddf4b54347)

## 1. Короткий вывод

Проект нужно не «переписать постранично», а разделить на четыре явных контура:

1. публичный сайт клуба;
2. административную панель контента;
3. серверную интеграцию с АСБ;
4. отдельный фоновый процесс Telegram-бота и уведомлений.

Целевой вариант — один монорепозиторий, один модульный backend на FastAPI, один Vue 3 SPA и два процесса из одного backend-образа: HTTP API и worker/bot. Микросервисы для текущего масштаба не нужны.

Переносить следует бизнес-смысл и контент, но не текущие технические решения. В частности, нельзя копировать публичные изменяющие endpoints, произвольный URL импорта, хранение секретов в репозитории, BLOB-картинки в основных таблицах, дублирование шапки в каждом шаблоне и статические изображения вместо турнирной таблицы.

## 2. Что фактически есть в исходном проекте

### 2.1. Размер и состав

| Часть | Файлы | Строки | Содержание |
|---|---:|---:|---|
| Java production | 33 | 1 575 | 7 контроллеров, 5 сущностей, 5 репозиториев, 6 сервисов, конфигурация |
| Java tests | 1 | 9 | только `contextLoads()` |
| Thymeleaf | 14 | 1 133 | публичные страницы, auth и формы администрирования |
| JavaScript | 7 | 608 | загрузка игроков/матчей, слайдеры, интеграции |
| CSS | 7 | 1 701 | стили отдельных страниц и общий файл |
| Изображения/медиа | 67 | 86,21 MiB | логотипы, фотографии, таблицы, GIF и MP4 |

Домашняя страница в проверенном запуске передавала около 14 MiB. Это должно рассматриваться как отдельная задача миграции медиа, а не как набор файлов для прямого копирования.

### 2.2. Пользовательские функции

| Область | Что есть сейчас | Что делать в новой системе |
|---|---|---|
| Главная | три прошедших/предстоящих матча, ссылки на мужскую и женскую таблицы | собрать серверный `home`-агрегат: следующий матч, последние результаты, ссылки на команды и таблицу |
| О клубе | история, турниры, достижения мужской и женской команд | перенести текст как версионируемый контент; достижения оставить динамическими |
| Игроки | фильтр по полу, карточки, добавление/редактирование/удаление | публичный состав + отдельный admin CRUD; связать игрока с клубной командой |
| Матчи | расписание, результаты, фильтры, добавление/редактирование, логотипы | единая страница матчей и REST API с фильтрами, статусом матча и нормальными полями счёта |
| Турнирные таблицы | отдельные мужская/женская страницы, внешние виджеты и местами изображения | нормализованный backend-адаптер АСБ, кеш и таблица из данных, а не картинка |
| Правила | длинная статическая статья | перенести в статический контент Vue или Markdown; CMS в MVP не нужен |
| Контакты | капитаны, Telegram, VK, карта залов | перенести; залы сделать данными с адресом и координатами; карта должна иметь текстовый fallback |
| Аккаунты | регистрация, вход, профиль; обычный пользователь почти ничего не может делать | admin login обязателен; публичную регистрацию сохранить только при подтверждённом пользовательском сценарии |
| Достижения | сортируемые изображения и подписи по мужской/женской команде | публичная выдача + защищённый admin CRUD и единое хранилище медиа |
| Telegram | подписка, отписка, рассылка, напоминания за 3 дня/1 день/3 часа, опрос «приду/не приду» | вынести в worker; ответы хранить отдельно по матчу и подписчику; обеспечить идемпотентность |

### 2.3. Интеграции

| Интеграция | Текущее использование | Решение при переносе |
|---|---|---|
| АСБ / Infobasket | браузерные widgets и server-side импорт JSON | только backend-адаптер; разрешённые host/competition/team IDs; timeout, retry, лимит ответа, кеш |
| Telegram Bot API | long polling при старте приложения, уведомления и опросы | отдельный worker; API не должен ждать Telegram при запуске |
| Yandex Maps | карта залов и ссылки маршрута | ключ из environment/secret store; без ключа показывать список адресов и внешние ссылки |
| VK | ссылки на группу и незавершённый VK ID widget | ссылки сохранить; VK ID не переносить, пока нет полноценного OAuth callback и продуктовой необходимости |

### 2.4. Сущности и данные источника

#### Пользователь

`id`, `username`, BCrypt `password`, `role`, `email`, `firstName`, `lastName`.

#### Игрок

`id`, `fullName`, `age`, `position`, `fact`, числовой `gender`, `imageType`, `imageData`.

#### Матч

`id`, обязательный уникальный `externalGameId`, числовой `gender`, `tournamentName`, названия команд, зал, дата, время, строковый счёт, URL/BLOB логотипов, три флага отправки уведомлений и число зрителей.

#### Достижение

`id`, BLOB изображения, `teamType`, `title`, `imageUrl`, `sortOrder`.

#### Подписчик Telegram

`chatId` одновременно является primary key; дополнительно хранятся один `matchId`, `username`, время подписки, ответ `coming` и один `messageId`. Такая модель теряет историю ответов и не позволяет корректно представить несколько матчей.

### 2.5. Legacy-маршруты и их целевое соответствие

| Legacy | Назначение | Новый frontend/API |
|---|---|---|
| `/` | главная | `/` + `GET /api/v1/home` |
| `/about` | история и достижения | `/club` + `GET /api/v1/achievements` |
| `/players?gender=0|1` | состав | `/teams/women`, `/teams/men` + `GET /api/v1/players` |
| `/schedule` | матчи | `/matches` + `GET /api/v1/matches` |
| `/tablemale`, `/tablefemale` | таблицы | `/standings?team=men|women` + `GET /api/v1/standings` |
| `/contacts` | контакты и залы | `/contacts` + `GET /api/v1/venues` |
| `/rules` | правила | `/rules` |
| `/register`, `/login`, `/profile` | аккаунт | `/register`, `/login`, `/profile`; registration может быть отключена feature flag |
| `/players/add`, `/schedule/add`, `/achievements/upload` | админские формы | `/admin/players`, `/admin/matches`, `/admin/achievements` |
| `/api/matches/load` | импорт с произвольного URL | `POST /api/v1/admin/imports/asb`, только фиксированные идентификаторы |

На уровне reverse proxy или Vue Router необходимо оставить постоянные 301/308 redirects со старых публичных URL, чтобы не ломать внешние ссылки.

### 2.6. Что в исходнике не является готовой функцией

- VK ID показывает виджет, но не завершает серверную аутентификацию.
- `/map` возвращает отсутствующий шаблон.
- JavaScript игрока вызывает несуществующий endpoint загрузки изображения.
- Публичная страница игроков зависит от API, который для гостя закрыт.
- В исходном коде одновременно объявлены два обработчика `GET /players`.
- Ручное создание матча конфликтует с обязательным `externalGameId`.
- Женская таблица содержит мужской заголовок.
- Части интерфейса используют устаревшие данные 2024–2025 годов.

Эти элементы нельзя включать в критерий «полный паритет» без исправления смысла.

## 3. Почему нельзя делать механический порт

До начала миграции нужно закрыть следующие риски:

1. В истории репозитория находятся реальные реквизиты БД и Telegram. Их следует считать скомпрометированными и немедленно перевыпустить.
2. Часть изменяющих endpoints достижений публична; CSRF отключён глобально.
3. `POST /api/matches/load` принимает произвольный URL и создаёт SSRF-риск.
4. Методные `@PreAuthorize` фактически не включены глобальной конфигурацией method security.
5. CORS разрешён без явного списка origin.
6. Схема БД меняется через `ddl-auto=update`, миграций нет.
7. Приложение регистрирует Telegram-бота синхронно при старте и зависит от внешней сети.
8. Изображения хранятся и в Git, и в БД BLOB; content type часто захардкожен.
9. В шаблонах повторяется layout, есть невалидная разметка и DOM-ошибки.
10. Мобильная навигация обрезается, а длинные русские строки не имеют правил вместимости.

Подробные подтверждения находятся в локальном [техническом аудите](../.lazyweb/dogfood/web-appsite-predatory-beavers-2026-08-07/report.md) и [визуальном аудите](../.lazyweb/design-improve/web-appsite-predatory-beavers-club-site-2026-08-07/report.html).

## 4. Целевая архитектура

### 4.1. Архитектурное решение

Использовать модульный монолит:

```text
Browser
  │
  ├── Vue 3 SPA (public + admin)
  │       │
  │       └── /api/v1/*
  │
Reverse proxy / TLS
  │
  ├── FastAPI HTTP process
  │       ├── PostgreSQL
  │       ├── S3-compatible media storage
  │       └── ASB adapter (outbound allowlist)
  │
  └── Worker process (same Python package/image)
          ├── Telegram long polling
          ├── notification scheduler
          └── PostgreSQL job/outbox tables
```

API и worker используют одну доменную модель и одну БД, но запускаются независимо. Отказ Telegram или АСБ не должен делать публичный сайт недоступным.

### 4.2. Предлагаемый стек

#### Backend

- Python 3.13 как консервативный baseline; Python 3.14 включить в CI и поднять baseline после проверки всех зависимостей.
- FastAPI и Pydantic v2.
- SQLAlchemy 2 async + `asyncpg`.
- Alembic для каждой схемной миграции.
- `httpx` для внешних HTTP-запросов.
- `pwdlib` с Argon2 для новых паролей и временной поддержкой BCrypt при миграции.
- `aiogram` либо другой один выбранный async Telegram SDK; выбор зафиксировать ADR до реализации.
- `uv` и `uv.lock` для воспроизводимых Python-зависимостей.
- pytest, pytest-asyncio, Ruff и mypy.

#### Frontend

- Vue 3 Single File Components.
- Composition API и `<script setup lang="ts">`.
- TypeScript в strict mode.
- Vue Router 4 с lazy-loaded route components и typed `meta` для auth/roles.
- Pinia только для клиентского cross-route состояния: `auth`, feature flags, UI preferences.
- Серверные данные — через query/composable слой, а не копированием всех ответов в Pinia.
- Vite, `vite.config.ts`, ESM.
- Vitest + Vue Test Utils; Playwright для E2E.
- `vue-tsc`, ESLint и форматирование в CI.
- Сгенерированные TypeScript-типы из OpenAPI backend.

#### Infrastructure

- PostgreSQL.
- S3-compatible object storage; локально MinIO, в production — выбранный S3-провайдер.
- один backend Docker image с командами `api` и `worker`;
- отдельный web image или статическая сборка за reverse proxy;
- Docker Compose для локальной разработки;
- GitHub Actions для CI;
- structured JSON logs, request ID, health/readiness endpoints.

Официальные документы подтверждают выбранные базовые паттерны: FastAPI рекомендует разносить крупное приложение по `APIRouter`, SQLAlchemy имеет штатный async ORM, Alembic ведёт версионируемую историю схемы, Vue рекомендует `<script setup>` для SFC + Composition API, Vue Router — динамические imports для маршрутов, а Vite предоставляет Vue TypeScript template и production build.

### 4.3. Структура нового репозитория

Предлагаемое имя: `predatory-beavers-platform`.

```text
predatory-beavers-platform/
├─ apps/
│  ├─ backend/
│  │  ├─ pyproject.toml
│  │  ├─ uv.lock
│  │  ├─ alembic.ini
│  │  ├─ migrations/
│  │  ├─ src/predatory_beavers/
│  │  │  ├─ api/
│  │  │  │  ├─ main.py
│  │  │  │  ├─ dependencies.py
│  │  │  │  ├─ errors.py
│  │  │  │  └─ v1/
│  │  │  ├─ modules/
│  │  │  │  ├─ auth/
│  │  │  │  ├─ teams/
│  │  │  │  ├─ players/
│  │  │  │  ├─ matches/
│  │  │  │  ├─ standings/
│  │  │  │  ├─ achievements/
│  │  │  │  ├─ venues/
│  │  │  │  ├─ media/
│  │  │  │  └─ notifications/
│  │  │  ├─ integrations/
│  │  │  │  ├─ asb/
│  │  │  │  ├─ telegram/
│  │  │  │  └─ object_storage/
│  │  │  ├─ worker/
│  │  │  ├─ db/
│  │  │  └─ settings.py
│  │  └─ tests/
│  │
│  └─ web/
│     ├─ package.json
│     ├─ pnpm-lock.yaml
│     ├─ vite.config.ts
│     ├─ src/
│     │  ├─ app/
│     │  ├─ router/
│     │  ├─ layouts/
│     │  ├─ pages/
│     │  │  ├─ public/
│     │  │  ├─ auth/
│     │  │  └─ admin/
│     │  ├─ features/
│     │  ├─ entities/
│     │  ├─ shared/
│     │  ├─ stores/
│     │  ├─ api/generated/
│     │  ├─ content/
│     │  └─ styles/
│     └─ tests/
│
├─ scripts/
│  ├─ export_legacy_data.py
│  ├─ import_legacy_data.py
│  ├─ verify_migration.py
│  └─ optimize_legacy_media.py
├─ infra/
│  ├─ compose.yaml
│  ├─ caddy/
│  └─ docker/
├─ docs/
│  ├─ architecture.md
│  ├─ content-inventory.md
│  ├─ migration-runbook.md
│  ├─ api-contract.md
│  └─ adr/
├─ .github/workflows/ci.yml
├─ .env.example
├─ Makefile или Taskfile.yml
└─ README.md
```

Внутри backend-модуля допустимы `models.py`, `schemas.py`, `repository.py`, `service.py`, `router.py`. Не нужно создавать десятки абстракций ради «чистой архитектуры», но HTTP, бизнес-правила и инфраструктурные клиенты не должны быть смешаны в одном файле.

## 5. Новая модель данных

### 5.1. Основные таблицы

| Таблица | Основные поля | Важные ограничения |
|---|---|---|
| `users` | UUID, username, email, password_hash, role, names, active, timestamps | case-insensitive unique username/email; role enum |
| `sessions` | opaque token hash, user_id, created/expires/last_seen, metadata | token не хранить открытым; удаление при logout/role change |
| `teams` | UUID, slug, name, category `MEN/WOMEN`, logo_asset_id, active | unique slug; в MVP две клубные команды |
| `players` | UUID, team_id, full_name, birth_date или age_text, position, fact, photo_asset_id, sort_order, active | не хранить возраст как вечное число, если известна дата рождения |
| `competitions` | UUID, name, season, external source/ID | unique `(source, external_id)` при наличии ID |
| `venues` | UUID, name, address, latitude, longitude | координаты валидируются; адрес доступен без карты |
| `matches` | UUID, competition_id, category, starts_at, venue_id, home/away names and logos, scores, status, source, external_id | partial unique `(source, external_id)`; manual match допускает `external_id=NULL` |
| `achievements` | UUID, team_id, title, media_asset_id, sort_order, optional achieved_at | индекс `(team_id, sort_order)` |
| `media_assets` | UUID, storage_key, mime, size, width, height, checksum, alt_text | unique storage key/checksum; файлы не лежат в row BLOB |
| `standings_snapshots` | competition/team, normalized JSON rows, fetched_at, source metadata | один актуальный snapshot + история по необходимости |
| `telegram_subscriptions` | chat_id, username, active, subscribed_at | одна подписка на chat, без привязки к одному матчу |
| `attendance_responses` | match_id, chat_id, response, message_id, responded_at | unique `(match_id, chat_id)` |
| `notification_deliveries` | match_id, chat_id, kind, scheduled_for, status, sent_at, error | unique `(match_id, chat_id, kind)` обеспечивает идемпотентность |
| `admin_audit_log` | actor, action, entity_type/id, before/after JSON, request_id, created_at | append-only на уровне приложения |

### 5.2. Нормализация legacy-полей

| Legacy | Новое поле/решение |
|---|---|
| `gender = 0/1` | enum `WOMEN/MEN` в API; `team_id` в игроках |
| `score = "72:65"` | `home_score INT NULL`, `away_score INT NULL`, `status` |
| отдельные `matchDate` + `startTime` | `starts_at TIMESTAMPTZ`, timezone `Europe/Moscow` вводится явно |
| обязательный `externalGameId` | nullable external ID + partial unique constraint |
| три `sent*` флага | строки `notification_deliveries` |
| `spectatorsCount` | вычислять из `attendance_responses`; кеш допустим, но не источник истины |
| BLOB фото/логотипа | объект в S3 + metadata row |
| `age` | предпочтительно `birth_date`; если данных нет — временное nullable legacy поле |
| `Subscriber.matchId` | отдельная связь ответа с каждым матчем |

### 5.3. Что оставить статическим

История клуба, правила баскетбола, контактные подписи и ссылки на соцсети в MVP должны лежать в `apps/web/src/content/` как типизированные данные/Markdown. Для редко меняющегося текста полноценная CMS добавит больше сложности, чем пользы. Если редактору понадобится изменение без deploy, это отдельная фаза после запуска.

## 6. Контракт API v1

### 6.1. Общие правила

- префикс `/api/v1`;
- JSON в `snake_case` либо `camelCase`, но один стиль для всего API; рекомендуется `snake_case` на backend и генерация клиента без ручного дублирования типов;
- даты/время — ISO 8601 с timezone;
- списки — единый объект `items`, `total`, `page`, `page_size`;
- ошибки — единый Problem Details-подобный формат с `status`, `code`, `detail`, `errors`, `request_id`;
- OpenAPI является контрактом; клиент Vue генерируется в CI;
- публичные GET кешируются, изменяющие endpoints не кешируются;
- optimistic concurrency для admin update через `updated_at`/version либо `If-Match`;
- upload отделён от JSON CRUD.

### 6.2. Публичные endpoints

```text
GET  /api/v1/home
GET  /api/v1/teams
GET  /api/v1/players?team=men&active=true&page=1&page_size=24
GET  /api/v1/players/{player_id}
GET  /api/v1/matches?team=men&status=upcoming&from=&to=&page=
GET  /api/v1/matches/{match_id}
GET  /api/v1/standings?team=men&season=2026-2027
GET  /api/v1/achievements?team=women
GET  /api/v1/venues
```

`GET /home` не должен отдавать произвольный dump таблиц. Он возвращает ровно данные стартового экрана: следующий матч, до трёх последних/ближайших матчей, краткую информацию о командах и timestamps источников.

### 6.3. Auth endpoints

```text
POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/auth/me
POST /api/v1/auth/register       # только если включена публичная регистрация
POST /api/v1/auth/change-password
```

Для first-party SPA под одним доменом рекомендуется opaque server-side session cookie `__Host-pb_session` с `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`. Все изменяющие cookie-authenticated запросы должны иметь CSRF-защиту. Состояние авторизации не хранить в `localStorage`.

### 6.4. Admin endpoints

```text
GET/POST/PATCH/DELETE /api/v1/admin/players[/{id}]
GET/POST/PATCH/DELETE /api/v1/admin/matches[/{id}]
GET/POST/PATCH/DELETE /api/v1/admin/achievements[/{id}]
POST                  /api/v1/admin/media
POST                  /api/v1/admin/imports/asb
GET                   /api/v1/admin/imports/{job_id}
POST                  /api/v1/admin/notifications/broadcast
GET                   /api/v1/admin/audit-log
```

Импорт АСБ принимает не URL, а управляемый запрос:

```json
{
  "team": "men",
  "competition_id": "48154",
  "external_team_id": "7433",
  "season": "2026-2027"
}
```

Backend сам строит URL только к разрешённому ASB host. Необходимы connect/read timeout, лимит размера ответа, запрет redirect на другой host, проверка content type, логирование job и upsert по `(source, external_id)`.

### 6.5. Health endpoints

```text
GET /health/live   # процесс жив, без внешних вызовов
GET /health/ready  # БД и обязательная конфигурация готовы
```

Telegram и АСБ не должны входить в обязательный readiness HTTP API.

## 7. Авторизация и матрица прав

### 7.1. Роли

- `VISITOR` — не хранится в БД; читает публичный сайт.
- `USER` — профиль и будущие пользовательские функции. Если таких функций нет, public registration отключена.
- `EDITOR` — управляет игроками, матчами, достижениями и медиа.
- `ADMIN` — всё перечисленное, пользователи, роли, импорт, рассылка и audit log.

### 7.2. Матрица

| Операция | Visitor | User | Editor | Admin |
|---|:---:|:---:|:---:|:---:|
| Читать публичные данные | ✓ | ✓ | ✓ | ✓ |
| Смотреть свой профиль | — | ✓ | ✓ | ✓ |
| Менять игроков/матчи/достижения | — | — | ✓ | ✓ |
| Запускать импорт АСБ | — | — | — | ✓ |
| Делать Telegram-рассылку | — | — | — | ✓ |
| Управлять ролями | — | — | — | ✓ |
| Смотреть audit log | — | — | — | ✓ |

Права проверяются backend dependency/policy на каждом изменяющем endpoint. Vue route guards служат только UX-слоем и не являются защитой.

## 8. Vue-приложение

### 8.1. Карта страниц

#### Public layout

- `/` — следующий матч, последние результаты, быстрые переходы к составам и таблицам;
- `/club` — история, турниры, достижения;
- `/teams/men` и `/teams/women` — состав;
- `/matches` — матчи и результаты с фильтрами команды/статуса/диапазона дат;
- `/standings` — сезон и команда;
- `/rules` — статическая статья;
- `/contacts` — капитаны, соцсети, Telegram-бот, список залов и карта.

#### Auth layout

- `/login`;
- `/register`, только если feature flag включён;
- `/profile`.

#### Admin layout

- `/admin` — обзор состояния данных и последних импортов;
- `/admin/players`;
- `/admin/matches`;
- `/admin/achievements`;
- `/admin/imports`;
- `/admin/notifications`;
- `/admin/audit-log`.

### 8.2. Компоненты, которые должны быть общими

```text
AppHeader
MobileNavigationDrawer
AppFooter
TeamSwitcher
MatchCard
MatchScore
StandingsTable
PlayerCard
AchievementGallery
VenueCard
AsyncState        # loading / empty / error / retry
ConfirmDialog
FileUploadField
AdminDataTable
AdminFormActions
```

Не копировать header/footer по страницам. Не манипулировать DOM через `innerHTML`; пользовательский текст выводится обычными Vue bindings.

### 8.3. State и data flow

- `useAuthStore` хранит только текущего пользователя, статус проверки сессии и actions login/logout.
- `useUiStore` — drawer, toast queue, сохранённая команда/сезон.
- Матчи, игроки, достижения и таблицы считаются server state и загружаются query/composable слоем с abort/cancellation.
- Фильтры матчей синхронизируются с URL query, чтобы страницу можно было переслать ссылкой.
- Route components загружаются динамически.
- В `router.meta` типизированно задаются `requiresAuth` и `roles`; async guard сначала завершает проверку сессии и не использует устаревшие refs.

### 8.4. UI-правила

- Дизайн спортивный, но контент остаётся в прямоугольной safe zone; диагонали и «когти» — фон/pseudo-elements.
- Проверять 390, 768, 1024 и 1440 px.
- Русский текст тестируется с запасом +50% длины.
- Один главный CTA на экран; на публичном сайте это ближайший релевантный переход, а не набор одинаковых кнопок.
- Навигация на mobile — доступный drawer с focus trap, `Esc`, aria labels и возвратом фокуса.
- Таблица на mobile: сначала ключевые колонки; вторичные метрики раскрываются или горизонтально скроллятся в явно обозначенном контейнере.
- У всех data pages есть loading, empty, stale и error состояния.
- Карта не является единственным способом получить адрес.
- Не использовать изображения турнирной таблицы как данные.

### 8.5. Бюджеты производительности

| Метрика | Цель MVP |
|---|---:|
| initial JS для public shell | ≤ 250 KiB gzip |
| общий CSS | ≤ 80 KiB gzip |
| стартовая страница без кеша | ≤ 2 MiB transfer |
| одна hero/feature image | ≤ 500 KiB |
| LCP на типичном mobile 4G | < 2,5 s |
| CLS | < 0,1 |

Медиа конвертировать в AVIF/WebP с JPEG/PNG fallback по необходимости, создавать варианты ширины, указывать `width`/`height`, использовать `srcset`, `sizes` и lazy loading ниже первого экрана. GIF 7,86 MiB и MP4 из текущего Git нельзя переносить в public bundle без отдельного решения.

## 9. Telegram worker

### 9.1. Сценарии, которые сохраняются

- `/start`;
- `/subscribe`;
- `/unsubscribe`;
- показ ближайшего матча;
- напоминания за 3 дня, 1 день и 3 часа;
- кнопки «приду / не приду» с общими счётчиками;
- admin broadcast.

### 9.2. Как реализовать безопасно

1. Worker запускается отдельно от HTTP API.
2. Каждую минуту он выбирает due deliveries с блокировкой и лимитом.
3. Unique constraint не позволяет повторно создать одинаковое напоминание.
4. Отправка обновляет `status`, `sent_at`, `attempts`, `last_error`.
5. Временная ошибка получает bounded retry/backoff; постоянная ошибка не блокирует другие сообщения.
6. Ответ на опрос делает upsert в `attendance_responses`.
7. Счётчики считаются по таблице ответов, а не по одному mutable row подписчика.
8. `admin_chat_id` и bot token читаются только из secrets.
9. Для broadcast необходимы admin permission, подтверждение и audit log.

## 10. Перенос данных и контента

### 10.1. До начала

1. Отозвать и перевыпустить Telegram token, пароль БД, Yandex/VK ключи, если они действуют.
2. Зафиксировать источник: commit, schema dump и read-only snapshot БД.
3. Составить row counts и список медиа до преобразования.
4. Уточнить, какие аккаунты, подписчики и история посещаемости действительно должны переноситься с точки зрения персональных данных.

### 10.2. Pipeline

```text
Legacy PostgreSQL + Git media
        │
        ├── export_legacy_data.py → immutable JSONL manifest
        ├── export blobs         → files + SHA-256
        │
        ├── normalize/validate
        │      ├── gender → team/category enum
        │      ├── score → integer pair
        │      ├── Moscow date/time → timestamptz
        │      └── media → optimized variants
        │
        ├── import_legacy_data.py → new PostgreSQL + object storage
        │
        └── verify_migration.py
               ├── row counts
               ├── field invariants
               ├── checksums
               └── sampled UI/API comparisons
```

Экспорт должен быть повторяемым и не менять legacy БД. Импорт — идемпотентным: повторный запуск либо даёт тот же результат, либо завершается понятным conflict report.

### 10.3. Маппинг

| Источник | Назначение | Примечание |
|---|---|---|
| `UserEntity` | `users` | BCrypt сначала проверить на реальном hash sample; после успешного login делать rehash Argon2 |
| `PlayerEntity` | `players`, `media_assets` | `imageData` вынести в object storage; пустые/повреждённые изображения отметить |
| `MatchEntity` | `matches`, `competitions`, `venues`, `media_assets` | распарсить счёт; manual rows не требуют external ID |
| `AchievementEntity` | `achievements`, `media_assets` | сохранить sort order и team mapping |
| `SubscriberEntity` | `telegram_subscriptions`, возможно `attendance_responses` | старый row отражает только последний матч; нельзя выдумывать полную историю |
| Git images | `media_assets` или versioned content assets | убрать дубли, пробелы/кириллицу в storage key можно заменить безопасными slug |
| Thymeleaf texts | `src/content` | сохранить текст, исправить явные опечатки и дубли |

### 10.4. Cutover

1. Развернуть новую систему на staging и выполнить полный import.
2. Провести acceptance по публичным страницам, admin, АСБ и Telegram sandbox bot.
3. Перевести legacy admin в read-only на короткое окно.
4. Выполнить финальный delta import.
5. Сверить counts/checksums и выполнить smoke suite.
6. Переключить домен.
7. Legacy держать read-only ограниченный срок для rollback.
8. После подтверждения удалить доступ приложения к старой БД; архивировать по принятой retention policy.

## 11. Безопасность

Обязательные требования до production:

- секретов нет в Git, Docker image, frontend env и логах;
- exact CORS origins; в production при same-origin CORS вообще не нужен;
- session cookie `Secure`, `HttpOnly`, `SameSite=Lax/Strict`, без `Domain`, с `__Host-` prefix;
- session ID непрозрачный, серверное состояние, rotation после login/смены роли, invalidation при logout;
- CSRF token для POST/PATCH/PUT/DELETE;
- rate limit login/register/import/broadcast;
- одинаковая ошибка login без раскрытия существования пользователя;
- Argon2 для новых паролей; BCrypt migration только как переходный слой;
- проверка прав на backend для каждой операции;
- импорт АСБ без пользовательского URL и с egress allowlist;
- uploads: allowlist MIME + magic bytes, лимит размера/разрешения, случайный storage key, повторное кодирование изображения;
- публичная выдача никогда не возвращает password hash, BLOB или внутренние поля delivery;
- prepared ORM queries, ограничения и индексы в БД;
- security headers: HSTS, CSP, `X-Content-Type-Options`, frame policy, referrer policy;
- audit trail для admin mutations/import/broadcast;
- dependency/container/secret scanning в CI.

## 12. Тестовая стратегия

### 12.1. Backend

- unit tests: match status, home aggregate, notification windows, attendance counts, role policy, ASB mapping;
- repository integration tests на реальном PostgreSQL;
- API tests: validation, pagination, errors, auth, permissions, uploads;
- Alembic test: пустая БД → `upgrade head`, затем проверка актуального head;
- migration tests на обезличенном fixture legacy export;
- contract snapshot/OpenAPI lint.

### 12.2. Frontend

- component tests: filters, cards, AsyncState, admin forms, mobile drawer;
- router tests: anonymous/user/editor/admin paths и отсутствие redirect loops;
- generated API types проверяются `vue-tsc`;
- визуальные stress cases с длинным русским текстом и отсутствующими изображениями.

### 12.3. E2E acceptance

1. Гость открывает все public routes без console errors.
2. Переключает мужскую/женскую команду и получает правильные данные.
3. Фильтрует upcoming/finished matches, URL отражает фильтр.
4. Editor создаёт, меняет и удаляет игрока/матч/достижение.
5. User и anonymous получают 403/401 на те же операции.
6. Admin запускает ASB import; неизвестный team/competition rejected.
7. Файл с неверным MIME/размером rejected.
8. Worker не отправляет одно уведомление дважды.
9. Telegram callback изменяет один ответ конкретного пользователя конкретному матчу.
10. Mobile 390 px: nav работает клавиатурой/тачем, ничего не перекрыто и не обрезано.

## 13. CI/CD и окружения

### 13.1. CI pull request

```text
backend:  ruff check + ruff format --check + mypy + pytest
database: alembic upgrade from empty + current --check-heads
frontend: eslint + vue-tsc + vitest + vite build
contract: generate client + fail on uncommitted diff
e2e:      Playwright critical paths
security: secret scan + dependency audit + image scan
```

### 13.2. Окружения

- `local`: Compose, seeded demo data, MinIO, mock/sandbox integrations;
- `staging`: production-like, отдельные DB/bucket/bot/key;
- `production`: backups, TLS, least-privilege credentials, monitoring and alerting.

Нельзя использовать production Telegram token или production DB в developer `.env`.

### 13.3. Observability

- JSON logs с `request_id`, route, status, duration и actor ID без PII/secret values;
- метрики HTTP latency/error rate, DB pool, import jobs, notification queue and failures;
- error tracking для backend и Vue;
- alert, если upcoming match import давно не обновлялся или notification failures превышают порог;
- ежедневный backup PostgreSQL и bucket policy/versioning по возможностям провайдера;
- документированная restore drill.

## 14. План работ

### Этап 0. Безопасность и решения — 1–2 дня

- ротация секретов;
- решение о public registration;
- подтверждение источника АСБ и договорённости об API;
- решение по хранению персональных Telegram-данных;
- ADR: auth sessions, object storage, Telegram SDK.

Готово, когда секреты отозваны, спорные продуктовые решения записаны, а новый repo не содержит credentials.

### Этап 1. Каркас репозитория — 2–3 дня

- monorepo, FastAPI, Vue/Vite, Compose;
- PostgreSQL, Alembic initial migration;
- API/web health, lint, tests, CI;
- OpenAPI client generation.

Готово, когда чистый clone поднимается одной документированной командой и CI зелёный.

### Этап 2. Домен и public API — 5–8 дней

- teams, players, matches, achievements, venues, media;
- публичные endpoints и home aggregate;
- seed data и API tests.

### Этап 3. Public Vue — 6–10 дней

- общий layout и responsive nav;
- главная, клуб, составы, матчи, таблицы, правила, контакты;
- loading/empty/error/stale states;
- responsive images и performance budget.

### Этап 4. Auth и admin — 5–8 дней

- sessions, CSRF, roles;
- admin layouts/forms/tables;
- media upload и audit log.

### Этап 5. АСБ и Telegram — 5–8 дней

- allowlisted ASB adapter, import jobs, standings cache;
- worker, subscriptions, reminders, attendance poll, broadcast;
- failure/retry/idempotency tests.

### Этап 6. Миграция и production hardening — 4–7 дней

- export/import/verify scripts;
- media optimization;
- staging dress rehearsal;
- E2E/accessibility/performance/security review;
- runbook, backup/restore, cutover.

Оценка для одного опытного full-stack разработчика: примерно 28–46 рабочих дней, то есть 6–9 недель. Для двух разработчиков с разделением backend/frontend — ориентировочно 4–6 недель, потому что миграция данных, интеграции и финальный QA всё равно имеют последовательные участки. Это оценка после чтения кода, но до доступа к реальной БД и подтверждения внешнего API.

## 15. Приоритеты backlog

### P0 — обязательно для запуска

- секреты и auth security;
- public pages и responsive navigation;
- players/matches/achievements CRUD;
- данные таблиц вместо изображений;
- safe ASB import;
- Telegram subscriptions/reminders/poll;
- migrations, backups, logs, CI, smoke/E2E;
- legacy data/content migration.

### P1 — после стабильного запуска

- публичные аккаунты и профиль, если подтверждён сценарий;
- CMS для history/rules/contacts;
- VK ID;
- richer player profiles/statistics;
- календарная подписка iCal;
- push/email notifications;
- analytics dashboard.

### Не делать в MVP

- микросервисы;
- GraphQL;
- realtime WebSockets без реального live-score источника;
- собственную дизайн-систему-пакет;
- универсальный CMS;
- загрузку данных с произвольных URL;
- хранение исходных фото/видео внутри Git frontend bundle.

## 16. Definition of Done всего переноса

Перенос закончен только если:

- все согласованные public pages и admin flows работают;
- ни один anonymous/user запрос не меняет admin-данные;
- на `/api/v1` нет произвольного external fetch;
- Alembic создаёт схему с нуля и обновляет staging;
- данные и media прошли автоматическую сверку;
- Telegram-уведомления идемпотентны и не блокируют API;
- нет секретов в Git/history нового репозитория;
- UI проходит 390/768/1024/1440, keyboard и базовый WCAG AA audit;
- public homepage укладывается в согласованный performance budget;
- CI, backup/restore и rollback runbook проверены;
- старые публичные URL корректно перенаправляются;
- README позволяет новому разработчику поднять проект без устных инструкций.

## 17. Рекомендуемый первый milestone

Не начинать с визуального копирования главной. Первый milestone должен быть вертикальным срезом:

1. monorepo + Compose + CI;
2. миграции `teams`, `players`, `media_assets`;
3. `GET /api/v1/players` и защищённый admin CRUD;
4. Vue страницы `/teams/men`, `/teams/women`, `/admin/players`;
5. upload в object storage;
6. role tests и mobile QA.

Этот срез одновременно проверит архитектуру API, БД, auth, uploads, OpenAPI client, Vue Router, Pinia auth state, admin UX и CI. После него матчи и достижения добавляются тем же шаблоном без архитектурной перестройки.

## 18. Источники архитектурных решений

- [FastAPI: Bigger Applications — Multiple Files](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [FastAPI: Security](https://fastapi.tiangolo.com/tutorial/security/)
- [SQLAlchemy: Asynchronous I/O](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html)
- [Alembic documentation](https://alembic.sqlalchemy.org/en/latest/)
- [Vue: TypeScript with Composition API](https://vuejs.org/guide/typescript/composition-api)
- [Vue: `<script setup>`](https://vuejs.org/api/sfc-script-setup.html)
- [Vue Router: Lazy Loading Routes](https://router.vuejs.org/guide/advanced/lazy-loading)
- [Pinia documentation](https://pinia.vuejs.org/)
- [Vite: Getting Started](https://vite.dev/guide/)
- [OWASP: Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [Официальный сайт АСБ](https://asbasket.ru/)
- [VTB United League](https://vtb-league.com/en/)
