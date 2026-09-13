# Маппинг полей задачи Bitrix24 → GLPI (дополнительные поля через плагин fields)

> Дата: 2026-09-13. Проект: integration-service (внешний сервис интеграции, плагин `bitrix` не используется).
> Задача: передавать задачу из B24 в GLPI оптимально, не ломая логику/архитектуру GLPI и сохраняя
> штатное обновление; мапить в доп. поля (плагин fields) только те поля, что не имеют нативного эквивалента;
> учесть механизм возврата обновлённого статуса/задачи в B24.

## 1. Контекст и ограничения

- Интеграция идёт через **внешний сервис** (этот проект) поверх GLPI REST API (Legacy API, пользователь
  `integration-api`, Super-Admin). Плагин `bitrix` **не используется**.
- Жёсткие правила: не трогать логику/схему GLPI через SQL, сохранить возможность **штатного обновления** GLPI.
- **Итог проверки плагинов (2026-09-13):** `fields` 1.24.4 — **установлен и активен** (state=1), GLPI 11.0.8;
  совместим (REST через input требует GLPI ≥11.0.2 + fields ≥1.23.3).

## 2. Что уже маппится в нативные поля GLPI (точное совпадение логики)

| Поле задачи B24 | Поле тикета GLPI | Комментарий |
|---|---|---|
| `TITLE` | `name` | |
| `DESCRIPTION` (тело проблемы) | `content` | через `extract_problem_description` (L1-шаблон отрезается) |
| `CREATED_DATE` | `date` | |
| `DEADLINE` | `time_to_resolve` | |
| `CLOSED_DATE` | `closedate` | |
| `PRIORITY` (1..4) | `priority` (1..5) | `PRIORITY_MAP` |
| `STATUS` (1..6) | `status` | `STATUS_MAP` |
| категория (классификатор) | `itilcategories_id` | `CATEGORY_KEYWORDS`, по имени → id |
| ID задачи | `externalid` | обратная привязка |
| `RESPONSIBLE_ID` | `Ticket_User` type=2 (assignee) | |
| постановщик | `Ticket_User` type=1 (requester) | |
| `AUDITORS` | `Ticket_User` type=3 (observer) | |
| тип запроса | `requesttypes_id=7`, `type=2` (Запрос) | |

→ Эти поля **не трогаем**: они и есть «маппинг только точных совпадений».

## 3. Поля B24 без нативного эквивалента в GLPI

`PARENT_ID` (родительская задача), `GROUP_ID` (группа/проект), ссылка на задачу (URL), теги,
маркеры из описания (`[b]Категория:[/b]`, `[b]Приоритет:[/b]`), кастомные `UF_*`, признак компании/отдела.
Для них два разумных места хранения: доп. поля плагина `fields` **и/или** собственная БД интеграции.

## 4. Плагин `fields` — что умеет (REST)

- **Запись**: кастомные поля передаются **в `input` вместе с нативными** при `POST/PUT /Ticket`
  (контейнер `dom` — автоопределение, без `c_id`; `tab`/`domtab` — с `c_id`).
- **Чтение**: `listSearchOptions/Ticket` (id кастомных полей **≥8100**), `searchItems?...&forcedisplay[]=8101`,
  либо прямой саб-айтем `GET /apirest.php/PluginFieldsTicket<container>/`.
- **Создание** контейнеров/полей — только через web-UI плагина (REST для создания НЕТ).
- **Ключ поля** (API key) выводится из label: lower-case → singular → только alnum → цифры→слова
  (`2`→`two`), суффикс `field`. Пример: `B24 URL` → `btwofoururlfield`. **Кириллица в ключе нежелательна**
  (не-ASCII ключ) → использовать латинские label или сверять ключи через `listSearchOptions`.
- **Штатное обновление**: данные в отдельных таблицах `glpi_plugin_fields_*` → обновление ядра GLPI их не трогает.
  Риски: зависимость от плагина (при его отключении данные недоступны), создание полей только вручную.

## 5. Варианты

### A. Только плагин `fields` (гипотеза пользователя)
Плюсы: данные видны/фильтруются в GLPI UI; REST-запись через input.
Минусы: дублирование с БД интеграции; зависимость от плагина; создание полей вручную; при штатном
обновлении/бекапе GLPI несёт доп. данные; риск для «не ломать GLPI» — минимальный, но есть операционный.

### B. Только БД интеграции (текущее состояние)
`tasks.payload` (JSONB) уже хранит **полный** task B24. GLPI остаётся 100% нативным; обновление/бекап GLPI
не затронуты; reverse-sync читает источник истины из своей БД. Минус: в GLPI UI не видно B24-специфику
(кроме того, что уже в `content`/L1-шаблоне).

### C. Гибрид (рекомендуется)
Нативные поля (п.2) + **ограниченный набор** display/фильтр-полей через `fields` (для работы оператора в GLPI)
+ полный payload в БД интеграции (источник истины для reverse-sync).

## 6. Рекомендуемое решение (C)

**Один `dom`-контейнер «Bitrix24» на тикет** с текстовыми полями (латинские label, значение = ссылка/маркер):

| Поле плагина (label) | Ключ (пример) | Значение из B24 |
|---|---|---|
| B24 URL | `btwofoururlfield` | ссылка на задачу |
| B24 Status | `btwofourstatusfield` | статус B24 (1..6) |
| B24 Priority | `btwofourpriorityfield` | маркер `Приоритет:` (Высокий/Средний/…) |
| B24 Category | `btwofourcategoryfield` | маркер `Категория:` из описания |
| B24 Parent | `btwofourparentfield` | `PARENT_ID` |
| B24 Group | `btwofourgroupfield` | `GROUP_ID` |

