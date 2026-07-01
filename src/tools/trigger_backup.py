"""Trigger a Velero backup and record structured completion metadata.

Creates a velero.io Backup object (the same object `velero backup create`
itself builds and submits), waits for it to reach a terminal phase, then
writes duration/resource-count/warning counts plus the git/image state of
the included namespaces back onto the Backup object as an annotation - so a
later restore can be checked for staleness against what's running now, not
just against clock time.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any

from kubernetes.client.exceptions import ApiException
from mcp.types import ToolAnnotations

from core.server import mcp
from core.utils import get_tool_config
from core.velero import (
    METADATA_ANNOTATION_KEY,
    collect_workload_images,
    create_backup,
    detect_gitops_revision,
    get_backup,
    patch_backup_metadata,
)

TERMINAL_PHASES = {"Completed", "PartiallyFailed", "Failed", "FailedValidation"}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Trigger Velero Backup",
        readOnlyHint=False,
        destructiveHint=False,
    ),
)
def trigger_backup(
    backup_name: str,
    included_namespaces: list[str],
    storage_location: str | None = None,
    ttl_hours: int = 720,
    wait: bool = True,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Trigger a Velero backup and record structured metadata once it completes.

    Args:
        backup_name: Name for the Velero Backup object (must be unique).
        included_namespaces: Kubernetes namespaces to include in the backup.
        storage_location: Velero BackupStorageLocation name (defaults to Velero's own).
        ttl_hours: Backup retention window in hours (default 30 days).
        wait: If True, block until the backup reaches a terminal phase before returning.
        timeout_seconds: Max seconds to wait for completion when wait=True.

    Returns:
        Structured metadata: phase, duration, resource counts, warning/error counts,
        and the git/image state of included_namespaces at backup time.
    """
    tool_config = get_tool_config("trigger_backup")
    velero_namespace = tool_config.get("velero_namespace", "velero")
    poll_interval = tool_config.get("poll_interval_seconds", 5)

    triggered_at = datetime.now(timezone.utc)

    create_backup(
        name=backup_name,
        included_namespaces=included_namespaces,
        storage_location=storage_location,
        ttl_hours=ttl_hours,
        velero_namespace=velero_namespace,
    )

    result: dict[str, Any] = {
        "backup_name": backup_name,
        "velero_namespace": velero_namespace,
        "included_namespaces": included_namespaces,
        "triggered_at": triggered_at.isoformat(),
        "phase": "New",
    }

    if not wait:
        return result

    deadline = time.monotonic() + timeout_seconds
    backup: dict[str, Any] = {}
    while time.monotonic() < deadline:
        backup = get_backup(backup_name, velero_namespace)
        phase = backup.get("status", {}).get("phase", "New")
        result["phase"] = phase
        if phase in TERMINAL_PHASES:
            break
        time.sleep(poll_interval)
    else:
        result["phase"] = "Timeout"
        result["note"] = (
            f"Backup did not reach a terminal phase within {timeout_seconds}s"
        )
        return result

    status = backup.get("status", {})
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
            "resources_backed_up": progress.get("itemsBackedUp"),
            "resources_total": progress.get("totalItems"),
            "warnings": status.get("warnings", 0),
            "errors": status.get("errors", 0),
            "gitops_revision": detect_gitops_revision(),
            "workload_images": collect_workload_images(included_namespaces),
        }
    )

    try:
        patch_backup_metadata(backup_name, velero_namespace, result)
        result["metadata_annotation"] = METADATA_ANNOTATION_KEY
    except ApiException as e:
        logging.error(
            f"Failed to write metadata annotation on backup {backup_name}: {e}"
        )
        result["metadata_write_error"] = str(e)

    return result
