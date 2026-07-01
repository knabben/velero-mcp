"""Velero Backup CRD helpers: create/fetch/annotate Backup objects, and
snapshot the workload state (images, GitOps revision) they cover.

Mirrors what `velero backup create` itself does: build a Backup object and
submit it via the Kubernetes API, then poll `status.phase`
(see https://github.com/velero-io/velero/blob/main/pkg/cmd/cli/backup/create.go).
This uses the `kubernetes` CustomObjectsApi instead of controller-runtime.
"""

import json
from typing import Any

from kubernetes.client.exceptions import ApiException

from .k8s import apps_v1_api, custom_objects_api

VELERO_GROUP = "velero.io"
VELERO_VERSION = "v1"
BACKUP_PLURAL = "backups"
RESTORE_PLURAL = "restores"

METADATA_ANNOTATION_KEY = "failover.dev/backup-metadata"

_WORKLOAD_LISTERS = (
    "list_namespaced_deployment",
    "list_namespaced_stateful_set",
    "list_namespaced_daemon_set",
)

# Checked in order; the first GitOps controller with any objects registered wins.
_GITOPS_SOURCES = (
    ("source.toolkit.fluxcd.io", "v1", "gitrepositories", "flux"),
    ("argoproj.io", "v1alpha1", "applications", "argocd"),
)


def create_backup(
    name: str,
    included_namespaces: list[str],
    velero_namespace: str,
    storage_location: str | None = None,
    ttl_hours: int = 720,
) -> dict[str, Any]:
    """Create a velero.io/v1 Backup object."""
    spec: dict[str, Any] = {
        "includedNamespaces": included_namespaces,
        "ttl": f"{ttl_hours}h0m0s",
    }
    if storage_location:
        spec["storageLocation"] = storage_location

    body = {
        "apiVersion": f"{VELERO_GROUP}/{VELERO_VERSION}",
        "kind": "Backup",
        "metadata": {"name": name, "namespace": velero_namespace},
        "spec": spec,
    }
    result: dict[str, Any] = custom_objects_api().create_namespaced_custom_object(
        group=VELERO_GROUP,
        version=VELERO_VERSION,
        namespace=velero_namespace,
        plural=BACKUP_PLURAL,
        body=body,
    )
    return result


def get_backup(name: str, velero_namespace: str) -> dict[str, Any]:
    """Fetch a Backup object by name."""
    result: dict[str, Any] = custom_objects_api().get_namespaced_custom_object(
        group=VELERO_GROUP,
        version=VELERO_VERSION,
        namespace=velero_namespace,
        plural=BACKUP_PLURAL,
        name=name,
    )
    return result


def list_backup_objects(velero_namespace: str) -> list[dict[str, Any]]:
    """List all Backup objects in the Velero namespace."""
    result: dict[str, Any] = custom_objects_api().list_namespaced_custom_object(
        group=VELERO_GROUP,
        version=VELERO_VERSION,
        namespace=velero_namespace,
        plural=BACKUP_PLURAL,
    )
    items: list[dict[str, Any]] = result.get("items", [])
    return items


def parse_backup_metadata(backup: dict[str, Any]) -> dict[str, Any] | None:
    """Parse the structured metadata `trigger_backup` writes onto a Backup object.

    Returns None if the backup has no metadata annotation (e.g. it wasn't
    created through this MCP server) or the annotation isn't valid JSON.
    """
    raw = backup.get("metadata", {}).get("annotations", {}).get(METADATA_ANNOTATION_KEY)
    if not raw:
        return None
    try:
        parsed: dict[str, Any] = json.loads(raw)
        return parsed
    except json.JSONDecodeError:
        return None


def patch_backup_metadata(
    name: str, velero_namespace: str, metadata: dict[str, Any]
) -> None:
    """Write structured completion metadata onto the Backup object as an annotation.

    Keeping this on the Backup object itself (rather than a separate store) means
    it travels with the object and is queryable via kubectl/the K8s API directly.
    """
    patch = {
        "metadata": {
            "annotations": {METADATA_ANNOTATION_KEY: json.dumps(metadata, default=str)}
        }
    }
    custom_objects_api().patch_namespaced_custom_object(
        group=VELERO_GROUP,
        version=VELERO_VERSION,
        namespace=velero_namespace,
        plural=BACKUP_PLURAL,
        name=name,
        body=patch,
    )


def collect_workload_images(namespaces: list[str]) -> dict[str, list[str]]:
    """Collect unique container images running in each namespace.

    Used to check restore staleness against what's actually running now,
    not just against clock time.
    """
    api = apps_v1_api()
    result: dict[str, list[str]] = {}
    for namespace in namespaces:
        images: set[str] = set()
        for lister_name in _WORKLOAD_LISTERS:
            lister = getattr(api, lister_name)
            for item in lister(namespace).items:
                pod_spec = item.spec.template.spec
                containers = list(pod_spec.containers) + list(
                    pod_spec.init_containers or []
                )
                images.update(c.image for c in containers)
        result[namespace] = sorted(images)
    return result


def detect_gitops_revision() -> dict[str, Any]:
    """Best-effort lookup of the GitOps commit currently driving the cluster.

    There's no single canonical source for "the commit currently deployed" -
    it depends on which GitOps controller (if any) manages the cluster. This
    checks the common ones (Flux, Argo CD) and returns the first match found.
    """
    api = custom_objects_api()

    for group, version, plural, source in _GITOPS_SOURCES:
        try:
            items = api.list_cluster_custom_object(group, version, plural).get(
                "items", []
            )
        except ApiException:
            continue
        if not items:
            continue

        obj = items[0]
        if source == "flux":
            revision = obj.get("status", {}).get("artifact", {}).get("revision")
        else:
            revision = obj.get("status", {}).get("sync", {}).get("revision")

        return {
            "source": source,
            "name": obj.get("metadata", {}).get("name"),
            "revision": revision,
        }

    return {"source": None, "name": None, "revision": None}


def create_restore(
    name: str, backup_name: str, velero_namespace: str
) -> dict[str, Any]:
    """Create a velero.io/v1 Restore object referencing an existing Backup."""
    body = {
        "apiVersion": f"{VELERO_GROUP}/{VELERO_VERSION}",
        "kind": "Restore",
        "metadata": {"name": name, "namespace": velero_namespace},
        "spec": {"backupName": backup_name},
    }
    result: dict[str, Any] = custom_objects_api().create_namespaced_custom_object(
        group=VELERO_GROUP,
        version=VELERO_VERSION,
        namespace=velero_namespace,
        plural=RESTORE_PLURAL,
        body=body,
    )
    return result


def get_restore(name: str, velero_namespace: str) -> dict[str, Any]:
    """Fetch a Restore object by name."""
    result: dict[str, Any] = custom_objects_api().get_namespaced_custom_object(
        group=VELERO_GROUP,
        version=VELERO_VERSION,
        namespace=velero_namespace,
        plural=RESTORE_PLURAL,
        name=name,
    )
    return result
