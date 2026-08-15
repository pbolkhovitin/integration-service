# Harden Integration Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate event-loop blocking, incorrect status filtering, task races/staleness, unauthenticated mutating endpoints, and medium issues (scheduled reverse sync, GLPI session lifecycle, dead config, code duplication).

**Architecture:** Minimal changes to existing files (no async-httpx/Celery migration). TDD with frequent commits. Verified by `pytest`, `ruff`, `mypy`.

**Tech Stack:** FastAPI, APScheduler, SQLAlchemy 2.0 async, asyncpg, httpx (sync), pytest.

---

### Task 1: Unblock the event loop (Bitrix calls via `asyncio.to_thread`)

**Files:** `app/services/poller.py:116`, `:211`, `:452`; test `tests/test_poller.py`

- [x] **1.1** Write test: mock `app.services.poller.asyncio.to_thread` (side_effect records calls) and assert `get_tasks`/`get_task_tags` are only invoked through `to_thread`.
- [x] **1.2** Run test → FAIL.
- [x] **1.3** Wrap the calls:

```python
# _poll_for_user (~116) and cleanup_orphaned_tasks (~452)
page = await asyncio.to_thread(
    bitrix_client.get_tasks, responsible_id=responsible_id, start=start
)

# _process_task (~211)
tags = await asyncio.to_thread(bitrix_client.get_task_tags, int(task_id))
```

- [x] **1.4** Run test → PASS; no regressions (`patch_to_thread` fixture in `conftest.py` already patches the poller module).
- [x] **1.5** Commit: `fix: run Bitrix sync HTTP calls in asyncio.to_thread`

---

### Task 2: Fix the Bitrix24 status filter

**Files:** `app/services/poller.py:188-191`; test `tests/test_poller.py`

- [x] **2.1** Test for the new helper (skip only 4,5,6,7):

```python
def test_skip_statuses():
    for st in (4, 5, 6, 7):
        assert _is_skipped_bitrix_status(st) is True
    for st in (1, 2, 3):
        assert _is_skipped_bitrix_status(st) is False
```

- [x] **2.2** Run → FAIL.
- [x] **2.3** Add to poller:

```python
# Closed/inactive Bitrix24 statuses: 4 — awaiting control, 5 — completed,
# 6/7 — deferred. 1 (new), 2, 3 (in progress) — processed.
_SKIPPED_BITRIX_STATUSES = {4, 5, 6, 7}


def _is_skipped_bitrix_status(status) -> bool:
    return status in _SKIPPED_BITRIX_STATUSES
```

Replace `if status in (3, 5):` with `if _is_skipped_bitrix_status(status):`.
- [x] **2.4** Run → PASS.
- [x] **2.5** Commit: `fix: skip only closed Bitrix24 statuses (4,5,6,7)`

---

### Task 3: Race (IntegrityError) + retry + retry-endpoint

**Files:** `app/services/poller.py:193-275`, `app/api/bitrix.py`, `app/config/settings.py`; test `tests/test_poller.py`

- [x] **3.1** Tests `_should_retry_task`: `completed`/`cancelled` → skip; `failed` with `attempts < max_attempts` → retry; `failed` with `attempts >= max_attempts` → skip; `processing` fresh → skip; `processing` older than threshold → retry.
- [x] **3.2** Run → FAIL.
- [x] **3.3** Implementation (threshold derived from interval; heartbeat = `processing` write refreshes `updated_at`):

```python
def _stale_processing_seconds() -> int:
    # Threshold > page processing time (GLPI timeout 30s),
    # but no less than 2 poll intervals.
    return max(2 * settings.BITRIX24_POLL_INTERVAL_SECONDS, 60)

def _should_retry_task(task: Task, now=None) -> bool:
    if task.status in ("completed", "cancelled"):
        return False
    if task.status == "failed":
        return task.attempts < task.max_attempts
    if task.status == "processing":
        now = now or datetime.now(timezone.utc)
        updated = task.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (now - updated).total_seconds() > _stale_processing_seconds()
    return True  # pending and others
```

In `_process_task`:
- existing task branch: `if existing is not None: if not _should_retry_task(existing): return "skipped"`; on retry `existing.attempts += 1; existing.status = "processing"; existing.last_error = None; await db.commit()` (heartbeat).
- INSERT wrapped:

```python
from sqlalchemy.exc import IntegrityError
...
try:
    db.add(task)
    await db.commit()
except IntegrityError:
    logger.info("Task %s already exists (concurrent poll) — skipping", task_id)
    return "skipped"
```

- GLPI error: `task.attempts += 1; task.status = "failed"; task.last_error = str(exc)`.
- [x] **3.4** Test retry-endpoint: `POST /api/bitrix24/sync/retry` (with `X-Admin-Token`) requeues `failed` tasks (`attempts < max_attempts`) to `pending`, returns counter; without token → 401. Run → FAIL.
- [x] **3.5** Implementation in `app/services/poller.py` + `app/api/bitrix.py`:

```python
# poller.py
async def retry_failed_tasks() -> dict:
    async with async_session_factory() as db:
        result = await db.execute(
            select(Task).where(Task.source == "bitrix24", Task.status == "failed")
        )
        tasks = result.scalars().all()
        requeued = 0
        for t in tasks:
            if t.attempts < t.max_attempts:
                t.status = "pending"
                t.last_error = None
                requeued += 1
        await db.commit()
        return {"requeued": requeued, "failed_total": len(tasks)}

# api/bitrix.py
@router.post("/sync/retry", dependencies=[Depends(require_admin_token)])
async def sync_retry() -> dict:
    return await retry_failed_tasks()
```

- [x] **3.6** Run full `pytest` → PASS.
- [x] **3.7** Commit: `fix: IntegrityError race, retry failed/stale tasks, add /sync/retry`

---

### Task 4: Conditional CORS + auth on mutating endpoints

**Files:** `app/config/settings.py`, `app/main.py:45-55`, `app/api/bitrix.py`, `.env.example`, `tests/test_main.py`, `tests/test_settings.py`, README

- [x] **4.1** Tests: CORS middleware present iff `CORS_ORIGINS` non-empty; 401 on missing/wrong `X-Admin-Token` and 200 with correct for `POST /sync/trigger`/`/sync/cleanup`/`/sync/reverse-test`.
- [x] **4.2** Run → FAIL.
- [x] **4.3** Implementation:

```python
# settings.py
CORS_ORIGINS: str = ""          # comma-separated; empty = CORS disabled
ADMIN_API_TOKEN: SecretStr = SecretStr("")

@property
def cors_origins(self) -> list[str]:
    return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
```

```python
# main.py — register middleware only when the list is non-empty
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
```

```python
# api/bitrix.py — dependency + apply to mutating endpoints
from fastapi import Header, HTTPException, Depends

def require_admin_token(x_admin_token: str = Header(default="")) -> None:
    expected = settings.ADMIN_API_TOKEN.get_secret_value()
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")
```

Apply `dependencies=[Depends(require_admin_token)]` to `sync_trigger`, `sync_cleanup`, `sync_retry`, `reverse_sync_trigger`. Update the CORS test in `test_main.py`.
- [x] **4.4** Run → PASS.
- [x] **4.5** Commit: `security: conditional CORS and admin-token auth on mutating sync endpoints`

---

### Task 5: Reverse sync on schedule

**Files:** `app/services/poller.py`, `app/config/settings.py`, README

- [x] **5.1** Test: with `TEST_MODE=True` and non-empty `test_task_ids`, `start_poller()` registers job `bitrix24_reverse_sync`; `get_poller_status()` returns both entries.
- [x] **5.2** Run → FAIL.
- [x] **5.3** Implementation:

```python
# settings.py
BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS: int = 60

# poller.py
from app.services.reverse_sync import reverse_sync_test_tasks

# in start_poller(), after the main job:
if settings.TEST_MODE and settings.test_task_ids:
    _scheduler.add_job(
        reverse_sync_test_tasks,
        "interval",
        seconds=settings.BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS,
        id="bitrix24_reverse_sync",
        name="Bitrix24 Reverse Sync (GLPI -> Bitrix24)",
        max_instances=1,
    )
```

`get_poller_status()`: select job by `id`, return `next_run` for each.
- [x] **5.4** Run → PASS.
- [x] **5.5** Commit: `feat: schedule GLPI->Bitrix24 reverse sync in poller`

---

### Task 6: GLPI session lifecycle + default ticket fields

**Files:** `app/services/glpi.py`, `app/services/poller.py`, `app/services/reverse_sync.py`, `tests/test_glpi.py`

- [x] **6.1** Tests: `kill_session` sends `DELETE /apirest.php/killSession` with `Session-Token`; `create_ticket` includes `categories_id/groups_id/entities_id` when passed.
- [x] **6.2** Run → FAIL.
- [x] **6.3** Implementation in glpi.py:

```python
def kill_session(self, session_token: str) -> Any:
    url = f"{self._base_url}/apirest.php/killSession"
    return self._call(method="DELETE", url=url, session_token=session_token)

def create_ticket(self, name, content, session_token,
                  category_id=None, group_id=None, entity_id=None) -> dict:
    ticket = {"name": name, "content": content, "type": 1}
    if category_id is not None: ticket["categories_id"] = category_id
    if group_id is not None: ticket["groups_id"] = group_id
    if entity_id is not None: ticket["entities_id"] = entity_id
    payload = {"input": [ticket]}
    ...
```

