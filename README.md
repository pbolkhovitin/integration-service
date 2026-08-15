# Интеграционный сервис Bitrix24 / GLPI / MANGO Office

Сервис для синхронизации данных между **Bitrix24**, **GLPI** и **MANGO Office**.

**Текущий статус:** MVP — опрос Bitrix24 REST API → создание тикетов в GLPI + обратная синхронизация статусов и комментариев (GLPI → Bitrix24, test mode). MVP укреплён: Bitrix24-вызовы вынесены из event loop, мутирующие эндпоинты закрыты `X-Admin-Token`, retry зависших/failed-задач, уникальность `(source, source_id)`, lifecycle GLPI-сессий, reverse sync по расписанию с whitelist-защитой. В работе (Phase 1.5): расширенный маппинг Bitrix24↔GLPI + L1-шаблон, орг-структура/пользователи по ID, активы NetBox→GLPI, Zabbix, SLA-отчёт в Metabase. Нейминг NetBox — см. `docs/netbox-naming-conventions.md`, план — `docs/superpowers/plans/2026-08-15-phase1-5-mapping-and-sla.md`.

## Поток данных (MVP — polling mode)

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────┐
│  Bitrix24 REST   │◀────│  Integration     │────▶│  GLPI    │
│  API (polling)   │     │  Service (API)   │     │ (тикеты) │
│  tasks.task.*    │     │  :8000           │     │ :8080    │
└──────────────────┘     └────────┬─────────┘     └──────────┘
                                  │
                                  ▼
                        ┌──────────────────┐
                        │  PostgreSQL 15   │
                        │  ┌────────────┐  │
                        │  │   tasks    │  │
                        │  │ attempts   │  │
                        │  │  outbox    │  │  (unused in MVP)
                        │  └────────────┘  │
                        └──────────────────┘
```

## Архитектура

### MVP (текущий режим) — Polling + Sync

| Сервис    | Назначение                                    | Порты          |
|-----------|-----------------------------------------------|----------------|
| `postgres`| PostgreSQL 15 (хранилище задач)               | `5432`         |
| `api`     | FastAPI + APScheduler (poller внутри процесса) | `8000`         |
| `redis`   | Redis 7 (в `docker-compose.yml`; пока не используется приложением) | `6379` |

`docker-compose.yml` (прод) — postgres, redis, api. `docker-compose.mvp.yml`
(локальный быстрый старт) — postgres + api, без Redis. Celery/Flower не
используются. Poller опрашивает Bitrix24 каждые N секунд и создаёт тикеты в
GLPI синхронно; Bitrix24-вызовы выполняются в `asyncio.to_thread` (не блокируют
event loop).

Обратная синхронизация (GLPI → Bitrix24) работает в test mode для whitelist-задач:
отслеживает изменение статуса и новые followup-комментарии в GLPI, обновляет
Bitrix24 задачи через REST API.

### Production (будущее) — Webhook + Celery

| Сервис    | Назначение                                    | Порты     |
|-----------|-----------------------------------------------|-----------|
| `postgres`| Основное хранилище данных (PostgreSQL 15)     | `5432`    |
| `redis`   | Брокер задач Celery + RedBeat scheduler       | `6379`    |
| `api`     | FastAPI приложение (вебхуки + management)      | `8000`    |
| `worker`  | Celery worker (асинхронная обработка задач)    | —         |
| `beat`    | Celery Beat (планировщик периодических задач)  | —         |
| `flower`  | Веб-мониторинг Celery                          | `5555`    |

Шесть контейнеров. Входящие вебхуки от Bitrix24 и MANGO Office, асинхронная
обработка через Celery, retry с exponential backoff, transactional outbox.

## Быстрый старт — MVP (Docker Compose)

### Предварительные требования

- Docker 24+
- Docker Compose v2
- Доступ к Bitrix24 (self-hosted или облачный)
- Доступ к GLPI (API включен)

### Запуск

```bash
# 1. Клонировать репозиторий
git clone git@github.com:pbolkhovitin/integration-service.git
cd integration-service

# 2. Настроить окружение
cp .env.example .env
# Отредактировать .env — см. раздел "Переменные окружения" ниже

# 3. Собрать и запустить
docker compose -f docker-compose.mvp.yml build
docker compose -f docker-compose.mvp.yml up -d

# 4. Применить миграции БД
docker exec -e DATABASE_URL='postgresql+asyncpg://integration:${POSTGRES_PASSWORD}@postgres:5432/integration' \
  integration-api alembic upgrade head

# 5. Проверить состояние
docker compose -f docker-compose.mvp.yml ps
# Оба контейнера (postgres, api) должны быть Up

curl http://localhost:8000/health
# → {"status":"healthy"}

curl http://localhost:8000/ready
# → {"status":"ready"}

curl http://localhost:8000/api/bitrix24/sync/status
# → {"status":"running","interval_seconds":60,"responsible_ids":[70],...}
```

### Ручной запуск синхронизации

```bash
# Немедленный poll (без ожидания расписания)
curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" http://localhost:8000/api/bitrix24/sync/trigger
# → {"status":"completed"}
```

### Логи

```bash
# Логи API (включая логи poller)
docker compose -f docker-compose.mvp.yml logs -f api

