"""Documentation task definitions."""

import re
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

from .quality import update_pyscn_badge
from .shared import execute, is_ci, logged, open_command, run, run_steps
from .test import update_coverage_badge


def update_package_version_badge() -> None:
    """Update the package version badge in README.md from pyproject.toml."""
    readme = Path('README.md')
    pyproject = Path('pyproject.toml')
    if not readme.exists() or not pyproject.exists():
        return
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding='utf-8'), re.MULTILINE)
    if not match:
        return
    version = match.group(1)
    content = readme.read_text(encoding='utf-8')
    updated = re.sub(
        r'(\[!\[Version\]\(https://img\.shields\.io/badge/version-)[^)]+(\))',
        rf'\g<1>{version}-blue\2',
        content,
    )
    if updated != content:
        readme.write_text(updated, encoding='utf-8')
        print(f'Updated version badge to {version}.')


def update_python_badge() -> None:
    """Update the Python version badge in README.md from pyproject.toml classifiers."""
    readme = Path('README.md')
    pyproject = Path('pyproject.toml')
    if not readme.exists() or not pyproject.exists():
        return
    versions = re.findall(
        r'"Programming Language :: Python :: (\d+\.\d+)"',
        pyproject.read_text(encoding='utf-8'),
    )
    if not versions:
        return
    label = '%20%7C%20'.join(versions)
    content = readme.read_text(encoding='utf-8')
    updated = re.sub(
        r'(\[!\[Python\]\(https://img\.shields\.io/badge/python-)[^)]+(\))',
        rf'\g<1>{label}-blue?logo=python&logoColor=white\2',
        content,
    )
    if updated != content:
        readme.write_text(updated, encoding='utf-8')
        print(f'Updated Python badge to {" | ".join(versions)}.')


@task
@logged('document.build')
@run('uv run properdocs build')
def build(context: Context) -> None:
    """Build the documentation."""


@task
@logged('document.view')
def view(context: Context) -> None:
    """Open the built documentation in the default browser. Skipped in CI."""
    if is_ci():
        return
    execute(context, f'{open_command()} site/index.html')


@task
@logged('document')
def document(context: Context) -> None:
    """Build and open the documentation; reports all failures before exiting."""
    update_package_version_badge()
    update_python_badge()
    update_coverage_badge()
    update_pyscn_badge()
    run_steps(build, view)(context)


namespace = Collection('document')
namespace.add_task(cast(Task, document), default=True, name='all')
namespace.add_task(cast(Task, build))
namespace.add_task(cast(Task, view))
