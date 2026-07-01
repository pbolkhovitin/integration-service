# Интеграционный сервис Bitrix24 / GLPI / MANGO Office

Сервис для синхронизации данных между **Bitrix24**, **GLPI** и **MANGO Office**.
Принимает вебхуки от Bitrix24 (лиды) и MANGO Office (звонки, записи разговоров),
создаёт тикеты в GLPI, регистрирует звонки в Bitrix24 и транскрибирует
аудиозаписи через **faster-whisper**.

## Поток данных

```
┌──────────────┐     ┌──────────────────┐     ┌──────────┐
│   Bitrix24   │────▶│  FastAPI (API)   │◀────│  MANGO   │
│   (лиды)     │     │  /webhook/bitrix │     │  Office  │
│              │     │  /webhook/mango  │     │ (звонки) │
└──────────────┘     └────────┬─────────┘     └──────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  PostgreSQL      │
                    │  ┌────────────┐  │
                    │  │   tasks    │  │
                    │  │ attempts   │  │  Transactional
                    │  │  outbox    │  │  Outbox
                    │  └────────────┘  │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                              ▼
    ┌──────────────────┐          ┌──────────────────┐
    │  Celery Worker   │          │  Redis (Broker)  │
    │  ┌────────────┐  │          │  + RedBeat       │
    │  │  GLPI      │  │          │  (планировщик)   │
    │  │  Bitrix24  │  │          └──────────────────┘
    │  │  Whisper   │  │
    │  │  (транскр.)│  │
    │  └────────────┘  │
    └──────────────────┘
              │
              ▼
    ┌──────────────────┐     ┌──────────────────┐
    │  GLPI (тикеты)   │     │  Bitrix24 (звонки)│
    └──────────────────┘     └──────────────────┘
```

## Быстрый старт (Production Mode)

### Предварительные требования

- Docker 24+
- Docker Compose v2

### Запуск

```bash
# 1. Клонировать репозиторий
git clone <repo-url> integration-service
cd integration-service

# 2. Настроить окружение
cp .env.example .env
# Отредактировать .env — указать обязательные переменные
# (GLPI_URL, GLPI_APP_TOKEN, GLPI_USER_TOKEN,
#  BITRIX24_WEBHOOK_URL, BITRIX24_USER_ID,
#  POSTGRES_PASSWORD)

# 3. Создать файл маппинга сотрудников
cp config/employee_mapping.json.example config/employee_mapping.json

# 4. Запустить сервисы
docker compose up -d

# 5. Применить миграции БД
docker compose exec api alembic upgrade head

# 6. Проверить состояние
docker compose ps
# Все 6 контейнеров должны быть в статусе Up

curl http://localhost:8000/health
# → {"status": "healthy"}

### Мониторинг

Flower (Celery Monitor) доступен по адресу: http://localhost:5555

## MVP Sync Mode (быстрый старт без Docker)

Режим MVP (Minimum Viable Product) — облегчённый вариант запуска для демонстрации
и тестирования без Docker, Celery и Redis. Обработка вебхука Bitrix24 и создание
тикета в GLPI происходят **синхронно** внутри FastAPI request handler.

### Предварительные требования

- Python 3.11+
- PostgreSQL 15+ (локально или доступный удалённо)
- git

### Быстрый запуск

```bash
# 1. Клонировать и перейти в директорию
git clone <repo-url> integration-service
cd integration-service

# 2. Создать виртуальное окружение
python3.11 -m venv venv
source venv/bin/activate

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Настроить окружение
cp .env.example .env
# Отредактировать .env:
#   GLPI_URL, GLPI_APP_TOKEN, GLPI_USER_TOKEN,
#   BITRIX24_WEBHOOK_URL, BITRIX24_USER_ID
#   POSTGRES_SERVER=localhost, POSTGRES_PASSWORD=your_password

# 5. Создать файл маппинга сотрудников
cp config/employee_mapping.json.example config/employee_mapping.json

# 6. Применить миграции БД
alembic upgrade head

