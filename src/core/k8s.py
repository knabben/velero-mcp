"""Kubernetes client helpers for velero MCP server.

Works both in-cluster (ServiceAccount token) and out-of-cluster - e.g. running
via `kmcp run` / `uv run` on a dev machine, pointed at a remote or local
cluster through kubeconfig.
"""

import logging
from functools import lru_cache

from kubernetes import client, config

from .utils import load_config


@lru_cache(maxsize=1)
def _load_kube_config() -> None:
    """Load Kubernetes config once, preferring in-cluster credentials."""
    try:
        config.load_incluster_config()
        logging.info("Loaded in-cluster Kubernetes config")
        return
    except config.ConfigException:
        pass

    # Out-of-cluster: use kubeconfig (KUBECONFIG env var or ~/.kube/config),
    # optionally pinned to a specific context via kmcp.yaml `kubernetes.context`
    # so the server doesn't silently depend on whatever context is current.
    context = (load_config("kmcp.yaml").get("kubernetes") or {}).get("context") or None
    config.load_kube_config(context=context)
    logging.info(f"Loaded kubeconfig (context={context or 'current-context'})")


def custom_objects_api() -> client.CustomObjectsApi:
    """Client for Velero and other CRDs (Backup, GitRepository, Application, ...)."""
    _load_kube_config()
    return client.CustomObjectsApi()


def apps_v1_api() -> client.AppsV1Api:
    """Client for Deployments/StatefulSets/DaemonSets, to snapshot workload images."""
    _load_kube_config()
    return client.AppsV1Api()
