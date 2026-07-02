#!/usr/bin/env bash
# Pre-conditions:  05-simulate-failure.sh exited 0; CA secret deleted; cluster paused.
# Post-conditions: CA secret restored; cluster unpaused; workload nodes reachable.
# Recovery:        Re-run safe — each run creates a uniquely named Velero restore object.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/helpers.sh
source "$SCRIPT_DIR/helpers.sh"

banner "CA Secret Recovery"

step 1 "Verify backup and current state"
info "Running: kubectl get backup.velero.io $BACKUP_NAME -n $VELERO_NAMESPACE"
if ! kubectl get backup.velero.io "$BACKUP_NAME" -n "$VELERO_NAMESPACE" &>/dev/null; then
  error "Velero backup '$BACKUP_NAME' not found — run 04-backup.sh first"
fi
kubectl get backup.velero.io "$BACKUP_NAME" -n "$VELERO_NAMESPACE" \
  --no-headers -o custom-columns="NAME:.metadata.name,PHASE:.status.phase,ERRORS:.status.errors,CREATED:.metadata.creationTimestamp"
BACKUP_PHASE=$(kubectl get backup.velero.io "$BACKUP_NAME" -n "$VELERO_NAMESPACE" \
  -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
success "Velero backup '$BACKUP_NAME' confirmed (phase: ${BACKUP_PHASE})"

echo ""
info "Current CA secret state:"
if secret_exists default "${CLUSTER_NAME}-ca"; then
  echo -e "  ${_GREEN}✓${_RESET} ${CLUSTER_NAME}-ca — present"
else
  echo -e "  ${_RED}✗${_RESET} ${CLUSTER_NAME}-ca — missing (expected)"
fi
echo ""

sleep 5
clear

step 2 "Restore — recover CA secret from Velero backup"
RESTORE_NAME="${BACKUP_NAME}-restore-$(date +%Y%m%d%H%M%S)"
info "Running: velero restore create $RESTORE_NAME --from-backup $BACKUP_NAME --include-resources secrets --existing-resource-policy=update --wait"
velero restore create "$RESTORE_NAME" \
  --from-backup "$BACKUP_NAME" \
  --include-resources secrets \
  --existing-resource-policy=update \
  --wait \
  --namespace "$VELERO_NAMESPACE"

info "Running: kubectl get restore.velero.io $RESTORE_NAME -n $VELERO_NAMESPACE"
kubectl get restore.velero.io "$RESTORE_NAME" -n "$VELERO_NAMESPACE" \
  --no-headers -o custom-columns="NAME:.metadata.name,PHASE:.status.phase,WARNINGS:.status.warnings,ERRORS:.status.errors"
success "Restore '$RESTORE_NAME' completed"

echo ""
info "CA secret state after restore:"
if secret_exists default "${CLUSTER_NAME}-ca"; then
  echo -e "  ${_GREEN}✓${_RESET} ${CLUSTER_NAME}-ca — restored"
else
  echo -e "  ${_RED}✗${_RESET} ${CLUSTER_NAME}-ca — still missing"
  error "Restore did not recover the CA secret"
fi
echo ""
mark "CA secret restored"

sleep 5
clear

step 3 "Adopt — unpause CAPI reconciliation"
info "Cluster was paused before deletion to prevent CA auto-regeneration."
info "Unpausing lets controllers resume reconciliation with the restored CA."
info "Running: kubectl patch cluster $CLUSTER_NAME -n default --type=merge -p '{\"spec\":{\"paused\":false}}'"
kubectl patch cluster "$CLUSTER_NAME" -n default \
  --type=merge -p '{"spec":{"paused":false}}'
success "Cluster '$CLUSTER_NAME' unpaused — controllers resuming reconciliation"

banner "Recovery complete ✓"

sleep 5
clear

banner "Recovery Verification"

step 4 "Verify all five CA secrets are restored"
ALL_OK=true
for s in "${CLUSTER_NAME}-ca" "${CLUSTER_NAME}-etcd" "${CLUSTER_NAME}-proxy" \
         "${CLUSTER_NAME}-sa" "${CLUSTER_NAME}-kubeconfig"; do
  if secret_exists default "$s"; then
    echo -e "  ${_GREEN}✓${_RESET} $s"
  else
    echo -e "  ${_RED}✗${_RESET} $s — MISSING"
    ALL_OK=false
  fi
done
if [[ "$ALL_OK" != "true" ]]; then
  error "One or more secrets missing — re-run from step 2"
fi
success "All five CA secrets present"
mark "Secrets verified"

step 5 "Verify CAPI objects"
echo ""
info "Running: kubectl get cluster,machinedeployment,machineset,machine -l cluster.x-k8s.io/cluster-name=$CLUSTER_NAME -A"
kubectl get cluster,machinedeployment,machineset,machine \
  -l "cluster.x-k8s.io/cluster-name=${CLUSTER_NAME}" -A 2>/dev/null || \
  kubectl get cluster,machinedeployment,machineset,machine -A 2>/dev/null
echo ""

step 6 "Reach workload cluster using restored credentials"
RESTORED_KUBECONFIG="$GENERATED_DIR/${CLUSTER_NAME}-restored.kubeconfig"
mkdir -p "$GENERATED_DIR"
info "Running: kubectl get secret ${CLUSTER_NAME}-kubeconfig -n default -o jsonpath='{.data.value}' | base64 -d"
kubectl get secret "${CLUSTER_NAME}-kubeconfig" -n default \
  -o jsonpath='{.data.value}' | base64 -d > "$RESTORED_KUBECONFIG"
success "Kubeconfig extracted → $RESTORED_KUBECONFIG"

info "Running: kubectl --kubeconfig $RESTORED_KUBECONFIG get nodes"
echo ""
if kubectl --kubeconfig "$RESTORED_KUBECONFIG" get nodes; then
  echo ""
  success "Workload nodes reachable via Velero-restored credentials"
  mark "Workload nodes reachable"
else
  echo ""
  warn "Could not reach workload nodes — controllers may still be reconciling"
  warn "Wait 60s and re-run, or check: clusterctl describe cluster $CLUSTER_NAME"
  exit 1
fi

banner "Recovery verified ✓"
echo ""
echo -e "  ${_BOLD}Key takeaway:${_RESET} The CA secret IS the cluster identity."
echo -e "  Back it up externally. Test the restore."
echo ""
success "Demo complete — run ./07-cleanup.sh to tear down"