Call `kill_session(glpi_session)` in `finally` of poll cycle (`_poll_bitrix24`, `cleanup_orphaned_tasks`) and `reverse_sync_test_tasks`. Extend `create_ticket` call in `_process_task` with `category_id/group_id/entity_id` from `settings.GLPI_DEFAULT_*`.
- [x] **6.4** Run → PASS.
- [x] **6.5** Commit: `feat: kill GLPI sessions and apply default category/group/entity`

---

### Task 7: Deduplicate cleanup logic

**Files:** `app/services/poller.py`

- [x] **7.1** Extract helpers and reuse in `_reconcile_deletions` and `cleanup_orphaned_tasks`:

```python
async def _fetch_all_bitrix_task_ids(bitrix_client) -> set[str]: ...
async def _close_orphan_tickets(glpi_client, glpi_session, orphans) -> int: ...
```

- [x] **7.2** Run full `pytest` → PASS (behavior unchanged).
- [x] **7.3** Commit: `refactor: deduplicate orphan-fetch/close logic`

---

### Task 8: README + .env.example

**Files:** `README.md`, `.env.example`

- [x] **8.1** Add `CORS_ORIGINS`, `ADMIN_API_TOKEN`, `BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS`.
- [x] **8.2** Update curl examples (`-H "X-Admin-Token: ..."` for POST), statuses (skip 4,5,6,7), scheduled reverse sync, `/sync/retry` endpoint, CORS behavior.
- [x] **8.3** Commit: `docs: document new env vars, auth, retry endpoint, schedules`

---

### Task 9: Explicit unique constraint on (source, source_id)

**Files:** new migration `alembic/versions/20260815_120000_add_source_source_id_unique.py`, `app/models/task.py`; test `tests/test_task_model_v2.py`

- [x] **9.1** Test: Task model declares `__table_args__` with `UniqueConstraint("source", "source_id")`; migration contains `create_index` + dedup.
- [x] **9.2** Run → FAIL.
- [x] **9.3** Migration (dedup + unique index):

```python
def upgrade() -> None:
    # Deduplicate (source, source_id): keep completed, otherwise oldest.
    op.execute(
        """
        DELETE FROM tasks a USING tasks b
        WHERE a.source = b.source AND a.source_id = b.source_id
          AND (a.id > b.id OR (a.id < b.id AND a.status = 'completed'
               AND b.status <> 'completed'))
          AND (a.status = 'completed' OR b.status = 'completed' OR a.id > b.id)
        """
    )
    op.create_index(
        "ix_tasks_source_source_id", "tasks",
        ["source", "source_id"], unique=True,
    )

def downgrade() -> None:
    op.drop_index("ix_tasks_source_source_id", table_name="tasks")
```

In `app/models/task.py` add to the model:

```python
__table_args__ = (
    UniqueConstraint("source", "source_id", name="ix_tasks_source_source_id"),
)
```

- [x] **9.4** Run `pytest tests/test_task_model_v2.py` → PASS.
- [x] **9.5** Commit: `feat: enforce (source, source_id) uniqueness with dedup migration`

---

### Final verification

- [x] `pytest` — all green
- [x] `ruff check app tests`
- [x] `mypy app` (signatures `to_thread`, `kill_session`, `_should_retry_task`)
- [x] `alembic upgrade head` / `alembic downgrade -1` — migration 9.3 works on a DB
- [x] Final commit if needed

---

### Risks closed (from v1)
| Risk v1 | Resolution in v2 |
|---|---|
| CORS-`*` / empty list breaks browser | CORS middleware registered only when `CORS_ORIGINS` non-empty (Task 4) |
| `failed` after exhausting `max_attempts` is a dead end | `POST /sync/retry` for manual/periodic reprocessing (Task 3) |
| 5-minute staleness threshold | Threshold = `max(2×poll_interval, 60)`; heartbeat via `processing` write (Task 3) |
| Uniqueness implicit (idempotency_key) | Explicit unique index `(source, source_id)` + dedup migration (Task 9) |

---

## Post-plan update (2026-08-15)

После выполнения плана уточнены требования к записи в Bitrix24:

- **`BITRIX24_REVERSE_SYNC_ENABLED`** (Task 5) сначала был сделан default `false`
  (запрет любых автоматических записей). Затем по требованию пользователя
  **возвращён default `true`**, но с **жёсткой whitelist-защитой**:
  `reverse_sync._sync_one_task` отказывается писать в любую задачу вне
  `TEST_TASK_IDS` (счётчик `skipped_not_whitelisted`).
- Тестовые задачи Bitrix24 `TEST_TASK_IDS=[35591, 35633]` — разрешённая
  площадка для записи (тестирование/отладка); в прод-задачи запись
  технически невозможна.
- Статус-эндпоинты теперь отдают `reverse_sync.enabled`/`auto_enabled` и
  `auto_write_enabled`.