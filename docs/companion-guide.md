# When Clusters Break: A Companion Guide

## Why Distributed Systems Fail

Kubernetes is a distributed system, and Cluster API adds another layer of distributed coordination on top of it for cluster lifecycle. Before diving into CAPI-specific failures, it's worth grounding ourselves in what makes distributed systems fundamentally hard.

Every interaction between components in a Kubernetes cluster — between the API server and etcd, between a controller and the infrastructure provider, between the management cluster and a workload cluster — is a network call that can fail in any of the ways distributed systems fail. 
 * Packets get lost or delayed. 
 * Replies vanish. Nodes fall silent. 
 * Clocks drift. 
 * A process pauses for garbage collection and other nodes declare it dead, only for it to resume without knowing anything happened. 
 
These aren't edge cases; they're the normal operating conditions of any distributed system, on Day 2 operations.

The defining characteristic is *partial failure*: some components work while others don't, and you can't always or easily tell which is which. A timeout doesn't tell you whether the remote node crashed, the network dropped your packet, or the response is just slow. 

Kubernetes handles many of these problems through its reconciliation model and maturity — controllers continuously compare desired state against actual state and attempt to converge. But the controllers themselves run on infrastructure that is subject to the same failure modes.

This is the core tension in Cluster API: the system that manages your clusters' lifecycles is itself hosted on a cluster of machines, subject to all the same distributed systems problems it's designed to manage in others.

## Cluster API Architecture

### The Two-Cluster Model

Cluster API introduces a management cluster that declaratively manages one or more workload clusters. The management cluster runs the CAPI controllers, infrastructure providers, bootstrap providers, and control plane providers. It stores all the desired state — Cluster objects, MachineDeployments, MachineSets, Machines — as custom resources in its own etcd.

Workload clusters are independent Kubernetes clusters. They have their own API server, their own etcd, their own control plane. Once provisioned, they run independently of the management cluster. 

If the **management cluster** goes down, workload clusters continue serving traffic. What they lose is lifecycle management: nobody can scale them, upgrade them, replace failed nodes, or rotate certificates.

This separation is both the strength and the vulnerability of the architecture. Independence means resilience — a management cluster failure doesn't cascade to workloads. But it also means the management cluster is a single point of failure for all lifecycle operations across potentially dozens of workload clusters, and needs to have a strategy in place for recovery from possible failures.

### The Object Model

Every lifecycle operation in Cluster API is expressed as a change to Kubernetes objects in the management cluster.

A **Cluster** object is the top-level resource. It defines the target cluster's network configuration and references the infrastructure-specific resources (like a DockerCluster or VSphereCluster) and the control plane provider.

A **MachineDeployment** manages a set of Machines the same way a Kubernetes Deployment manages Pods. When you want to scale workers or roll out a new Kubernetes version, you modify the MachineDeployment spec and let the controller handle the rollout.

A **MachineSet** ensures a specified number of Machines exist. It creates or deletes Machine objects to match the desired replica count, similar to how a ReplicaSet manages Pods.

A **Machine** represents a single node. It links to an infrastructure-specific resource (which represents the actual VM or container) and a bootstrap resource (which provides the cloud-init or ignition config that turns a bare machine into a Kubernetes node).

The reconciliation flow is straightforward: you change desired state → controllers detect the delta → controllers drive infrastructure to match. Create a Cluster → controllers provision infrastructure. Scale a MachineDeployment → MachineSet creates new Machine objects → infrastructure provider creates new VMs → bootstrap provider configures them as Kubernetes nodes. Delete a Machine → infrastructure provider destroys the VM.

### Setting Up Locally with Docker Provider

The Docker infrastructure provider (CAPD) lets you run the full CAPI lifecycle on a laptop using Docker containers as "machines." It's the fastest way to experience CAPI without cloud credentials.

The setup requires kind (to create the management cluster), clusterctl (to initialize CAPI), and Docker. The management cluster runs inside a kind cluster, and workload cluster nodes are Docker containers managed by CAPD. The demo repository automates this entire setup — `01-bootstrap.sh` creates the kind cluster and runs `clusterctl init --infrastructure docker`, which installs the CAPI core controllers, the kubeadm bootstrap provider, the kubeadm control plane provider, and CAPD.