# Логи PostgreSQL
docker compose -f docker-compose.mvp.yml logs -f postgres
```

## Быстрый старт — Production (Docker Compose)

```bash
# 1. Клонировать и настроить
git clone git@github.com:pbolkhovitin/integration-service.git
cd integration-service
cp .env.example .env
# Отредактировать .env — указать все обязательные переменные

# 2. Создать файл маппинга сотрудников
cp config/employee_mapping.json.example config/employee_mapping.json

# 3. Запустить все сервисы
docker compose up -d

# 4. Применить миграции БД
docker compose exec api alembic upgrade head

# 5. Проверить
docker compose ps
# Все 6 контейнеров должны быть Up
curl http://localhost:8000/health
# → {"status": "healthy"}

# Мониторинг — Flower
# http://localhost:5555
```

## Локальная разработка (без Docker)

```bash
# 1. Создать виртуальное окружение
python3.11 -m venv venv
source venv/bin/activate

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Запустить PostgreSQL
# (через Docker или локально)

# 4. Настроить .env
cp .env.example .env
# Отредактировать .env:
#   DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/integration
#   GLPI_URL=http://localhost:8080
#   GLPI_APP_TOKEN=<token>
#   GLPI_USER_TOKEN=<token>
#   BITRIX24_WEBHOOK_URL=https://b24.example.com/rest/USER/TOKEN
#   BITRIX24_RESPONSIBLE_IDS=70

# 5. Применить миграции
alembic upgrade head

# 6. Запустить API (включая poller)
uvicorn app.main:app --reload --port 8000
```

## Тестирование

```bash
# Полный прогон (test_bitrix.py — нереализованный Phase 2 — исключён)
DATABASE_URL='postgresql+asyncpg://test:test@localhost:5432/test' \
  python -m pytest -q --ignore=tests/test_bitrix.py

# Проверка кода
ruff check app tests
mypy app
```

**Текущее состояние:** **239 проходящих теста**. 13 failed + 5 errors —
предсуществующие, не входили в план харднинга:

| Файл | Кол-во | Причина |
|------|--------|---------|
| `tests/test_glpi.py` | 6 | Устаревшие ожидания (метод `init_session` — GET, а не POST) |
| `tests/test_settings.py` | 3 | Сравнение `SecretStr` с пустой строкой |
| `tests/test_webhook_bitrix.py` | 4 failed + 5 errors | Эндпоинты Phase 2 (`/webhook/bitrix/lead`) ещё не реализованы |
| `tests/test_bitrix.py` | — | Импортирует несуществующий `BitrixLead` (нереализованный Phase 2) |

## Переменные окружения

### Обязательные (MVP)

| Переменная                  | Описание                              | Пример                                          |
|-----------------------------|---------------------------------------|-------------------------------------------------|
| `POSTGRES_PASSWORD`         | Пароль PostgreSQL                     | `integration123`                                 |
| `GLPI_URL`                  | Базовый URL GLPI API                  | `http://glpi-app:80` (Docker) / `http://host:8080` (local) |
| `GLPI_APP_TOKEN`            | GLPI App-Token (заголовок)            | `intg-svc-token-xxxx`                            |
| `GLPI_USER_TOKEN`           | GLPI User Token (query param)         | `glpi-user-token-xxxx`                           |
| `BITRIX24_WEBHOOK_URL`      | Bitrix24 webhook URL (без метода)     | `https://b24.example.com/rest/445/y1uz...`      |
| `BITRIX24_RESPONSIBLE_IDS`  | ID ответственных через запятую        | `70` или `70,71,72`                              |

> **Аутентификация мутирующих эндпоинтов:** `POST /api/bitrix24/sync/trigger`,
> `/sync/cleanup`, `/sync/retry`, `/sync/reverse-test` требуют заголовок
> `X-Admin-Token`, равный значению `ADMIN_API_TOKEN`. Если токен не задан —
> эндпоинты отвечают `401`. CORS-мидлвара регистрируется только при непустом
> `CORS_ORIGINS`.

### Опциональные (MVP)

