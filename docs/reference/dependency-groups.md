# Dependency groups

The template uses [PEP 735 dependency groups](https://peps.python.org/pep-0735/) (uv's first-class support for them) to separate dev-time tooling from runtime requirements. Each group corresponds to one workflow task family.

## Runtime

The `[project.dependencies]` list in `pyproject.toml`. These ship in the published wheel. Add via:

```bash
uv add <package>
```

## Dev-time groups

Listed under `[dependency-groups]` in `pyproject.toml`. They're installed by `develop.bootstrap` and on demand by individual workflow tasks, but never end up in the wheel.

| Group | Packages | Used by |
| --- | --- | --- |
| `dev` | Aggregator that pulls in every group below. | Default `uv sync` target; convenient for IDE setup. |
| `develop` | `pre-commit`, `commitizen`, `tomlkit` | `./workflow.cmd develop.*`, `release.*` (commitizen). |
| `lint` | `ruff`, `pylint`, `ty`, `complexipy` | `./workflow.cmd lint` and `format`. |
| `test` | `pytest`, `pytest-cov`, `pytest-xdist`, `pytest-env`, `pytest-metadata`, `pytest-html`, `coverage`, `tox`, `tox-uv` | `./workflow.cmd test`. |
| `document` | `properdocs`, `mkdocstrings[python]`, `mkdocs-include-markdown-plugin` | `./workflow.cmd document`. |
| `quality` | `pyscn` | `./workflow.cmd quality`. |
| `security` | `pip-audit`, `cyclonedx-py` | `./workflow.cmd secure`. |

## Adding to a group

```bash
uv add --group <group-name> <package>
```

This updates both `pyproject.toml` and `uv.lock`. Commit both. See [How-to: add a dependency](../how-to/add-a-dependency.md) for the full flow.

## Why groups, not extras

The template used to put dev tools under `[project.optional-dependencies]` (PEP 621 extras). Those work but they conflate two ideas:

- **Optional features** of the library (e.g. an `excel` extra for openpyxl support).
- **Internal dev concerns** (lint, test, docs).

Dependency groups are explicitly for the second category. They never ship in the wheel and are never user-installable via `pip install <pkg>[<extra>]`. Cleaner contract, less to explain to users.

## How CI picks them up

The CI workflow installs only the group it needs for each job — `lint` job installs the `lint` group, `test` job installs `test`. This keeps job containers small and parallel-safe.

The container images built by `./workflow.cmd container.publish` cache the `dev` group's resolution so subsequent CI runs skip the install step.

## See also

- [Add a dependency](../how-to/add-a-dependency.md) — adding to any group.
- [Configuration files](configuration-files.md) — where the groups are declared.
