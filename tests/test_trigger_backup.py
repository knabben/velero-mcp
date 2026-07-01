"""Tests for the trigger_backup MCP tool (src/tools/trigger_backup.py).

Patches target attributes on the imported `tb` module object directly
(via patch.object) rather than the "tools.trigger_backup.xxx" string path.
DynamicMCPServer (exercised by test_server.py/test_tools.py) reloads every
file under src/tools/ through importlib and overwrites
sys.modules["tools.trigger_backup"] with a fresh module object, so
string-path patching can silently patch a different module instance than
the one `tb.trigger_backup` actually closes over.

`tb` is fetched via importlib.import_module (not `import tools.trigger_backup
as tb`): tools/__init__.py does `from .trigger_backup import trigger_backup`,
which - because the imported name matches the submodule name - overwrites
the `tools.trigger_backup` *attribute* on the tools package with the
function itself, shadowing the submodule. importlib.import_module reads
sys.modules directly and isn't affected by that shadowing.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

tb = importlib.import_module("tools.trigger_backup")


TOOL = tb.trigger_backup


class TestTriggerBackupNoWait:
    """Test the fire-and-forget path (wait=False)."""

    def test_returns_immediately_without_polling(self) -> None:
        with (
            patch.object(tb, "get_tool_config", return_value={}),
            patch.object(tb, "create_backup") as mock_create,
            patch.object(tb, "get_backup") as mock_get,
        ):
            result = TOOL(
                backup_name="nightly",
                included_namespaces=["default"],
                wait=False,
            )

        mock_create.assert_called_once()
        mock_get.assert_not_called()
        assert result["phase"] == "New"
        assert result["backup_name"] == "nightly"


class TestTriggerBackupWait:
    """Test the polling path (wait=True)."""

    def test_completed_backup_collects_metadata_and_annotates(self) -> None:
        completed_backup = {
            "status": {
                "phase": "Completed",
                "startTimestamp": "2026-07-01T10:00:00Z",
                "completionTimestamp": "2026-07-01T10:02:30Z",
                "progress": {"itemsBackedUp": 42, "totalItems": 42},
                "warnings": 1,
                "errors": 0,
            }
        }
        gitops = {"source": "flux", "name": "demo-repo", "revision": "main@sha1:abc"}
        images = {"default": ["app:v1.2.3"]}

        with (
            patch.object(tb, "get_tool_config", return_value={}),
            patch.object(tb, "create_backup") as mock_create,
            patch.object(tb, "get_backup", return_value=completed_backup) as mock_get,
            patch.object(tb, "detect_gitops_revision", return_value=gitops),
            patch.object(tb, "collect_workload_images", return_value=images),
            patch.object(tb, "patch_backup_metadata") as mock_patch,
            patch.object(tb.time, "sleep"),
        ):
            result = TOOL(
                backup_name="nightly",
                included_namespaces=["default"],
                wait=True,
                timeout_seconds=30,
            )

        mock_create.assert_called_once()
        mock_get.assert_called_once()
        assert result["phase"] == "Completed"
        assert result["duration_seconds"] == 150.0
        assert result["resources_backed_up"] == 42
        assert result["resources_total"] == 42
        assert result["warnings"] == 1
        assert result["gitops_revision"] == gitops
        assert result["workload_images"] == images

        mock_patch.assert_called_once_with("nightly", "velero", result)
        assert result["metadata_annotation"] == "failover.dev/backup-metadata"

    def test_timeout_reports_timeout_phase_without_metadata_collection(self) -> None:
        with (
            patch.object(tb, "get_tool_config", return_value={}),
            patch.object(tb, "create_backup"),
            patch.object(tb, "get_backup") as mock_get,
            patch.object(tb, "patch_backup_metadata") as mock_patch,
        ):
            result = TOOL(
                backup_name="nightly",
                included_namespaces=["default"],
                wait=True,
                timeout_seconds=0,
            )

        mock_get.assert_not_called()
        mock_patch.assert_not_called()
        assert result["phase"] == "Timeout"
        assert "note" in result

    def test_uses_configured_velero_namespace(self) -> None:
        completed_backup = {
            "status": {
                "phase": "Completed",
                "startTimestamp": "2026-07-01T10:00:00Z",
                "completionTimestamp": "2026-07-01T10:00:05Z",
            }
        }
        with (
            patch.object(
                tb, "get_tool_config", return_value={"velero_namespace": "backups-ns"}
            ),
            patch.object(tb, "create_backup") as mock_create,
            patch.object(tb, "get_backup", return_value=completed_backup),
            patch.object(tb, "detect_gitops_revision", return_value={}),
            patch.object(tb, "collect_workload_images", return_value={}),
            patch.object(tb, "patch_backup_metadata"),
        ):
            result = TOOL(
                backup_name="nightly",
                included_namespaces=["default"],
            )

        assert result["velero_namespace"] == "backups-ns"
        assert mock_create.call_args.kwargs["velero_namespace"] == "backups-ns"