| Переменная                       | По умолчанию | Описание                            |
|----------------------------------|-------------|-------------------------------------|
| `POSTGRES_SERVER`                | `postgres`  | Хост PostgreSQL                     |
| `POSTGRES_PORT`                  | `5432`      | Порт PostgreSQL                     |
| `POSTGRES_DB`                    | `integration` | Имя БД                           |
| `POSTGRES_USER`                  | `integration` | Пользователь БД                  |
| `BITRIX24_POLL_INTERVAL_SECONDS` | `60`        | Интервал опроса Bitrix24 (секунды)  |
| `BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS` | `60` | Интервал обратной синхронизации GLPI→Bitrix24 (секунды) |
| `BITRIX24_REVERSE_SYNC_ENABLED` | `true` | Автозапись в Bitrix24 по расписанию. **Жёстко ограничена whitelist-задачами** (`TEST_TASK_IDS`) — в прод-задачи запись невозможна |
| `BITRIX24_ORG_WEBHOOK_URL` | — | Вебхук Bitrix24 **с правами `user` + `department`** для org sync (обычный `BITRIX24_WEBHOOK_URL` их не имеет). Пусто = org sync выключен |
| `ORG_SYNC_ENABLED` | `false` | Автозапуск org sync по расписанию (ручной `POST /api/bitrix24/sync/org` работает всегда) |
| `ORG_SYNC_INTERVAL_SECONDS` | `3600` | Интервал org sync (секунды) |
| `ORG_SYNC_ROOT_ENTITY_ID` | `25` | Корневая GLPI-entity, под которую зеркалится дерево отделов Bitrix24 («АО «АПО «Аврора») |
| `ORG_SYNC_USER_PROFILE_ID` | `1` | GLPI-профиль для синхронизируемых пользователей (1 = Self-Service) |
| `CORS_ORIGINS`                | —           | Разрешённые CORS-origin через запятую (пусто = CORS отключен) |
| `ADMIN_API_TOKEN`             | —           | Секрет для мутирующих эндпоинтов `/api/bitrix24/sync/*` (заголовок `X-Admin-Token`) |
| `GLPI_DEFAULT_CATEGORY_ID`       | `1`         | Категория по умолчанию (Инцидент)   |
| `GLPI_DEFAULT_GROUP_ID`          | `1`         | Группа по умолчанию (IT-поддержка L1) |
| `GLPI_DEFAULT_ENTITY_ID`         | `2`         | Орг. единица (Департамент IT)       |
| `APP_HOST`                       | `0.0.0.0`   | Хост Uvicorn                        |
| `APP_PORT`                       | `8000`      | Порт Uvicorn                        |
| `DATABASE_URL`                   | —           | URL подключения (переопределяет POSTGRES_*) |
| `TEST_MODE`                      | `True`      | Включить reverse sync (GLPI→Bitrix24) |
| `TEST_TASK_IDS`                  | `35591,35633` | ID задач для reverse sync (через запятую) |

### Production-only (Phase 2+)

| Переменная               | Описание                              |
|--------------------------|---------------------------------------|
| `REDIS_URL`              | Redis URL (брокер Celery)             |
| `CELERY_BROKER_URL`      | Celery broker URL                     |
| `CELERY_RESULT_BACKEND`  | Celery result backend                 |
| `BITRIX24_USER_ID`       | ID пользователя Bitrix24              |
| `EMPLOYEE_MAPPING_PATH`  | Путь к маппингу сотрудников           |
| `WHISPER_MODEL_SIZE`     | Размер модели транскрибации           |
| `RETRY_BACKOFF_BASE`     | База экспоненты retry                 |
| `RETRY_BACKOFF_MAX`      | Макс. задержка retry                  |

### GLPI OAuth2 (Phase 4, планируется)

| Переменная            | Описание                    |
|-----------------------|-----------------------------|
| `GLPI_CLIENT_ID`      | OAuth2 Client ID            |
| `GLPI_CLIENT_SECRET`  | OAuth2 Client Secret        |
| `GLPI_USERNAME`       | Имя пользователя GLPI      |
| `GLPI_PASSWORD`       | Пароль пользователя GLPI    |

## API Endpoints

### MVP (текущие)

```
GET  /health
  → 200 {"status": "healthy"}
  Liveness probe — проверяет что процесс жив.

GET  /ready
  → 200 {"status": "ready"}
  → 200 {"status": "unhealthy"}
  Readiness probe — проверяет подключение к PostgreSQL.

GET  /api/bitrix24/sync/status
  → 200 {"status": "running", "interval_seconds": 60,
         "responsible_ids": [70], "next_run": "2025-...",
         "reverse_sync": {"enabled": true, "auto_enabled": true,
                          "interval_seconds": 60, "next_run": "..."}}
  Статус poller'а: интервал, ID ответственных, следующий запуск,
  состояние reverse sync (enabled — джоб зарегистрирован,
  auto_enabled — разрешена автозапись в Bitrix24).

POST /api/bitrix24/sync/trigger   (требует X-Admin-Token)
  → 200 {"status": "completed"}
  Ручной запуск poll-цикла (немедленно, без ожидания расписания).

POST /api/bitrix24/sync/cleanup    (требует X-Admin-Token)
  → 200 {"bitrix24_tasks_fetched": 134, "db_tasks_total": 5, ...
  Детектит задачи, удалённые в Bitrix24, и закрывает (status=5 solved)
  связанные тикеты в GLPI.

POST /api/bitrix24/sync/retry      (требует X-Admin-Token)
  → 200 {"requeued": 1, "failed_total": 2}
  Переводит failed-задачи (attempts < max_attempts) обратно в pending
  для повторной обработки следующим poll-циклом.

GET  /api/bitrix24/sync/reverse-status
  → 200 {"test_mode": true, "test_task_ids": [35591, 35633], "active": true,
         "auto_write_enabled": true}
  Статус reverse sync: включён ли test mode, whitelist-задачи,
  разрешена ли автозапись в Bitrix24.

POST /api/bitrix24/sync/reverse-test (требует X-Admin-Token)
  → 200 {"checked": 2, "status_updated": 1, "comments_sent": 3, ...}
  Ручной запуск обратной синхронизации (GLPI статусы → Bitrix24).
  Работает только для whitelist-задач при TEST_MODE=True.

GET  /api/bitrix24/sync/org-status
  → 200 {"org_sync_enabled": false, "org_webhook_configured": true,
         "root_entity_id": 25, "user_profile_id": 1, "interval_seconds": 3600}
  Статус org sync: настройки переноса пользователей/отделов.

POST /api/bitrix24/sync/org (требует X-Admin-Token)
  → 200 {"departments_total": 50, "departments_created": 48,
         "users_total": 379, "users_active": 340,
         "users_created": 300, "users_updated": 40, "errors": []}
  Перенос оргструктуры Bitrix24 → GLPI: зеркалит дерево отделов в
  GLPI-entity (под ORG_SYNC_ROOT_ENTITY_ID) и создаёт/обновляет
  пользователей (матчинг по email, профиль ORG_SYNC_USER_PROFILE_ID).
  Требует BITRIX24_ORG_WEBHOOK_URL (права user+department).
```

