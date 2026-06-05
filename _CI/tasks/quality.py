"""Quality task definitions."""

import json
import re
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

from .configuration import PYSCN_REPORTS_DIR
from .shared import execute, is_ci, logged, open_command, run, run_steps

GRADE_COLORS = {'A': 'brightgreen', 'B': 'green', 'C': 'yellow', 'D': 'orange', 'F': 'red'}
BADGE_PATTERN = re.compile(r'(!\[pyscn quality\]\(https://img\.shields\.io/badge/pyscn-)[^)]+(\)\[)')


def latest_pyscn_report() -> Path:
    """Return the most recently created pyscn HTML report."""
    return max(PYSCN_REPORTS_DIR.glob('analyze_*.html'), key=lambda p: p.stat().st_mtime)


def latest_pyscn_json() -> Path:
    """Return the most recently created pyscn JSON report."""
    return max(PYSCN_REPORTS_DIR.glob('analyze_*.json'), key=lambda p: p.stat().st_mtime)


def update_pyscn_badge() -> None:
    """Update the pyscn badge in README.md with the grade from the latest report."""
    readme = Path('README.md')
    if not readme.exists():
        return
    try:
        report = json.loads(latest_pyscn_json().read_text(encoding='utf-8'))
        grade = report['summary']['grade']
    except (ValueError, KeyError, FileNotFoundError):
        return
    color = GRADE_COLORS.get(grade, 'lightgrey')
    content = readme.read_text(encoding='utf-8')
    updated = re.sub(
        r'(\[!\[pyscn quality\]\(https://img\.shields\.io/badge/pyscn-)[^)]+(\))',
        rf'\g<1>{grade}-{color}\2',
        content,
    )
    if updated != content:
        readme.write_text(updated, encoding='utf-8')
        print(f'Updated pyscn badge to grade {grade}.')


@task
@logged('quality.pyscn-analyze')
def pyscn_analyze(context: Context) -> None:
    """Run pyscn comprehensive analysis with HTML report."""
    execute(context, 'uv run pyscn analyze src/')
    execute(context, 'uv run pyscn analyze --json src/')
    update_pyscn_badge()
    if not is_ci():
        execute(context, f'{open_command()} {latest_pyscn_report()}')


@task
@logged('quality.pyscn-check')
@run('uv run pyscn check src/')
def pyscn_check(context: Context) -> None:
    """Run pyscn CI-friendly quality gate."""


@logged('quality.pyscn-analyze')
def pyscn_analyze_only(context: Context) -> None:
    """Run pyscn analyze without opening the report."""
    execute(context, 'uv run pyscn analyze src/')
    execute(context, 'uv run pyscn analyze --json src/')
    update_pyscn_badge()


@task
@logged('quality.pyscn')
def pyscn(context: Context) -> None:
    """Run all pyscn steps; reports all failures before exiting."""
    run_steps(pyscn_analyze_only, pyscn_check)(context)


@task
@logged('quality')
def quality(context: Context) -> None:
    """Run all quality steps; reports all failures before exiting."""
    run_steps(pyscn)(context)


namespace = Collection('quality')
namespace.add_task(cast(Task, quality), default=True, name='all')
namespace.add_task(cast(Task, pyscn))
namespace.add_task(cast(Task, pyscn_analyze), name='pyscn-analyze')
namespace.add_task(cast(Task, pyscn_check), name='pyscn-check')
