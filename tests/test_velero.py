"""Tests for Velero Backup CRD helpers (src/core/velero.py)."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core import velero  # noqa: E402


class TestCreateBackup:
    """Test Backup object construction and submission."""

    def test_create_backup_builds_expected_body(self) -> None:
        mock_api = MagicMock()
        with patch("core.velero.custom_objects_api", return_value=mock_api):
            velero.create_backup(
                name="nightly-2026-07-01",
                included_namespaces=["default", "payments"],
                velero_namespace="velero",
                storage_location="aws-primary",
                ttl_hours=48,
            )

        mock_api.create_namespaced_custom_object.assert_called_once()
        kwargs = mock_api.create_namespaced_custom_object.call_args.kwargs
        assert kwargs["group"] == "velero.io"
        assert kwargs["version"] == "v1"
        assert kwargs["namespace"] == "velero"
        assert kwargs["plural"] == "backups"
        body = kwargs["body"]
        assert body["metadata"]["name"] == "nightly-2026-07-01"
        assert body["spec"]["includedNamespaces"] == ["default", "payments"]
        assert body["spec"]["storageLocation"] == "aws-primary"
        assert body["spec"]["ttl"] == "48h0m0s"

    def test_create_backup_omits_storage_location_when_not_given(self) -> None:
        mock_api = MagicMock()
        with patch("core.velero.custom_objects_api", return_value=mock_api):
            velero.create_backup(
                name="nightly",
                included_namespaces=["default"],
                velero_namespace="velero",
            )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        assert "storageLocation" not in body["spec"]


class TestPatchBackupMetadata:
    """Test writing structured metadata back onto the Backup object."""

    def test_patch_writes_json_annotation(self) -> None:
        mock_api = MagicMock()
        with patch("core.velero.custom_objects_api", return_value=mock_api):
            velero.patch_backup_metadata(
                "nightly", "velero", {"phase": "Completed", "warnings": 0}
            )

        kwargs = mock_api.patch_namespaced_custom_object.call_args.kwargs
        assert kwargs["name"] == "nightly"
        assert kwargs["namespace"] == "velero"
        annotation = kwargs["body"]["metadata"]["annotations"][
            velero.METADATA_ANNOTATION_KEY
        ]
        assert '"phase": "Completed"' in annotation


class TestCollectWorkloadImages:
    """Test snapshotting container images running in a namespace."""

    def _fake_item(
        self, images: list[str], init_images: list[str] | None = None
    ) -> SimpleNamespace:
        containers = [SimpleNamespace(image=i) for i in images]
        init_containers = (
            [SimpleNamespace(image=i) for i in init_images] if init_images else None
        )
        pod_spec = SimpleNamespace(
            containers=containers, init_containers=init_containers
        )
        return SimpleNamespace(
            spec=SimpleNamespace(template=SimpleNamespace(spec=pod_spec))
        )

    def test_collects_unique_images_across_workload_kinds(self) -> None:
        mock_api = MagicMock()
        mock_api.list_namespaced_deployment.return_value = SimpleNamespace(
            items=[self._fake_item(["app:v1.2.3"])]
        )
        mock_api.list_namespaced_stateful_set.return_value = SimpleNamespace(
            items=[self._fake_item(["db:v9"], init_images=["migrate:v9"])]
        )
        mock_api.list_namespaced_daemon_set.return_value = SimpleNamespace(
            items=[self._fake_item(["app:v1.2.3"])]  # duplicate of deployment image
        )

        with patch("core.velero.apps_v1_api", return_value=mock_api):
            result = velero.collect_workload_images(["default"])

        assert result["default"] == ["app:v1.2.3", "db:v9", "migrate:v9"]


class TestDetectGitopsRevision:
    """Test best-effort GitOps commit detection."""

    def test_prefers_flux_when_present(self) -> None:
        mock_api = MagicMock()
        mock_api.list_cluster_custom_object.side_effect = [
            {
                "items": [
                    {
                        "metadata": {"name": "demo-repo"},
                        "status": {"artifact": {"revision": "main@sha1:abc123"}},
                    }
                ]
            }
        ]

        with patch("core.velero.custom_objects_api", return_value=mock_api):
            result = velero.detect_gitops_revision()

        assert result == {
            "source": "flux",
            "name": "demo-repo",
            "revision": "main@sha1:abc123",
        }

    def test_falls_back_to_argocd_when_flux_absent(self) -> None:
        mock_api = MagicMock()

        def side_effect(group: str, version: str, plural: str) -> dict:
            if plural == "gitrepositories":
                raise ApiException(status=404)
            return {
                "items": [
                    {
                        "metadata": {"name": "demo-app"},
                        "status": {"sync": {"revision": "def456"}},
                    }
                ]
            }

        mock_api.list_cluster_custom_object.side_effect = side_effect

        with patch("core.velero.custom_objects_api", return_value=mock_api):
            result = velero.detect_gitops_revision()

        assert result == {"source": "argocd", "name": "demo-app", "revision": "def456"}

    def test_returns_none_when_no_gitops_controller_found(self) -> None:
        mock_api = MagicMock()
        mock_api.list_cluster_custom_object.side_effect = ApiException(status=404)

        with patch("core.velero.custom_objects_api", return_value=mock_api):
            result = velero.detect_gitops_revision()

        assert result == {"source": None, "name": None, "revision": None}


class TestListBackupObjects:
    def test_returns_items_list(self) -> None:
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "nightly-1"}}]
        }

        with patch("core.velero.custom_objects_api", return_value=mock_api):
            result = velero.list_backup_objects("velero")

        kwargs = mock_api.list_namespaced_custom_object.call_args.kwargs
        assert kwargs["namespace"] == "velero"
        assert kwargs["plural"] == "backups"
        assert result == [{"metadata": {"name": "nightly-1"}}]

    def test_missing_items_key_returns_empty_list(self) -> None:
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {}

        with patch("core.velero.custom_objects_api", return_value=mock_api):
            assert velero.list_backup_objects("velero") == []


class TestParseBackupMetadata:
    def test_parses_valid_json_annotation(self) -> None:
        backup = {
            "metadata": {
                "annotations": {
                    velero.METADATA_ANNOTATION_KEY: '{"phase": "Completed"}'
                }
            }
        }
        assert velero.parse_backup_metadata(backup) == {"phase": "Completed"}

    def test_missing_annotation_returns_none(self) -> None:
        assert velero.parse_backup_metadata({"metadata": {"annotations": {}}}) is None

    def test_invalid_json_returns_none(self) -> None:
        backup = {
            "metadata": {"annotations": {velero.METADATA_ANNOTATION_KEY: "not-json"}}
        }
        assert velero.parse_backup_metadata(backup) is None


class TestCreateRestore:
    def test_builds_expected_body(self) -> None:
        mock_api = MagicMock()
        with patch("core.velero.custom_objects_api", return_value=mock_api):
            velero.create_restore("restore-1", "nightly-1", "velero")

        kwargs = mock_api.create_namespaced_custom_object.call_args.kwargs
        assert kwargs["namespace"] == "velero"
        assert kwargs["plural"] == "restores"
        body = kwargs["body"]
        assert body["metadata"]["name"] == "restore-1"
        assert body["spec"]["backupName"] == "nightly-1"


class TestGetRestore:
    def test_fetches_by_name(self) -> None:
        mock_api = MagicMock()
        with patch("core.velero.custom_objects_api", return_value=mock_api):
            velero.get_restore("restore-1", "velero")

        kwargs = mock_api.get_namespaced_custom_object.call_args.kwargs
        assert kwargs["name"] == "restore-1"
        assert kwargs["namespace"] == "velero"
        assert kwargs["plural"] == "restores"
