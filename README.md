# datadog slo overrides CLI

[![Version](https://img.shields.io/badge/version-0.0.0-blue)](https://pypi.org/project/datadog_slo_overrides_cli/)
[![Python](https://img.shields.io/badge/python-3.13%20%7C%203.14-blue?logo=python&logoColor=white)](https://www.python.org)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://opensource.org/license/apache-2.0)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/badge/type%20checker-ty-blue)](https://github.com/astral-sh/ty)
[![Pylint](https://img.shields.io/badge/linting-pylint-yellowgreen)](https://github.com/pylint-dev/pylint)
[![complexipy](https://img.shields.io/badge/complexity-complexipy-blue)](https://github.com/rohaquinlop/complexipy)
[![pyscn](https://img.shields.io/badge/quality-pyscn-blue)](https://pyscn.ludo-tech.org)
[![pytest](https://img.shields.io/badge/tested%20with-pytest-0A9EDC?logo=pytest&logoColor=white)](https://pytest.org)
[![tox](https://img.shields.io/badge/tested%20with-tox-blue)](https://tox.wiki)
[![ProperDocs](https://img.shields.io/badge/documented%20with-properdocs-blue)](https://properdocs.org/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Conventional Commits](https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow.svg)](https://conventionalcommits.org)
[![Changelog](https://img.shields.io/badge/changelog-Keep%20a%20Changelog%201.1.0-orange)](https://keepachangelog.com/en/1.1.0/)
[![Documentation: Diátaxis](https://img.shields.io/badge/docs-Di%C3%A1taxis-009485?logo=readthedocs&logoColor=white)](https://diataxis.fr/)
[![Build](https://img.shields.io/badge/build-unknown-lightgrey)](https://github.com/features/actions)
[![Coverage](https://img.shields.io/badge/coverage-62%25-orange)](https://coverage.readthedocs.io/)
[![pyscn quality](https://img.shields.io/badge/pyscn-not%20rated-lightgrey)](https://pyscn.ludo-tech.org)

CLI to set overrides idempotently for multiple SLO's

## Usage

<!-- usage-start -->
Set Datadog **SLO corrections** ("SLO overrides") on many SLOs at once, selected by tag.
A correction excludes a time window from an SLO's error budget (e.g. for planned downtime);
this tool is the bulk, idempotent, scriptable way to apply them.

### Install

```sh
uv tool install datadog-slo-overrides       # global `datadog-slo-overrides` command
# or run without installing:
uvx datadog-slo-overrides --help
# or, from a checkout:
uv run datadog-slo-overrides --help
```

Check the installed version with `datadog-slo-overrides --version`.

### Commands

| Command | What it does |
|---------|--------------|
| `run` | Preview (default) or `--apply` corrections to every SLO matching the tags. |
| `init-config` | Write a starter config of non-secret defaults. |
| `init-envrc` | Write a starter `.envrc` for optional, direnv-managed credential loading. |

### Credentials

The tool never stores credentials. They are resolved in this order (first wins):

1. `--api-key` / `--app-key` flags
2. `DD_API_KEY` / `DD_APP_KEY` environment variables
3. an optional `.envrc` in the config dir, loaded via [direnv](https://direnv.net/)

direnv-loaded values can never override a flag or a real environment variable.

**Optional direnv setup** (keep secret-fetching logic in a file that direnv's approval model governs,
rather than the tool executing shell itself):

```sh
datadog-slo-overrides init-envrc                 # writes ~/.config/datadog-slo-overrides/.envrc
# edit it to `export DD_API_KEY=...` / `export DD_APP_KEY=...` (e.g. from Vault), then:
direnv allow ~/.config/datadog-slo-overrides
```

If the `.envrc` is present but unapproved, or doesn't export the keys, the tool prints an
actionable hint instead of failing silently. The config dir honours `XDG_CONFIG_HOME`.

### Selecting SLOs

- `--tag key:value` — repeat to require several tags. One tag is sent to Datadog's
  (single-tag) server query; the rest are ANDed client-side.
- `--tags-query "<raw>"` — a raw single-tag Datadog query, used as-is instead of `--tag`.

### Idempotency strategies

`run` is idempotent: re-running the same command never creates duplicate corrections.
`--strategy` controls when an existing correction counts as already covering your window:

| `--strategy` | Skips (creates nothing) when… |
|--------------|-------------------------------|
| `skip-if-covered` *(default)* | your window is **fully inside** an existing correction |
| `skip-if-overlap` | any existing correction **overlaps** your window (may leave gaps) |
| `skip-if-exact` | an existing correction matches your window **exactly** |

### Examples

Preview which SLOs would be corrected (dry run — nothing is written):

```sh
datadog-slo-overrides run --tag app:gitlab --tag customer:sbp \
    --start 2026-06-10T22:00 --end 2026-06-11T00:00
```

Apply a 2-hour scheduled-maintenance correction:

```sh
datadog-slo-overrides run --tag app:gitlab --tag customer:sbp \
    --start 2026-06-10T22:00 --end 2026-06-11T00:00 \
    --category "Scheduled Maintenance" --description "DB maintenance" \
    --apply
```

When a matched SLO is already covered, it is skipped rather than duplicated:

```text
Already satisfied under --strategy skip-if-covered (will skip): 1
  SBP - SLO monitor for the sbp gitlab Website  (fbb8a2c3…)  -> correction d9e08dd2-…

DRY RUN — would create 0, skip 1 already present. Re-run with --apply to write.
```

### Configuration file

`init-config` writes non-secret defaults (`site`, `timezone`, `category`, `strategy`) to
`~/.config/datadog-slo-overrides/config.toml`. CLI flags override the config, which overrides
the built-in defaults. Credentials are **never** read from this file.

```sh
datadog-slo-overrides init-config
```

Run `datadog-slo-overrides run --help` for the full list of options.
<!-- usage-end -->

## Developing further

> Development flow as [Paleofuturistic Python](https://github.com/schubergphilis/paleofuturistic_python)

Prerequisite: [uv](https://docs.astral.sh/uv/)

### Setup

- Fork and clone this repository.
- On first run of any workflow command, the bootstrap step will prompt to install pre-commit hooks.

### Workflow

All commands are invoked via `./workflow.cmd <namespace>.<task>`:

| Command | Description |
|---------|-------------|
| `./workflow.cmd format` | Format code and sort imports |
| `./workflow.cmd lint` | Run all linters (ruff, pylint, ty, complexipy, commitizen) |
| `./workflow.cmd test` | Run all tests (pytest) |
| `./workflow.cmd build` | Run security checks and build the package |
| `./workflow.cmd release -i <type>` | Bump version, tag, push, build, publish, and upload SBOM |
| `./workflow.cmd quality` | Run code quality analysis (pyscn) |
| `./workflow.cmd secure` | Run security audit and generate SBOM |
| `./workflow.cmd document` | Build and view documentation (properdocs) |
| `./workflow.cmd develop.pre-commit` | Run all pre-commit hooks on the codebase |
| `./workflow.cmd bootstrap --force` | Re-run the development environment setup |

### Development cycle

- Add dependencies: `uv add some_lib_you_need`
- Develop (optional, tinker: `uvx --with-editable . ptpython`)
- Format: `./workflow.cmd format`
- Lint: `./workflow.cmd lint`
- Test: `./workflow.cmd test`
- Build: `./workflow.cmd build`
- Review docs: `./workflow.cmd document`
- Make a pull request.
