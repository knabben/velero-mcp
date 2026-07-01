# velero

An MCP (Model Context Protocol) server, built with FastMCP and scaffolded via
[kmcp](https://kagent.dev/docs/kmcp), that lets an agent trigger and audit
Velero backups from natural-language status queries instead of someone
hand-inspecting Velero CRs.

## Features

- **Dynamic Tool Loading**: Tools are automatically discovered and loaded from `src/tools/`
- **`trigger_backup` tool**: creates a `velero.io/v1` Backup object (the same
  object `velero backup create` itself builds and submits), optionally waits
  for it to reach a terminal phase, then writes structured completion
  metadata - duration, resource counts, warning/error counts, the GitOps
  revision (Flux/Argo CD, best-effort), and the container images running in
  the backed-up namespaces - back onto the Backup object as an annotation.
  This lets a later restore be checked for staleness against what's actually
  running now, not just against clock time.
- **In-cluster or out-of-cluster**: the Kubernetes client prefers in-cluster
  ServiceAccount credentials and falls back to kubeconfig, so the server can
  run inside the cluster it manages or on a dev machine pointed at a remote
  cluster (see [Configuration](#configuration)).
- **Configuration Management**: Tool-specific configuration via `kmcp.yaml`
- **Fail-Fast**: Server won't start if any tool fails to load

## Project Structure

```
src/
├── tools/
│   ├── trigger_backup.py  # Trigger a Velero backup + record metadata
│   └── __init__.py        # Tool registry
├── core/
│   ├── server.py          # Dynamic MCP server / tool discovery
│   ├── k8s.py              # Kubernetes client setup (in-cluster or kubeconfig)
│   ├── velero.py           # Velero Backup CRD helpers
│   └── utils.py             # Shared config/env utilities
└── main.py                 # Entry point
kmcp.yaml                   # Configuration file
tests/                      # Unit tests (mocked Kubernetes client, no cluster required)
```

## Configuration

`kmcp.yaml`:

```yaml
kubernetes:
  # Kubeconfig context to use when running out-of-cluster. Empty = current-context.
  context: ""
tools:
  trigger_backup:
    velero_namespace: velero      # namespace Velero (and its Backup CRs) run in
    poll_interval_seconds: 5      # how often to poll Backup status while waiting
```

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

From the Inspector you can list tools and invoke `trigger_backup` directly.
With no cluster reachable, the tool call fails cleanly with a Kubernetes
config error (`isError=True`) rather than crashing the server - that's the
expected result when validating the tool wiring without a live cluster.

### End-to-end validation against a real cluster

To actually exercise a backup, you need a cluster with Velero installed and
a kubeconfig (or in-cluster ServiceAccount) that can reach it:

1. Point `kmcp.yaml`'s `kubernetes.context` at the target cluster, or rely on
   `KUBECONFIG` / the current context.
2. Grant the identity running the server RBAC for: create/get/patch on
   `backups.velero.io` in the `velero_namespace`; read on
   Deployments/StatefulSets/DaemonSets in any namespace you'll back up; and
   (optionally) read on `gitrepositories.source.toolkit.fluxcd.io` /
   `applications.argoproj.io` if you want GitOps revision detection.
3. Call `trigger_backup` with `wait=true` and inspect the returned metadata,
   or `kubectl get backup <name> -n velero -o jsonpath='{.metadata.annotations}'`
   to see the annotation written back.

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
```