After initialization, you can verify the controllers are running by checking deployments across the CAPI namespaces: `capi-system`, `capd-system`, `capi-kubeadm-bootstrap-system`, and `capi-kubeadm-control-plane-system`. Each namespace contains a controller-manager deployment that runs the reconciliation loops for its domain.


## Day-2 Operations and Debugging

### The Hidden Critical State: Certificates and Secrets

When CAPI creates a workload cluster, it generates a set of certificates and secrets that are stored in the management cluster. These are not incidental metadata — they are the cryptographic foundation of the workload cluster's identity and security.

**Cluster CA certificate and key** (`<cluster>-ca`): This is the root certificate authority for the entire workload cluster. Every component certificate — kubelet, API server, controller-manager — is signed by this CA. If you lose the CA key, you cannot issue new certificates. You cannot add nodes. You cannot rotate existing certificates when they expire. The cluster is frozen.

**etcd CA certificate and key** (`<cluster>-etcd`): Separate from the cluster CA, this signs all etcd peer and client certificates. etcd members authenticate each other using certificates signed by this CA. Losing it means you cannot add new etcd members or recover from etcd member failures.

**Front proxy CA** (`<cluster>-proxy`): Used by the API server's aggregation layer. Less commonly discussed but still critical for the functioning of API aggregation (metrics-server, custom API servers).

**Service Account signing key** (`<cluster>-sa`): Every ServiceAccount token in the workload cluster is signed by this key. If it's lost, no new tokens can be issued, and in-cluster authentication breaks — pods can't talk to the API server using their service account credentials.

**Admin kubeconfig** (`<cluster>-kubeconfig`): Your access credential to the workload cluster. Contains the cluster CA certificate and an admin client certificate. Without it, you need to manually reconstruct access using the CA certificate (if you still have it).

All of these are stored as Kubernetes Secrets in the management cluster, labeled with `cluster.x-k8s.io/cluster-name`. This is why backing up the management cluster isn't just about backing up CAPI objects — the secrets are equally critical.

### Diagnostics and Monitoring Endpoints

CAPI controllers expose diagnostics endpoints that are essential for day-2 operations.

**Metrics endpoint**: All CAPI controllers serve Prometheus metrics at `/metrics` over HTTPS with authentication. You can scrape these for reconciliation latency, queue depth, error counts, and controller-specific metrics. To access them, create a ClusterRole granting GET access to the `/metrics` non-resource URL, bind it to a ServiceAccount, then port-forward the controller and scrape with a bearer token. The CAPI documentation on diagnostics covers this setup in detail.

**Log level endpoint**: Controllers support dynamic log level adjustment at `/debug/flags/v` via PUT requests. This is invaluable during debugging — you can increase verbosity to level 6 or higher without restarting the controller, observe the detailed reconciliation logs, then reduce it back to normal. This requires a similar RBAC setup with PUT access to the `/debug/flags/v` non-resource URL.

**Profiling endpoint**: When enabled, pprof endpoints are available for CPU and memory profiling. Useful for diagnosing controller performance issues or memory leaks.

For monitoring at scale, the recommendation is to deploy a Prometheus instance on the management cluster that scrapes all CAPI controller metrics, configure alerts on reconciliation failures and queue saturation, and feed the data into Grafana dashboards. The key metrics to watch are controller reconciliation duration, work queue depth and retry counts, and error rates by controller.

### Debugging Cluster API Systematically

When something goes wrong in CAPI — a Machine is stuck provisioning, a cluster won't scale, an upgrade stalls — the debugging approach should be layered, moving from high-level object state down through provider-specific resources to controller logs.

**Layer 1: clusterctl describe**. Start here. `clusterctl describe cluster <name> --show-conditions all` gives you the full object tree with conditions. It surfaces problems by design — if a Machine is stuck, the condition will tell you why (e.g., `WaitingForBootstrapData`, `WaitingForInfrastructure`, `WaitingForNodeRef`). The tool intentionally hides detail when things are healthy and surfaces it when they're not.