### Production (Phase 2+)

```
POST /webhook/bitrix/lead
  Входящий вебхук от Bitrix24 при новом лиде.
  Idempotency: поле idempotency_key в JSON теле.
  → 200 {"status": "success", "task_id": "uuid", "glpi_ticket": {...}}
  → 200 {"status": "duplicate", "task_id": "uuid"}

POST /webhook/mango/call
  Входящий вебхук от MANGO Office при звонке.
  → 200 {"status": "success", "task_id": "uuid"}

GET  /tasks/{id}
  Статус задачи по UUID.

GET  /tasks?status=pending&source=bitrix&limit=50&offset=0
  Список задач с фильтрацией и пагинацией.
```

## Структура базы данных

### Таблица `tasks`

Центральная таблица — каждая задача представляет единицу интеграционной работы.

| Колонка            | Тип                     | Описание                                     |
|--------------------|-------------------------|----------------------------------------------|
| `id`               | `UUID` (PK)             | Первичный ключ, `gen_random_uuid()`          |
| `source`           | `VARCHAR(50)`           | Система-источник (`bitrix24`)                |
| `source_id`        | `VARCHAR(255)`          | ID объекта в исходной системе (ID задачи Bitrix24) |
| `type`             | `VARCHAR(50)`           | Тип задачи (`create_ticket`)                 |
| `payload`          | `JSONB`                 | Полные данные задачи из Bitrix24             |
| `status`           | `ENUM`                  | Статус: `pending`, `processing`, `completed`, `failed`, `cancelled` |
| `attempts`         | `INTEGER`               | Количество попыток выполнения (по умолч. 0)  |
| `max_attempts`     | `INTEGER`               | Максимум попыток (по умолч. 3)               |
| `last_error`       | `TEXT`                  | Сообщение об ошибке последней попытки         |
| `result`           | `JSONB`                 | Результат (GLPI ticket ID + данные)          |
| `idempotency_key`  | `VARCHAR(255)`          | Ключ идемпотентности (`b24:{task_id}`)       |
| `worker_id`        | `VARCHAR(100)`          | ID worker'а (unused в MVP)                   |
| `lease_expires_at` | `TIMESTAMPTZ`           | Время истечения лизена (unused в MVP)        |
| `last_glpi_status` | `VARCHAR(50)`           | Последний известный статус GLPI (reverse sync) |
| `last_glpi_followup_id` | `INTEGER`          | ID последнего обработанного followup в GLPI (reverse sync) |
| `created_at`       | `TIMESTAMPTZ`           | Дата создания                                |
| `updated_at`       | `TIMESTAMPTZ`           | Дата обновления                              |

**Индексы:** `ix_tasks_status`, `ix_tasks_source`, `ix_tasks_idempotency_key`
(partial unique), `ix_tasks_lease`, **`ix_tasks_source_source_id` (UNIQUE на
`(source, source_id)`)** — гарантирует, что задача из Bitrix24 создаёт только
один тикет GLPI. Индекс добавлен миграцией `a1b2c3d4e5f6`
(дедуп-удаление дублей + unique index).

### Таблица `task_attempts`

Аудит-лог попыток выполнения (append-only). Пока не заполняется в MVP.

| Колонка          | Тип                     | Описание                                   |
|------------------|-------------------------|--------------------------------------------|
| `id`             | `INTEGER` (PK)          | Автоинкремент                              |
| `task_id`        | `UUID` (FK → tasks.id)  | Ссылка на задачу (CASCADE DELETE)          |
| `attempt_number` | `INTEGER`               | Номер попытки (1-based)                    |
| `status_before`  | `ENUM(taskstatus)`      | Статус до попытки                          |
| `status_after`   | `ENUM(taskstatus)`      | Статус после попытки                       |
| `error`          | `TEXT`                  | Ошибка (если неудача)                      |
| `started_at`     | `TIMESTAMPTZ`           | Время начала                               |
| `completed_at`   | `TIMESTAMPTZ`           | Время завершения (NULL = выполняется)      |
| `metadata`       | `JSONB`                 | Доп. данные (worker, retry delay)          |

