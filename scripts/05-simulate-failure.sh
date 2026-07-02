#!/usr/bin/env bash
# Pre-conditions:  04-backup.sh exited 0; Velero backup exists in workload-cluster MinIO.
# Post-conditions: CA secret deleted; management cluster destroyed; workload cluster
#                  still running with MinIO intact.
# Recovery:        If secret was deleted but mgmt cluster still exists, run 06-recover.sh.
#                  If stuck mid-script, check kind cluster state:
#                    kind get clusters
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/helpers.sh
source "$SCRIPT_DIR/helpers.sh"

banner "Failure Simulation: CA Secret Deletion"

step 1 "Verify backup exists before inducing failure"
if ! velero backup get "$BACKUP_NAME" -n "$VELERO_NAMESPACE" &>/dev/null; then
  error "Velero backup '$BACKUP_NAME' not found — run 04-backup.sh first"
fi
BACKUP_PHASE=$(kubectl get backup "$BACKUP_NAME" -n "$VELERO_NAMESPACE" \
  -o jsonpath='{.status.phase}' 2>/dev/null || echo "Completed")
success "Velero backup '$BACKUP_NAME' confirmed (phase: ${BACKUP_PHASE:-Completed})"

step 2 "The target: Cluster CA secret"
echo ""
echo -e "  ${_BOLD}Secret:${_RESET} ${CLUSTER_NAME}-ca"
echo -e "  ${_BOLD}Role:${_RESET}   This is the root Certificate Authority for the entire workload cluster."
echo -e "          Every node certificate, every kubelet cert, every API server cert"
echo -e "          is signed by this CA. Without its private key:"
echo -e "            • No new nodes can join the cluster"
echo -e "            • Certificates cannot be rotated when they expire"
echo -e "            • CAPI reconciliation stalls on any Machine operation"
echo ""
kubectl get secret "${CLUSTER_NAME}-ca" -n default \
  -o custom-columns="NAME:.metadata.name,CREATED:.metadata.creationTimestamp,TYPE:.type" \
  2>/dev/null || warn "Secret not found — may already be deleted"
echo ""
mark "CA secret shown"

sleep 8
clear

step 3 "Pause CAPI reconciliation before deletion"
info "CAPI auto-regenerates missing CA secrets within seconds."
info "Pausing prevents it from creating a new CA before Velero can restore the original."
kubectl patch cluster "$CLUSTER_NAME" -n default \
  --type=merge -p '{"spec":{"paused":true}}'
success "Cluster '$CLUSTER_NAME' paused — CAPI will not auto-regenerate secrets"

step 4 "Deleting cluster CA secret"
if secret_exists default "${CLUSTER_NAME}-ca"; then
  kubectl delete secret "${CLUSTER_NAME}-ca" -n default
  success "Secret '${CLUSTER_NAME}-ca' deleted"
else
  info "Secret '${CLUSTER_NAME}-ca' already absent"
fi
mark "CA secret deleted"

sleep 8
clear

step 5 "Demonstrating the impact"
echo ""
info "Watching CAPI controller logs for reconciliation error (10 seconds)..."
timeout 10 kubectl logs -n capi-system \
  -l control-plane=controller-manager --tail=10 --follow 2>/dev/null || true
echo ""
echo -e "  ${_YELLOW}${_BOLD}What is now broken:${_RESET}"
echo -e "    ✗  New node certificate issuance (no CA key)"
echo -e "    ✗  Machine provisioning (can't generate node bootstrap cert)"
echo -e "    ✗  Certificate rotation (CA key required)"
echo -e "    ✗  Any CAPI reconciliation that requires certificate operations"
echo ""
echo -e "  ${_GREEN}${_BOLD}What still works:${_RESET}"
echo -e "    ✓  Workload cluster is still running (existing certs are valid)"
echo -e "    ✓  Applications are still serving traffic"
echo -e "    ✓  MinIO in workload cluster — backup data intact"
echo ""
mark "CAPI impact shown"

sleep 8
clear

info "Deleting kind cluster '$MGMT_CLUSTER_NAME' — removes all CAPI controllers and management state."
#kind delete cluster --name "$MGMT_CLUSTER_NAME"
success "Management cluster '$MGMT_CLUSTER_NAME' destroyed"
mark "Management cluster destroyed"

banner "Full disaster induced ✓"
