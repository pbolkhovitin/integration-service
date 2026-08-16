"""Tests for app.services.org_sync — Bitrix24 users/departments → GLPI.

Mocking strategy: patch ``app.services.org_sync.BitrixClient`` /
``GLPIClient`` with MagicMock and run ``asyncio.to_thread`` synchronously
(no real HTTP / OS threads).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import org_sync


async def _sync_to_thread(fn, *args, **kwargs):
    """Run ``asyncio.to_thread(fn, ...)`` synchronously (for mocks)."""
    return fn(*args, **kwargs)


@pytest.fixture(autouse=True)
def _patch_to_thread():
    with patch(
        "app.services.org_sync.asyncio.to_thread",
        new=_sync_to_thread,
    ):
        yield


@pytest.fixture(autouse=True)
def _patch_maps():
    """Isolate the org mapping DB tables."""
    with (
        patch("app.services.org_sync.load_department_map", new=AsyncMock(return_value={})),
        patch("app.services.org_sync.upsert_department_map", new=AsyncMock()),
        patch("app.services.org_sync.load_user_map", new=AsyncMock(return_value={})),
        patch("app.services.org_sync.upsert_user_map", new=AsyncMock()),
    ):
        yield


@pytest.fixture
def clients():
    """Return (bitrix_mock, glpi_mock) wired into the org_sync module."""
    bitrix = MagicMock()
    glpi = MagicMock()
    glpi.init_session.return_value = "org-session"
    glpi.kill_session.return_value = None
    with (
        patch("app.services.org_sync.BitrixClient", return_value=bitrix),
        patch("app.services.org_sync.GLPIClient", return_value=glpi),
    ):
        yield bitrix, glpi


class TestGetOrgSyncStatus:
    def test_returns_config(self, monkeypatch) -> None:
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_ENABLED", True)
        monkeypatch.setattr(org_sync.settings, "BITRIX24_ORG_WEBHOOK_URL", "x")
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_ROOT_ENTITY_ID", 25)
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_USER_PROFILE_ID", 1)
        status = org_sync.get_org_sync_status()
        assert status["org_sync_enabled"] is True
        assert status["org_webhook_configured"] is True
        assert status["root_entity_id"] == 25
        assert status["user_profile_id"] == 1


class TestSyncOrgStructure:
    async def test_error_without_org_webhook(self, monkeypatch) -> None:
        monkeypatch.setattr(org_sync.settings, "BITRIX24_ORG_WEBHOOK_URL", "")
        result = await org_sync.sync_org_structure()
        assert "error" in result

    async def test_creates_entities_and_users(self, clients, monkeypatch) -> None:
        bitrix, glpi = clients
        monkeypatch.setattr(org_sync.settings, "BITRIX24_ORG_WEBHOOK_URL", "wh")
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_ROOT_ENTITY_ID", 25)
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_USER_PROFILE_ID", 1)

        # Bitrix24: root dept (1) + "Бухгалтерия" (2) under root
        #          + "Разработка 1С" (3) under 2.
        bitrix.get_departments.return_value = [
            {"id": 1, "name": "АО «АПО «Аврора»", "parent_id": None},
            {"id": 2, "name": "Бухгалтерия", "parent_id": 1},
            {"id": 3, "name": "Разработка 1С", "parent_id": 2},
        ]
        # Bitrix24 users: one active with a department, one inactive.
        bitrix.get_users.side_effect = [
            {
                "users": [
                    {
                        "id": 10,
                        "name": "Иван",
                        "last_name": "Иванов",
                        "email": "ivan@example.ru",
                        "work_position": "Бухгалтер",
                        "active": True,
                        "department_ids": [2],
                    },
                    {
                        "id": 11,
                        "name": "Петя",
                        "last_name": "Петров",
                        "email": "petr@example.ru",
                        "work_position": "",
                        "active": False,
                        "department_ids": [],
                    },
                ],
                "next": 0,
            }
        ]
        # GLPI: only the root entity exists, no users.
        glpi.get_entities.return_value = [
            {"id": 25, "name": "АО АПО Аврора", "parent_id": 0}
        ]
        glpi.get_user_emails.return_value = []
        glpi.create_entity.side_effect = [
            {"id": 26, "message": "ok"},
            {"id": 27, "message": "ok"},
        ]
        glpi.create_user.return_value = {"id": 100, "message": "ok"}

        result = await org_sync.sync_org_structure()

        assert result["departments_total"] == 3
        assert result["departments_created"] == 2  # depts 2 and 3
        assert result["users_active"] == 1
        assert result["users_created"] == 1
        assert result["users_updated"] == 0
        assert result["errors"] == []

        # Root dept (parent=None) maps to root entity — no create call.
        glpi.create_entity.assert_any_call(
            "Бухгалтерия", 25, "org-session"
        )
        glpi.create_entity.assert_any_call(
            "Разработка 1С", 26, "org-session"
        )
        # Active user created with profile 1 and entity of dept 2 (id=26).
        glpi.create_user.assert_called_once_with(
            name="ivan@example.ru",
            realname="Иванов",
            firstname="Иван",
            email="ivan@example.ru",
            entities_id=26,
            profiles_id=1,
            phone=None,
            mobile=None,
            comment="Бухгалтер",
            sync_field="10",
            session_token="org-session",
        )
        # Session released.
        glpi.kill_session.assert_called_once_with("org-session")

    async def test_matches_existing_user_by_email(self, clients, monkeypatch) -> None:
        bitrix, glpi = clients
        monkeypatch.setattr(org_sync.settings, "BITRIX24_ORG_WEBHOOK_URL", "wh")
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_ROOT_ENTITY_ID", 25)
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_USER_PROFILE_ID", 1)

        bitrix.get_departments.return_value = [
            {"id": 1, "name": "Root", "parent_id": None}
        ]
        bitrix.get_users.side_effect = [
            {
                "users": [
                    {
                        "id": 10,
                        "name": "Иван",
                        "last_name": "Иванов",
                        "email": "  IVAN@EXAMPLE.RU  ",
                        "work_position": "",
                        "active": True,
                        "department_ids": [],
                    }
                ],
                "next": 0,
            }
        ]
        glpi.get_entities.return_value = [
            {"id": 25, "name": "Root", "parent_id": 0}
        ]
        # Existing GLPI user 50 already has this email (case-insensitive).
        glpi.get_user_emails.return_value = [
            {"users_id": 50, "email": "ivan@example.ru", "is_default": True}
        ]

        result = await org_sync.sync_org_structure()

        assert result["users_created"] == 0
        assert result["users_updated"] == 1
        glpi.update_user.assert_called_once_with(
            50,
            realname="Иванов",
            firstname="Иван",
            entities_id=25,
            phone=None,
            mobile=None,
            comment=None,
            sync_field="10",
            session_token="org-session",
        )
        glpi.create_user.assert_not_called()

    async def test_user_without_email_gets_fallback_login(
        self, clients, monkeypatch
    ) -> None:
        bitrix, glpi = clients
        monkeypatch.setattr(org_sync.settings, "BITRIX24_ORG_WEBHOOK_URL", "wh")
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_ROOT_ENTITY_ID", 25)
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_USER_PROFILE_ID", 1)

        bitrix.get_departments.return_value = [
            {"id": 1, "name": "Root", "parent_id": None}
        ]
        bitrix.get_users.side_effect = [
            {
                "users": [
                    {
                        "id": 77,
                        "name": "Нет",
                        "last_name": "Почты",
                        "email": "",
                        "work_position": "",
                        "active": True,
                        "department_ids": [],
                    }
                ],
                "next": 0,
            }
        ]
        glpi.get_entities.return_value = [
            {"id": 25, "name": "Root", "parent_id": 0}
        ]
        glpi.get_user_emails.return_value = []
        glpi.create_user.return_value = {"id": 200}

        result = await org_sync.sync_org_structure()

        assert result["users_created"] == 1
        glpi.create_user.assert_called_once_with(
            name="b24_77",
            realname="Почты",
            firstname="Нет",
            email=None,
            entities_id=25,
            profiles_id=1,
            phone=None,
            mobile=None,
            comment=None,
            sync_field="77",
            session_token="org-session",
        )

    async def test_paginated_users(self, clients, monkeypatch) -> None:
        bitrix, glpi = clients
        monkeypatch.setattr(org_sync.settings, "BITRIX24_ORG_WEBHOOK_URL", "wh")
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_ROOT_ENTITY_ID", 25)
        monkeypatch.setattr(org_sync.settings, "ORG_SYNC_USER_PROFILE_ID", 1)

        bitrix.get_departments.return_value = [
            {"id": 1, "name": "Root", "parent_id": None}
        ]
        bitrix.get_users.side_effect = [
            {
                "users": [
                    {
                        "id": 1,
                        "name": "А",
                        "last_name": "А",
                        "email": "a@x.ru",
                        "work_position": "",
                        "active": True,
                        "department_ids": [],
                    }
                ],
                "next": 50,
            },
            {
                "users": [
                    {
                        "id": 2,
                        "name": "Б",
                        "last_name": "Б",
                        "email": "b@x.ru",
                        "work_position": "",
                        "active": True,
                        "department_ids": [],
                    }
                ],
                "next": 0,
            },
        ]
        glpi.get_entities.return_value = [
            {"id": 25, "name": "Root", "parent_id": 0}
        ]
        glpi.get_user_emails.return_value = []
        glpi.create_user.side_effect = [{"id": 300}, {"id": 301}]

        result = await org_sync.sync_org_structure()

        assert result["users_total"] == 2
        assert result["users_created"] == 2
        assert bitrix.get_users.call_count == 2
        bitrix.get_users.assert_any_call(start=0)
        bitrix.get_users.assert_any_call(start=50)
