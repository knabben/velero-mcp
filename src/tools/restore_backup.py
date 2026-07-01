"""Restore from a picked Velero backup - advisory by default.

Destructive side of the RTO/RPO policy: full autonomy is fine for backups
and drift checks, but restoring into a live cluster is not something this
tool does unsupervised. Without confirm=True it only returns a plan (the
backup's phase/namespaces) for a human to review; the Restore object is
only created once confirm=True is passed explicitly.
"""

import time
from datetime import datetime
from typing import Any

from mcp.types import ToolAnnotations

from core.server import mcp
from core.utils import get_tool_config
from core.velero import create_restore, get_backup, get_restore

TERMINAL_PHASES = {"Completed", "PartiallyFailed", "Failed", "FailedValidation"}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Restore Velero Backup",
        readOnlyHint=False,
        destructiveHint=True,
    ),
)
def restore_backup(
    backup_name: str,
    confirm: bool = False,
    restore_name: str | None = None,
    wait: bool = True,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Restore from a previously created Velero backup.

    Advisory by default: without confirm=True this only returns a plan (the
    backup's current phase and namespaces) and makes no changes to the
    cluster. Pass confirm=True to actually create the Restore object.

    Args:
        backup_name: Name of an existing Velero Backup object (see `list_backups`).
        confirm: Must be True to actually perform the restore.
        restore_name: Name for the Restore object (defaults to "restore-<backup_name>").
        wait: If True (and confirm=True), block until the restore reaches a
            terminal phase before returning.
        timeout_seconds: Max seconds to wait for completion when wait=True.

    Returns:
        Without confirm: {"action": "plan", ...}, no cluster changes made.
        With confirm: restore phase, duration, and resource/warning/error counts.
    """
    tool_config = get_tool_config("restore_backup")
    velero_namespace = tool_config.get("velero_namespace", "velero")
    poll_interval = tool_config.get("poll_interval_seconds", 5)

    backup = get_backup(backup_name, velero_namespace)
    backup_status = backup.get("status", {})

    if not confirm:
        included_namespaces = backup.get("spec", {}).get("includedNamespaces", [])
        return {
            "action": "plan",
            "backup_name": backup_name,
            "backup_phase": backup_status.get("phase"),
            "included_namespaces": included_namespaces,
            "note": "No changes made. Re-call with confirm=True to restore.",
        }

    restore_name = restore_name or f"restore-{backup_name}"
    create_restore(restore_name, backup_name, velero_namespace)

    result: dict[str, Any] = {
        "action": "restore",
        "restore_name": restore_name,
        "backup_name": backup_name,
        "velero_namespace": velero_namespace,
        "phase": "New",
    }

    if not wait:
        return result

    deadline = time.monotonic() + timeout_seconds
    restore: dict[str, Any] = {}
    while time.monotonic() < deadline:
        restore = get_restore(restore_name, velero_namespace)
        phase = restore.get("status", {}).get("phase", "New")
        result["phase"] = phase
        if phase in TERMINAL_PHASES:
            break
        time.sleep(poll_interval)
    else:
        result["phase"] = "Timeout"
        result["note"] = (
            f"Restore did not reach a terminal phase within {timeout_seconds}s"
        )
        return result

    status = restore.get("status", {})
    start = status.get("startTimestamp")
    completion = status.get("completionTimestamp")
    duration_seconds = None
    if start and completion:
        duration_seconds = (
            datetime.fromisoformat(completion.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))
        ).total_seconds()

    progress = status.get("progress", {}) or {}
    result.update(
        {
            "start_timestamp": start,
            "completion_timestamp": completion,
            "duration_seconds": duration_seconds,
            "resources_restored": progress.get("itemsRestored"),
            "resources_total": progress.get("totalItems"),
            "warnings": status.get("warnings", 0),
            "errors": status.get("errors", 0),
        }
    )
    return result