### Таблица `outbox`

Transactional outbox. Не используется в MVP, подготовлена для Phase 2 (Celery).

### Примечание по enum

PostgreSQL enum `taskstatus` содержит lowercase-значения: `pending`, `processing`,
`completed`, `failed`, `cancelled`. SQLAlchemy модели используют строковые литералы
(`SAEnum("pending", "processing", ...)`) вместо Python enum для совместимости с
asyncpg (см. раздел "Известные проблемы" ниже).

## Архитектура кода

```
integration-service/
├── app/
│   ├── api/
│   │   └── bitrix.py           # /api/bitrix24/sync/* — status, trigger, cleanup, retry, reverse-status, reverse-test
│   ├── config/
│   │   └── settings.py         # Pydantic Settings (переменные окружения)
│   ├── core/
│   │   └── database.py         # Async SQLAlchemy engine + session factory
│   ├── models/
│   │   ├── base.py             # TimestampMixin (UUID PK, created_at, updated_at)
│   │   ├── task.py             # Task модель (source, source_id, status, payload)
│   │   ├── task_attempt.py     # TaskAttempt (аудит-лог попыток)
│   │   └── outbox.py           # Outbox (transactional outbox, unused в MVP)
│   ├── services/
│   │   ├── bitrix.py           # BitrixClient — REST API клиент Bitrix24
│   │   ├── glpi.py             # GLPIClient — REST API клиент GLPI
│   │   ├── poller.py           # APScheduler poller — опрос Bitrix24 → создание GLPI тикетов
│   │   └── reverse_sync.py     # Обратная синхронизация GLPI→Bitrix24 (status + followups)
│   │   └── org_sync.py         # Перенос пользователей и отделов Bitrix24 → GLPI
│   └── main.py                 # FastAPI app, lifespan, health/ready probes
├── alembic/                    # Миграции БД
│   └── versions/
├── config/                     # employee_mapping.json (для Production)
├── scripts/                    # Вспомогательные скрипты
├── tests/                      # Тесты
├── docker-compose.mvp.yml      # MVP: postgres + api (2 контейнера)
├── docker-compose.yml          # Production: 6 контейнеров
├── Dockerfile                  # Многостадийная сборка (builder → runtime)
├── requirements.txt            # Python-зависимости
└── .env.example                # Шаблон переменных окружения
```

## Стек технологий

| Компонент            | MVP                          | Production (будущее)           |
|----------------------|------------------------------|--------------------------------|
| Веб-фреймворк       | FastAPI 0.115 + Uvicorn 0.30 | FastAPI 0.115 + Uvicorn 0.30   |
| Планировщик         | APScheduler 3.10             | Celery 5.4 + RedBeat 2.2       |
| Брокер задач        | —                            | Redis 7                        |
| База данных         | PostgreSQL 15 + asyncpg      | PostgreSQL 15 + asyncpg        |
| ORM                 | SQLAlchemy 2.0 (async)       | SQLAlchemy 2.0 (async)         |
| Миграции            | Alembic 1.13                 | Alembic 1.13                   |
| Конфигурация        | Pydantic Settings 2.5        | Pydantic Settings 2.5          |
| HTTP-клиент         | httpx 0.27 (sync)            | httpx 0.27                     |
| Логирование         | logging (stdlib)             | loguru 0.7                     |
| Транскрибация       | —                            | faster-whisper 1.0 (large-v3)  |
| Мониторинг          | —                            | Flower 2.0                     |
| Retry               | —                            | tenacity 9.0                   |
| Тестирование        | pytest 8.3 + pytest-asyncio  | pytest 8.3 + pytest-asyncio    |

## Сервисы

### BitrixClient (`app/services/bitrix.py`)

Синхронный httpx-клиент для Bitrix24 REST API.

- **Аутентификация:** URL-based webhook (`/rest/{user}/{token}/{method}`)
- **Методы:**
  - `get_tasks(responsible_id, start)` — список задач с пагинацией
  - `get_task(task_id)` — одна задача по ID
  - `get_task_tags(task_id)` — теги задачи (legacy `task.item.gettags.json`)
  - `delete_task(task_id)` — удаление задачи
  - `update_task_status(task_id, status)` — обновление статуса
  - `add_comment(task_id, message)` — добавление комментария
  - `update_task_description(task_id, description)` — описание (fallback для задач без forumTopicId)
- **Пагинация:** 50 задач на страницу (`tasks.task.list.json`)
- **Маппинг полей:** camelCase → SCREAMING_SNAKE (для совместимости с poller)
- **Retry:** 1 попытка на 5xx, sleep 2s на 429 (rate-limit)

> **Примечание:** `task.ctasks.getlist.json` НЕ поддерживает фильтр
> `filter[RESPONSIBLE_ID]` и возвращает задачи из всех пользователей.
> Используйте `tasks.task.list.json` — он корректно фильтрует по `RESPONSIBLE_ID`.

