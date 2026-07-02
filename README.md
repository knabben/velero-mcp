# Velero MCP Server

[When Clusters Break](https://www.youtube.com/watch?v=Cw_gez2_YO4)

An MCP (Model Context Protocol) server, built with FastMCP and scaffolded via
[kmcp](https://kagent.dev/docs/kmcp), that turns RTO/RPO from a static
promise into a measured, continuously-verified fact - full autonomy on the
safe side (triggering backups, scoring them against policy), advisory-only
on the destructive side (restoring).

[docs/companion-guide.md](docs/companion-guide.md) for the write-up behind
the talk.

## Tools

- **`trigger_backup`**: creates a `velero.io/v1` Backup object (the same
  object `velero backup create` itself builds and submits), optionally waits
  for it to reach a terminal phase, then writes structured completion
  metadata - duration, resource counts, warning/error counts, the GitOps
  revision (Flux/Argo CD, best-effort), and the container images running in
  the backed-up namespaces - back onto the Backup object as an annotation.
- **`list_backups`**: lists Backup objects, scores each against its
  workload tier's RPO target (see [Policies](#policies)), and returns
  `recommended_backup` - the newest Completed backup that meets RPO and
  still matches what's actually running now (images + GitOps revision), not
  just the newest by clock time.
- **`restore_backup`**: **advisory by default.** Without `confirm=True` it
  only returns a plan (the backup's phase/namespaces) and makes no cluster
  changes. Only creates the `velero.io/v1` Restore object once a human (or
  an agent explicitly instructed to) passes `confirm=True`. This is where
  the "advisory-only on the destructive side" policy is enforced in the
  tool itself, not just in documentation.

## Project Structure

```
src/
├── tools/
│   ├── trigger_backup.py   # Trigger a Velero backup + record metadata
│   ├── list_backups.py     # List backups scored against RPO policy
│   ├── restore_backup.py   # Restore from a picked backup (advisory unless confirmed)
│   └── __init__.py         # Tool registry
├── core/
│   ├── server.py           # Dynamic MCP server / tool discovery
│   ├── k8s.py              # Kubernetes client setup (in-cluster or kubeconfig)
│   ├── velero.py           # Velero Backup/Restore CRD helpers
│   ├── policy.py           # RPO/RTO tier policy resolution
│   └── utils.py            # Shared config/env utilities
└── main.py                 # Entry point
kmcp.yaml                   # Configuration file
tests/                      # Unit tests (mocked Kubernetes client, no cluster required)
AGENT.md                    # kagent Agent system-prompt text (nothing else - see agent.yaml)
agent.yaml                  # Deployable kagent Agent manifest (tools wiring, requireApproval)
rbac.yaml                   # ClusterRole for the ServiceAccount kmcp creates for this MCPServer
```

## Configuration

`kmcp.yaml`'s `tools:` section is validated by the `kmcp` CLI against a fixed
schema - each entry needs `name`/`enabled`, and per-tool settings must live
under a nested `config:` key (flat keys are silently dropped by the CLI and
will fail `kmcp` commands with `tool name is required` if `name` is missing):

```yaml
kubernetes:
  # Kubeconfig context to use when running out-of-cluster. Empty = current-context.
  context: ""
tools:
  trigger_backup:
    name: trigger_backup
    enabled: true
    config:
      velero_namespace: velero      # namespace Velero (and its Backup CRs) run in
      poll_interval_seconds: 5      # how often to poll status while waiting
  list_backups:
    name: list_backups
    enabled: true
    config:
      velero_namespace: velero
  restore_backup:
    name: restore_backup
    enabled: true
    config:
      velero_namespace: velero
      poll_interval_seconds: 5
policies:
  default_tier: standard
  tiers:
    critical:
      rpo_minutes: 60
      rto_minutes: 15
    standard:
      rpo_minutes: 1440
      rto_minutes: 240
  # Map namespace -> tier name above. Namespaces not listed use default_tier.
  namespace_tiers:
    payments: critical
```

### Policies

`policies` is a plain, deterministic lookup table - no LLM reasoning needed
for the RPO check itself. `list_backups` resolves a backup's tier as the
*strictest* (lowest RPO) tier among its included namespaces, then compares
the backup's age against that tier's `rpo_minutes`. `rto_minutes` is
currently informational (surfaced per backup); it becomes a measured fact
once a restore-drill tool feeds real restore durations back into this same
metadata store - not built yet.

## Quick Start

### Option 1: Local Development (with Python/uv)

1. **Install Dependencies**:
   ```bash
   uv sync
   ```

2. **Run the Server**:
   ```bash
   # Stdio mode (default MCP transport)
   uv run python src/main.py

   # HTTP mode
   uv run python src/main.py --transport http --host 0.0.0.0 --port 8080
   ```

3. **Add New Tools**:
   ```bash
   kmcp add-tool mytool
   # Edit src/tools/mytool.py
   ```

If `uv` isn't available, a plain venv works too: `python -m venv .venv`,
activate it, then `pip install -e .` plus the dev tools listed under
`[tool.uv] dev-dependencies` in `pyproject.toml`.

### Option 2: Docker-Only Development (no local Python/uv required)

```bash
kmcp build --verbose
docker run -i velero:latest
kmcp deploy mcp --apply
```

## Testing and Validation

### Unit tests

Tests mock the Kubernetes client, so no cluster is required:

```bash
uv run pytest tests/ -v
```

### Lint and type-check

CI runs both of these (see [CI](#ci)); they're clean on `main`:

```bash
uv run ruff check .
uv run mypy .
```

`uv run black .` is available for formatting but isn't CI-gated.

### Dev-mode smoke test (MCP Inspector)

```bash
kmcp run --project-dir .
```

This builds the Docker image and opens the MCP Inspector. Skip the image
build with `--no-inspector` and instead point the Inspector directly at the
local process:

- **Transport Type**: STDIO
- **Command**: `uv`
- **Arguments**: `run python src/main.py`

From the Inspector you can list tools and invoke them directly. With no
cluster reachable, a tool call fails cleanly with a Kubernetes config error
(`isError=True`) rather than crashing the server - that's the expected
result when validating tool wiring without a live cluster.

### End-to-end validation against a real cluster

You need a cluster with Velero installed and a kubeconfig (or in-cluster
ServiceAccount) that can reach it:

1. Point `kmcp.yaml`'s `kubernetes.context` at the target cluster, or rely on
   `KUBECONFIG` / the current context.
2. Apply `rbac.yaml`: `kubectl apply -f rbac.yaml`. **This is required even
   after `kmcp deploy` / the MCPServer reconciles** - kmcp's controller
   creates the ServiceAccount (`<namespace>/<mcpserver-name>`) and a
   ClusterRoleBinding pointing at a ClusterRole named after the MCPServer
   (e.g. `velero`), but it doesn't know what permissions a custom tool
   needs, so that ClusterRole doesn't exist until you define it. Without
   this step every tool call fails with `403 Forbidden: ... clusterrole
   "velero" not found`. `rbac.yaml` grants: create/get/patch on
   `backups.velero.io`, create/get on `restores.velero.io`, cluster-wide
   read on Deployments/StatefulSets/DaemonSets (namespaces aren't known in
   advance), and read on the GitOps CRDs used for revision detection.
   Verify with `kubectl auth can-i list backups.velero.io --as=system:serviceaccount:<ns>:<mcpserver-name>`.
3. Call `trigger_backup` with `wait=true`, then `list_backups` to see it
   scored against RPO, then `restore_backup` (first without `confirm`, to
   see the plan, then with `confirm=true`) to actually restore it.

## CI

`.github/workflows/tests.yml` runs `ruff check`, `mypy`, and `pytest` via
`uv` on every push and pull request.

## Deployment

### Docker

```bash
kmcp build
docker run -i velero:latest
```

### Kubernetes

```bash
kmcp deploy mcp --apply
kubectl get mcpserver velero
kubectl apply -f rbac.yaml    # required - see End-to-end validation above
```

### kagent Agent

`agent.yaml` deploys a kagent Agent wired to this MCPServer's three tools,
with `requireApproval: [restore_backup]` as a second, independent
enforcement of the advisory-only restore policy on top of the tool's own
`confirm` gate. `AGENT.md` is the same system-prompt text embedded in
`agent.yaml` - kept as a separate file for readability, not meant to be
pasted into `systemMessage` wholesale (it has no wiring/YAML in it, unlike
earlier drafts of this doc that mixed the two and caused the agent to
deploy with zero tools registered).

```bash
kubectl apply -f agent.yaml -n kagent
kubectl get agent velero-agent -n kagent
```
