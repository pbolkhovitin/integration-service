# AGENTS.md — Integration Service (Bitrix24 ↔ GLPI ↔ MANGO)

> Инструкция для агента. **Сначала прочитай `docs/project-context.md`** (компактный
> статус/решения/активные задачи). Детали — по ссылкам ниже, точечно по задаче.

## Проект
FastAPI-сервис интеграции Bitrix24 (задачи) → GLPI (тикеты/ITSM) + обратная
синхронизация (L1-шаблон, статусы, комментарии, учёт времени). Дальше: активы
NetBox→GLPI, Zabbix, SLA/Metabase, MANGO.

## Доступ
- Сервер: `ssh signal-glpi` (root@172.17.231.49, весь стек). Без VPN-маршрута
  (172.17.0.0/16 через VPN signal.lc) сервер недоступен.
- GLPI: http://glpi.ais.local (web) / 172.17.231.49:8080. NetBox/Zabbix: netbox/zbx.signal.lc.

## Критические правила
1. **Запись в Bitrix24 — ТОЛЬКО whitelist-задачи** (35591, 35633 + runtime через
   `POST /sync/test-tasks`). Всё остальное в B24 — read-only. Проверка в коде
   (`allowed_test_task_ids`).
2. **Деплой**: на сервере `cd /opt/integration-service && git pull && docker compose up -d --build api` +
   `docker exec integration-api alembic upgrade head`. НЕ коммитить/пушить без явного запроса.
3. **Не переименовывать сущности/категории GLPI через SQL** — ломает кэш `completename`
   (только через API/org sync; восстановление — `docs/org-structure-update.md`).
4. **GLPI API list-эндпоинты пагинированы** (range): `get_categories`/`get_entities`/
   `get_user_emails` — НЕ запрашивать без range (возвращает только первую страницу).
5. **Акторы тикета** (requester/assignee/observer) назначаются через `Ticket_User`
   (type 1/2/3) — GLPI 11 игнорирует `_users_id_requester` при создании.
6. Интеграция работает под GLPI-пользователем **integration-api (Super-Admin)** —
   Admin в GLPI 11 не даёт CREATE на Entity.
7. Bitrix24 list-фильтры диапазонов не работают (только равенство) → окно задач
   фильтруется клиентски (BITRIX24_SYNC_LOOKBACK_DAYS).
8. **Чат задач B24** живёт в IM-модуле (задача ссылается через `chatId`), НЕ в форуме
   (`forumTopicId`). Чтение `im.v2.Chat.Message.list`, запись `im.message.add` — нужен
   **im-скоуп** (`BITRIX24_IM_WEBHOOK_URL`, fallback org webhook).
9. **Тест-задачи: только разрешённые пользователи** — Болховитин (577), Техподдержка ИТ (70),
   Гриднев (445), Ушков (545). Уваркин (172) и другие — исключены
   (`BITRIX24_TEST_TASK_USER_IDS`).

## Что читать
- **Первым**: `docs/project-context.md` (статус, активные задачи, ограничения, эндпоинты).
- По задаче: `docs/superpowers/plans/2026-08-15-phase1-5-mapping-and-sla.md` (план/решения),
  `docs/org-structure-update.md` (орг), `docs/netbox-naming-conventions.md` (активы),
  `docs/superpowers/plans/2026-08-15-harden-integration-service.md` (прошлый план).
- Код: `app/services/ticket_mapper.py`, `org_sync.py`, `reverse_sync.py`, `poller.py`.

## Команды
- Тесты (локально, venv `/tmp/opencode/integration-venv`):
  `DATABASE_URL='postgresql+asyncpg://test:test@localhost:5432/test' python -m pytest -q --ignore=tests/test_bitrix.py`
  (13 failed + 5 errors — предсуществующие, вне плана).
- Проверка на проде: `POST /api/bitrix24/sync/org` (структура), `POST /sync/trigger` (тикеты),
  `POST /sync/reverse-test` (L1) — с `X-Admin-Token`.
