# SBOM and security model

The template's security pipeline has three layers: known-vulnerability scanning, SBOM generation, and (optional) SBOM upload to an OWASP Dependency Track server. This page explains what each layer does and what threat model it addresses.

## The threat model

The template assumes:

- You publish a Python package consumed by people you mostly don't know.
- Your dependency graph is large and changes frequently.
- Some of your dependencies will, eventually, ship a vulnerability.
- Your downstream consumers will eventually ask "is your software affected by CVE-X?" — and they'll ask faster than you can audit by hand.

The pipeline answers those questions before the questions are asked.

## Layer 1 — pip-audit

`./workflow.cmd secure.audit` runs [pip-audit](https://github.com/pypa/pip-audit) against `uv.lock`, comparing every pinned package against the [PyPI advisory database](https://github.com/pypa/advisory-database).

What it catches: a vulnerability with a known CVE/PYSEC ID affecting a version in your lockfile.

What it doesn't catch:

- Zero-days (no advisory exists yet).
- Vulnerabilities in your *own* code.
- Misuse of a safe dependency.

Runs on every CI pipeline. See [Triage a security finding](../how-to/triage-a-security-finding.md) for the response workflow.

## Layer 2 — CycloneDX SBOM

`./workflow.cmd secure.sbom-extract --write` generates a [CycloneDX](https://cyclonedx.org/) 1.7 bill of materials and writes it to `src/<project_slug>/sbom.cdx.json`. Because that path is inside the package data tree, `uv build` automatically ships the SBOM **inside the wheel** — a downstream consumer unpacks the wheel and finds `<project_slug>/sbom.cdx.json` alongside the Python modules.

### What's in it

**Metadata header** declares:

- A `lifecycles` entry of `phase: build` — this SBOM was produced during the build, not as a post-shipment inventory.
- A `tools.components` list naming what produced the SBOM (cyclonedx-python-lib, uv, the project's own generator), each with a version pin.
- `supplier` + `authors` derived from `pyproject.toml`'s `[project.authors]`.
- A `properties` entry recording the chosen `git_hosting_service` for downstream tools that want template-aware context.

**Components** are organised by **scope** so a consumer can distinguish what ships from what doesn't:

| Source | CycloneDX `scope` | Source path |
| --- | --- | --- |
| Project itself (root) | `required` | `[project]` block in pyproject.toml; root carries the project's licence and a `vcs` external_reference pointing at the git remote when present |
| Runtime dependencies | `required` | `uv export --no-dev` against `uv.lock` — exactly what ships in the wheel |
| Dev / lint / test / docs / quality / security groups | `optional` | full lockfile via `uv export --all-groups`, minus the runtime set |
| Vendored CI tooling | `excluded` | every package in `_CI/lib/vendor.txt` |
| Pipeline components | `excluded` | GitHub Actions (`uses:`) or GitLab CI images, sourced from `_CI/tasks/<host>.py`'s `iter_pipeline_components()` |

Plus one **synthetic build-environment component** (type `platform`, scope `excluded`) that groups the vendored + pipeline material into a single sub-graph.

**Dependencies** form a two-level graph:

- The project root depends on each runtime + dev component and on the build-environment.
- The build-environment depends on the vendored + pipeline components.

Reading top-down: "the project depends on these runtime + dev components for itself, and on the build-environment to be assembled. The build-environment in turn depends on these vendored + pipeline components."

### Per-component enrichment

Each PyPI component (runtime, dev, vendored) carries:

- **`licenses`** — declared SPDX expression. The lookup walks `PEP 639 License-Expression → legacy License header → LICENSE-File text → Trove classifier mapping`. Vendored entries first try text-detection over the `_CI/lib/vendor/<name>/LICENSE*` files (the vendoring tool drops dist-info but keeps the LICENSE), then defer to the venv-installed copy when the same package is also a transitive dev dep. A handful of packages with no licence-bearing file locally end up with `licenses: []` — graceful degradation rather than failure.
- **`hashes`** — SHA-256 from `uv.lock`'s `wheels[*].hash` (or `sdist.hash` fallback). Pipeline and vendored components carry no hash here — the GitHub-Action PURL already encodes the commit SHA, and vendored entries aren't in the lockfile.
- **`external_references`** — every PyPI component points at its PyPI project page (`type=website`); GitHub Actions point at their repo (`type=vcs`); GitLab images point at their registry (`type=distribution`).

### Validation

`./workflow.cmd secure.sbom-validate` runs the CycloneDX 1.7 JSON-schema validator in a clean `uv run python` subprocess (so the venv-installed validator wins over the older vendored `jsonschema` that the workflow.cmd launcher places earlier on `sys.path`). The aggregate `./workflow.cmd secure` runs all three sub-steps; a clean run means: no known vulns, a fresh SBOM written, validated against the schema.

### What this enables

- A downstream consumer can extract the SBOM from the wheel with `unzip -p <wheel> <project_slug>/sbom.cdx.json` or `importlib.resources` — no separate artefact to track.
- A security responder can answer "are we affected by X?" against your project in seconds, not hours.
- Compliance frameworks (SLSA, NIST SSDF, EU CRA) that mandate SBOMs are satisfied — the SBOM travels with the artefact instead of needing to be re-correlated post-release.

The SBOM exists whether you have a Dependency Track server or not. It's part of every release.

## Layer 3 — Dependency Track (optional)

If `integrate_dependency_track` was enabled at generation time, `./workflow.cmd secure.sbom-upload` POSTs the SBOM to an OWASP Dependency Track instance.

What DT adds on top of layer 2:

- Continuous re-evaluation. DT re-checks your project against new CVEs every time the advisory database updates — without you re-running anything.
- Aggregation. One pane of glass across many projects in the same DT instance.
- Policy. You can set DT to fail builds based on policies (e.g. "no critical CVEs older than 30 days").
- Notification. DT can email/Slack on new findings against any tracked project.

Without DT, you only see what `pip-audit` reports at the moment you ran it. With DT, your release is *continuously* re-assessed against the world.

See [Upload an SBOM to Dependency Track](../how-to/upload-an-sbom-to-dependency-track.md) for setup.

## What about overrides?

`.security-overrides` is a project-local allow-list with mandatory expiry dates. It applies to `pip-audit`. It does **not** suppress findings in the SBOM or in Dependency Track — those continue to show the world the full truth. Override = "we accept this locally for now," not "make this invisible."

The expiry dates are load-bearing: a stale override is a security regression hidden in plain sight. The template's lint config doesn't enforce this; treat it as a code-review convention.

## What's deliberately out of scope

- **SAST**: Bandit / Semgrep / pyright security rules are not shipped. Add them as a `secure.*` task if you want them.
- **Container scanning**: Trivy / Grype are not shipped. The deps image built by `container.publish` is a dev convenience, not a published artifact, so we don't gate releases on it.
- **License compliance**: SBOM includes license metadata, but the template doesn't enforce license policies. DT does, if you turn it on there.

## See also

- [Triage a security finding](../how-to/triage-a-security-finding.md) — response playbook.
- [Upload an SBOM to Dependency Track](../how-to/upload-an-sbom-to-dependency-track.md) — wiring up layer 3.
