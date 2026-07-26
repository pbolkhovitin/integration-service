# Интеграционный сервис Bitrix24 / GLPI / MANGO Office

Сервис для синхронизации данных между **Bitrix24**, **GLPI** и **MANGO Office**.

**Текущий статус:** MVP — опрос Bitrix24 REST API → создание тикетов в GLPI.

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
| `postgres`| PostgreSQL 15 (хранилище задач)               | `5433→5432`    |
| `api`     | FastAPI + APScheduler (poller внутри процесса) | `8000`         |

Два контейнера. Без Redis, Celery, Flower. Poller опрашивает Bitrix24 каждые N
секунд и создает тикеты в GLPI синхронно внутри AsyncIO.

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
curl -X POST http://localhost:8000/api/bitrix24/sync/trigger
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

### Опциональные (MVP)

| Переменная                       | По умолчанию | Описание                            |
|----------------------------------|-------------|-------------------------------------|
| `POSTGRES_SERVER`                | `postgres`  | Хост PostgreSQL                     |
| `POSTGRES_PORT`                  | `5432`      | Порт PostgreSQL                     |
| `POSTGRES_DB`                    | `integration` | Имя БД                           |
| `POSTGRES_USER`                  | `integration` | Пользователь БД                  |
| `BITRIX24_POLL_INTERVAL_SECONDS` | `60`        | Интервал опроса Bitrix24 (секунды)  |
| `GLPI_DEFAULT_CATEGORY_ID`       | `1`         | Категория по умолчанию (Инцидент)   |
| `GLPI_DEFAULT_GROUP_ID`          | `1`         | Группа по умолчанию (IT-поддержка L1) |
| `GLPI_DEFAULT_ENTITY_ID`         | `2`         | Орг. единица (Департамент IT)       |
| `APP_HOST`                       | `0.0.0.0`   | Хост Uvicorn                        |
| `APP_PORT`                       | `8000`      | Порт Uvicorn                        |
| `DATABASE_URL`                   | —           | URL подключения (переопределяет POSTGRES_*) |

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
         "responsible_ids": [70], "next_run": "2025-..."
  Статус poller'а: интервал, ID ответственных, следующий запуск.

POST /api/bitrix24/sync/trigger
  → 200 {"status": "completed"}
  Ручной запуск poll-цикла (немедленно, без ожидания расписания).
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
| `created_at`       | `TIMESTAMPTZ`           | Дата создания                                |
| `updated_at`       | `TIMESTAMPTZ`           | Дата обновления                              |

**Индексы:** `ix_tasks_status`, `ix_tasks_source`, `ix_tasks_idempotency_key` (partial unique), `ix_tasks_lease`.

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
│   │   └── bitrix.py           # /api/bitrix24/sync/status, /sync/trigger
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
│   │   └── poller.py           # APScheduler poller — опрос Bitrix24 → создание GLPI тикетов
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
- **Методы:** `get_tasks(responsible_id, start)`, `get_task(task_id)`
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
- **Методы:** `init_session()`, `create_ticket(name, content, session_token)`, `show_ticket(id)`
- **Сессии:** GLPI сессии истекают быстро. В MVP poller создаёт новую сессию на каждый poll-цикл.

### Poller (`app/services/poller.py`)

APScheduler-based poller, работающий внутри FastAPI процесса.

- **Расписание:** `interval` mode, каждые `BITRIX24_POLL_INTERVAL_SECONDS` (по умолч. 60s)
- **Логика:**
  1. Для каждого `responsible_id` из `BITRIX24_RESPONSIBLE_IDS`
  2. Постранично загружает задачи из Bitrix24 (50/страница)
  3. Пропускает закрытые задачи (status 3, 5)
  4. Проверяет идемпотентность по `source_id` в БД
  5. Создаёт Task запись (status=`processing`)
  6. Создаёт GLPI тикет через `GLPIClient.create_ticket()`
  7. Обновляет Task (status=`completed`, result=GLPI ticket data)
- **Идемпотентность:** `idempotency_key = "b24:{task_id}"`
- **Жизненный цикл:** `start_poller()` / `stop_poller()` в lifespan FastAPI

## Деплой на infrastructure

### Текущая конфигурация (MVP)

- **Сервер:** VM в Proxmox (Debian 13, 4 vCPU, 8GB RAM, 20GB disk)
- **Docker-проекты:**
  - `glpi` (glpi-app, glpi-db, glpi-dbgate, glpi-mailpit, glpi-openldap)
  - `sla-dashboard` (SLA-мониторинг, SQLite)
  - `integration-service` (postgres, api) — MVP
- **Сети:**
  - `glpi_default` — GLPI контейнеры + integration-api
  - `integration_default` — integration-api + integration-postgres
- **Порты:**
  - GLPI: `:8080` → glpi-app
  - Integration API: `:8000` → integration-api
  - Integration PostgreSQL: `:5433` → postgres:5432

### Git hooks

```bash
# Деплой через SSH
ssh -i ~/.ssh/<your-key> root@<server-ip>

# Обновление кода
cd /opt/integration-service
git pull origin main

# Пересборка и перезапуск
docker compose -f docker-compose.mvp.yml build api
docker compose -f docker-compose.mvp.yml up -d api

# Применение миграций (если есть новые)
docker exec -e DATABASE_URL='postgresql+asyncpg://integration:integration123@postgres:5432/integration' \
  integration-api alembic upgrade head
```

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
