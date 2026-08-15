"""Tests for app.services.test_tasks — runtime whitelist management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.services import test_tasks


class TestTestTaskWhitelist:
    async def test_env_plus_db_allowed(self, monkeypatch) -> None:
        monkeypatch.setattr(test_tasks.settings, "TEST_TASK_IDS", "35591,35633")

        fake_result = MagicMock()
        fake_result.all.return_value = [(99999,)]
        fake_db = AsyncMock()
        fake_db.execute.return_value = fake_result
        fake_cm = AsyncMock()
        fake_cm.__aenter__.return_value = fake_db
        fake_cm.__aexit__.return_value = None

        with patch(
            "app.services.test_tasks.async_session_factory",
            return_value=fake_cm,
        ):
            allowed = await test_tasks.allowed_test_task_ids()

        assert allowed == {35591, 35633, 99999}

    async def test_add_test_task(self) -> None:
        fake_result = MagicMock()
        fake_result.scalar_one_or_none.return_value = None
        fake_db = AsyncMock()
        fake_db.execute.return_value = fake_result
        fake_cm = AsyncMock()
        fake_cm.__aenter__.return_value = fake_db
        fake_cm.__aexit__.return_value = None

        with patch(
            "app.services.test_tasks.async_session_factory",
            return_value=fake_cm,
        ):
            result = await test_tasks.add_test_task(40001)

        assert result == {"task_id": 40001, "added": True}
        fake_db.add.assert_called_once()

    async def test_add_test_task_duplicate(self) -> None:
        fake_result = MagicMock()
        fake_result.scalar_one_or_none.return_value = 1  # already exists
        fake_db = AsyncMock()
        fake_db.execute.return_value = fake_result
        fake_cm = AsyncMock()
        fake_cm.__aenter__.return_value = fake_db
        fake_cm.__aexit__.return_value = None

        with patch(
            "app.services.test_tasks.async_session_factory",
            return_value=fake_cm,
        ):
            result = await test_tasks.add_test_task(40001)

        assert result == {"task_id": 40001, "added": False, "already_present": True}
        fake_db.add.assert_not_called()

    async def test_remove_test_task(self) -> None:
        row = MagicMock()
        fake_result = MagicMock()
        fake_result.scalar_one_or_none.return_value = row
        fake_db = AsyncMock()
        fake_db.execute.return_value = fake_result
        fake_cm = AsyncMock()
        fake_cm.__aenter__.return_value = fake_db
        fake_cm.__aexit__.return_value = None

        with patch(
            "app.services.test_tasks.async_session_factory",
            return_value=fake_cm,
        ):
            result = await test_tasks.remove_test_task(40001)

        assert result == {"task_id": 40001, "removed": True}
        fake_db.delete.assert_called_once_with(row)

    async def test_list_test_tasks(self, monkeypatch) -> None:
        monkeypatch.setattr(test_tasks.settings, "TEST_TASK_IDS", "35591,35633")

        fake_result = MagicMock()
        fake_result.all.return_value = [(99999,)]
        fake_db = AsyncMock()
        fake_db.execute.return_value = fake_result
        fake_cm = AsyncMock()
        fake_cm.__aenter__.return_value = fake_db
        fake_cm.__aexit__.return_value = None

        with patch(
            "app.services.test_tasks.async_session_factory",
            return_value=fake_cm,
        ):
            data = await test_tasks.list_test_tasks()

        assert data["env_task_ids"] == [35591, 35633]
        assert data["runtime_task_ids"] == [99999]
        assert data["allowed_task_ids"] == [35591, 35633, 99999]
