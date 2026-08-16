# PROJECT CONTEXT — Integration Service (Bitrix24 ↔ GLPI ↔ MANGO)

> Компактный файл контекста: текущее состояние, решения, активные задачи.
> Обновлять при завершении/смене этапов. Подробности — в указанных файлах.

## Что это
FastAPI-сервис интеграции **Bitrix24** (задачи) → **GLPI** (тикеты/ITSM) + обратная
синхронизация (GLPI → Bitrix24 по L1-шаблону). Планируется MANGO (звонки),
активы NetBox→GLPI, Zabbix (мониторинг), SLA-отчёт в Metabase.

## Стек
FastAPI + APScheduler 3.10.4 + SQLAlchemy 2.0 (asyncpg) + PostgreSQL 15 + httpx (sync) + pytest.
GLPI 11.0.8 (Docker), NetBox 4.5.7, Zabbix, MariaDB, Redis.

## Доступ (VPN)
- Ноутбук → VPN **signal.lc** (NM, `172.15.0.0/24`), маршрут `172.17.0.0/16` через VPN (metric 200,
  при прямом подключении к 172.17 VPN не мешает). DNS `*.ais.local` через systemd-resolved.
- **signal-glpi**: `ssh signal-glpi` (root@172.17.231.49) — сервер всего стека.
- **GLPI web**: http://glpi.ais.local (или http://172.17.231.49:8080)
- **NetBox**: https://netbox.signal.lc (172.17.231.21) — источник истины активов (read-only)
- **Zabbix**: https://zbx.signal.lc (172.17.231.21)

## Ключевые файлы
- `docs/superpowers/plans/2026-08-15-phase1-5-mapping-and-sla.md` — план Phase 1.5 (задачи/решения/статус)
- `docs/org-structure-update.md` — runbook обновления орг.структуры GLPI
- `docs/netbox-naming-conventions.md` — нейминг NetBox (для утверждения)
- `app/services/ticket_mapper.py` — маппинг/классификатор/L1-шаблон
- `app/services/org_sync.py` — орг-структура/пользователи по ID
- `app/services/reverse_sync.py` — L1-writeback/статусы/время/чат
- `app/services/comment_mirror.py` — зеркалирование чата B24→GLPI
- `app/services/poller.py` — поллер/маппинг тикетов

## Справочники API (официальные)
**Bitrix24:**
- MCP/инструменты: https://apidocs.bitrix24.ru/ai-tools/mcp.html
- API Reference: https://apidocs.bitrix24.ru/api-reference/index.html

**GLPI:**
- Общая документация API: https://help.glpi-project.org/documentation/modules/configuration/general/api/api
- REST API v2: https://help.glpi-project.org/documentation/modules/configuration/general/api/restful-api-v2
- Developer API: https://glpi-developer-documentation.readthedocs.io/en/master/devapi/index.html
- Разработка плагинов: https://glpi-developer-documentation.readthedocs.io/en/master/plugins/index.html

## Текущий статус (2026-08-16)
- **Phase A реализована, в отладке/тестировании.** Маппинг тикета, org sync, L1-writeback
  работают end-to-end на сервере (проверено на тест-задачах 35591/35633 + 755 задач с 01.05).
- GLPI: чистый реинсталл 11.0.8, плагины (Fields/Tag/Datainjection/Mreporting/Metabase),
  категории «Запрос»(15 сервисных)/«Инцидент», орг-структура (АО + ООО top-level).
- Классификатор: «Другое» **10%** (было 48%) на 755 задачах.
- VPN/доступ настроены.

## Активные задачи (в работе)
1. **Отладка Phase A** (текущее): маппинг, L1-шаблон, классификатор, орг-структура.
2. Верификация классификатора/категорий на широкой базе.
3. Дальше (по плану): **Фаза D** — активы NetBox→GLPI + аудит GLPI↔NetBox; **Zabbix**
   (media type + netbox-zabbix-sync); **Фаза C** — SLA + Metabase-дашборд; полная загрузка.

