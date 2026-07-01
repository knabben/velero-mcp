"""Tests for the list_backups MCP tool (src/tools/list_backups.py).

See tests/test_trigger_backup.py for why `lb` is fetched via
importlib.import_module rather than a normal import statement.

Backup completion timestamps are computed relative to the real clock at test
run time (not hardcoded dates) so these tests don't depend on wall-clock date.
"""

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

lb = importlib.import_module("tools.list_backups")

TOOL = lb.list_backups

FAKE_POLICY_CONFIG = {
    "policies": {
        "default_tier": "standard",
        "tiers": {
            "critical": {"rpo_minutes": 60, "rto_minutes": 15},
            "standard": {"rpo_minutes": 1440, "rto_minutes": 240},
        },
        "namespace_tiers": {"payments": "critical"},
    }
}


def _iso(minutes_ago: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return when.isoformat().replace("+00:00", "Z")


def _backup(
    name: str,
    phase: str,
    minutes_ago: float | None,
    namespaces: list[str],
    metadata: dict | None = None,
) -> dict:
    annotations = {}
    if metadata is not None:
        annotations["failover.dev/backup-metadata"] = json.dumps(metadata)
    completion = _iso(minutes_ago) if minutes_ago is not None else None
    return {
        "metadata": {"name": name, "annotations": annotations},
        "spec": {"includedNamespaces": namespaces},
        "status": {"phase": phase, "completionTimestamp": completion},
    }


class TestListBackups:
    def test_flags_backup_within_rpo(self) -> None:
        with (
            patch.object(lb, "get_tool_config", return_value={}),
            patch("core.policy.load_config", return_value=FAKE_POLICY_CONFIG),
            patch.object(
                lb,
                "list_backup_objects",
                return_value=[_backup("recent", "Completed", 10, ["payments"])],
            ),
        ):
            result = TOOL()

        entry = result["backups"][0]
        assert entry["tier"] == "critical"
        assert entry["rpo_minutes"] == 60
        assert entry["meets_rpo"] is True
        assert result["recommended_backup"]["name"] == "recent"
        assert "rpo_breached" not in result["recommended_backup"]

    def test_recommends_newest_completed_when_none_meet_rpo(self) -> None:
        with (
            patch.object(lb, "get_tool_config", return_value={}),
            patch("core.policy.load_config", return_value=FAKE_POLICY_CONFIG),
            patch.object(
                lb,
                "list_backup_objects",
                return_value=[_backup("ancient", "Completed", 120, ["payments"])],
            ),
        ):
            result = TOOL()

        recommended = result["recommended_backup"]
        assert recommended["name"] == "ancient"
        assert recommended["meets_rpo"] is False
        assert recommended["rpo_breached"] is True

    def test_namespace_filter_excludes_non_matching_backups(self) -> None:
        with (
            patch.object(lb, "get_tool_config", return_value={}),
            patch("core.policy.load_config", return_value=FAKE_POLICY_CONFIG),
            patch.object(
                lb,
                "list_backup_objects",
                return_value=[
                    _backup("a", "Completed", 10, ["payments"]),
                    _backup("b", "Completed", 10, ["other-ns"]),
                ],
            ),
        ):
            result = TOOL(namespace_filter=["payments"])

        assert [b["name"] for b in result["backups"]] == ["a"]

    def test_matches_current_state_true_when_metadata_matches_live(self) -> None:
        metadata = {
            "workload_images": {"payments": ["app:v1"]},
            "gitops_revision": {"revision": "main@sha1:abc"},
        }
        with (
            patch.object(lb, "get_tool_config", return_value={}),
            patch("core.policy.load_config", return_value=FAKE_POLICY_CONFIG),
            patch.object(
                lb,
                "list_backup_objects",
                return_value=[
                    _backup("recent", "Completed", 1, ["payments"], metadata=metadata)
                ],
            ),
            patch.object(
                lb, "collect_workload_images", return_value={"payments": ["app:v1"]}
            ),
            patch.object(
                lb, "detect_gitops_revision", return_value={"revision": "main@sha1:abc"}
            ),
        ):
            result = TOOL()

        assert result["recommended_backup"]["matches_current_state"] is True

    def test_matches_current_state_false_on_image_drift(self) -> None:
        metadata = {
            "workload_images": {"payments": ["app:v1"]},
            "gitops_revision": {"revision": "main@sha1:abc"},
        }
        with (
            patch.object(lb, "get_tool_config", return_value={}),
            patch("core.policy.load_config", return_value=FAKE_POLICY_CONFIG),
            patch.object(
                lb,
                "list_backup_objects",
                return_value=[
                    _backup("recent", "Completed", 1, ["payments"], metadata=metadata)
                ],
            ),
            patch.object(
                lb, "collect_workload_images", return_value={"payments": ["app:v2"]}
            ),
            patch.object(
                lb, "detect_gitops_revision", return_value={"revision": "main@sha1:abc"}
            ),
        ):
            result = TOOL()

        assert result["recommended_backup"]["matches_current_state"] is False

    def test_matches_current_state_none_without_metadata(self) -> None:
        with (
            patch.object(lb, "get_tool_config", return_value={}),
            patch("core.policy.load_config", return_value=FAKE_POLICY_CONFIG),
            patch.object(
                lb,
                "list_backup_objects",
                return_value=[_backup("recent", "Completed", 1, ["payments"])],
            ),
        ):
            result = TOOL()

        assert result["recommended_backup"]["matches_current_state"] is None

    def test_no_completed_backups_returns_none_recommendation(self) -> None:
        with (
            patch.object(lb, "get_tool_config", return_value={}),
            patch("core.policy.load_config", return_value=FAKE_POLICY_CONFIG),
            patch.object(
                lb,
                "list_backup_objects",
                return_value=[_backup("failed", "Failed", None, ["payments"])],
            ),
        ):
            result = TOOL()

        assert result["recommended_backup"] is None
