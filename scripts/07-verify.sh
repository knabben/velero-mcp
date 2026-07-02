#!/usr/bin/env bash
# Pre-conditions:  06-recover.sh exited 0.
# Post-conditions: Workload cluster nodes reachable via restored-secret credentials.
# Recovery:        Check kubectl get cluster,machines -A; re-run 06-recover.sh if secrets missing.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/helpers.sh
source "$SCRIPT_DIR/helpers.sh"

banner "Step 7 — Recovery Verification"

step 1 "Verify all five CA secrets are restored"
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
  error "One or more secrets missing — re-run 06-recover.sh"
fi
success "All five CA secrets present"

step 2 "Verify CAPI objects are restored"
echo ""
kubectl get cluster,machinedeployment,machineset,machine \
  -l "cluster.x-k8s.io/cluster-name=${CLUSTER_NAME}" -A 2>/dev/null || \
  kubectl get cluster,machinedeployment,machineset,machine -A 2>/dev/null
echo ""

step 3 "Extract kubeconfig from restored secret"
RESTORED_KUBECONFIG="$GENERATED_DIR/${CLUSTER_NAME}-restored.kubeconfig"
mkdir -p "$GENERATED_DIR"
kubectl get secret "${CLUSTER_NAME}-kubeconfig" -n default \
  -o jsonpath='{.data.value}' | base64 -d > "$RESTORED_KUBECONFIG"
success "Kubeconfig extracted from restored secret → $RESTORED_KUBECONFIG"

step 4 "Reach workload cluster nodes using restored credentials"
info "Running: kubectl --kubeconfig $RESTORED_KUBECONFIG get nodes"
echo ""
if kubectl --kubeconfig "$RESTORED_KUBECONFIG" get nodes; then
  echo ""
  success "Workload cluster nodes reachable via credentials from Velero-restored secrets"
else
  echo ""
  warn "Could not reach workload nodes — controllers may still be reconciling"
  warn "Wait 60s and re-run ./07-verify.sh, or check: clusterctl describe cluster $CLUSTER_NAME"
  exit 1
fi

banner "Recovery verified ✓"
echo ""
echo -e "  ${_BOLD}The four-step recovery model:${_RESET}"
echo ""
echo -e "  ${_GREEN}1. Detect${_RESET}   — CA secret gone; lifecycle management broken"
echo -e "  ${_GREEN}2. Bootstrap${_RESET} — Fresh management cluster + CAPI + Velero"
echo -e "  ${_GREEN}3. Restore${_RESET}  — Velero restores CAPI objects + all five CA secrets"
echo -e "  ${_GREEN}4. Adopt${_RESET}    — Controllers find existing workload cluster infrastructure"
echo -e "              and resume lifecycle management"
echo ""
echo -e "  ${_BOLD}Key takeaway:${_RESET} The certificates and secrets ARE the cluster identity."
echo -e "  Back them up. Back them up externally. Test the restore."
echo ""
success "Demo complete — run ./08-cleanup.sh to tear down"
