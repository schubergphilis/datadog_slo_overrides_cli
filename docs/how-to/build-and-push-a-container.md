# Build and push a container

The template ships a `Dockerfile.deps` that builds a dependency-cache image — wheels for everything in `uv.lock`, frozen into an OCI layer the test/lint/build jobs can pull from. Two tasks operate on it.

## Build locally

```bash
./workflow.cmd container.build
```

Uses the detected container engine (docker or podman) to build `datadog_slo_overrides_cli-deps:latest` from `Dockerfile.deps`. The build args are read from `[tool.docker-versions]` in `pyproject.toml`.

## Publish in CI

```bash
./workflow.cmd container.publish
```

In CI, this delegates to the host-specific publish in `_CI/tasks/gitlab.py`:

- **GitHub**: logs into `ghcr.io` with `GITHUB_ACTOR` / `GITHUB_TOKEN`, checks for an existing tag, builds and pushes only if missing.
- **GitLab**: writes kaniko credentials from `CI_REGISTRY_*` and runs the kaniko executor (daemonless build + push). Required where privileged docker-in-docker isn't allowed.

The image tag is the SHA-256 of `uv.lock` (first 16 hex chars), so re-builds on the same dependency set are no-ops.

The full image reference is written to `.deps-image` for downstream CI steps to pick up.

## Publish locally (uncommon)

`container.publish` outside CI falls back to a local build + tag, no push. If you need to push from a developer machine, set the same env vars CI sets and run the task — the host code path checks for those, not for a "is this CI" flag.

## Customizing the build

The Dockerfile is small and host-agnostic. Common edits:

- **Base image**: edit `[tool.docker-versions]` in `pyproject.toml`. The `base-image` key feeds into the Dockerfile build arg.
- **Extra system packages**: edit `Dockerfile.deps` directly. Reflect any user-visible behaviour change in `_CI/README.md`.

## When you outgrow the deps cache

The deps image is a dev-cycle optimization, not a production artifact. Production container images are out of scope for the template — write them in your own `Dockerfile` next to the deps file.

## See also

- [Reference: configuration files](../reference/configuration-files.md) — where `[tool.docker-versions]` lives.
- [The _CI tasks architecture](../explanation/the-ci-tasks-architecture.md) — why container logic is split across `container.py` and the host submodule.