# 7. Запустить сервис
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 8. Проверить состояние
curl http://localhost:8000/health
# → {"status": "healthy"}
curl http://localhost:8000/ready
# → {"status": "ready"}
```

### Отличия от Production Mode

| | MVP Sync Mode | Production Mode |
|---|---|---|
| Обработка | Синхронная (в запросе) | Асинхронная (Celery worker) |
| Зависимости | Python + PostgreSQL | Docker (6 контейнеров) |
| Retry | Нет (ошибка = HTTP 500) | Да (exponential backoff) |
| Идемпотентность | Да (по idempotency_key) | Да |
| Масштабирование | 1 процесс | N workers + Celery Beat |
| Использование | Демо, разработка, тесты | Production |

### Аутентификация GLPI

В MVP-режиме используется аутентификация через **App-Token** (`GLPI_APP_TOKEN` +
`GLPI_USER_TOKEN`). Production-режим в будущих релизах добавит поддержку OAuth2.

### Переменные окружения

Полный список доступных переменных окружения см. в файле `.env.example`.

## Архитектура

Сервис состоит из 6 контейнеров Docker:

| Сервис    | Назначение                                    | Порты     |
|-----------|-----------------------------------------------|-----------|
| `postgres`| Основное хранилище данных (PostgreSQL 15)     | `5432`    |
| `redis`   | Брокер задач Celery + RedBeat scheduler       | `6379`    |
| `api`     | FastAPI приложение (вебхуки + management)      | `8000`    |
| `worker`  | Celery worker (асинхронная обработка задач)    | —         |
| `beat`    | Celery Beat (планировщик периодических задач)  | —         |
| `flower`  | Веб-мониторинг Celery                          | `5555`    |

### FastAPI (api)

Принимает входящие вебхуки от внешних систем:

- `POST /webhook/bitrix` — уведомления о новых лидах из Bitrix24
- `POST /webhook/mango` — уведомления о звонках и аудиозаписях из MANGO Office
- `GET /health` — проверка состояния сервиса
- `GET /tasks/{id}` — статус задачи по ID
- `GET /tasks` — список задач с фильтрацией

Запросы проходят **идемпотент-контроль**: повторные вебхуки с тем же
`idempotency_key` (поле JSON-тела) не создают дубликатов.

> **Phase 1 (MVP):** В синхронном режиме обработка вебхука /webhook/bitrix/lead
> происходит в том же запросе, без Celery. Эндпоинты /webhook/mango, /tasks
> и /tasks/{id} будут добавлены в следующих фазах.

### Celery Worker (worker)

Асинхронно выполняет задачи, созданные через transactional outbox:

- **Создание тикета в GLPI** — по новому лиду из Bitrix24
- **Регистрация звонка в Bitrix24** — по событию из MANGO Office
- **Транскрибация аудиозаписи** — через faster-whisper large-v3
- **Обновление статуса звонка** — после завершения транскрибации

### Celery Beat (beat)

Планировщик периодических задач на базе **RedBeat** (Redis). Выполняет:

- **Очистка устаревших лизенов** — возврат в очередь зависших задач
- **Переотправка outbox-сообщений** — повторная публикация неопубликованных записей
- **Пул устаревших задач** — обработка задач, превысивших лимит попыток

## Переменные окружения

Все настройки задаются через переменные окружения (файл `.env`).

### PostgreSQL

| Переменная          | Обязательная | По умолчанию      | Описание                   |
|---------------------|-------------|-------------------|----------------------------|
| `POSTGRES_SERVER`   | да          | `postgres`        | Хост PostgreSQL            |
| `POSTGRES_PORT`     | да          | `5432`            | Порт PostgreSQL            |
| `POSTGRES_DB`       | да          | `integration`     | Имя БД                     |
| `POSTGRES_USER`     | да          | `integration`     | Пользователь БД            |
| `POSTGRES_PASSWORD` | **да**      | —                 | Пароль БД (не задан по умолчанию!) |

### Redis

| Переменная    | Обязательная | По умолчанию             | Описание          |
|---------------|-------------|--------------------------|-------------------|
| `REDIS_URL`   | да          | `redis://redis:6379/0`   | URL подключения   |

### GLPI

**MVP (Phase 1) — аутентификация через App-Token:**

| Переменная          | Обязательная | По умолчанию      | Описание                     |
|---------------------|-------------|-------------------|------------------------------|
| `GLPI_URL`          | да          | `http://glpi:80`  | Базовый URL GLPI API         |
| `GLPI_APP_TOKEN`    | **да**      | —                 | GLPI App-Token (заголовок)   |
| `GLPI_USER_TOKEN`   | **да**      | —                 | GLPI API User Token (Basic Auth, username) |