### GLPIClient (`app/services/glpi.py`)

Синхронный httpx-клиент для GLPI legacy API (`apirest.php`).

- **Аутентификация:** App-Token (заголовок) + User Token (query param `user_token`)
- **Важно:** GLPI legacy API **не использует** HTTP Basic Auth. `user_token` передается
  как query param или в POST body. Basic Auth вызывает ошибку `Unable to extract nonce`.
- **Методы:**
  - `init_session()` — инициализация сессии, возвращает `session_token`
  - `create_ticket(name, content, session_token)` — создание тикета-инцидента
  - `show_ticket(ticket_id, session_token)` — получение тикета по ID
  - `update_ticket(ticket_id, session_token, **fields)` — обновление полей тикета
    (используется для закрытия orphan-тикетов при reconciliation)
  - `get_ticket_followups(ticket_id, session_token)` — ITIL followup-комментарии
    (используется reverse sync для синхронизации комментариев в Bitrix24)
- **Сессии:** GLPI сессии истекают быстро. В MVP poller создаёт новую сессию на каждый poll-цикл.

### Poller (`app/services/poller.py`)

APScheduler-based poller, работающий внутри FastAPI процесса.

- **Расписание:** `interval` mode, каждые `BITRIX24_POLL_INTERVAL_SECONDS` (по умолч. 60s)
- **Логика:**
  1. Для каждого `responsible_id` из `BITRIX24_RESPONSIBLE_IDS`
  2. Постранично загружает задачи из Bitrix24 (50/страница)
  3. Пропускает закрытые/неактивные задачи (статусы 4, 5, 6, 7;
     1 «новое», 2 «в работе/ожидает», 3 «в работе» — обрабатываются)
  4. Проверяет идемпотентность по `source_id` в БД (race защищён unique
     constraint + обработкой IntegrityError)
  5. Создаёт Task запись (status=`processing`)
  6. Создаёт GLPI тикет через `GLPIClient.create_ticket()`
  7. Обновляет Task (status=`completed`, result=GLPI ticket data)
  Зависшие задачи в `processing` (старше 2×poll-интервала) и `failed`
  (attempts < max_attempts) автоматически ретраятся.
- **Идемпотентность:** `idempotency_key = "b24:{task_id}"`
- **Reconciliation:** после обработки всех задач проверяет, какие ранее
  синхронизированные задачи пропали из Bitrix24 (удалены), и закрывает
  соответствующие тикеты GLPI (status=5, решено). Safety-порог:
  минимум 10 задач за цикл — иначе reconciliation пропускается
  (защита от ложных срабатываний при API-ошибке).
- **Жизненный цикл:** `start_poller()` / `stop_poller()` в lifespan FastAPI

### Кодовая архитектура (дополнение)

#### Reverse Sync (`app/services/reverse_sync.py`)

Обратная синхронизация — GLPI → Bitrix24. Работает **только** в test mode
для whitelist-задач (ID из `TEST_TASK_IDS`). **Запись в Bitrix24 разрешена
только для задач из whitelist** (`TEST_TASK_IDS`): `_sync_one_task` проверяет
каждый task_id перед записью и отказывается писать в любую другую задачу
(счётчик `skipped_not_whitelisted`). Автоматически запускается по расписанию
каждые `BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS`, если
`BITRIX24_REVERSE_SYNC_ENABLED=true` (по умолчанию), и вручную через
`POST /api/bitrix24/sync/reverse-test`.

- **Механизм:**
  1. Читает из БД записи Task с source='bitrix24' и matching source_id
  2. Извлекает ID GLPI-тикета из поля `result` (поддерживает list и dict-форматы)
  3. Инициирует сессию GLPI
  4. Запрашивает текущий статус тикета и ITIL followup-комментарии
  5. Если статус изменился → обновляет Bitrix24 задачу через `update_task_status()`
  6. Если появились новые followup'ы → добавляет их в описание Bitrix24 задачи
     через `update_task_description()`
  7. Запоминает последние `last_glpi_status` и `last_glpi_followup_id` в БД
- **Маппинг статусов:** `{1→1 (new→open), 2→2 (assigned→pending), 3→4 (hold→frozen),
  4→3 (resolved→closed), 5→5 (solved→completed), 6→6 (cancelled→deferred)}`
- **Description-append для задач без forumTopicId:** если у Bitrix24-задачи
  `forumTopicId=None`, то `tasks.task.comment.add` молча не срабатывает.
  Вместо комментариев reverse sync дописывает текст followup'ов в
  `DESCRIPTION` задачи через `tasks.task.update`. Максимальная длина —
  60000 символов (ограничение Bitrix24 TEXT). Отдельные followup'ы
  длиннее 2000 символов обрезаются.
- **API endpoints:** `GET /api/bitrix24/sync/reverse-status`,
  `POST /api/bitrix24/sync/reverse-test`

#### Org Sync (`app/services/org_sync.py`)

