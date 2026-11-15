# GitHub Pages + Render Free + Supabase Free

Конфигурация подготовлена 15 ноября 2026 года. Облачные учётные данные вводит владелец проекта.
Подготовка не переключает и не очищает локальную SQLite.

| Часть | Размещение |
| --- | --- |
| Публичный сайт | GitHub Pages, `predatory-beavers-website-new-frontend` |
| Python API | Render Web Service, Docker, **Free** |
| Данные и аккаунты | Supabase PostgreSQL, отдельная схема `beavers` |
| Загруженные фото и портреты | Supabase Storage, **private** bucket `club-media` |
| Автообновление | GitHub Actions напрямую к Supabase, независимо от сна Render |

## Supabase и переменные

1. Создайте Free-проект в Европе. Сохраните Database password в менеджере паролей.
2. **Data API отключите**: сайт использует собственный Python API. Схему `beavers` не добавляйте
   в Exposed schemas: в ней хранятся в том числе аккаунты и служебные записи.
3. **Connect → Direct (Connection string) → Connection Method → Session pooler**: скопируйте точную строку своего проекта,
   порт **5432**. Подставьте пароль вместо `[YOUR-PASSWORD]`; специальные символы в пароле
   должны быть percent-encoded. Добавьте `?sslmode=require` (или `&sslmode=require`, если query уже есть).
   Принимаются префиксы `postgresql://` и `postgresql+psycopg://`. Host не следует угадывать.
4. **Settings → API Keys → Secret keys → default → Copy**: нужен серверный `sb_secret_…`.
   Publishable key здесь не требуется. Storage использует secret в заголовке `apikey`.
5. Bucket `club-media` можно создать с выключенным **Public bucket**; команда переноса также
   умеет создать его приватным. Существующий публичный bucket отвергается.

Session pooler работает по IPv4 с обычными серверными соединениями. Прямой бесплатный endpoint
часто требует IPv6; transaction pooler с портом 6543 здесь не нужен.
[Подключения](https://supabase.com/docs/guides/database/connecting-to-postgres),
[ключи](https://supabase.com/docs/guides/getting-started/api-keys),
[отключение Data API](https://supabase.com/docs/guides/api/securing-your-api).

Скопируйте `.env.render.example` в локальный **`.env.render`**. Замените три значения:

- `APP_DATABASE_URL` — строка Session pooler с паролем и TLS;
- `APP_SUPABASE_URL` — `https://<project-ref>.supabase.co`, без `/storage/v1`;
- `APP_SUPABASE_SECRET_KEY` — серверный `sb_secret_…`.

Остальные значения подходят для `https://dembler.github.io`. При изменении фронта обновите
`APP_CORS_ORIGINS` точным origin без пути репозитория и завершающего `/`.
Заполненный файл игнорируется Git и Docker. В чат и репозиторий его не отправляют.

## Перенос существующего сайта

Из каталога бэкенда в PowerShell:

```powershell
# Новый проверенный приватный bundle. Исходная SQLite не изменяется.
.venv/Scripts/python.exe scripts/export_deployment.py export --output data/deployment/cloud-transfer
# Проверка без подключения к облаку.
.venv/Scripts/python.exe scripts/import_cloud_bundle.py --bundle data/deployment/cloud-transfer
# Один раз в ПУСТУЮ цель: миграции, private media, строки БД, состояние источников.
.venv/Scripts/python.exe scripts/import_cloud_bundle.py --bundle data/deployment/cloud-transfer --env-file .env.render --apply
```

Сохраняются UUID, связи, JSON, даты, версии записей и хеши паролей. Сессии и ключи повторной
отправки старой установки не переносятся: администратор входит заново. Все строки вставляются
одной транзакцией; непустая цель отвергается. Фото проверяются чтением обратно из Storage;
отличающееся содержимое под существующим ключом не перезаписывается. При ошибке БД могут остаться
приватные файлы без ссылок; повтор с тем же bundle использует идентичные файлы.
После переключения локальная SQLite остаётся резервной копией, а не репликой Supabase.

## Render: New Web Service

- Source: `Dembler/predatory-beavers-website-new-backend`, ветка `main` с опубликованными изменениями.
- Language: **Docker**. Root Directory пустой, Dockerfile `./Dockerfile`.
- Region: **Frankfurt**. Instance type: **Free**, внизу **$0/month**.
- **Environment Variables → Add from .env**: вставьте заполненный `.env.render`.
- **Advanced → Docker Command**: `python scripts/start_render.py`.
- **Advanced → Health Check Path**: `/health/ready`.
- **Deploy web service** — после переноса данных и публикации подготовленного кода.

Команда запуска проверяет конфигурацию PostgreSQL/Storage, применяет миграции и запускает API
на `0.0.0.0:$PORT`. SQLite и локальный планировщик в этом режиме отвергаются. Для Blueprint
те же параметры описаны в `render.yaml`.
[Docker command](https://render.com/docs/blueprint-spec#dockercommand).

Приватность bucket проверяется при переносе и обращениях к Storage; startup не создаёт bucket.

Проверьте `<API-URL>/health/ready`, данные 2026/27 и фото. Измените тестовую запись через админку
и проверьте её после перезапуска: БД и медиа хранятся вне Render.

## GitHub Pages и авторизация

В репозитории фронта: **Settings → Secrets and variables → Actions → Variables**.
Добавьте `VITE_API_BASE_URL` с HTTPS-адресом Render **без** `/api/v1`.
В **Settings → Pages → Source** выберите **GitHub Actions** и запустите workflow
`Deploy frontend to GitHub Pages`. Подробности: `deploy/github-pages.md` в репозитории фронта.

Ключи Supabase/FIBA, строку БД и пароли не помещают в `VITE_*`: они входят в публичную сборку.
Для GitHub Pages + `onrender.com` заданы Secure `__Host-` cookie с `SameSite=None`.
Некоторые браузеры блокируют сторонние cookie независимо от CORS. Для надёжного входа
используйте общий домен `club.example` + `api.club.example` с `SameSite=lax` либо админку
на origin API. Публичный просмотр сайта от cookie не зависит.

## Автообновление и резервные копии

Workflow бэкенда запускает сборщик отдельно от Render. В **backend repository → Settings →
Secrets and variables → Actions → Secrets** добавьте подключения; точные имена указаны
в `.github/workflows/sports-sync.yml`. АСБ проверяется раз в час, FIBA — раз в сутки.
Без FIBA API-ключа отображается `auth_required`, прежние результаты сохраняются.

Сборщик использует блокировку PostgreSQL и временную спортивную SQLite, переносит только diff.
Конфликт с редакторской правкой отменяет запись; физического удаления строк нет. Manifests
и статусы хранятся в PostgreSQL, портреты — в private Storage. Render восстанавливает кэш
после холодного старта. Локальный компьютер для расписания не требуется.

GitHub может задерживать/пропускать scheduled jobs; в публичном репозитории расписание
отключается после 60 дней без активности. Это не обещание точного времени публикации.
[Ограничения расписаний](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Render Free засыпает после 15 минут без запросов; его диск временный. Supabase Free имеет
лимиты БД, Storage и трафика, может приостанавливаться при низкой активности и не включает
автоматические бэкапы. Сохраняйте отдельно `pg_dump` схемы и копию private Storage;
локальный проверенный bundle храните вне репозитория.
[Render Free](https://render.com/docs/free), [Supabase Free](https://supabase.com/pricing).

Альтернативная установка SQLite на постоянный Docker volume:
[deployment-docker.md](deployment-docker.md). Для Render Free этот вариант не подходит.