**Layer 2: Machine phases**. `kubectl get machines -A -o wide` shows the phase of each Machine: Pending, Provisioning, Provisioned, Running, Deleting, Failed. A Machine stuck in Provisioning usually means the infrastructure provider can't create the VM. A Machine stuck in Provisioned but not Running usually means the bootstrap process failed — the VM exists but kubeadm didn't complete.

**Layer 3: Provider resources**. Every Machine has a corresponding infrastructure object (e.g., DockerMachine, VSphereMachine, AWSMachine). `kubectl describe <provider-machine> -n <namespace>` shows provider-specific conditions and events — VM creation errors, network issues, storage problems. This is where platform-specific failures surface.

**Layer 4: Controller logs**. `kubectl logs -n capi-system deploy/capi-controller-manager` shows the reconciliation loops in action. Look for error-level messages, repeated reconciliation failures, and stuck objects. Increase log verbosity via the debug endpoint if needed. Don't forget to check provider-specific controller logs (e.g., `capd-controller-manager` in `capd-system`).

**Layer 5: Node-level debugging**. For bootstrap failures, you need to get onto the node itself. With CAPD, that means `docker logs <machine-name>`. With VM-based providers, it means SSH and checking cloud-init logs (`/var/log/cloud-init-output.log`), kubelet logs (`journalctl -u kubelet`), and containerd logs.

### Common Day-2 Problems

**Certificate expiry**: CAPI-generated certificates default to one year. If you miss rotation, kubelet on workload cluster nodes can no longer authenticate to the API server, and nodes drop to NotReady. Monitor certificate expiry proactively. The management cluster stores the CA keys needed for rotation — another reason its backup is critical.

**Stuck rolling updates**: A MachineDeployment upgrade creates a new MachineSet with the updated spec and scales down the old one. If the new Machine provisions successfully but the old one won't drain (because a PodDisruptionBudget blocks eviction, or a finalizer is stuck), the rollout stalls. Check MachineDeployment conditions, node cordoning status, and PDB configurations.

**Provider version skew**: Upgrading CRDs without restarting the controller (or vice versa) leaves objects in an inconsistent state. The controller may not recognize new fields, or the API server may reject updates because the stored version doesn't match what the controller expects. Always upgrade CRDs and controllers together, and verify with `clusterctl upgrade plan` before proceeding.

**Infrastructure drift**: Someone modifies a VM, network, or storage configuration outside of CAPI — through the cloud console, vCenter, or direct API calls. The CAPI controller sees a mismatch between the Machine spec and the actual infrastructure but may not be able to reconcile it. The solution is organizational: CAPI-managed infrastructure should not be modified out-of-band.


## Failure Modes

### Understanding Partial Failure in CAPI

The failure modes in Cluster API map directly to the distributed systems challenges described earlier. The key insight is that failures are *graduated* — they escalate through severity levels, and each level demands a different response.

### Level 1: Workload Node Failure

A worker node in a workload cluster fails — the VM crashes, the kubelet stops responding, or the container runtime hangs. This is the most common failure and the one CAPI handles most gracefully.

If MachineHealthCheck is configured (and it should be), it monitors the node's condition via the workload cluster's API server. When a node reports NotReady for longer than the configured timeout, MachineHealthCheck marks the corresponding Machine as unhealthy. The Machine controller then deletes the Machine object, which triggers the infrastructure provider to destroy the VM. The MachineSet controller detects that the replica count is below desired and creates a new Machine, which goes through the full provisioning cycle.

This is self-healing by design. No human intervention required. The workload cluster temporarily has reduced capacity, but Kubernetes' scheduler handles rescheduling pods to healthy nodes.

**What can go wrong**: MachineHealthCheck has a `maxUnhealthy` threshold to prevent cascading deletions — if too many nodes are unhealthy simultaneously (which could indicate a network partition rather than node failure), it won't delete any of them. This is a safety mechanism, but it means you need to investigate manually when the threshold is breached.

### Level 2: Provider Controller Crash

