# Обновление орг.структуры в GLPI (runbook)

Механизм зеркалирования орг.структуры Bitrix24 → GLPI и его повторные прогоны
(исправление структуры). Реализован в `app/services/org_sync.py`.

## Запуск

```
curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  http://localhost:8000/api/bitrix24/sync/org
```

Идемпотентный прогон. При каждом запуске:
- создаёт **недостающие** сущности,
- **переименовывает** существующие (матч по `org_department_map`),
- **пере-родительствует** (если отдел B24 сменил родителя),
- **деактивирует** (`is_active=0`) сущности, удалённые из Bitrix24,
- обновляет пользователей и карту `org_user_map`/`org_department_map`.

Ожидаемый ответ: `departments_created/updated/deactivated`, `users_*`, `errors`.

## Правила структуры (текущие)

| Элемент | GLPI |
|---|---|
| Корень B24 «АО «АПО «Аврора»» | `ORG_SYNC_ROOT_ENTITY_ID` (сейчас 130 — «АО «АПО «Аврора»») |
| Дети корня с «ООО » в названии | **top-level** (`ORG_SYNC_TOP_ENTITY_ID=0`), самостоятельные юр.лица холдинга |
| «Головное предприятие» (депт 34) | отдел **под АО** (администрация) |
| СП «…» и прочие | **под АО** |
| Глубокая вложенность | повторяет дерево B24 |

## Настройки (`.env`)

- `ORG_SYNC_ROOT_ENTITY_ID` — entity АО (top-level юр.лицо холдинга)
- `ORG_SYNC_TOP_ENTITY_ID` — GLPI system root (0), родитель для ООО
- `ORG_SYNC_USER_PROFILE_ID` — профиль создаваемых пользователей
- `BITRIX24_ORG_WEBHOOK_URL` — вебхук с правами user+department

## Верификация после прогона

```sql
-- Дерево (completename)
SELECT id, name, entities_id FROM glpi_entities ORDER BY entities_id, id;
-- Корень и ООО на верхнем уровне (должно быть АО + ООО, без чужих)
SELECT id, name FROM glpi_entities WHERE entities_id=0;
-- ООО НЕ должны быть детьми АО
SELECT COUNT(*) FROM glpi_entities WHERE entities_id=<АО> AND name LIKE 'ООО%';  -- → 0
```

## Как чинить структуру

1. **Перепрогнать org sync** — исправляет rename/re-parent/deactivate по карте:
   `POST /api/bitrix24/sync/org`
2. **Если есть дубли** (созданные прошлыми ошибочными прогонами под другим
   родителем и не попавшие в карту):
   - удалить лишние сущности синка в GLPI (кроме корня 0 и АО) —
     `DELETE FROM glpi_entities WHERE id NOT IN (0, <АО>)`,
   - очистить карту: `TRUNCATE org_department_map;` (БД интеграции),
   - перепрогнать org sync.
3. **ВАЖНО: не переименовывать сущности напрямую через SQL** — GLPI хранит
   кэш `completename`, который через SQL не пересчитается (структура в БД
   верна, но выгрузка/UI показывают старое полное имя). Все изменения —
   **только через API/org sync**. Если SQL-переименование уже сделано —
   восстановить `completename`: временно переименовать entity через SQL,
   затем вернуть имя через API `PUT /Entity/{id}` (GLPI пересчитает потомков).

## Примечания

- Права API-пользователя GLPI: **Super-Admin** (иначе «нет прав» на глубокие
  сущности; Admin в GLPI 11 не даёт CREATE на Entity).
- При ошибке `ERROR_GLPI_ADD`/«нет прав» во время прогона org sync сам
  пересоздаёт GLPI-сессию (кэш прав) и повторяет создание.