Перенос пользователей и оргструктуры из Bitrix24 в GLPI. Требует вебхука
`BITRIX24_ORG_WEBHOOK_URL` с правами **`user` + `department`** (обычный
`BITRIX24_WEBHOOK_URL` их не имеет) и GLPI API-пользователя с правами на
создание сущностей и пользователей.

- **Отделы:** полное дерево `department.get` зеркалится в GLPI-entity под
  `ORG_SYNC_ROOT_ENTITY_ID` (по умолч. 25 — «АО «АПО «Аврора»). Существующие
  сущности матчатся по имени (case-insensitive), недостающие создаются;
  иерархия Bitrix24 сохраняется.
- **Пользователи:** `user.get` (постранично). Активные пользователи
  создаются/обновляются в GLPI, матчинг по **email** (case-insensitive,
  через `glpi_useremails`). Логин = email (или `b24_{id}`, если email нет),
  профиль = `ORG_SYNC_USER_PROFILE_ID` (по умолч. 1 — Self-Service),
  дефолтная entity = entity отдела пользователя (первый из `UF_DEPARTMENT`).
- **Идемпотентность:** повторный запуск создаёт только недостающее и
  обновляет ФИО/entity.
- **Запуск:** вручную `POST /api/bitrix24/sync/org` (с `X-Admin-Token`);
  по расписанию — при `ORG_SYNC_ENABLED=true` (интервал
  `ORG_SYNC_INTERVAL_SECONDS`, джоб `bitrix24_org_sync`).
- **Статус:** `GET /api/bitrix24/sync/org-status`

## Деплой на signal-glpi (Proxmox)

### Текущая конфигурация (MVP)

- **Сервер:** `signal-glpi` (VM в Proxmox, Debian). SSH: `ssh root@signal-glpi`
- **Docker-проекты** (в `/opt/...`):
  - `integration-service` — `docker-compose.yml`: **postgres, redis, api** (3 контейнера)
  - `glpi` (glpi-app, glpi-db, glpi-dbgate, glpi-mailpit, glpi-openldap)
  - `mts-stats`, `sla-dashboard`, `docs-signal-infa`, `homer`, `traefik`
- **Сети:** `integration-net` (postgres, redis, api)
- **Порты/маршрутизация:**
  - Integration API: `:8000` → `integration-api` (+ traefik router `Host(api.ais.local)`)
  - Integration PostgreSQL: `:5432` → `integration-postgres`
  - Integration Redis: `:6379` → `integration-redis` (зарезервирован под Phase 2)
  - GLPI: `:8080`/`:8090` → `glpi-app`
- **Резервный** `docker-compose.mvp.yml` — 2 контейнера (postgres + api) для локального быстрого старта без Redis.

### Деплой обновлений

```bash
ssh root@signal-glpi
cd /opt/integration-service

# 1. Обновление кода
git pull origin main

# 2. Пересборка и перезапуск API
docker compose up -d --build api

# 3. Применение миграций (вручную — Dockerfile не запускает alembic)
docker exec integration-api alembic upgrade head

# 4. Проверка
curl -s http://localhost:8000/health
curl -s http://localhost:8000/api/bitrix24/sync/status
```

> **Перед деплоем:**
> - В `/opt/integration-service/.env` должны быть заданы `ADMIN_API_TOKEN`
>   (и при необходимости `CORS_ORIGINS`) — иначе мутирующие эндпоинты
>   (`/sync/trigger`, `/sync/cleanup`, `/sync/retry`, `/sync/reverse-test`)
>   отвечают `401`.
> - Перед применением миграции с unique-индексом (`a1b2c3d4e5f6`) сделать
>   бэкап БД — дедуп-удаление дублей `(source, source_id)` необратимо.

## Известные проблемы и решения

### asyncpg enum serialization

**Проблема:** asyncpg сериализует Python enum как `.name` (UPPERCASE) вместо
`.value` (lowercase). PostgreSQL enum `taskstatus` ожидает lowercase.

**Ошибка:** `invalid input value for enum taskstatus: "PROCESSING"`

**Решение:** Модели используют строковые литералы вместо Python enum:

```python
# НЕПРАВИЛЬНО (asyncpg отправит "PROCESSING"):
status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus, ...))

# ПРАВИЛЬНО (asyncpg отправит "processing"):
status: Mapped[str] = mapped_column(
    SAEnum("pending", "processing", "completed", "failed", "cancelled", name="taskstatus")
)
```

**См. коммит:** `30feaf4`

### GLPI Basic Auth не работает

**Проблема:** GLPI legacy API (`apirest.php`) не поддерживает HTTP Basic Auth.
Попытка использовать `httpx.BasicAuth(user_token, "")` вызывает ошибку
`Unable to extract nonce` (sodium decryption failure).

**Решение:** `user_token` передается как query parameter:

```python
# НЕПРАВИЛЬНО:
response = client.get(url, auth=httpx.BasicAuth(user_token, ""))

# ПРАВИЛЬНО:
response = client.get(url, params={"user_token": user_token})
```

### GLPI API отключен по умолчанию

GLPI может иметь `enable_api=0` в БД. Нужно включить:

```sql
UPDATE glpi_configs SET value='1' WHERE context='core' AND name='enable_api';
```

