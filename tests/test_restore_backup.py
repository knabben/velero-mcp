"""Tests for the restore_backup MCP tool (src/tools/restore_backup.py).

See tests/test_trigger_backup.py for why `rb` is fetched via
importlib.import_module rather than a normal import statement.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

rb = importlib.import_module("tools.restore_backup")

TOOL = rb.restore_backup


class TestRestoreBackupPlan:
    """Without confirm=True, nothing touches the cluster."""

    def test_returns_plan_without_creating_restore(self) -> None:
        backup = {
            "status": {"phase": "Completed"},
            "spec": {"includedNamespaces": ["payments"]},
        }
        with (
            patch.object(rb, "get_tool_config", return_value={}),
            patch.object(rb, "get_backup", return_value=backup) as mock_get_backup,
            patch.object(rb, "create_restore") as mock_create,
        ):
            result = TOOL(backup_name="nightly")

        mock_get_backup.assert_called_once_with("nightly", "velero")
        mock_create.assert_not_called()
        assert result["action"] == "plan"
        assert result["backup_phase"] == "Completed"
        assert result["included_namespaces"] == ["payments"]


class TestRestoreBackupConfirmed:
    def test_completed_restore_collects_metadata(self) -> None:
        backup = {"status": {"phase": "Completed"}, "spec": {}}
        completed_restore = {
            "status": {
                "phase": "Completed",
                "startTimestamp": "2026-07-01T10:00:00Z",
                "completionTimestamp": "2026-07-01T10:11:00Z",
                "progress": {"itemsRestored": 40, "totalItems": 42},
                "warnings": 2,
                "errors": 0,
            }
        }
        with (
            patch.object(rb, "get_tool_config", return_value={}),
            patch.object(rb, "get_backup", return_value=backup),
            patch.object(rb, "create_restore") as mock_create,
            patch.object(rb, "get_restore", return_value=completed_restore),
            patch.object(rb.time, "sleep"),
        ):
            result = TOOL(backup_name="nightly", confirm=True, timeout_seconds=30)

        mock_create.assert_called_once_with("restore-nightly", "nightly", "velero")
        assert result["action"] == "restore"
        assert result["phase"] == "Completed"
        assert result["duration_seconds"] == 660.0
        assert result["resources_restored"] == 40
        assert result["resources_total"] == 42
        assert result["warnings"] == 2

    def test_custom_restore_name_is_used(self) -> None:
        backup = {"status": {"phase": "Completed"}, "spec": {}}
        with (
            patch.object(rb, "get_tool_config", return_value={}),
            patch.object(rb, "get_backup", return_value=backup),
            patch.object(rb, "create_restore") as mock_create,
        ):
            result = TOOL(
                backup_name="nightly",
                confirm=True,
                restore_name="my-restore",
                wait=False,
            )

        mock_create.assert_called_once_with("my-restore", "nightly", "velero")
        assert result["restore_name"] == "my-restore"

    def test_no_wait_returns_immediately(self) -> None:
        backup = {"status": {"phase": "Completed"}, "spec": {}}
        with (
            patch.object(rb, "get_tool_config", return_value={}),
            patch.object(rb, "get_backup", return_value=backup),
            patch.object(rb, "create_restore"),
            patch.object(rb, "get_restore") as mock_get_restore,
        ):
            result = TOOL(backup_name="nightly", confirm=True, wait=False)

        mock_get_restore.assert_not_called()
        assert result["phase"] == "New"

    def test_timeout_reports_timeout_phase(self) -> None:
        backup = {"status": {"phase": "Completed"}, "spec": {}}
        with (
            patch.object(rb, "get_tool_config", return_value={}),
            patch.object(rb, "get_backup", return_value=backup),
            patch.object(rb, "create_restore"),
            patch.object(rb, "get_restore") as mock_get_restore,
        ):
            result = TOOL(backup_name="nightly", confirm=True, timeout_seconds=0)

        mock_get_restore.assert_not_called()
        assert result["phase"] == "Timeout"
        assert "note" in result

    def test_uses_configured_velero_namespace(self) -> None:
        backup = {"status": {"phase": "Completed"}, "spec": {}}
        with (
            patch.object(
                rb, "get_tool_config", return_value={"velero_namespace": "backups-ns"}
            ),
            patch.object(rb, "get_backup", return_value=backup) as mock_get_backup,
            patch.object(rb, "create_restore") as mock_create,
        ):
            result = TOOL(backup_name="nightly", confirm=True, wait=False)

        mock_get_backup.assert_called_once_with("nightly", "backups-ns")
        mock_create.assert_called_once_with("restore-nightly", "nightly", "backups-ns")
        assert result["velero_namespace"] == "backups-ns"
