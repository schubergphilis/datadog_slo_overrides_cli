"""Test task definitions."""

import json
import re
import tomllib
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

from .shared import execute, logged, open_command, run, run_steps

COVERAGE_REPORT = Path('reports/coverage.json')
PYPROJECT = Path('pyproject.toml')
SCAFFOLD_TEST_FILE = Path('tests/test_datadog_slo_overrides_cli.py')
SCAFFOLD_MARKER = 'def test_sanity'


def coverage_color(pct: float) -> str:
    """Return a badge color for a coverage percentage."""
    if pct >= 90:
        return 'brightgreen'
    if pct >= 80:
        return 'green'
    if pct >= 70:
        return 'yellow'
    if pct >= 60:
        return 'orange'
    return 'red'


def read_coverage_pct() -> int | None:
    """Return the rounded total coverage percentage from the latest pytest-cov JSON report."""
    if not COVERAGE_REPORT.exists():
        return None
    try:
        report = json.loads(COVERAGE_REPORT.read_text(encoding='utf-8'))
        return round(report['totals']['percent_covered'])
    except (ValueError, KeyError):
        return None


def scaffold_is_pristine() -> bool:
    """Return True iff the scaffolded smoke test `test_sanity` is still in the project.

    The presence of `def test_sanity` in ``tests/test_<slug>.py`` is the signal that the user
    hasn't started writing real tests yet. While it's there, the ratchet stays dormant so
    a 100%-coverage scaffold doesn't lock new development out of the build.
    """
    return SCAFFOLD_TEST_FILE.exists() and SCAFFOLD_MARKER in SCAFFOLD_TEST_FILE.read_text(encoding='utf-8')


def ratchet_fail_under() -> None:
    """Bump ``fail_under`` in pyproject.toml after a green test run, gated on the scaffold signal.

    In ``auto-detect`` mode (the default), the ratchet is dormant while the scaffolded
    ``test_sanity`` is still present in ``tests/test_<slug>.py``. The moment the user removes
    that function — i.e., the moment they replace the scaffold with real tests — the ratchet
    engages and behaves strictly: it bumps ``fail_under`` upward to the latest coverage
    percentage and never lets it drop. Once any non-zero ``fail_under`` is written, the
    dormancy gate never closes again, regardless of whether ``test_sanity`` reappears.

    In ``strict`` mode (set ``[tool.test-ratchet] mode = "strict"`` in pyproject.toml), the
    gate is bypassed and the ratchet engages immediately on run #1.

    Every invocation prints a single status line so the user can tell at a glance whether the
    ratchet is dormant, just bumped, or holding steady.
    """
    if not COVERAGE_REPORT.exists() or not PYPROJECT.exists():
        return
    pct = read_coverage_pct()
    if pct is None:
        return

    content = PYPROJECT.read_text(encoding='utf-8')
    match = re.search(r'^fail_under\s*=\s*(\d+)', content, re.MULTILINE)
    if not match:
        return
    current = int(match.group(1))

    try:
        ratchet_config = tomllib.loads(content).get('tool', {}).get('test-ratchet', {})
    except tomllib.TOMLDecodeError:
        ratchet_config = {}
    mode = ratchet_config.get('mode', 'auto-detect')

    if mode == 'auto-detect' and current == 0 and scaffold_is_pristine():
        print(f'[ratchet] coverage={pct}% — scaffold still pristine (test_sanity present); ratchet dormant')
        print(f'[ratchet]   ratchet engages when you remove `test_sanity` in {SCAFFOLD_TEST_FILE}')
        print('[ratchet]   to engage immediately, set [tool.test-ratchet] mode = "strict" in pyproject.toml')
        return

    if pct > current:
        updated = content[: match.start()] + f'fail_under = {pct}' + content[match.end() :]
        PYPROJECT.write_text(updated, encoding='utf-8')
        print(f'[ratchet] coverage={pct}% — fail_under bumped {current} → {pct}')
    else:
        print(f'[ratchet] coverage={pct}% — fail_under stays at {current}')


def update_coverage_badge() -> None:
    """Update the coverage badge in README.md from the latest coverage report."""
    readme = Path('README.md')
    if not readme.exists() or not COVERAGE_REPORT.exists():
        return
    try:
        report = json.loads(COVERAGE_REPORT.read_text(encoding='utf-8'))
        pct = round(report['totals']['percent_covered'])
    except (ValueError, KeyError):
        return
    color = coverage_color(pct)
    content = readme.read_text(encoding='utf-8')
    updated = re.sub(
        r'(\[!\[Coverage\]\(https://img\.shields\.io/badge/coverage-)[^)]+(\))',
        rf'\g<1>{pct}%25-{color}\2',
        content,
    )
    if updated != content:
        readme.write_text(updated, encoding='utf-8')
        print(f'Updated coverage badge to {pct}%.')


@task
@logged('test.pytest')
@run('uv run pytest')
def pytest(context: Context) -> None:
    """Run pytest."""


@task
@logged('test.coverage')
def coverage(context: Context) -> None:
    """Show test coverage report in terminal."""
    execute(context, 'uv run coverage report')
    execute(context, f'uv run coverage json -o {COVERAGE_REPORT}')
    update_coverage_badge()
    ratchet_fail_under()


@task
@logged('test.view')
def view(context: Context) -> None:
    """Run tests and open HTML test and coverage reports in browser."""
    test(context)
    execute(context, f'{open_command()} reports/tests.html')
    execute(context, f'{open_command()} reports/coverage/index.html')


@task
@logged('test.tox')
@run('uv run tox run-parallel')
def tox(context: Context) -> None:
    """Run the pytest matrix across every supported Python version via tox."""


@task
@logged('test')
def test(context: Context) -> None:
    """Run all test steps; reports all failures before exiting."""
    run_steps(pytest)(context)
    update_coverage_badge()
    ratchet_fail_under()


namespace = Collection('test')
namespace.add_task(cast(Task, test), default=True, name='all')
namespace.add_task(cast(Task, pytest))
namespace.add_task(cast(Task, coverage))
namespace.add_task(cast(Task, tox))
namespace.add_task(cast(Task, view))
