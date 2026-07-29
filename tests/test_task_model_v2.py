"""Tests for the new Task model columns (last_glpi_status, last_glpi_followup_id).

Verifies:
1. Task model can be instantiated with the new fields
2. New fields are nullable (can be None)
3. New fields accept and return correct types (str, int)
4. Migration is importable
5. Migration has proper upgrade/downgrade functions
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.models.task import Task


# ===================================================================
# Model instantiation — new fields
# ===================================================================


class TestTaskNewColumnsInstantiation:
    """Task can be instantiated with the new ``last_glpi_status`` and
    ``last_glpi_followup_id`` columns."""

    def test_defaults_are_none(self) -> None:
        """Both new columns default to None when not provided."""
        task = Task(
            source="GLPI",
            source_id="42",
            type="create_ticket",
        )
        assert task.last_glpi_status is None
        assert task.last_glpi_followup_id is None

    def test_set_last_glpi_status(self) -> None:
        """``last_glpi_status`` accepts a string value."""
        task = Task(
            source="GLPI",
            source_id="42",
            type="sync_status",
            last_glpi_status="processing",
        )
        assert task.last_glpi_status == "processing"
        assert isinstance(task.last_glpi_status, str)

    def test_set_last_glpi_followup_id(self) -> None:
        """``last_glpi_followup_id`` accepts an integer value."""
        task = Task(
            source="GLPI",
            source_id="42",
            type="sync_followup",
            last_glpi_followup_id=567,
        )
        assert task.last_glpi_followup_id == 567
        assert isinstance(task.last_glpi_followup_id, int)

    def test_set_both_new_fields(self) -> None:
        """Both new columns can be set simultaneously."""
        task = Task(
            source="GLPI",
            source_id="99",
            type="full_sync",
            last_glpi_status="completed",
            last_glpi_followup_id=1234,
        )
        assert task.last_glpi_status == "completed"
        assert task.last_glpi_followup_id == 1234

    def test_explicit_none_for_status(self) -> None:
        """Passing ``None`` explicitly for ``last_glpi_status`` stores None."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_status=None,
        )
        assert task.last_glpi_status is None

    def test_explicit_none_for_followup_id(self) -> None:
        """Passing ``None`` explicitly for ``last_glpi_followup_id`` stores None."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_followup_id=None,
        )
        assert task.last_glpi_followup_id is None

    def test_other_columns_unaffected(self) -> None:
        """Setting the new fields does not affect existing columns."""
        task = Task(
            source="BITRIX24",
            source_id="deal-77",
            type="create_ticket",
            status="pending",
            attempts=0,
            last_glpi_status="failed",
        )
        assert task.source == "BITRIX24"
        assert task.source_id == "deal-77"
        assert task.type == "create_ticket"
        assert task.status == "pending"
        # Other defaults are still correct
        assert task.attempts == 0
        assert task.payload is None


# ===================================================================
# Adversarial / edge cases  —  new fields
# ===================================================================


class TestTaskNewColumnsAdversarial:
    """Edge cases and attack vectors for the two new columns."""

    def test_last_glpi_status_empty_string(self) -> None:
        """An empty string is a valid value for the varchar column."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_status="",
        )
        assert task.last_glpi_status == ""

    def test_last_glpi_status_very_long_string(self) -> None:
        """A very long status string (fits in String(50))."""
        long_status = "x" * 50
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_status=long_status,
        )
        assert task.last_glpi_status == long_status
        assert len(task.last_glpi_status) == 50

    def test_last_glpi_followup_id_zero(self) -> None:
        """Zero is a valid integer value for followup_id."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_followup_id=0,
        )
        assert task.last_glpi_followup_id == 0

    def test_last_glpi_followup_id_negative(self) -> None:
        """Negative integer is a valid value for followup_id."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_followup_id=-1,
        )
        assert task.last_glpi_followup_id == -1

    def test_last_glpi_followup_id_large(self) -> None:
        """A large integer value for followup_id (fits in Integer)."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_followup_id=2_147_483_647,
        )
        assert task.last_glpi_followup_id == 2_147_483_647

    def test_last_glpi_status_unicode(self) -> None:
        """Unicode characters in the status string."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_status="статус_выполнено",
        )
        assert task.last_glpi_status == "статус_выполнено"

    def test_last_glpi_followup_id_max_safe_integer(self) -> None:
        """Python-level max safe integer — SQLAlchemy Integer is 32-bit
        but the Python attribute is unbounded at the model layer."""
        task = Task(
            source="GLPI",
            source_id="1",
            type="test",
            last_glpi_followup_id=2**31 - 1,
        )
        assert task.last_glpi_followup_id == 2147483647


