"""List Velero backups, scored against per-tier RPO policy.

Turns RPO from a static promise into a measured fact: each backup's age is
checked against its tier's RPO target, and the newest backup that both meets
RPO and still matches what's actually running now (images + GitOps
revision) is surfaced as `recommended_backup` - not just the newest backup
by clock time.
"""

from datetime import datetime, timezone
from typing import Any

from mcp.types import ToolAnnotations

from core.policy import get_tier_policy, resolve_tier
from core.server import mcp
from core.utils import get_tool_config
from core.velero import (
    collect_workload_images,
    detect_gitops_revision,
    list_backup_objects,
    parse_backup_metadata,
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Velero Backups",
        readOnlyHint=True,
        destructiveHint=False,
    ),
)
def list_backups(namespace_filter: list[str] | None = None) -> dict[str, Any]:
    """List Velero backups with RPO posture and a recommended restore candidate.

    Args:
        namespace_filter: Only consider backups covering at least one of
            these namespaces. Omit to consider every backup.

    Returns:
        `backups`: every backup's phase, age, tier, and whether it meets its
        tier's RPO target.
        `recommended_backup`: the newest Completed backup that meets RPO,
        with `matches_current_state` set when metadata (from trigger_backup)
        is available to compare against what's running now. Falls back to
        the newest Completed backup (flagged `rpo_breached: true`) if none
        meet RPO.
    """
    tool_config = get_tool_config("list_backups")
    velero_namespace = tool_config.get("velero_namespace", "velero")

    now = datetime.now(timezone.utc)
    backups: list[dict[str, Any]] = []

    for raw in list_backup_objects(velero_namespace):
        spec = raw.get("spec", {})
        status = raw.get("status", {})
        included = spec.get("includedNamespaces", []) or []

        if namespace_filter and not (set(included) & set(namespace_filter)):
            continue

        completion = status.get("completionTimestamp")
        age_minutes = None
        if completion:
            completed_at = datetime.fromisoformat(completion.replace("Z", "+00:00"))
            age_minutes = (now - completed_at).total_seconds() / 60

        tier = resolve_tier(included)
        policy = get_tier_policy(tier)
        rpo_minutes = policy.get("rpo_minutes")
        meets_rpo = (
            age_minutes is not None
            and rpo_minutes is not None
            and age_minutes <= rpo_minutes
        )

        backups.append(
            {
                "name": raw.get("metadata", {}).get("name"),
                "phase": status.get("phase"),
                "included_namespaces": included,
                "completion_timestamp": completion,
                "age_minutes": age_minutes,
                "tier": tier,
                "rpo_minutes": rpo_minutes,
                "rto_minutes": policy.get("rto_minutes"),
                "meets_rpo": meets_rpo,
                "metadata": parse_backup_metadata(raw),
            }
        )

    completed = [b for b in backups if b["phase"] == "Completed"]
    completed.sort(key=lambda b: b["completion_timestamp"] or "", reverse=True)

    recommended = next((b for b in completed if b["meets_rpo"]), None)
    if recommended is None and completed:
        recommended = completed[0]
        recommended["rpo_breached"] = True

    if recommended is not None:
        if recommended["metadata"]:
            current_images = collect_workload_images(recommended["included_namespaces"])
            current_gitops = detect_gitops_revision()
            recommended["current_workload_images"] = current_images
            recommended["current_gitops_revision"] = current_gitops
            recommended["matches_current_state"] = (
                recommended["metadata"].get("workload_images") == current_images
                and recommended["metadata"].get("gitops_revision", {}).get("revision")
                == current_gitops.get("revision")
            )
        else:
            recommended["matches_current_state"] = None

    return {"backups": backups, "recommended_backup": recommended}