> **Phase 4 (планируется):** OAuth2-аутентификация через
> `GLPI_CLIENT_ID`, `GLPI_CLIENT_SECRET`, `GLPI_USERNAME`, `GLPI_PASSWORD`.
> На данный момент эти переменные не используются.

### Bitrix24

| Переменная             | Обязательная | По умолчанию | Описание                        |
|------------------------|-------------|--------------|---------------------------------|
| `BITRIX24_WEBHOOK_URL` | **да**      | —            | Входящий вебхук Bitrix24        |
| `BITRIX24_USER_ID`     | **да**      | —            | ID пользователя Bitrix24 (ответственный за лиды) |

### Employee Mapping

| Переменная                 | Обязательная | По умолчанию                         | Описание                     |
|----------------------------|-------------|---------------------------------------|------------------------------|
| `EMPLOYEE_MAPPING_PATH`    | нет         | `/app/config/employee_mapping.json`   | Путь к файлу маппинга        |

### Whisper (faster-whisper)

| Переменная           | Обязательная | По умолчанию | Описание                                |
|----------------------|-------------|--------------|-----------------------------------------|
| `WHISPER_MODEL_SIZE` | нет         | `large-v3`   | Размер модели (tiny/base/small/medium/large-v3) |
| `WHISPER_DEVICE`     | нет         | `cpu`        | Устройство (cpu / cuda)                 |
| `WHISPER_COMPUTE_TYPE` | нет       | `int8`       | Тип вычислений (int8 / float16 / float32) |
| `WHISPER_BATCH_SIZE` | нет         | `8`          | Размер батча при транскрибации          |

### Celery

| Переменная               | Обязательная | По умолчанию             | Описание                  |
|--------------------------|-------------|--------------------------|---------------------------|
| `CELERY_BROKER_URL`      | да          | `redis://redis:6379/0`   | Брокер сообщений Celery   |
| `CELERY_RESULT_BACKEND`  | нет         | `redis://redis:6379/0`   | Бэкенд результатов        |

### Retry

| Переменная           | Обязательная | По умолчанию | Описание                          |
|----------------------|-------------|--------------|-----------------------------------|
| `RETRY_BACKOFF_BASE` | нет         | `2`          | Базовый множитель экспоненты      |
| `RETRY_BACKOFF_MAX`  | нет         | `300`        | Максимальная задержка (секунды)   |

### FastAPI

| Переменная  | Обязательная | По умолчанию | Описание              |
|-------------|-------------|--------------|-----------------------|
| `APP_HOST`  | нет         | `0.0.0.0`    | Хост для Uvicorn      |
| `APP_PORT`  | нет         | `8000`       | Порт для Uvicorn      |

## API Endpoints

### Вебхуки

```
POST /webhook/bitrix/lead
  Принимает: application/json
  Тело: объект лида Bitrix24 (name — обязателен, остальные поля опциональны)
  Idempotency: поле idempotency_key в JSON теле запроса
  Ответ: 200 {"status": "success", "task_id": "uuid", "glpi_ticket": {...}}
    — при успешном создании тикета
    {"status": "duplicate", "task_id": "uuid"}
    — повторный запрос с тем же idempotency_key (COMPLETED/FAILED)
    {"status": "in_progress", "task_id": "uuid"}
    — задача с таким idempotency_key уже выполняется (PENDING/PROCESSING)
  Ошибки: 422 — некорректный payload (Pydantic validation)
          500 — ошибка при создании тикета в GLPI

```

### Health Check

```
GET /health
  Ответ: 200 {"status": "healthy"}

GET /ready
  Ответ: 200 {"status": "ready"} — БД доступна
  Ответ: 200 {"status": "unhealthy"} — БД недоступна
```

### Управление задачами

```
GET /tasks/{id}
  _Реализуется в Phase 2 (асинхронная обработка)._

GET /tasks?status=pending&source=bitrix&limit=50&offset=0
  _Реализуется в Phase 2 (асинхронная обработка)._
```

> **Примечание:** В MVP Sync Mode эндпоинты `/webhook/mango`, `/tasks/{id}` и
> `/tasks` не реализованы — они будут добавлены в следующих фазах вместе с
> Celery-воркером и асинхронной обработкой.

