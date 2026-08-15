"""Tests for app.services.poller — status filtering, retry logic, threading.

Covers:
- _is_skipped_bitrix_status: only closed/inactive statuses are skipped
- _should_retry_task: completed/cancelled never, failed until max_attempts,
  stale processing only
- _poll_for_user: Bitrix24 HTTP calls run inside asyncio.to_thread
- retry_failed_tasks: requeues failed tasks with remaining attempts
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config.settings import settings
from app.models.task import Task
from app.services import poller


class TestSkippedBitrixStatuses:
    """_is_skipped_bitrix_status — only 4,5,6,7 are skipped."""

    def test_closed_statuses_skipped(self) -> None:
        for st in (4, 5, 6, 7):
            assert poller._is_skipped_bitrix_status(st) is True

    def test_active_statuses_processed(self) -> None:
        for st in (1, 2, 3):
            assert poller._is_skipped_bitrix_status(st) is False

    def test_none_status_not_skipped(self) -> None:
        assert poller._is_skipped_bitrix_status(None) is False


class TestShouldRetryTask:
    """_should_retry_task decision table."""

    def _task(
        self,
        status: str = "pending",
        attempts: int = 0,
        max_attempts: int = 3,
        updated_at: datetime | None = None,
    ) -> Task:
        t = Task(source="bitrix24", source_id="1", type="create_ticket")
        t.status = status
        t.attempts = attempts
        t.max_attempts = max_attempts
        t.updated_at = updated_at or datetime.now(timezone.utc)
        return t

    def test_completed_never_retried(self) -> None:
        assert poller._should_retry_task(self._task(status="completed")) is False

    def test_cancelled_never_retried(self) -> None:
        assert poller._should_retry_task(self._task(status="cancelled")) is False

    def test_failed_with_remaining_attempts_retried(self) -> None:
        assert poller._should_retry_task(
            self._task(status="failed", attempts=1, max_attempts=3)
        ) is True

    def test_failed_exhausted_not_retried(self) -> None:
        assert poller._should_retry_task(
            self._task(status="failed", attempts=3, max_attempts=3)
        ) is False

    def test_processing_fresh_not_retried(self) -> None:
        t = self._task(status="processing")
        assert poller._should_retry_task(t) is False

    def test_processing_stale_retried(self) -> None:
        t = self._task(
            status="processing",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert poller._should_retry_task(t) is True

    def test_pending_retried(self) -> None:
        assert poller._should_retry_task(self._task(status="pending")) is True


class TestBitrixCallsRunInThread:
    """Bitrix24 sync HTTP calls must run via asyncio.to_thread."""

    @pytest.mark.asyncio
    async def test_get_tasks_called_via_to_thread(self, monkeypatch) -> None:
        calls: list = []
        real_to_thread = asyncio.to_thread

        async def fake_to_thread(fn, *args, **kwargs):
            calls.append(fn)
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(poller.asyncio, "to_thread", fake_to_thread)

        bitrix = MagicMock()
        bitrix.get_tasks.return_value = {"tasks": [], "next": 0}
        glpi = MagicMock()

        result = await poller._poll_for_user(bitrix, glpi, "sess", 70)

        assert result == set()
        bitrix.get_tasks.assert_called_once_with(responsible_id=70, start=0)
        assert any(c is bitrix.get_tasks for c in calls), (
            "get_tasks must run inside asyncio.to_thread"
        )


class TestRetryFailedTasks:
    """retry_failed_tasks requeues failed tasks with remaining attempts."""

    @pytest.mark.asyncio
    async def test_requeues_failed_with_remaining_attempts(self, monkeypatch) -> None:
        failed_task = Task(source="bitrix24", source_id="1", type="create_ticket")
        failed_task.status = "failed"
        failed_task.attempts = 1
        failed_task.max_attempts = 3
        failed_task.last_error = "boom"

        exhausted_task = Task(source="bitrix24", source_id="2", type="create_ticket")
        exhausted_task.status = "failed"
        exhausted_task.attempts = 3
        exhausted_task.max_attempts = 3

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, stmt):
                return MagicMock(
                    scalars=lambda: MagicMock(all=lambda: [failed_task, exhausted_task])
                )

            async def commit(self):
                pass

        monkeypatch.setattr(
            poller, "async_session_factory", lambda: FakeSession()
        )

        result = await poller.retry_failed_tasks()

        assert result == {"requeued": 1, "failed_total": 2}
        assert failed_task.status == "pending"
        assert failed_task.last_error is None
        assert exhausted_task.status == "failed"


class TestSchedulerJobs:
    """Job registration for poller and reverse sync."""

    def _shutdown(self, sched) -> None:
        if sched.running:
            sched.shutdown(wait=False)

    def test_reverse_sync_job_registered_when_enabled_and_test_mode(
        self, monkeypatch
    ) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        monkeypatch.setattr(settings, "BITRIX24_REVERSE_SYNC_ENABLED", True)
        monkeypatch.setattr(settings, "TEST_MODE", True)
        monkeypatch.setattr(settings, "TEST_TASK_IDS", "35591,35633")

        sched = AsyncIOScheduler()
        try:
            poller._register_poller_jobs(sched)
            ids = [j.id for j in sched.get_jobs()]
            assert "bitrix24_poll" in ids
            assert "bitrix24_reverse_sync" in ids
        finally:
            self._shutdown(sched)

    def test_reverse_sync_job_registered_by_default_in_test_mode(
        self, monkeypatch
    ) -> None:
        """Auto reverse sync is on by default (whitelist-guarded)."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        monkeypatch.setattr(settings, "TEST_MODE", True)
        monkeypatch.setattr(settings, "TEST_TASK_IDS", "35591,35633")

        sched = AsyncIOScheduler()
        try:
            poller._register_poller_jobs(sched)
            ids = [j.id for j in sched.get_jobs()]
            assert "bitrix24_reverse_sync" in ids
        finally:
            self._shutdown(sched)

    def test_reverse_sync_job_not_registered_when_disabled(
        self, monkeypatch
    ) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        monkeypatch.setattr(settings, "BITRIX24_REVERSE_SYNC_ENABLED", False)
        monkeypatch.setattr(settings, "TEST_MODE", True)
        monkeypatch.setattr(settings, "TEST_TASK_IDS", "35591,35633")

        sched = AsyncIOScheduler()
        try:
            poller._register_poller_jobs(sched)
            ids = [j.id for j in sched.get_jobs()]
            assert "bitrix24_reverse_sync" not in ids
            assert "bitrix24_poll" in ids
        finally:
            self._shutdown(sched)

    def test_reverse_sync_job_not_registered_when_test_mode_off(
        self, monkeypatch
    ) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        monkeypatch.setattr(settings, "BITRIX24_REVERSE_SYNC_ENABLED", True)
        monkeypatch.setattr(settings, "TEST_MODE", False)

        sched = AsyncIOScheduler()
        try:
            poller._register_poller_jobs(sched)
            ids = [j.id for j in sched.get_jobs()]
            assert "bitrix24_reverse_sync" not in ids
            assert "bitrix24_poll" in ids
        finally:
            self._shutdown(sched)

    def test_get_poller_status_includes_reverse_sync(self, monkeypatch) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        monkeypatch.setattr(settings, "BITRIX24_REVERSE_SYNC_ENABLED", True)
        monkeypatch.setattr(settings, "TEST_MODE", True)
        monkeypatch.setattr(settings, "TEST_TASK_IDS", "35591,35633")

        sched = AsyncIOScheduler()
        poller._register_poller_jobs(sched)
        monkeypatch.setattr(poller, "_scheduler", sched)
        try:
            status = poller.get_poller_status()
            assert status["status"] == "running"
            assert status["reverse_sync"]["enabled"] is True
            assert status["reverse_sync"]["auto_enabled"] is True
            assert (
                status["reverse_sync"]["interval_seconds"]
                == settings.BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS
            )
        finally:
            self._shutdown(sched)

    def test_get_poller_status_reports_disabled_by_default(
        self, monkeypatch
    ) -> None:
        """Status must show reverse sync is off when the flag is unset."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        monkeypatch.setattr(settings, "BITRIX24_REVERSE_SYNC_ENABLED", False)
        monkeypatch.setattr(settings, "TEST_MODE", True)
        monkeypatch.setattr(settings, "TEST_TASK_IDS", "35591,35633")

        sched = AsyncIOScheduler()
        poller._register_poller_jobs(sched)
        monkeypatch.setattr(poller, "_scheduler", sched)
        try:
            status = poller.get_poller_status()
            assert status["reverse_sync"]["enabled"] is False
            assert status["reverse_sync"]["auto_enabled"] is False
        finally:
            self._shutdown(sched)