An infrastructure provider controller (e.g., CAPD, CAPV, CAPA) crashes or restarts. The controller pod is typically managed by a Kubernetes Deployment, so it will be restarted automatically. During the restart window, reconciliation for that provider pauses — any pending Machine creations, deletions, or updates stall.

This is usually transient. Once the controller recovers, it resumes processing its work queue. Machines that were mid-provisioning may need to start over depending on how far the provider got before crashing.

**What can go wrong**: If the crash is caused by a bug triggered by specific object state (e.g., a nil pointer on a particular Machine spec), the controller will crash-loop. The fix is to either patch the object that's causing the crash or upgrade the provider. This is a case where controller logs are essential.

### Level 3: Management Cluster Degraded

The management cluster itself becomes unhealthy — etcd loses quorum (in a multi-node management cluster), a CRD version skew causes API server errors, or a partial upgrade leaves controllers in an inconsistent state.

This is more dangerous than a provider crash because it affects *all* CAPI operations, not just one provider. Workload clusters continue running (they're independent), but all lifecycle operations stop: no scaling, no upgrades, no node replacement, no certificate rotation.

**What can go wrong**: etcd quorum loss in the management cluster is the most critical scenario. With a 3-member etcd cluster, losing 2 members means no writes — the API server becomes read-only. CAPI objects exist but can't be modified. If you have a single-node management cluster (common in development and even some production setups), any control plane failure means total loss of CAPI operations.

### Level 4: Management Cluster Gone — or Just Its Secrets

The most severe failure covers a spectrum: at one end, total loss of the management cluster (etcd destroyed, nodes wiped); at the other, loss of the critical secrets while the cluster nodes are still running. The outcome for lifecycle management is the same in both cases.

Consider losing just the cluster CA secret (`<cluster-name>-ca`). The management cluster is still up. `kubectl get clusters` still works. But the CAPI controller can no longer issue certificates for the workload cluster. The next node provisioning attempt, the next certificate rotation, the next Machine reconciliation that requires a new TLS credential — all fail. The CA key is the root of every certificate in the workload cluster. Without it, you cannot add nodes, rotate expiring certs, or replace failed machines. The cluster is frozen in place.

This is actually a *worse* position than it appears, because it can go undetected. The workload cluster continues serving traffic today. The failure only becomes apparent when the next lifecycle operation is attempted — or when certificates start expiring.

In the total-loss scenario, workload clusters continue running but `kubectl` against the management cluster returns "connection refused." Either way — secret loss or full cluster loss — there is no path forward without a backup of those secrets.

This is why Velero exists in this architecture, and why the backup scope matters: backing up the CAPI objects alone is not enough. The certificate secrets must be included.

**Why you cannot substitute a new CA**: It is tempting to think that any valid CA certificate would work — just generate a new self-signed CA and move on. This is incorrect. The existing workload cluster nodes were bootstrapped using the *original* CA. Every kubelet certificate, every API server serving certificate, every client certificate on those nodes was signed by that specific CA private key. The nodes' trust anchors are hard-coded at bootstrap time into kubeadm's `cluster-info` ConfigMap. A new CA — even a structurally valid one — will not be trusted by those nodes, and certificates it signs will be rejected. The original CA is not just *a* CA; it is *the* CA those nodes were built with. Restoration must recover the exact original bytes.


## Recovery Strategy with Velero

### What Velero Does

Velero is a CNCF project that backs up and restores Kubernetes resources and persistent volume data. In the context of CAPI, Velero backs up the management cluster's desired state — the objects in etcd that describe your workload clusters, plus the secrets and certificates that secure them.

It stores backups in S3-compatible object storage (AWS S3, GCS, MinIO, or any S3-compatible endpoint). Critically, this storage must be *external* to the management cluster infrastructure. If your backup storage is on the same infrastructure that fails, your backup fails with it.

### What to Back Up

A Velero backup of a CAPI management cluster needs to capture three categories of resources.

**CAPI core objects**: Cluster, MachineDeployment, MachineSet, Machine, and their associated status and conditions. These define the desired state of your workload clusters.

**Provider-specific resources**: DockerCluster, DockerMachine (or VSphereCluster, VSphereMachine, AWSCluster, AWSMachine, etc.). These contain the infrastructure-specific configuration that ties CAPI objects to actual VMs.

**Secrets and certificates**: All the secrets labeled with `cluster.x-k8s.io/cluster-name`, including CA keys, etcd CA keys, front proxy CA keys, service account signing keys, admin kubeconfigs, and bootstrap tokens. These are the *most critical* resources to back up — without them, even a perfect restore of CAPI objects won't give you access to your workload clusters. The five certificate secrets alone are sufficient to re-establish full lifecycle management; no other data in the management cluster matters more.

**CRDs**: The Custom Resource Definitions for all CAPI and provider types. CRDs must restore before the custom resources that depend on them. Velero handles this ordering automatically.

In practice, you can scope your Velero backup narrowly using `--include-namespaces` and `--include-resources` to target exactly the CAPI namespaces, the five secret types, and the CAPI object types — rather than taking a full-cluster backup. A narrow backup is faster, easier to audit, and makes the relationship between backup scope and failure surface explicit: what you back up is exactly what you'd lose.

### What Not to Confuse

Velero backs up the *management cluster's* view of the workload clusters — the CAPI objects and secrets. It does not back up the workload clusters' own etcd data, running pod state, or application data. Those need their own backup strategy (potentially also using Velero, but running on the workload clusters themselves).

VM-level snapshots (vSphere snapshots, EBS snapshots) are complementary but not a substitute. They capture the infrastructure state but not the CAPI object model. You need both for complete disaster recovery.

### Backup Frequency

How often to back up depends on your tolerance for data loss (RPO — Recovery Point Objective). Consider what changes between backups: new clusters created, machines scaled, upgrades applied, certificates rotated. For most environments, daily backups with a 7-day retention provide a reasonable baseline. Environments with frequent cluster lifecycle changes may want more frequent backups.

Velero supports cron-based schedules: `velero schedule create daily-mgmt --schedule="0 2 * * *"` creates a backup at 2 AM daily.

### The Recovery Workflow

Recovery from a total management cluster loss follows four steps, matching the presentation's slide 15.

**Step 1 — Detect**: The management cluster is unreachable. `kubectl` commands against it return connection refused or timeout. Workload clusters are still serving traffic — verify this independently using saved kubeconfigs or direct node access.

**Step 2 — Bootstrap**: Provision a fresh management cluster. This doesn't need to be identical infrastructure — it needs to run the same CAPI version and providers. Install CAPI and providers using `clusterctl init` with the same provider versions as the original. Install Velero and point it at your backup storage location.

**Step 2.5 — Pause before restore (critical)**: Before running the restore, pause CAPI reconciliation on the affected cluster:

```bash
kubectl patch cluster <cluster-name> -n default \
  --type=merge -p '{"spec":{"paused":true}}'
```

This step is non-obvious but essential. CAPI controllers are designed to self-heal: when a CA secret goes missing, the controller detects its absence and regenerates it within seconds — creating a fresh, structurally valid CA. While this sounds helpful, it creates a trap during restore. By the time you run `velero restore`, a CAPI-generated CA is already in place. Velero's default behavior is to skip resources that already exist, so it skips the original CA entirely. The management cluster now holds a *new* CA, but the existing workload cluster nodes carry certificates signed by the *original* CA. New node bootstraps fail. Certificate operations fail silently. The mismatch goes unnoticed until something breaks.

Pausing the cluster suspends all CAPI reconciliation, preventing auto-regeneration during the restore window.

**Step 3 — Restore**: Run `velero restore create --from-backup <backup-name> --existing-resource-policy=update`. The `--existing-resource-policy=update` flag is a belt-and-suspenders measure: even if a resource already exists (for example, if the cluster was not paused in time, or if a partial restore left stale state), Velero will overwrite it with the backed-up version rather than skipping it. Velero restores CRDs first, then custom resources, then secrets. The CAPI objects and original certificates are recreated in the management cluster's etcd.

**Step 4 — Unpause**: After the restore completes, unpause the cluster:

```bash
kubectl patch cluster <cluster-name> -n default \
  --type=merge -p '{"spec":{"paused":false}}'
```

Controllers now resume reconciliation with the correct certificate chain in place.

**Step 5 — Adopt**: The CAPI controllers start reconciling, discover that the infrastructure described by the Machine objects *already exists* — the VMs are running, the workload clusters are healthy — and adopt the existing resources rather than creating new ones. The restored certificates allow the management cluster to authenticate to the workload clusters and resume lifecycle management.

The adoption process may take a few minutes as controllers work through their queues. Any Machine that was mid-provisioning when the CA was lost (its bootstrap failed because no CA was available) will be stuck in `BootstrapFailed` state and must be deleted manually — the MachineDeployment will create a replacement that bootstraps successfully with the restored CA. Monitor with `clusterctl describe cluster <name>` and watch for conditions to converge.

### Testing Your Recovery

An untested backup is not a backup — it's a hope. The recommendation is to test the full recovery flow quarterly: spin up a fresh management cluster in a test environment, restore from a production backup, and verify that controllers successfully adopt the existing workload clusters without disruption.

The `kcd-lima-demo` repository automates this entire flow using the Docker provider, providing a safe environment to practice and validate the recovery procedure.


## Running the Demo

The repository provides nine numbered scripts that walk through the complete lifecycle:

| Script | What It Does | Talk Section |
|--------|-------------|--------------|
| `00-prereqs.sh` | Validates all five tools are installed and at minimum versions | Pre-show |
| `01-bootstrap.sh` | Creates kind management cluster + initializes CAPI with Docker provider | Cluster API Lifecycle |
| `02-workload-cluster.sh` | Provisions CAPD workload cluster, displays object tree and all five CA secrets | Cluster API Lifecycle |
| `03-velero-install.sh` | Deploys MinIO (local S3) + Velero; no cloud credentials | Recovery with Velero |
| `04-backup.sh` | Takes targeted Velero backup scoped to CAPI certificate secrets and objects | Recovery with Velero |
| `05-simulate-failure.sh` | Pauses CAPI reconciliation, deletes the workload cluster CA secret (with confirmation), shows controller error | Where Things Break |
| `06-recover.sh` | Runs Velero restore with `--existing-resource-policy=update`, then unpauses CAPI so controllers reconcile against the restored original CA | Recovery with Velero |
| `07-verify.sh` | Extracts kubeconfig from restored secrets, verifies workload cluster reachability | Recovery with Velero |
| `08-cleanup.sh` | Tears down all kind clusters, containers, and generated files | Post-demo |

Prerequisites: Docker, kind, kubectl, clusterctl, velero CLI. No cloud accounts required. Full demo runs in approximately 15 minutes.

The failure simulation (`05-simulate-failure.sh`) targets the cluster CA secret rather than destroying the entire management cluster. This makes the same point more precisely: it is the *cryptographic state* — not the cluster nodes — that constitutes the irreplaceable part of lifecycle management.

The pause/unpause wrapping the delete and restore is not theatrical — it solves a real operational problem. CAPI's self-healing loop regenerates a missing CA within seconds. Without pausing, the regenerated CA races against Velero, and Velero skips the original. The existing workload nodes then reject the new CA because their own certificates were signed by the original. Pausing eliminates the race; `--existing-resource-policy=update` is a belt-and-suspenders fallback for environments where pausing may not have happened in time.


## References

- Cluster API Book: https://cluster-api.sigs.k8s.io/
- Cluster API Diagnostics: https://cluster-api.sigs.k8s.io/tasks/diagnostics
- Cluster API Troubleshooting: https://cluster-api.sigs.k8s.io/user/troubleshooting
- clusterctl describe: https://cluster-api.sigs.k8s.io/clusterctl/commands/describe-cluster
- Velero Documentation: https://velero.io/docs/
- CNCF Velero Project: https://github.com/vmware-tanzu/velero

---

*Amim Knabben — github.com/knabben — @knabben*
*Kubernetes Contributor since 2020 — Emeritus SIG-Windows Tech Lead — CNCF Ambassador — Golden Kubestronaut*