Точные ключи сверять после создания через `GET /apirest.php/listSearchOptions/Ticket`.
Настройка имён ключей — в `settings.py` (словарь `BITRIX24_FIELDS_KEYS`, по умолчанию пустой = выкл.).

**Обратная синхронизация (возврат в B24)** — уже реализована (`reverse_sync.py`):
- статус: `tasks.task.update` (mapped из GLPI status) — при отсутствии времени пишется min (60с) ДО статуса;
- комментарии: `im.message.add` в чат задачи (fallback — description);
- время: `elapseditem.add` (дельта);
- L1-шаблон с `Категория:`/`Приоритет:` возвращается в `DESCRIPTION`.
Полный payload для запросов в B24 — из `tasks.payload` (БД интеграции), не из GLPI.

## 7. Перенос анализа маркеров из проекта отчёта (bitrix24-add-report)

В описаниях B24 встречаются BB-маркеры. Парсер (проверен на проде отчёта: 299 задач с «Категория:»,
значения не совпадают 1:1 с категориями):

```python
_CATEGORY_MARKER_RE = re.compile(r'Категор\w*\s*:?\s*\[?/?b\]?\s*([^\n\[\]]+)', re.IGNORECASE)
```
- значение «Категория:» → **приоритет над классификатором** (`classify_category`), через словарь алиасов
  (пример из отчёта: `1с`→«1С…», `рабочее место`→«Настройка ПК», `эцп`/`мчд`→ЭДО, `телефония`→IP-телефония…);
- значение «Приоритет:» → маппинг Высокий/Средний/Низкий → GLPI `priority` (приоритет над `PRIORITY`).

## 8. Шаги реализации

1. В GLPI UI: создать контейнер `fields` «Bitrix24» (тип `dom`, itemtype `Ticket`) + 6 текстовых полей.
2. Сверить ключи полей через `listSearchOptions/Ticket`; занести в `.env`/`settings`.
3. `create_ticket` в `poller.py`: добавить ключи в `input`.
4. `ticket_mapper.py`: `extract_b24_markers(description)` + словарь алиасов + приоритет маркера над классификатором/приоритетом.
5. Reverse-sync: при возврате статуса/задачи в B24 учитывать маркеры (в L1-шаблоне) — без изменений в GLPI.
6. Тест на тест-задаче (whitelist) + `POST /sync/trigger`.

## 9. Реализовано (2026-09-13, веб-UI GLPI)

**Контейнер** (id=1): label `Bitrix24`, внутреннее имя `bitrixtwofour` (по правилу «цифры→слова»),
тип `dom`, itemtype `Ticket`, активен, `entities_id=0` (рекурсивно).

**Поля** (id 1..6, активны, container=1):

| id | Ключ (API) | Label | Тип | Назначение |
|---|---|---|---|---|
| 1 | `btwofoururlfield` | B24 URL | `url` | ссылка на задачу (кликабельная, target=_blank) |
| 2 | `btwofourstatusfield` | B24 Status | `text` | статус B24 (1..6) |
| 3 | `btwofourpriorityfield` | B24 Priority | `text` | маркер `Приоритет:` |
| 4 | `btwofourcategoryfield` | B24 Category | `text` | маркер `Категория:` |
| 5 | `btwofourparentfield` | B24 Parent | `text` | `PARENT_ID` |
| 6 | `btwofourgroupfield` | B24 Group | `text` | `GROUP_ID` |

> Примечание: B24 URL изначально был создан как `text` (не кликабельный); тип изменён на `url`
> напрямую в `glpi_plugin_fields_fields` (тип не редактируется в форме поля) + регенерация
> `php bin/console plugins:fields:regenerate_files`. Ключ и данные не изменились.

**Права (таблица `glpi_plugin_fields_profiles`, container=1):** Super-Admin → 4 (Запись),
Admin → 1 (Чтение), Technician → 1 (Чтение); остальные → 4 (дефолт `createForContainer`).

**Проверено через REST (integration-api):**
- `listSearchOptions/Ticket` → опции полей на id **76666–76671**, таблица
  `glpi_plugin_fields_ticketbitrixtwofours`.
- `POST /Ticket` c ключами в `input` (без `c_id`, dom-автоопределение) → тикет создан (201),
  значения записаны в JOIN-таблицу (`items_id`, `btwofour*`). **ВАЖНО:** `GET /Ticket/{id}` НЕ
  возвращает значения плагина (они в отдельной таблице) — читать через
  `searchItems?...&forcedisplay[]=76666` или `GET /PluginFieldsTicketbitrixtwofours/?searchText[items_id]=X`.
- Права плагина на контейнер проверяются в `PluginFieldsContainer::preItem`; API-сессии без
  активного профиля проверку пропускают (обработка в хуках `pre_item_add`/`item_add`).

**Ограничения/нюансы:** поле «Добавить поле» создаётся только через AJAX-форму на вкладке
«Поля» (прямой `field.form.php` даёт 403 — у `PluginFieldsField` пустой `$rightname`, а
`CommonGLPI::canCreate()` в GLPI 11 при пустом rightname возвращает false).

## 10. Что НЕ делать
- Не создавать/переименовывать категории и сущности через SQL (кэш `completename`).
- Не писать в таблицы плагина `fields` напрямую (только через REST `input`/UI).
- Не расширять контейнер полей произвольно — только набор из п.6 (остальное живёт в `tasks.payload`).