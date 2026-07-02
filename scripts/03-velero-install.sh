#!/usr/bin/env bash
# Pre-conditions:  01-bootstrap.sh and 02-workload-cluster.sh exited 0.
# Post-conditions: MinIO running in workload cluster (NodePort 30900); Velero pod
#                  Ready in management cluster; BSL Available; no credentials on disk.
# Recovery:        Re-run (idempotent). Check MinIO pod logs if BSL stuck.
#
# Architecture: MinIO lives in the workload cluster so backup data survives
# management cluster failure. Velero in the management cluster connects to
# MinIO over the shared Docker bridge network using the workload node's IP.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/helpers.sh
source "$SCRIPT_DIR/helpers.sh"

banner "Step 3 — Velero + MinIO Installation"

KUBECONFIG_FILE="$GENERATED_DIR/${CLUSTER_NAME}.kubeconfig"
if [[ ! -f "$KUBECONFIG_FILE" ]]; then
  error "Workload kubeconfig not found at $KUBECONFIG_FILE — run 02-workload-cluster.sh first"
fi

step 1 "Deploy MinIO in workload cluster"
info "MinIO will run in the workload cluster so backup data survives management cluster loss."
install_minio_workload "$KUBECONFIG_FILE"

step 2 "Resolve workload cluster MinIO endpoint"
MINIO_URL=$(get_workload_minio_url "$KUBECONFIG_FILE")
success "MinIO reachable from management cluster at $MINIO_URL"

step 3 "Install Velero in management cluster"
info "Velero will store backups in workload-cluster MinIO at $MINIO_URL"
install_velero_in_mgmt "$MINIO_URL"

banner "Velero ready ✓"
info "No cloud credentials required or written to disk."
info "MinIO endpoint: $MINIO_URL (workload cluster NodePort)"
kubectl get backupstoragelocation -n "$VELERO_NAMESPACE" 2>/dev/null
echo ""
success "Run ./04-backup.sh to take a targeted backup of CAPI certificate secrets"