# ===================================================================
# Migration import and structure
# ===================================================================


class TestMigrationImport:
    """The migration revision that adds the two new columns must be
    importable and expose the Alembic contract (upgrade / downgrade).

    The filename ``20260729_120000_add_reverse_sync_columns.py`` starts
    with a digit so we parse it via AST (``alembic.op`` is not available
    in the test environment to execute the module).
    """

    MIGRATION_PATH = "alembic/versions/20260729_120000_add_reverse_sync_columns.py"

    @staticmethod
    def _read_source():
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", TestMigrationImport.MIGRATION_PATH
        )
        with open(os.path.abspath(path)) as f:
            return f.read()

    @staticmethod
    def _parse_ast():
        import ast

        return ast.parse(TestMigrationImport._read_source())

    def test_file_exists(self) -> None:
        """The migration file exists on disk."""
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", self.MIGRATION_PATH
        )
        assert os.path.isfile(os.path.abspath(path))

    def test_has_upgrade_function(self) -> None:
        """The file defines an ``upgrade()`` function."""
        import ast

        tree = self._parse_ast()
        func_names = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        assert "upgrade" in func_names

    def test_has_downgrade_function(self) -> None:
        """The file defines a ``downgrade()`` function."""
        import ast

        tree = self._parse_ast()
        func_names = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        assert "downgrade" in func_names

    def test_revision_variables(self) -> None:
        """The file assigns revision, down_revision, branch_labels,
        depends_on at module level."""
        import ast

        tree = self._parse_ast()
        assigns = {}
        for node in ast.walk(tree):
            # Handle: revision: str = "2f8a1c3b5e7d"   (AnnAssign with value)
            if isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name
            ):
                if isinstance(node.value, ast.Constant):
                    assigns[node.target.id] = node.value.value
                elif isinstance(node.value, ast.Name) and (
                    node.value.id == "None"
                ):
                    assigns[node.target.id] = None
            # Handle: down_revision = "001"   (plain Assign)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(
                        node.value, (ast.Constant, ast.Name)
                    ):
                        if isinstance(node.value, ast.Constant):
                            assigns[target.id] = node.value.value
                        elif isinstance(node.value, ast.Name) and (
                            node.value.id == "None"
                        ):
                            assigns[target.id] = None
        assert assigns.get("revision") == "2f8a1c3b5e7d"
        assert assigns.get("down_revision") == "001"
        assert "branch_labels" in assigns
        assert "depends_on" in assigns

    def test_upgrade_uses_add_column(self) -> None:
        """The ``upgrade()`` function calls ``op.add_column`` for both
        new columns."""
        source = self._read_source()
        assert "op.add_column(" in source
        assert '"last_glpi_status"' in source or "'last_glpi_status'" in source
        assert (
            '"last_glpi_followup_id"' in source
            or "'last_glpi_followup_id'" in source
        )

    def test_downgrade_uses_drop_column(self) -> None:
        """The ``downgrade()`` function calls ``op.drop_column`` for both
        new columns."""
        source = self._read_source()
        assert "op.drop_column(" in source
        assert '"last_glpi_status"' in source or "'last_glpi_status'" in source
        assert (
            '"last_glpi_followup_id"' in source
            or "'last_glpi_followup_id'" in source
        )

    def test_upgrade_adds_both_columns(self) -> None:
        """Verify that ``upgrade`` has exactly 2 ``op.add_column`` calls."""
        source = self._read_source()
        # Find the upgrade function body
        import ast

        tree = self._parse_ast()
        upgrade_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
                upgrade_func = node
                break
        assert upgrade_func is not None

        add_column_calls = 0
        for node in ast.walk(upgrade_func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_column"
            ):
                add_column_calls += 1
        assert add_column_calls == 2, (
            f"Expected 2 op.add_column calls in upgrade(), "
            f"got {add_column_calls}"
        )

    def test_downgrade_drops_both_columns(self) -> None:
        """Verify that ``downgrade`` has exactly 2 ``op.drop_column`` calls."""
        import ast

        tree = self._parse_ast()
        downgrade_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
                downgrade_func = node
                break
        assert downgrade_func is not None

        drop_column_calls = 0
        for node in ast.walk(downgrade_func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "drop_column"
            ):
                drop_column_calls += 1
        assert drop_column_calls == 2, (
            f"Expected 2 op.drop_column calls in downgrade(), "
            f"got {drop_column_calls}"
        )


