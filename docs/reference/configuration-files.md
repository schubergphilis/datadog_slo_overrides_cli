# Configuration files

Every config file shipped at the project root, what it's for, and what NOT to edit.

## `pyproject.toml`

The single source of truth for project metadata and tool configuration. Sections worth knowing about:

| Section | Owner | Notes |
| --- | --- | --- |
| `[project]` | You | Name, description, classifiers, scripts, dependencies. Edit freely. |
| `[project.scripts]` | You | Console entry points. Add a `__main__.py` and an entry under this section to ship a CLI. |
| `[dependency-groups]` | You | Add deps via `uv add --group <name>`. See [Dependency groups](dependency-groups.md). |
| `[tool.uv]` | Template | uv-specific settings, required uv version. Don't lower `required-version`. |
| `[tool.ruff]` | Template (rule list), you (line-length etc.) | Rule selection is opinionated; ad-hoc disables go in code with `# noqa`. |
| `[tool.pylint]` | Template | Strict-by-default. Per-message disables go in code. |
| `[tool.pytest.ini_options]` | Template (framework), you (markers) | Don't disable coverage; add markers as needed. |
| `[tool.coverage]` | Template | `fail_under` is ratcheted upward automatically once the ratchet engages. Don't lower it. |
| `[tool.test-ratchet]` | Template (knob), you (mode) | `mode = "auto-detect"` (default) keeps the coverage ratchet dormant while the scaffolded `test_sanity` is in place; `mode = "strict"` engages it on run #1. See [Testing strategy](../explanation/testing-strategy.md#dormant-during-scaffold). |
| `[tool.tox]` | Template | Generated from `min_python_version` / `max_python_version`. |
| `[tool.commitizen]` | Template | Conventional-Commits parser config used by `cz changelog` and the lint hook. The template does **not** use commitizen's autorelease — the bump is chosen explicitly via `./workflow.cmd release -i <type>`. |
| `[tool.docker-versions]` | Template | Image tags consumed by `Dockerfile.deps` and CI. |

## `uv.lock`

Locked dependency graph. Managed by uv; never edit by hand. Commit it.

## `tox.ini`

Empty placeholder. Tox config lives in `pyproject.toml`'s `[tool.tox]`. The file exists because some IDEs (and older `tox` versions) expect it.

## `.pre-commit-config.yaml`

Pre-commit hook definitions. Adds: ruff format + check, pylint, commitizen. Edit to add hooks; don't remove the existing ones without thinking — they keep the main branch clean.

## `.security-overrides`

Allow-list for pip-audit findings. Each line is `<vuln-id> <YYYY-MM-DD> <justification>`. See [Triage a security finding](../how-to/triage-a-security-finding.md).

## `.gitignore`

Standard Python + the project's own outputs (`reports/`, `site/`, `dist/`, `.deps-image`, `_CI/.bootstrapped`).

## `Dockerfile.deps`

Multi-stage build for the dependency-cache image. Reads `[tool.docker-versions]` from `pyproject.toml`. See [Build and push a container](../how-to/build-and-push-a-container.md).

## `properdocs.yml`

Docs site config. Sections worth knowing:

- `nav:` — the navigation tree.
- `theme:` — `mkdocs` theme with auto color mode.
- `watch:` — `src/` is watched so docstring edits live-reload via mkdocstrings.
- `plugins:` — `include-markdown` (pulls README into `index.md`) and `mkdocstrings` (API reference from Google-style docstrings).

## `workflow.cmd` and `workflow.cmd.bat`

Polyglot launcher: a shell script on Unix, a batch file on Windows. Resolves to `uv run python -m _CI.invoke -- <args>`. Don't edit.

## `.github/` or `.gitlab-ci.yml`

The chosen host's CI config (only one of these exists per project, per the `git_hosting_service` answer). Edit to add jobs; preserve the existing lint/test/build flow if you want `copier update` to keep working.

## `.copier-answers.yml`

Copier's state file. Records the template URL, the revision, and your answers. Managed by copier — never edit it manually. To pull template updates into this project run `uvx copier update --trust` from the project root — see the [copier docs](https://copier.readthedocs.io/en/stable/updating/).