## Структура базы данных

### Таблица `tasks`

Центральная таблица — каждая задача представляет единицу интеграционной работы.

| Колонка            | Тип                     | Описание                                     |
|--------------------|-------------------------|----------------------------------------------|
| `id`               | `UUID` (PK)             | Первичный ключ, `gen_random_uuid()`          |
| `source`           | `VARCHAR(50)`           | Система-источник (bitrix / mango / glpi)     |
| `source_id`        | `VARCHAR(255)`          | ID объекта в исходной системе                |
| `type`             | `VARCHAR(50)`           | Тип задачи (create_ticket, register_call, …) |
| `payload`          | `JSONB`                 | Входные данные задачи                        |
| `status`           | `ENUM('pending','processing','completed','failed','cancelled')` | Статус жизненного цикла |
| `attempts`         | `INTEGER`               | Количество попыток выполнения (по умолч. 0)  |
| `max_attempts`     | `INTEGER`               | Максимум попыток (по умолч. 3)               |
| `last_error`       | `TEXT`                  | Сообщение об ошибке последней попытки         |
| `result`           | `JSONB`                 | Результат успешного выполнения                |
| `idempotency_key`  | `VARCHAR(255)`          | Ключ идемпотентности (уникален, если задан)   |
| `worker_id`        | `VARCHAR(100)`          | ID worker'а, владеющего лизеном               |
| `lease_expires_at` | `TIMESTAMPTZ`           | Время истечения лизена                        |
| `created_at`       | `TIMESTAMPTZ`           | Дата создания                                 |
| `updated_at`       | `TIMESTAMPTZ`           | Дата обновления                               |

**Индексы:**

- `ix_tasks_status` — поиск по статусу
- `ix_tasks_source` — поиск по источнику
- `ix_tasks_idempotency_key` — уникальный частичный индекс (`WHERE idempotency_key IS NOT NULL`)
- `ix_tasks_lease` — поик по `(worker_id, lease_expires_at)` для обработки лизенов

### Таблица `task_attempts`

Аппенд-только лог всех попыток выполнения задач.

| Колонка          | Тип                     | Описание                                   |
|------------------|-------------------------|--------------------------------------------|
| `id`             | `INTEGER` (PK)          | Автоинкрементный первичный ключ            |
| `task_id`        | `UUID` (FK → tasks.id)  | Ссылка на задачу (CASCADE DELETE)          |
| `attempt_number` | `INTEGER`               | Номер попытки (начиная с 1)                |
| `status_before`  | `ENUM(taskstatus)`      | Статус задачи до попытки                   |
| `status_after`   | `ENUM(taskstatus)`      | Статус задачи после попытки                |
| `error`          | `TEXT`                  | Сообщение об ошибке (если попытка не удалась) |
| `started_at`     | `TIMESTAMPTZ`           | Время начала попытки                       |
| `completed_at`   | `TIMESTAMPTZ`           | Время завершения (NULL = ещё выполняется)  |
| `metadata`       | `JSONB`                 | Дополнительные данные (worker, retry delay) |

**Индексы:**

- `ix_task_attempts_task_id` — поиск по task_id

### Таблица `outbox`

Transactional outbox для надёжной публикации сообщений в брокер.

| Колонка         | Тип                     | Описание                                      |
|-----------------|-------------------------|-----------------------------------------------|
| `id`            | `UUID` (PK)             | `gen_random_uuid()`                           |
| `task_id`       | `UUID` (FK → tasks.id)  | Ссылка на задачу (CASCADE DELETE)             |
| `routing_key`   | `VARCHAR(100)`          | Ключ маршрутизации (напр. `tasks:pending:primary`) |
| `payload`       | `JSONB`                 | Тело сообщения                                |
| `created_at`    | `TIMESTAMPTZ`           | Дата создания                                 |
| `published_at`  | `TIMESTAMPTZ`           | Дата публикации (NULL = не опубликовано)       |
| `retry_count`   | `INTEGER`               | Количество попыток публикации                 |
| `last_error`    | `TEXT`                  | Ошибка последней публикации                   |

**Индексы:**

- `ix_outbox_unpublished` — частичный индекс (`WHERE published_at IS NULL`) для быстрого поиска неопубликованных записей

