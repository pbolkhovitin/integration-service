# Маппинг Bitrix24 → GLPI (расширенный) + SLA-отчёт в GLPI. Phase 1.5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Максимально полно переносить задачи Bitrix24 в тикеты GLPI (пользователи, оргструктура, даты, категории, SLA-данные) и реализовать в GLPI SLA-отчёт, аналогичный `/home/pbolk/bitrix24-add-report`, через нестандартные решения в GLPI-плагинах (сохраняя возможность обновления GLPI).

**Architecture:** Нестандартные данные — в GLPI-плагинах (не в ядре). Стандартные — в поля ядра тикета через API. Маппинг пользователей/отделов — через таблицы соответствий в БД интеграционного сервиса, заполняемые org sync. SLA — нативные SLA GLPI + плагин для специфичных метрик отчёта.

**Tech Stack:** FastAPI (интеграционный сервис), GLPI REST API, GLPI-плагины (PHP, GLPI 10.x), pytest, MariaDB.

---

## 1. Текущее состояние (анализ)

Сейчас тикет GLPI создаётся так (`app/services/poller.py:273-303`):
- `name = "[Bitrix24 #{task_id}] {title}"`
- `content = _build_ticket_content(task_data)` — **плоский текст**: ID, Title, Created, Deadline, Responsible ID, Created by ID, Tags, Description.
- `category_id/group_id/entity_id` — из `GLPI_DEFAULT_*` (константы, одинаковые для всех).

**Gap-анализ (чего нет):**

| Данные Bitrix24 | Сейчас | Нужно |
|---|---|---|
| Постановщик `CREATED_BY` | только ID текстом | → requester тикета (`glpi_tickets_users` role 1) |
| Ответственный `RESPONSIBLE_ID` | только ID текстом | → техник/assignee (`glpi_tickets_users` role 2) |
| Дата создания `CREATED_DATE` | текстом | → поле `date` тикета |
| Дедлайн `DEADLINE` | текстом | → `time_to_resolve` (SLA) |
| Дата закрытия `CLOSED_DATE` | нет | → `closedate` / `solvedate` |
| Статус `STATUS` (1-7) | текстом (status пропускается при 4-7) | → статус тикета (маппинг) |
| Приоритет `PRIORITY` (1-4) | нет | → `priority` тикета |
| Категория (дериват) | нет | → `itilcategories_id` (через keyword-классификатор) |
| Теги `TAGS` | текстом в content | → теги тикета / поле плагина |
| Группа `GROUP_ID` | нет | → assign-группа |
| Комментарии B24 | нет | → followup'ы при создании |
| Трудозатраты `elapsed`/`DURATION_FACT_SECONDS` | нет | → поле плагина |
| История статусов | нет | → history GLPI + поле плагина (b24_status) |
| `externalid` | нет | → поле `externalid` ядра тикета (ID задачи B24) |
| Признак клиента (XLSX) | нет | → поле плагина (для SLA-отчёта) |

## 2. Цели этапа (Phase 1.5)

1. Полный маппинг полей задачи Bitrix24 → тикет GLPI (ядро + плагин).
2. Соответствия пользователей/отделов B24↔GLPI (используя org sync) и назначение requester/tech/entity.
3. Перенос комментариев и тегов B24.
4. Категоризация задач в категории GLPI (keyword-классификатор из b24-add-report).
5. Настройка SLA GLPI по приоритетам.
6. SLA-отчёт в GLPI (аналог b24-add-report): нестандартные метрики — в плагине.
7. Вся нестандартная логика — в GLPI-плагинах (обновление GLPI без конфликтов).

## 3. Целевой маппинг полей

### 3.1 Ядро тикета GLPI (стандартные поля, переживают обновления)