# ===================================================================
# Column metadata — SQLAlchemy column introspection
# ===================================================================


class TestTaskColumnMetadata:
    """SQLAlchemy column properties for the two new columns."""

    def test_last_glpi_status_is_string_column(self) -> None:
        """``last_glpi_status`` is a String(50) column."""
        col = Task.__table__.columns["last_glpi_status"]
        assert col.type.length == 50
        assert col.nullable is True

    def test_last_glpi_followup_id_is_integer_column(self) -> None:
        """``last_glpi_followup_id`` is an Integer column."""
        col = Task.__table__.columns["last_glpi_followup_id"]
        import sqlalchemy as sa

        assert isinstance(col.type, sa.Integer)
        assert col.nullable is True

    def test_new_columns_are_registered_in_table(self) -> None:
        """Both new column names exist in the ``tasks`` table metadata."""
        assert "last_glpi_status" in Task.__table__.columns
        assert "last_glpi_followup_id" in Task.__table__.columns

    def test_new_columns_are_after_lease_expires_at(self) -> None:
        """The new columns are defined after ``lease_expires_at``
        (positional sanity check — SQLAlchemy uses declaration order)."""
        cols = list(Task.__table__.columns.keys())
        lease_idx = cols.index("lease_expires_at")
        status_idx = cols.index("last_glpi_status")
        followup_idx = cols.index("last_glpi_followup_id")
        assert status_idx > lease_idx, (
            f"last_glpi_status at position {status_idx} should be "
            f"after lease_expires_at at {lease_idx}"
        )
        assert followup_idx > status_idx, (
            f"last_glpi_followup_id at position {followup_idx} should be "
            f"after last_glpi_status at {status_idx}"
        )


# ===================================================================
# Mapped type annotations — type-check level
# ===================================================================


class TestMappedTypeAnnotations:
    """Verify that the Mapped type annotations are correct.

    These tests confirm the Python annotations, not the DB schema.

    Note: ``get_type_hints(Task)`` returns the annotation as declared,
    i.e. ``Mapped[str | None]``.  ``Mapped.__args__`` is a 1-tuple
    ``(str | None,)`` — a single element that is the union type itself.
    ``typing.get_origin`` gives us ``Mapped``, and we drill into the
    args to find the union and its members.
    """

    @staticmethod
    def _unwrap_mapped_inner(hint):
        """Return the inner type from ``Mapped[T]``, or ``None`` if the
        annotation isn't a generic."""
        import typing

        origin = typing.get_origin(hint)
        if origin is None:
            return None
        # Mapped[T] — __args__ is (T,)
        args = typing.get_args(hint)
        return args[0] if args else None

    def test_last_glpi_status_optional_str_annotation(self) -> None:
        """The Mapped annotation for last_glpi_status is ``str | None``."""
        from typing import get_type_hints

        hints = get_type_hints(Task)
        inner = self._unwrap_mapped_inner(hints.get("last_glpi_status"))
        assert inner is not None, "Expected Mapped[str | None] wrapper"

        import typing

        args = typing.get_args(inner)
        # str | None → (str, NoneType)
        assert str in args, f"Expected str in {args}"
        assert type(None) in args, f"Expected NoneType in {args}"

    def test_last_glpi_followup_id_optional_int_annotation(self) -> None:
        """The Mapped annotation for last_glpi_followup_id is ``int | None``."""
        from typing import get_type_hints

        hints = get_type_hints(Task)
        inner = self._unwrap_mapped_inner(hints.get("last_glpi_followup_id"))
        assert inner is not None, "Expected Mapped[int | None] wrapper"

        import typing

        args = typing.get_args(inner)
        assert int in args, f"Expected int in {args}"
        assert type(None) in args, f"Expected NoneType in {args}"
