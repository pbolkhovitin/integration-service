"""Tests for app.services.ticket_mapper — field mapping, category, L1 template."""

from __future__ import annotations

from app.services.ticket_mapper import (
    L1_TEMPLATE,
    build_l1_template,
    classify_category,
    map_priority,
    map_status,
    parse_dt,
)


class TestMapStatus:
    def test_active_mapping(self) -> None:
        assert map_status(1) == 1
        assert map_status(2) == 4
        assert map_status(3) == 2

    def test_closed_mapping(self) -> None:
        assert map_status(4) == 4
        assert map_status(5) == 5
        assert map_status(6) == 4

    def test_default(self) -> None:
        assert map_status(None) == 1


class TestMapPriority:
    def test_mapping(self) -> None:
        assert map_priority(1) == 1
        assert map_priority(2) == 3
        assert map_priority(3) == 4
        assert map_priority(4) == 5

    def test_default(self) -> None:
        assert map_priority(None) == 3


class TestParseDt:
    def test_iso_with_tz(self) -> None:
        assert parse_dt("2026-08-14T10:00:00+03:00") == "2026-08-14 07:00:00"

    def test_iso_z(self) -> None:
        assert parse_dt("2026-08-14T10:00:00Z") == "2026-08-14 10:00:00"

    def test_naive(self) -> None:
        assert parse_dt("2026-08-14 10:00:00") == "2026-08-14 10:00:00"

    def test_none_and_invalid(self) -> None:
        assert parse_dt(None) is None
        assert parse_dt("garbage") is None


class TestClassifyCategory:
    def test_printer_keyword(self) -> None:
        assert classify_category("Не печатает принтер", "замените картридж") == (
            "Обслуживание принтера"
        )

    def test_1c_keyword(self) -> None:
        assert classify_category("1С не открывается", "") == (
            "Доступ и восстановление доступа к 1С"
        )

    def test_vpn_keyword(self) -> None:
        assert classify_category("Настроить VPN", "") == (
            "Настройка удаленного доступа (VPN)"
        )

    def test_word_boundary_no_false_positive(self) -> None:
        # "атс" must NOT match inside "песковатский"
        assert classify_category("Песковатский отчёт", "") == "Другое"

    def test_fallback_to_drugoe(self) -> None:
        assert classify_category("Странный запрос", "") == "Другое"


class TestBuildL1Template:
    def test_renders_all_fields(self) -> None:
        out = build_l1_template(
            fio="Иванов Иван",
            phone="+7 900 000-00-00",
            organization="АО «АПО «Аврора»",
            location="Каб. 42",
            category="Настройка ПК",
            priority="Средний",
            problem_description="Не включается компьютер",
        )
        assert "ФИО: Иванов Иван" in out
        assert "Телефон: +7 900 000-00-00" in out
        assert "Организация: АО «АПО «Аврора»" in out
        assert "Место положение: Каб. 42" in out
        assert "Категория: Настройка ПК" in out
        assert "Приоритет: Средний" in out
        assert "Описание проблемы: Не включается компьютер" in out

    def test_empty_fields(self) -> None:
        out = build_l1_template(problem_description="x")
        assert out.count(": ") == L1_TEMPLATE.count(": ")