## Retry-механизм

Система использует **Processing Lease** в сочетании с **Exponential Backoff** и **Full Jitter** для надёжной обработки задач.

### Processing Lease

- Каждый worker перед выполнением задачи устанавливает `worker_id` и `lease_expires_at`.
- Другие worker'ы видят, что задача занята, и пропускают её.
- Если lease истёк (worker упал), задача считается доступной для повторного взятия.
- Периодическая задача (Celery Beat) сканирует просроченные лизены и возвращает задачи в очередь.

### Exponential Backoff + Full Jitter

При неудачной попытке вычисляется задержка перед следующей:

```
delay = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
  jitter = delay * random.random()
  next_retry = now + jitter
```

- `RETRY_BACKOFF_BASE = 2` (по умолчанию) → задержки: 1.5с, 3с, 6с, 12с, …
- `RETRY_BACKOFF_MAX = 300` (5 минут) — потолок задержки
- Full Jitter предотвращает «Thundering Herd» при одновременном перезапуске множества задач
- После превышения `max_attempts` задача переходит в статус `failed`

### Логирование попыток

Каждая попытка (успешная или нет) фиксируется в таблице `task_attempts`:
статус до/после, ошибка, время начала/завершения, метаданные.

## Разработка

### Локальный запуск (без Docker)

```bash
# 1. Создать виртуальное окружение
python3.11 -m venv venv
source venv/bin/activate

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Запустить PostgreSQL и Redis
# (через Docker или локально)

# 4. Настроить .env (скопировать из .env.example)

# 5. Применить миграции
alembic upgrade head

# 6. Запустить API
uvicorn app.main:app --reload --port 8000

# *Celery worker запускается в Phase 2 (асинхронная обработка)*
```

### Тесты

```bash
# Все тесты
pytest

# С покрытием
pytest --cov=app --cov-report=term-missing

# Параллельный запуск
pytest -xvs
```

### Миграции БД (Alembic)

```bash
# Создать новую миграцию (авто-генерация)
alembic revision --autogenerate -m "description"

# Применить миграции
alembic upgrade head

# Откатить на одну
alembic downgrade -1
```

### Линтеры и форматтеры

```bash
# Форматирование
black app/ tests/

# Линтер
ruff check app/ tests/

# Проверка типов
mypy app/
```

## Стек технологий

| Компонент            | Технология                                    |
|----------------------|-----------------------------------------------|
| Веб-фреймворк        | FastAPI 0.115 + Uvicorn 0.30                  |
| Очередь задач        | Celery 5.4 + Redis 7                          |
| База данных          | PostgreSQL 15 + asyncpg                       |
| ORM                  | SQLAlchemy 2.0 (async)                        |
| Миграции             | Alembic 1.13                                  |
| Конфигурация         | Pydantic Settings 2.5                         |
| Транскрибация        | faster-whisper 1.0 (large-v3)                 |
| HTTP-клиент          | httpx 0.27                                    |
| Retry                | tenacity 9.0                                  |
| Логирование          | loguru 0.7                                    |
| Мониторинг           | Flower 2.0                                    |
| Планировщик          | RedBeat 2.2                                   |
| Тестирование         | pytest 8.3 + pytest-asyncio + pytest-cov      |

## Структура проекта

```
integration-service/
├── app/
│   ├── api/               # FastAPI эндпоинты (вебхуки, management)
│   ├── config/            # Pydantic Settings (конфигурация)
│   ├── core/              # Database engine, async session factory
│   ├── models/            # SQLAlchemy модели (Task, TaskAttempt, Outbox)
│   ├── services/          # Бизнес-логика (GLPI, Bitrix24, MANGO)
│   └── worker/            # Celery задачи
├── alembic/               # Миграции БД
│   └── versions/          # Файлы миграций
├── config/                # employee_mapping.json
├── scripts/               # Вспомогательные скрипты
├── tests/                 # Тесты
├── docker-compose.yml     # Оркестрация 6 сервисов
├── Dockerfile             # Многостадийная сборка (builder → runtime)
├── requirements.txt       # Python-зависимости
└── .env.example           # Шаблон переменных окружения
```

## Лицензия

MIT License — см. файл [LICENSE](LICENSE).

Автор: **pbolkhovitin**
