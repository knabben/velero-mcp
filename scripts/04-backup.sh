#!/usr/bin/env bash
# Pre-conditions:  02-workload-cluster.sh and 03-velero-install.sh exited 0;
#                  all five CA secrets exist.
# Post-conditions: Velero backup Completed in MinIO; cluster unpaused.
# Recovery:        Re-run. If backup stuck, check: velero backup describe $BACKUP_NAME
#                  If cluster stuck paused: kubectl patch cluster <name> -n default
#                  --type=merge -p '{"spec":{"paused":false}}'
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/helpers.sh
source "$SCRIPT_DIR/helpers.sh"

banner "Step 4 — Targeted CAPI Secret Backup"

step 1 "What will be backed up (the failure surface)"
info "These are the exact resources that will be deleted in the failure simulation:"
display_capi_secrets "$CLUSTER_NAME"

info "CAPI objects covered by selector 'cluster.x-k8s.io/cluster-name=${CLUSTER_NAME}':"
for resource in \
  cluster machinedeployment machineset machine \
  kubeadmcontrolplane \
  kubeadmconfig kubeadmconfigtemplate \
  dockercluster dockermachine dockermachinetemplate; do
  count=$(kubectl get "$resource" \
    -l "cluster.x-k8s.io/cluster-name=${CLUSTER_NAME}" \
    -A --no-headers 2>/dev/null | wc -l | tr -d ' ')
  printf "  %-35s %s object(s)\n" "$resource" "$count"
done
echo ""

info "ConfigMaps with cluster label:"
kubectl get configmap \
  -l "cluster.x-k8s.io/cluster-name=${CLUSTER_NAME}" \
  -n default --no-headers 2>/dev/null | \
  awk '{printf "  configmap/%-35s\n", $1}' || true
echo ""

# Warn about cluster-related configmaps that lack the selector label
info "Label-selector coverage check — cluster-related ConfigMaps without cluster label:"
unlabeled=0
while IFS= read -r cm; do
  if ! kubectl get configmap "$cm" -n default \
       -o jsonpath='{.metadata.labels.cluster\.x-k8s\.io/cluster-name}' \
       2>/dev/null | grep -q .; then
    warn "  configmap/$cm — no cluster label; WILL NOT be captured by selector"
    unlabeled=$((unlabeled + 1))
  fi
done < <(kubectl get configmap -n default --no-headers 2>/dev/null | \
           grep -i "${CLUSTER_NAME}" | awk '{print $1}')
if [[ $unlabeled -eq 0 ]]; then
  success "All cluster-related ConfigMaps carry the cluster label — selector is complete"
fi
echo ""

sleep 10 
clear

step 2 "Pause cluster before backup"
info "Taking a consistent backup requires a quiet control plane."

info "$ kubectl patch cluster $CLUSTER_NAME -n default --type=merge -p '{\"spec\":{\"paused\":true}}'"
kubectl patch cluster "$CLUSTER_NAME" -n default \
  --type=merge -p '{"spec":{"paused":true}}'
success "Cluster '$CLUSTER_NAME' paused — CAPI will not mutate objects during backup"

step 3 "Create targeted Velero backup"
if velero backup get "$BACKUP_NAME" -n "$VELERO_NAMESPACE" &>/dev/null; then
  info "Backup '$BACKUP_NAME' already exists — skipping"
else
  info "Running velero backup create..."
  info "Selector: cluster.x-k8s.io/cluster-name=${CLUSTER_NAME}"
  echo 
  info "$ velero backup create $BACKUP_NAME --include-namespaces default --include-resources secrets,configmaps,... --selector cluster.x-k8s.io/cluster-name=${CLUSTER_NAME} --wait --namespace $VELERO_NAMESPACE"
  echo
  velero backup create "$BACKUP_NAME" \
    --include-namespaces default \
    --include-resources \
      "secrets,configmaps,\
clusters.cluster.x-k8s.io,\
machines.cluster.x-k8s.io,\
machinedeployments.cluster.x-k8s.io,\
machinesets.cluster.x-k8s.io,\
kubeadmcontrolplanes.controlplane.cluster.x-k8s.io,\
kubeadmconfigs.bootstrap.cluster.x-k8s.io,\
kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io,\
dockerclusters.infrastructure.cluster.x-k8s.io,\
dockermachines.infrastructure.cluster.x-k8s.io,\
dockermachinetemplates.infrastructure.cluster.x-k8s.io" \
    --selector "cluster.x-k8s.io/cluster-name=${CLUSTER_NAME}" \
    --wait \
    --namespace "$VELERO_NAMESPACE"
  success "Backup '$BACKUP_NAME' completed"
fi

sleep 10
clear

step 4 "Unpause cluster after backup"
echo
info "$ kubectl patch cluster $CLUSTER_NAME -n default --type=merge -p '{\"spec\":{\"paused\":false}}'"
echo
kubectl patch cluster "$CLUSTER_NAME" -n default \
  --type=merge -p '{"spec":{"paused":false}}'
success "Cluster '$CLUSTER_NAME' unpaused — CAPI reconciliation resumed"

banner "Backup complete ✓"
info "Backup name:   $BACKUP_NAME"

echo
echo