| Bitrix24 поле | GLPI поле | Примечание |
|---|---|---|
| `ID` | `externalid` | ИД задачи B24. Имя тикета: `[Bitrix24 #{ID}] {TITLE}` (для видимости) |
| `TITLE` | `name` | Название тикета |
| `DESCRIPTION` | `content` | Чистый текст описания (без служебных заголовков) |
| `CREATED_DATE` | `date` | Дата создания тикета = дата задачи |
| `CLOSED_DATE` | `closedate`, `solvedate` | Если задача закрыта в B24 |
| `DEADLINE` | `time_to_resolve` | Дедлайн → резолв-тайм (SLA) |
| `PRIORITY` | `priority` | Маппинг 1→1, 2→3, 3→4, 4→5 (настраиваемо) |
| `STATUS` | `status` | Маппинг (см. 3.3) |
| Категория (дериват) | `itilcategories_id` | keyword-классификатор → категория GLPI |
| `CREATED_BY` | requester | `glpi_tickets_users` role 1 (через org-маппинг) |
| `RESPONSIBLE_ID` | assignee | `glpi_tickets_users` role 2 (через org-маппинг) |
| `GROUP_ID` | assign group | `glpi_groups_tickets` (через группу из плагина/маппинга) |
| `UF_DEPARTMENT` | `entities_id` | entity = орг-маппинг отдела (org sync) |
| `COMMENTS` | followup'ы | `glpi_itilfollowups` при создании |

### 3.2 Поля плагина `b24fields` (нет в ядре — нестандартные)

| Bitrix24 | Поле плагина | Тип | Назначение |
|---|---|---|---|
| `ID` | `b24_task_id` | int | Дублирование для надёжности (если externalid не используется) |
| `STATUS` (raw) | `b24_status` | int | Исходный статус B24 (для SLA-анализа) |
| `CHANGED_DATE` | `b24_changed_date` | datetime | Последнее изменение в B24 |
| `CREATED_BY` | `b24_created_by` | int | ИД постановщика в B24 |
| `DURATION_FACT_SECONDS` | `b24_duration_fact` | int | Плановые трудозатраты |
| `elapsed` | `b24_elapsed_seconds` | int | Фактические трудозатраты (агрегировано) |
| `GROUP_ID`/группа | `b24_group_name` | string | Имя группы задачи B24 |
| Теги | `b24_tags` | string | Теги (если не используем теги ядра) |
| Признак клиента | `b24_client_sign` | string | Из XLSX-маппинга (SLA-отчёт) |
| `UF_DEPARTMENT` root | `b24_department_path` | string | Путь отдела (компания/подразделение) |

### 3.3 Маппинг статусов (настраиваемый)

| Bitrix24 | GLPI | Комментарий |
|---|---|---|
| 1 — new | 1 — new | |
| 2 — pending | 4 — waiting | Ожидает |
| 3 — in progress | 2 — assigned | В работе |
| 4 — awaiting control | 4 — waiting | |
| 5 — completed | 5 — solved | (или 6 — closed) |
| 6 — deferred | 4 — waiting | |

> Примечание: сейчас poller **пропускает** задачи со статусом 4,5,6,7 (не создаёт тикеты). Для полноты переноса нужно **создавать** тикеты и для закрытых задач (с `closedate`), иначе закрытые задачи не попадут в отчёт. Это изменение поведения — вынести на согласование (настраиваемый флаг `INCLUDE_CLOSED_TASKS`).

## 4. Зависимость: org sync и таблицы соответствий

Маппинг пользователей/отделов B24→GLPI (уже разработан org sync в `app/services/org_sync.py`) должен **сохранять соответствия** в БД интеграции:

- `org_user_map`: `b24_user_id` → `glpi_user_id` (+ email)
- `org_department_map`: `b24_dept_id` → `glpi_entity_id`

Эти таблицы заполняются при каждом прогоне org sync и используются при создании тикета (requester/tech/entity). **Org sync — предпосылка Phase 1.5** (должен быть развёрнут и выполнен хотя бы один раз до включения маппинга).

## 5. SLA-отчёт в GLPI: какие данные нужны и как реализовать

### 5.1 Что считает b24-add-report (SLA-метрики)

