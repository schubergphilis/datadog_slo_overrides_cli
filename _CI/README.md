# _CI — Workflow Tooling

A portable, vendored CI/CD framework built on [Invoke](https://www.pyinvoke.org/).
All dependencies ship in `lib/vendor/`, so `./workflow.cmd` works immediately after clone — no global installs required.

## Design Principles

**Modular by concern.** Each task module owns one domain (lint, test, build, security, etc.) and exposes an Invoke `Collection` namespace. Modules never reach into each other's internals — they compose via `run_steps()` or direct function calls.

**Run-once bootstrap.** A sentinel file (`_CI/.bootstrapped`) ensures first-time setup (pre-commit installation, etc.) runs exactly once. Every task inherits bootstrap as an Invoke `pre` task, so it triggers automatically on first use and is a no-op thereafter. Pass `--force` to re-run.

**CI-aware.** Bootstrap steps declare a `ci_behavior` (`'run'` or `'skip'`). When the `CI` environment variable is set (GitHub Actions), interactive prompts are suppressed and steps execute or skip accordingly.

**Fail-last reporting.** `run_steps()` runs all steps even if earlier ones fail, then reports every failure before raising `SystemExit(1)`. No silent swallowing, no short-circuiting.

**Visual feedback.** The `@logged(name)` decorator emits pass/fail status per step, making terminal output scannable at a glance.

**Cross-platform.** The `workflow.cmd` launcher is a polyglot script — sh on Unix/macOS, cmd.exe on Windows — that delegates to the vendored Invoke CLI via `uv run`.

## Interface

All commands are invoked via `./workflow.cmd <namespace>.<task>`. Running a namespace without a task name executes its default.

```
bootstrap              One-time dev environment setup
bootstrap --force      Re-run bootstrap

build                  Security checks + package build
build.package          Package only (uv build)

container              Build and publish the deps image (alias for container.publish)
container.build        Build dependency Docker image locally
container.publish      Build and publish deps image to the container registry (CI) or locally

develop.pre-commit-install   Install pre-commit hooks
develop.pre-commit           Run all hooks on entire codebase

document               Build and view documentation
document.build         Build docs (properdocs)
document.view          Open docs in browser

format                 Format code and sort imports
format.ruff            Ruff import sorting + code formatting

lint                   Run all linters
lint.ruff              Ruff check
lint.pylint            Pylint
lint.ty                ty type checker
lint.complexipy        Cognitive complexity
lint.commitizen        Commit message validation

quality                Run all quality checks
quality.pyscn-analyze  Comprehensive analysis with HTML report
quality.pyscn-check    CI-friendly quality gate

release                Prepare release on `release/<version>` branch: validate, branch off main, bump, changelog, push branch + tag
release -i <type>      Version increment type: major, minor, patch, alpha, beta, rc
release --no-push      Keep the branch + tag local instead of pushing
release.validate       Ensure working tree is clean
release.bump           Bump version and create git tag
release.changelog      Print changelog to stdout (--write to persist and commit)
release.push           Push current branch and tags to remote
release.publish        Build, publish to PyPI, upload SBOM (invoked by CI on release-tag merge)
release.clean          Remove build artifacts (dist/ and sbom.json)

secure                 Run all security checks
secure.audit           pip-audit (supports --ignore with expiry dates)
secure.sbom-extract    Generate CycloneDX SBOM (--write to save to sbom.json)

test                   Run all tests
test.pytest            Run pytest with coverage and HTML reports
test.coverage          Show test coverage report in terminal
test.view              Open HTML test and coverage reports in browser
```

## Shared Utilities (`tasks/shared.py`)

| Utility | Purpose |
|---------|---------|
| `execute(ctx, cmd)` | Run a shell command; raise `SystemExit(1)` on failure |
| `@run(cmd)` | Decorator — replace function body with a shell command |
| `@logged(name)` | Decorator — print pass/fail status after execution |
| `run_steps(*fns)` | Run all steps, accumulate failures, raise once at the end |

## Configuration (`tasks/configuration.py`)

Centralized constants shared across task modules:

| Constant | Purpose |
|----------|---------|
| `PATHS` | Standard directories targeted by linters/formatters: `src/ _CI/tasks/ tests/` |
| `SECURITY_OVERRIDE_ENV` | Environment variable name for security audit overrides |
| `IGNORE_PATTERN` | Regex for parsing vulnerability IDs with optional expiry dates |
| `IMAGE_NAME` | Container image name for the deps cache |
| `PYSCN_REPORTS_DIR` | Directory for pyscn HTML reports |
| `SENTINEL` | Bootstrap sentinel file path |

## Bootstrap Framework (`tasks/bootstrap.py`)

New setup steps are added by appending to the `STEPS` list:

```python
STEPS: list[BootstrapStep] = [
    BootstrapStep(
        name='pre-commit hooks',
        action=install_pre_commit,
        prompt='Install pre-commit hooks? [y/N] ',
        ci_behavior='skip',  # 'run' or 'skip' in CI
    ),
    # Add more steps here
]
```

Each step has a `prompt` for local interactive use and a `ci_behavior` for unattended execution.

## Directory Layout

```
_CI/
  tasks/
    __init__.py        Namespace aggregation + bootstrap wiring
    configuration.py   Centralized constants
    shared.py          Core decorators and utilities
    bootstrap.py       One-time setup framework
    build.py           Package build
    container.py       Docker image
    develop.py         Pre-commit management
    document.py        ProperDocs documentation
    format_.py         Ruff formatting + import sorting
    lint.py            Ruff, pylint, ty, complexipy, commitizen
    quality.py         pyscn analysis
    release.py         Version bump, changelog, publish, SBOM upload
    secure.py          pip-audit, CycloneDX SBOM, Dependency Track upload
    test.py            pytest
  lib/
    vendor/            Vendored Invoke + dependencies (committed)
    vendor.txt         Pinned dependency list
```
