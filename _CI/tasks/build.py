"""Build task definitions."""

import re
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

from .secure import secure
from .shared import logged, run, run_steps

STATUS_COLORS = {'passing': 'brightgreen', 'failing': 'red'}


def update_build_badge(status: str) -> None:
    """Update the build badge in README.md."""
    readme = Path('README.md')
    if not readme.exists():
        return
    color = STATUS_COLORS.get(status, 'lightgrey')
    content = readme.read_text(encoding='utf-8')
    updated = re.sub(
        r'(\[!\[Build\]\(https://img\.shields\.io/badge/build-)[^)]+(\))',
        rf'\g<1>{status}-{color}\2',
        content,
    )
    if updated != content:
        readme.write_text(updated, encoding='utf-8')
        print(f'Updated build badge to {status}.')


@task
@logged('build.package')
@run('uv build')
def package(context: Context) -> None:
    """Build the package."""


@task
@logged('build')
def build(context: Context) -> None:
    """Run security checks and build the package; reports all failures before exiting."""
    try:
        run_steps(secure, package)(context)
    except SystemExit:
        update_build_badge('failing')
        raise
    update_build_badge('passing')


namespace = Collection('build')
namespace.add_task(cast(Task, build), default=True, name='all')
namespace.add_task(cast(Task, package))
