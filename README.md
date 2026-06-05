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
[![Pipeline](https://img.shields.io/badge/pipeline-unknown-lightgrey)](https://docs.gitlab.com/ee/ci/)
[![Coverage](https://img.shields.io/badge/coverage-60%25-orange)](https://coverage.readthedocs.io/)
[![pyscn quality](https://img.shields.io/badge/pyscn-not%20rated-lightgrey)](https://pyscn.ludo-tech.org)

CLI to set overrides idempotently for multiple SLO's

## Usage

Legacy: `pip install datadog_slo_overrides_cli`

Preferred: `uv add datadog_slo_overrides_cli`

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