## Эндпоинты интеграции (X-Admin-Token на POST)
- `GET /health`, `GET /api/bitrix24/sync/status`
- `POST /sync/trigger` — поллер; `POST /sync/cleanup` — orphan; `POST /sync/retry`
- `POST /sync/org` — **обновление орг.структуры** (идемпотентно)
- `POST /sync/reverse-test` — L1-writeback (обрабатывает **полный whitelist**: env + runtime)
- `GET/POST/DELETE /sync/test-tasks` — whitelist тест-задач

## Тестовые задачи (whitelist записи)
- База: env `TEST_TASK_IDS` (35591, 35633) + runtime `bitrix_test_tasks`.
- **Автодобавление**: задачи с ключевым словом `Test_GLPI` в title автоматически
  добавляются в whitelist при поллинге (`BITRIX24_AUTO_WHITELIST_KEYWORD`, пусто = off).
- Reverse sync обрабатывает весь whitelist (env + runtime).
- **Только разрешённые пользователи** для тест-задач: Болховитин (577), Техподдержка ИТ (70),
  Гриднев (445), Ушков (545) — `BITRIX24_TEST_TASK_USER_IDS`. Уваркин (172) и другие исключены
  из requester/assignee/observers тест-тикетов.

## Чат задач B24 ↔ GLPI (двусторонний)
- Чат задачи живёт в IM-модуле; задача ссылается на него через `chatId`. Требуется
  вебхук со скоупом `im` — `BITRIX24_IM_WEBHOOK_URL` (fallback org webhook).
- **B24 → GLPI**: `im.v2.Chat.Message.list` (chatId) → зеркалирование в GLPI followup
  (`mirrored_followups` — защита от петель). Первый прогон — только базовая линия
  (`last_b24_comment_id`), история не воспроизводится. Системные сообщения
  (`[USER=..] стал наблюдателем`) фильтруются.
- **GLPI → B24**: followup → `im.message.add` (CHAT_ID+MESSAGE) в чат задачи; нет чата → fallback
  в DESCRIPTION. Зеркальные followup не возвращаются (loop protection).
- **Ограничено тест-задачами** в текущей фазе.

## Настройки (.env, key)
`BITRIX24_WEBHOOK_URL`, `BITRIX24_ORG_WEBHOOK_URL` (права user+department),
`BITRIX24_SYNC_LOOKBACK_DAYS` (сейчас 108), `INCLUDE_CLOSED_TASKS=true`,
`ORG_SYNC_ROOT_ENTITY_ID=130` (АО), `ORG_SYNC_TOP_ENTITY_ID=0`,
`L1_MIN_ELAPSED_SECONDS=60`, `BITRIX24_CREATE_TASKS_ENABLED=false` (dev),
`GLPI_APP_TOKEN/USER_TOKEN` (integration-api, Super-Admin), `ADMIN_API_TOKEN`.

## Известные ограничения / проблемы
- **Bitrix24 list-фильтры диапазонов не работают** (только равенство) → окно фильтруется клиентски.
- **GLPI API пагинация**: list-эндпоинты (Entity/ITILCategory/User) возвращают только первую
  страницу → в клиенте использовать range-пагинацию (`get_categories`/`get_entities`/`get_user_emails`).
- **GLPI 11 создаёт тикет с requester=session-user** (игнорирует `_users_id_requester`) →
  акторы назначаются через `Ticket_User` (requester=1, assignee=2, observer=3).
- **Профиль Admin в GLPI 11 не даёт CREATE на Entity** → интеграция использует **Super-Admin**.
- **GLPI кэш `completename`** не пересчитывается при SQL-переименованиях → только через API;
  если сломано — процедура в `docs/org-structure-update.md`.
- «Описание проблемы» в L1 = контент тикета (сырое описание B24) — возможна чистка.
- 13 предсуществующих тестов сломаны (test_glpi/test_settings/test_webhook_bitrix) — вне плана.

## Как проверить после деплоя
1. `ssh signal-glpi && cd /opt/integration-service && git pull && docker compose up -d --build api`
2. `docker exec integration-api alembic upgrade head`
3. `POST /sync/org` → структура; `POST /sync/trigger` → тикеты; `POST /sync/reverse-test` → L1
4. Проверка в GLPI: тикет (externalid=номер B24, category, requester/observer, dates)
