"""Bitrix24 task → GLPI ticket field mapping (Phase 1.5).

Pure functions: field mapping (priority/status/dates), category
classification (keyword rules from bitrix24-add-report) and the L1
description template written back to Bitrix24.
"""

import re
from datetime import datetime, timezone
from typing import Any

# Bitrix24 status → GLPI ticket status (configurable).
STATUS_MAP: dict[int, int] = {
    1: 1,  # new → new
    2: 4,  # pending → waiting
    3: 2,  # in progress → assigned
    4: 4,  # awaiting control → waiting
    5: 5,  # completed → solved
    6: 4,  # deferred → waiting
}

# Bitrix24 priority → GLPI priority (1..5).
PRIORITY_MAP: dict[int, int] = {
    1: 1,  # low
    2: 3,  # normal
    3: 4,  # high
    4: 5,  # urgent
}

# Service categories (source: b24-add-report XLSX + server work) → GLPI
# itilcategory NAME. The GLPI category ids are resolved at runtime by name.
# Order matters: MORE SPECIFIC categories first (prefix matching), broad
# ones later — the first match wins.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Видеонаблюдение": [
        "камер", "видеонаблюд", "видеокамер", "камеры видеонаблюдения",
    ],
    "Электронная почта": [
        "почтовый ящик", "почт", "яндекс почт", "email", "e-mail", "письмо",
    ],
    "1С: работа с базами и отчётность": [
        "зуп", "ссг", "бухгалтерия", "акт сверки", "сверк", "ведомост",
        "ндфл", "проводк", "база 1с", "отчёт", "отчет", "отчётность",
        "журнал", "касса", "зарплат", "справочник", "каталоги",
    ],
    "Настройка и поддержка IP-телефонии": [
        "телефония", "ip-телефон", "voip", "атс", "звонок", "звонит",
        "телефонная линия", "не работает телефон", "стационарный телефон",
        "сотовый телефон",
    ],
    "Обслуживание принтера": [
        "принтер", "мфу", "сканер", "картридж", "заправка",
        "печать не работает", "замена картриджа", "печатающее устройство",
        "заправить картриджи", "картриджи",
    ],
    "Организация настройки доступов и сертификатов в ЭДО": [
        "эдо", "сертификат", "эцп", "мчд", "криптопро", "доступ в банк",
        "доступ в банки", "сбис", "диод", "токен", "диадок", "контур",
        "электронная отчётность", "электронная отчетность",
    ],
    "Доступы и права": [
        "доступ к 1с", "доступ в 1с", "доступ к базе 1с",
        "восстановление доступа к 1с", "1с не открывается", "доступ",
        "доступа", "нет доступа", "предоставить доступ",
        "предоставление доступа", "восстановить доступ", "восстановление доступа",
        "сетевой папк", "сетевая папка", "папке", "папка", "сетевой диск",
        "доступ к диску", "сетевой ресурс", "яндекс диск", "яндекс.диск",
        "контур", "электронная лаборатория", "общий каталог", "подключение к",
    ],
    "Настройка ПК": [
        "настройка пк", "настройка компьютера", "настройка ноутбука",
        "рабочее место пользователя", "обновление ос",
        "обновление операционной системы", "антивирус", "установка по",
        "установка программ", "настройка сетевого принтера", "обновление 1с",
        "интернет", "восстановление сети", "не работает интернет",
        "подключение периферийного оборудования", "настройка по", "пк",
        "компьютер", "ноутбук", "wi-fi", "wifi", "не работает wi",
        "нет связи", "не работает сеть", "монитор не", "не грузится",
    ],
    "Доступ к сетевым ресурсам": [
        "сетевая папка", "сетевой диск", "доступ к диску",
        "создание сетевой папки", "сетевой ресурс", "доступ к сетевой папке",
        "сетевой сервис",
    ],
    "Настройка удаленного доступа (VPN)": [
        "удаленный доступ", "удалённый доступ", "vpn", "rudesktop", "rdp",
        "рабочий стол",
    ],
    "Абонентское обслуживание рабочего места": [
        "покупка", "выдача", "установка пк", "ремонт", "абонентское",
        "мышь", "клавиатура", "веб-камера", "гарнитура", "микрофон",
        "наушники", "периферийное оборудование",
    ],
    "Сопровождение платформы Битрикс 24": [
        "битрикс", "bitrix", "битрикс 24", "битрикс24", "bitrix24",
    ],
    "Работы по серверам": [
        "сервер", "серверное оборудование", "windows server", "серверная",
    ],
    "Поддержка серверного оборудования": [
        "мониторинг сервера", "диагностика сервера",
        "восстановление после сбоев", "обновление сервера",
    ],
}

DEFAULT_CATEGORY = "Другое"


def extract_problem_description(content: str | None) -> str:
    """Extract the clean problem text from a description that may contain
    the L1 template (ФИО/Телефон/Организация/…/Описание проблемы: …).

    If the text has the "Описание проблемы:" marker, return everything
    after the LAST occurrence (the innermost, cleanest problem text).
    Otherwise return the content stripped.
    """
    text = (content or "").strip()
    marker = "описание проблемы:"
    if marker in text.lower():
        idx = text.lower().rfind(marker)
        return text[idx + len(marker):].strip()
    return text

# L1 write-back template (fields filled from the GLPI ticket).
L1_TEMPLATE = (
    "ФИО: {fio}\n"
    "Телефон: {phone}\n"
    "Организация: {organization}\n"
    "Место положение: {location}\n"
    "Категория: {category}\n"
    "Приоритет: {priority}\n"
    "Описание проблемы: {problem_description}"
)


def map_status(b24_status: int | None) -> int:
    """Map a Bitrix24 task status to a GLPI ticket status."""
    return STATUS_MAP.get(int(b24_status or 1), 1)


def map_priority(b24_priority: int | None) -> int:
    """Map a Bitrix24 task priority to a GLPI ticket priority."""
    return PRIORITY_MAP.get(int(b24_priority or 2), 3)


def parse_dt(value: Any) -> str | None:
    """Parse a Bitrix24 ISO datetime to GLPI API format (UTC, no TZ).

    Returns ``"YYYY-MM-DD HH:MM:SS"`` or None.
    """
    if not value:
        return None
    try:
        dt = value
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def classify_category(title: str | None, description: str | None) -> str:
    """Classify a Bitrix24 task into a service category by keywords.

    Uses prefix matching (word start) so that e.g. "почт" matches "почта".
    The L1 description template (ФИО/Телефон/Организация/…) is stripped so
    its service markers don't cause false matches. Specific categories are
    listed first in CATEGORY_KEYWORDS.
    """
    text = f"{title or ''} {description or ''}".lower()
    # Drop L1 template markers from the description text.
    for marker in (
        "фио:", "телефон:", "организация:", "местоположение:", "место положение:",
        "категория:", "приоритет:", "описание проблемы:", "[b]", "[/b]",
    ):
        text = text.replace(marker, " ")
    for category_name, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            pattern = r"\b" + re.escape(kw.lower())
            if re.search(pattern, text, re.UNICODE):
                return category_name
    return DEFAULT_CATEGORY


def build_l1_template(*, fio="", phone="", organization="", location="",
                      category="", priority="", problem_description="") -> str:
    """Build the L1 description template written back to Bitrix24."""
    return L1_TEMPLATE.format(
        fio=fio or "",
        phone=phone or "",
        organization=organization or "",
        location=location or "",
        category=category or "",
        priority=priority or "",
        problem_description=problem_description or "",
    )