- **first_response** — время до первого ответа/взятия в работу
- **resolution** — время решения (по приоритетам: SLA-пороги из конфига)
- **stuck** — «зависшие» задачи (>N дней без изменений)
- **comment_window** — задержка ответа на комментарий
- **stage durations** — этапы (operator/dispatcher/head) по истории статусов
- **category** — классификация по ключевым словам
- **client sign / company / subdivision** — из структуры B24 + XLSX-маппинга
- **elapsed (трудозатраты)** — фактические часы

### 5.2 Что уже есть в ядре GLPI (используем бесплатно)

| SLA-данные | Источник в GLPI |
|---|---|
| first_response | `takeintoaccountdate` (взятие в работу) + первый followup техника |
| resolution | `solvedate` − `date` |
| Нарушение SLA | `glpi_slas` (уже есть **234 SLA**), `slas_id_ttr/tto`, `time_to_resolve` |
| Категории | `glpi_itilcategories` (уже есть: Инцидент/Запрос + подкатегории) |
| История статусов | `glpi_logs` (тикет-история) |
| Комментарии | `glpi_itilfollowups` |
| Трудозатраты тех.работ | `glpi_tickets_tasks.actiontime` (штатные задачи тикета) |

### 5.3 Что требует плагина (нет в ядре)

- Признак клиента (`b24_client_sign`) — из XLSX
- Путь отдела (компания/подразделение) `b24_department_path`
- Исходный статус/даты B24 (`b24_status`, `b24_changed_date`)
- Фактические трудозатраты B24 (`b24_elapsed_seconds`)
- Специфичные метрики отчёта (stuck, comment_window, этапы) — **не пересчитываются нативно**

### 5.4 Стратегия реализации отчёта

1. **Штатный GLPI**: SLA по приоритетам (first response/resolution) настраиваются нативными SLA GLPI — тикеты получают `time_to_resolve` и SLA-уровни. Отчёты по категориям/исполнителям — штатный модуль Reports (CSV/экспорт).
2. **Плагин `b24sla`** — дашборд/отчёт с нестандартными метриками:
   - читает ядро (date, solvedate, status, categories) + поля плагина `b24fields`
   - считает stuck / comment_window / этапы / трудозатраты / клиента
   - экспорт XLSX (аналогично b24-add-report)
   - виджеты на дашборде GLPI (dashboard widgets через hook `dashboard_cards`)
3. **Плагин, а не правка ядра** — единственный способ не ломать обновления GLPI и переносить решение на новые версии (GLPI-плагины стабильны между мажорными версиями).

## 6. Архитектура GLPI-плагинов

### 6.1 Общие принципы

- Один плагин = одна задача; поля — через **own tables** плагина + hook `item_add_item`/`post_item` для показа на форме тикета.
- НЕ менять таблицы ядра (`glpi_tickets` и др.) — добавлять поля только в таблицы плагина.
- Связать поля плагина с тикетом через `tickets_id` (int, FK).
- Каждый плагин: `plugin.xml`, `hook.php`, `setup.php`, `install/mysql/`, `inc/`.
- Версия плагина привязана к GLPI API (GLPI 10.x), миграции через `install()`/`update()`.

### 6.2 Плагин `b24fields`

- Таблица `glpi_plugin_b24fields_tickets`:
  `id`, `tickets_id` (FK), `b24_task_id`, `b24_status`, `b24_changed_date`, `b24_created_by`, `b24_duration_fact`, `b24_elapsed_seconds`, `b24_group_name`, `b24_tags`, `b24_client_sign`, `b24_department_path`
- Hook `show_item` → выводит поля на форме тикета (read-only блок «Данные Bitrix24»)
- API: отдаёт поля через `GET /apirest.php/Ticket/{id}?with_b24fields=1` (реализуется в плагине)
- CRUD-доступ через `PluginB24fieldsTicket::getFromDBByTicket()`.

### 6.3 Плагин `b24sla`

