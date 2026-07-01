"""Tests for app.api.bitrix — Bitrix24 webhook endpoint.

All GLPIClient calls are mocked at the class level.  The database session
is replaced via ``app.dependency_overrides`` so no real DB is needed.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.bitrix import BitrixLead, _build_ticket_content
from app.models.task import Task, TaskStatus


# ===================================================================
# Unit — BitrixLead model
# ===================================================================


class TestBitrixLeadValidation:
    """``BitrixLead`` Pydantic model validation."""

    def test_name_required(self):
        """name is required — omitting it raises ``ValidationError``."""
        with pytest.raises(Exception):
            BitrixLead()

    def test_empty_name_raises(self):
        """Empty-string name violates ``min_length=1``."""
        with pytest.raises(Exception):
            BitrixLead(name="")

    def test_valid_name(self):
        lead = BitrixLead(name="Foo")
        assert lead.name == "Foo"

    def test_optional_fields_default_none(self):
        lead = BitrixLead(name="Foo")
        assert lead.phone is None
        assert lead.email is None
        assert lead.offer_content is None
        assert lead.idempotency_key is None

    def test_all_fields_explicit(self):
        lead = BitrixLead(
            name="A",
            phone="+7",
            email="a@b.c",
            offer_content="offer",
            idempotency_key="ik-1",
        )
        assert lead.name == "A"
        assert lead.phone == "+7"
        assert lead.email == "a@b.c"
        assert lead.offer_content == "offer"
        assert lead.idempotency_key == "ik-1"

    def test_idempotency_key_too_long_raises(self):
        """``max_length=255`` is enforced."""
        with pytest.raises(Exception):
            BitrixLead(name="Foo", idempotency_key="x" * 256)

    def test_idempotency_key_boundary_255(self):
        lead = BitrixLead(name="Foo", idempotency_key="x" * 255)
        assert len(lead.idempotency_key) == 255


# ===================================================================
# Unit — _build_ticket_content
# ===================================================================


class TestBuildTicketContent:
    """``_build_ticket_content`` — pure string builder."""

    def test_all_fields(self):
        lead = BitrixLead(
            name="Bob", phone="+1", email="b@b.com", offer_content="VIP"
        )
        expected = "[Lead: Bob]\nPhone: +1\nEmail: b@b.com\nOffer: VIP"
        assert _build_ticket_content(lead) == expected

    def test_missing_fields_use_na(self):
        lead = BitrixLead(name="Bob")
        result = _build_ticket_content(lead)
        assert "Phone: N/A" in result
        assert "Email: N/A" in result
        assert "Offer: N/A" in result

    def test_unicode_name(self):
        lead = BitrixLead(name="\U0001f525 \u041a\u043b\u0438\u0435\u043d\u0442")
        result = _build_ticket_content(lead)
        assert result.startswith("[Lead: \U0001f525 \u041a\u043b\u0438\u0435\u043d\u0442]")

    def test_very_long_name(self):
        name = "A" * 10_000
        lead = BitrixLead(name=name)
        result = _build_ticket_content(lead)
        assert result.startswith(f"[Lead: {'A' * 10_000}]")


# ===================================================================
# Helpers — mock session & dependencies
# ===================================================================


class _MockAsyncSession:
    """Minimal AsyncSession stand-in for testing.

    Records the added ``Task`` so tests can inspect it after the
    request completes.  ``execute`` returns no existing task by default;
    the caller provides ``existing_task`` to simulate idempotency hits.
    """

    def __init__(self, existing_task: Task | None = None) -> None:
        self.existing_task = existing_task
        self.added_task: Task | None = None
        self.committed = False

    async def execute(self, stmt):  # noqa: A003
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.existing_task
        return result

    def add(self, obj: object) -> None:  # noqa: A003
        if isinstance(obj, Task):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.added_task = obj

    async def commit(self) -> None:  # noqa: A003
        self.committed = True
        if self.added_task is not None and self.added_task.id is None:
            self.added_task.id = uuid.uuid4()

    async def close(self) -> None:  # noqa: A003
        pass


def _make_task(**overrides: object) -> Task:
    """Shortcut to create a ``Task`` with a valid UUID ``id``."""
    task = Task(
        source=overrides.get("source", "bitrix24"),  # type: ignore[arg-type]
        source_id=overrides.get("source_id", "src-1"),  # type: ignore[arg-type]
        type=overrides.get("type", "create_ticket"),  # type: ignore[arg-type]
        status=overrides.get("status", TaskStatus.PENDING),  # type: ignore[arg-type]
        idempotency_key=overrides.get("idempotency_key"),  # type: ignore[arg-type]
    )
    task.id = uuid.uuid4()
    return task


@pytest.fixture
def override_get_db():
    """Register a ``_MockAsyncSession`` as the ``get_db`` dependency.

    The fixture yields the mock session so tests can configure
    ``existing_task`` or inspect ``added_task``.
    """
    from app.core.database import get_db
    from app.main import app

    session = _MockAsyncSession()

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def mock_glpi():
    """Replace ``GLPIClient`` with a magic mock.

    The yielded mock *instance* (not the class) can be inspected for
    call assertions in individual tests.
    """
    instance = MagicMock()
    instance.init_session.return_value = "sess-abc"
    instance.create_ticket.return_value = {"id": 42, "message": "created"}

    with patch("app.api.bitrix.GLPIClient", return_value=instance):
        yield instance


# ===================================================================
# Endpoint — receive_lead  (POST /webhook/bitrix/lead)
# ===================================================================


class TestReceiveLeadIdempotency:
    """Idempotency checks — returning existing tasks without GLPI calls."""

    @pytest.mark.asyncio
    async def test_duplicate_completed_task(self, override_get_db):
        """Completed task with same idempotency_key returns status=duplicate."""
        from app.main import app

        existing = _make_task(
            status=TaskStatus.COMPLETED,
            idempotency_key="dup-key",
        )
        override_get_db.existing_task = existing

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={"name": "Lead", "idempotency_key": "dup-key"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "duplicate"
        assert data["task_id"] == str(existing.id)

    @pytest.mark.asyncio
    async def test_duplicate_failed_task(self, override_get_db):
        """Failed task with same idempotency_key returns status=duplicate."""
        from app.main import app

        existing = _make_task(
            status=TaskStatus.FAILED,
            idempotency_key="dup-key",
        )
        override_get_db.existing_task = existing

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={"name": "Lead", "idempotency_key": "dup-key"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "duplicate"
        assert data["task_id"] == str(existing.id)

    @pytest.mark.asyncio
    async def test_in_progress_processing(self, override_get_db):
        """Processing task with same key returns status=in_progress."""
        from app.main import app

        existing = _make_task(
            status=TaskStatus.PROCESSING,
            idempotency_key="ip-key",
        )
        override_get_db.existing_task = existing

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={"name": "Lead", "idempotency_key": "ip-key"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_progress"
        assert data["task_id"] == str(existing.id)

    @pytest.mark.asyncio
    async def test_in_progress_pending(self, override_get_db):
        """Pending task with same key returns status=in_progress."""
        from app.main import app

        existing = _make_task(
            status=TaskStatus.PENDING,
            idempotency_key="pend-key",
        )
        override_get_db.existing_task = existing

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={"name": "Lead", "idempotency_key": "pend-key"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_progress"
        assert data["task_id"] == str(existing.id)


class TestReceiveLeadSuccess:
    """Happy path — new lead, GLPI call succeeds."""

    @pytest.mark.asyncio
    async def test_happy_path_creates_task_and_returns_ticket(
        self, override_get_db, mock_glpi
    ):
        """New lead (no idempotency) → Task created → GLPI called → success."""
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={
                    "name": "New Client",
                    "phone": "+7-900-123-45-67",
                    "email": "client@example.com",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["glpi_ticket"] == {"id": 42, "message": "created"}

        # A valid task_id was returned
        task_id = data["task_id"]
        assert uuid.UUID(task_id)

        # Verify the task was added and committed
        session = override_get_db
        assert session.added_task is not None
        assert session.added_task.status == TaskStatus.COMPLETED
        assert session.added_task.source == "bitrix24"
        assert session.added_task.idempotency_key is None
        assert session.committed

        # GLPI was called with correct args
        mock_glpi.init_session.assert_called_once()
        mock_glpi.create_ticket.assert_called_once()
        _call_name, _call_args, call_kwargs = (
            mock_glpi.create_ticket.mock_calls[0]
        )
        assert call_kwargs["name"] == "New Client"
        assert "Phone: +7-900-123-45-67" in call_kwargs["content"]
        assert "Email: client@example.com" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_happy_path_with_idempotency_key(
        self, override_get_db, mock_glpi
    ):
        """New lead WITH idempotency_key → no duplicate hit → GLPI called."""
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={
                    "name": "With Key",
                    "idempotency_key": "fresh-key-001",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        session = override_get_db
        assert session.added_task is not None
        assert session.added_task.idempotency_key == "fresh-key-001"
        mock_glpi.init_session.assert_called_once()
        mock_glpi.create_ticket.assert_called_once()

    @pytest.mark.asyncio
    async def test_glpi_failure_returns_500_and_marks_task_failed(
        self, override_get_db, mock_glpi
    ):
        """GLPI init_session failure → 500, task FAILED, no create_ticket."""
        from app.main import app

        mock_glpi.init_session.side_effect = RuntimeError("GLPI unreachable")

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={"name": "Fail Lead"},
            )

        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert "GLPI unreachable" in detail

        session = override_get_db
        assert session.added_task is not None
        assert session.added_task.status == TaskStatus.FAILED
        assert "GLPI unreachable" in session.added_task.last_error


class TestReceiveLeadErrors:
    """Validation errors and other failure modes."""

    @pytest.mark.asyncio
    async def test_missing_name_returns_422(self, override_get_db):
        """POST without name field returns 422."""
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={"phone": "+7"},
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_name_returns_422(self, override_get_db):
        """POST with empty name returns 422."""
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={"name": ""},
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_body_returns_422(self, override_get_db):
        """POST with empty JSON body returns 422."""
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/bitrix/lead",
                json={},
            )

        assert resp.status_code == 422