Также required: создание API-клиента (App-Token) и пользователя с API-токеном
в GLPI Admin → Setup → API Clients.

### task.ctasks.getlist.json игнорирует фильтр

**Проблема:** Метод `task.ctasks.getlist.json` игнорирует параметр `filter[RESPONSIBLE_ID]`
и возвращает задачи из всех пользователей Bitrix24.

**Решение:** Используйте `tasks.task.list.json` — он корректно фильтрует по `RESPONSIBLE_ID`.

**Отличия API:**

| | `task.ctasks.getlist.json` | `tasks.task.list.json` |
|---|---|---|
| Фильтр | Игнорируется | Работает ✅ |
| Структура ответа | `result: [...]` | `result: {tasks: [...]}` |
| Имена полей | SCREAMING_SNAKE | camelCase |
| Пагинация `next` | Top-level | Top-level |
| Total | Нет | `result.total` (top-level) |

**См. коммиты:** `975b6d5`, `ddc8d97`

## Дорожная карта

### Phase 1 (MVP) — ✅ Выполнено

- [x] Polling Bitrix24 REST API → создание GLPI тикетов
- [x] Идемпотентность по source_id
- [x] Health/ready probes
- [x] Docker Compose deployment
- [x] Alembic миграции
- [x] Manual sync trigger API
- [x] Корректный метод API (`tasks.task.list.json` вместо `task.ctasks.getlist.json`)
- [x] Reverse sync (GLPI → Bitrix24): статусы и followup'ы в test mode
- [x] Reconciliation: авто-закрытие GLPI тикетов при удалении задачи в Bitrix24
- [x] Description-append для задач без forumTopicId
- [x] 239 проходящих pytest-теста (reverse sync, poller, API, миграции)
- [x] Bitrix24-вызовы в `asyncio.to_thread` — снятие блокировки event loop
- [x] Аутентификация мутирующих эндпоинтов (`X-Admin-Token`) + условный CORS
- [x] Retry зависших/failed-задач + ручной `POST /sync/retry`
- [x] Уникальность `(source, source_id)` — unique index + дедуп-миграция `a1b2c3d4e5f6`
- [x] Жизненный цикл GLPI-сессий (`kill_session`) + дефолтные category/group/entity
- [x] Reverse sync по расписанию с whitelist-защитой (запись только в `TEST_TASK_IDS`)
- [x] Org sync: перенос пользователей и оргструктуры Bitrix24 → GLPI (дерево отделов в entity, матчинг по email)

### Phase 2 (Production) — Планируется

- [ ] **Архитектурный переход:** Polling → Incoming Webhooks
  - Bitrix24 отправляет вебхук при новом лиде → `POST /webhook/bitrix/lead`
  - MANGO Office отправляет вебхук при звонке → `POST /webhook/mango/call`
  - Требуется: reverse proxy (nginx) с SSL для публичного URL
- [ ] **Celery Worker:** асинхронная обработка задач (GLPI, Bitrix24, Whisper)
- [ ] **Redis + RedBeat:** брокер задач + планировщик
- [ ] **Transaction Outbox:** надёжная публикация сообщений
- [ ] **Retry с exponential backoff:** tenacity + jitter
- [ ] **Employee Mapping:** маппинг Bitrix24 user → GLPI user/group
- [ ] **Task Attempts:** аудит-лог каждой попытки
- [ ] **Flower:** мониторинг Celery workers

### Phase 3 (MANGO Integration) — Планируется

- [ ] Регистрация звонков в Bitrix24 через MANGO REST API
- [ ] Транскрибация аудиозаписей (faster-whisper)
- [ ] Привязка транскрипции к тикетам GLPI
- [ ] MANGO webhook endpoint

### Phase 4 (Production Hardening) — Планируется

- [ ] OAuth2 аутентификация для GLPI
- [ ] Structured logging (loguru + JSON)
- [ ] Метрики (Prometheus)
- [ ] Rate limiting для входящих вебхуков
- [ ] Circuit breaker для внешних API
- [ ] Graceful shutdown для Celery workers

## Migration от MVP к Production

При переходе от polling к webhook-архитектуре потребуется:

1. **Reverse proxy** (nginx) с SSL-сертификатом для публичного URL
2. **Bitrix24:** настроить исходящие вебхуки (CRM → События → Лида)
3. **Redis + Celery:** добавить в docker-compose.yml
4. **Обновить docker-compose.yml:** раскомментировать Redis, worker, beat, flower
5. **Настроить .env:** добавить REDIS_URL, CELERY_*, BITRIX24_USER_ID
6. **Миграции:** новые поля в tasks (worker_id, lease_expires_at будут использоваться)
7. **Убрать APScheduler:** заменить на Celery Beat
8. **API endpoints:** добавить `/webhook/bitrix/lead`, `/webhook/mango/call`

Данные в БД (tasks, task_attempts) совместимы — polling и webhook используют
одну и ту же модель Task.

## Лицензия

MIT License — см. файл [LICENSE](LICENSE).

Автор: **pbolkhovitin**