- Расчёт нестандартных метрик из ядра + `b24fields`
- Дашборд-виджеты (hook `dashboard_cards`) + страница отчёта (menu)
- Экспорт XLSX (использует phpspreadsheet, штатная зависимость GLPI)

## 7. План задач (TDD)

### Фаза A — Расширенный маппинг в интеграционном сервисе

**Files:**
- Modify: `app/services/poller.py` (`_process_task`, `_build_ticket_content`)
- Create: `app/services/ticket_mapper.py` (маппинг полей, keyword-категоризация)
- Create: `app/services/org_map.py` (чтение таблиц соответствий)
- Modify: `app/config/settings.py` (флаги/маппинги)
- Test: `tests/test_ticket_mapper.py`

- [ ] **A.1 Таблицы соответствий org_user_map / org_department_map в БД интеграции** (модель + миграция alembic) + заполнение в `org_sync.sync_org_structure()`.

```python
# app/models/org_map.py
class OrgUserMap(Base):
    __tablename__ = "org_user_map"
    id: Mapped[int] = mapped_column(primary_key=True)
    b24_user_id: Mapped[int] = mapped_column(unique=True, index=True)
    glpi_user_id: Mapped[int]
    email: Mapped[str] = mapped_column(String(255), default="")

class OrgDepartmentMap(Base):
    __tablename__ = "org_department_map"
    id: Mapped[int] = mapped_column(primary_key=True)
    b24_dept_id: Mapped[int] = mapped_column(unique=True, index=True)
    glpi_entity_id: Mapped[int]
```

- [ ] **A.2 `ticket_mapper.py`: чистое формирование полей тикета** из `task_data` (даты ISO→datetime, приоритет, статус, категория, requester/tech/entity через org_map).

```python
# app/services/ticket_mapper.py
def map_priority(b24_priority: int) -> int:
    return {1: 1, 2: 3, 3: 4, 4: 5}.get(int(b24_priority or 2), 3)

def map_status(b24_status: int) -> int:
    return {1: 1, 2: 4, 3: 2, 4: 4, 5: 5, 6: 4}.get(int(b24_status or 1), 1)

def parse_dt(value) -> datetime | None: ...
def classify_category(title, description, rules) -> str | None: ...  # keyword-классификатор
def build_ticket_fields(task_data, org_user_map, org_dept_map, settings) -> dict: ...
```

- [ ] **A.3 Передача полей в GLPI**: `glpi_client.create_ticket` расширяется на `date`, `time_to_resolve`, `closedate`, `priority`, `status`, `itilcategories_id`, `externalid`, requester/tech/entity, followup'ы.

```python
# app/services/glpi.py — create_ticket(...) с новыми kwargs:
def create_ticket(
    self, name, content, session_token,
    category_id=None, group_id=None, entity_id=None,
    *, requester_id=None, assignee_id=None,
    date=None, time_to_resolve=None, closedate=None,
    priority=None, status=None, itilcategories_id=None,
    externalid=None, followups=None,
) -> dict: ...
```

- [ ] **A.4 Перенос комментариев B24 в followup'ы** (опционально, `INCLUDE_COMMENTS=true`).
- [ ] **A.5 Флаг `INCLUDE_CLOSED_TASKS`** — создавать тикеты для задач статусов 4-7 (с закрытием), не пропускать.
- [ ] **A.6 Тесты**: маппинг, категоризация, даты, статусы; интеграционный мок-тест `create_ticket` с новыми полями.
- [ ] **A.7 Commit** (по под-задачам).

### Фаза B — Плагин GLPI `b24fields`

**Files (в /opt/glpi/plugins/b24fields/):**
- `plugin.xml`, `b24fields.php` (setup), `hook.php`, `inc/ticket.class.php`, `install/mysql/install.sql`, `install/update_1_0_1.php`

- [ ] **B.1 Каркас плагина** (`plugin.xml`, `setup.php` c `plugin_init_b24fields()`, регистрация в GLPI).
- [ ] **B.2 Таблица плагина** (`install/mysql/install.sql`) — колонки из 6.2.
- [ ] **B.3 Hook `show_item`** — вывод блока «Данные Bitrix24» на форме тикета.
- [ ] **B.4 API плагина**: `GET /apirest.php/Ticket/{id}` + `b24fields` (через hook `api_get` или REST-расширение плагина).
- [ ] **B.5 Установка плагина на сервер** (файлы в `/opt/glpi/plugins/`, `php bin/console glpi:plugin:install b24fields`), тест на тестовой сущности.

### Фаза C — SLA в GLPI + отчёт

- [ ] **C.1 Настройка SLA GLPI**: SLAs first-response/resolution по приоритетам; тикеты получают `slas_id_*`/`time_to_resolve` автоматически (native GLPI). Категории сопоставить с классификатором (настройка маппинга).
- [ ] **C.2 Плагин `b24sla`**: страница отчёта (категория/подразделение/исполнитель/период), метрики first_response/resolution из ядра, stuck/comment_window/трудозатраты из `b24fields`, экспорт XLSX.
- [ ] **C.3 Дашборд-виджеты** (`dashboard_cards`): «Нарушенные SLA», «Зависшие», «Открыто по категориям».
- [ ] **C.4 Тесты**: расчёт метрик на тестовых тикетах; сравнение с эталоном b24-add-report.

## 8. Тестирование и приёмка

- Юнит-тесты интеграционного сервиса: маппинг, категоризация, даты, статусы (pytest).
- Тест на GLPI (тестовая entity): создание тикета со всеми полями → проверка в БД/API.
- Сверка SLA-метрик плагина с b24-add-report на одних и тех же задачах (до 5% расхождения из-за таймзон).
- `ruff`, `mypy` — без новых ошибок; GLPI — `php -l`, консольные проверки.

## 9. Риски и решения

| Риск | Решение |
|---|---|
| Правка ядра GLPI ломает обновления | Всё нестандартное — только в плагинах (b24fields, b24sla) |
| Таймзоны/форматы дат B24 vs GLPI | Нормализация ISO→datetime в `ticket_mapper`, единый UTC |
| Закрытые задачи не создаются сейчас | Флаг `INCLUDE_CLOSED_TASKS` (на согласование) |
| 3148 существующих тикетов без новых полей | Backfill-задача: разовый пере-маппинг по `externalid` (или оставить, новые тикеты — с полями) |
| Соответствия пользователей неполные | org sync перед маппингом; fallback: requester = техник, entity = GLPI_DEFAULT_ENTITY_ID |
| Маппинг статусов различается | Настраиваемая таблица маппингов в settings/env |
| Трудозатраты B24 (elapsed) недоступны для лимитированного вебхука | Доп. вебхук (уже есть с правами user/department — проверить task.elapsed) |

## 10. Rollout

1. Деплой org sync + первый прогон (заполнение соответствий).
2. Деплой интеграционного сервиса с маппингом (Фаза A) — новые тикеты создаются с полями.
3. Установка плагинов b24fields → b24sla на GLPI.
4. Настройка SLA и категорий.
5. Сверка отчёта с b24-add-report; затем выключение b24-add-report.
6. Backfill существующих тикетов (опционально).

---

### Открытые вопросы для согласования

1. `INCLUDE_CLOSED_TASKS` — создавать тикеты для закрытых задач (4-7)? (Сейчас — нет; для отчёта по закрытым нужно — да.)
2. Переносить ли **комментарии** B24 в GLPI followup'ы при создании (объём + права)?
3. Имя тикета: оставить префикс `[Bitrix24 #ID]` или только `TITLE` (externalid хранит ID)?
4. Категоризация: повторять keyword-классификатор b24-add-report или перенести классификацию в плагин (пересчёт в GLPI)?
5. Трудозатраты: использовать штатные `glpi_tickets_tasks` или поле плагина `b24_elapsed_seconds`?
