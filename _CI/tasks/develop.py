"""Development setup task definitions."""

from typing import cast

from invoke import Collection, Context, Task, task

from .shared import logged, run


@task
@logged('develop.pre-commit-install')
@run('uv run pre-commit install')
def pre_commit_install(context: Context) -> None:
    """Install and activate pre-commit hooks."""


@task
@logged('develop.pre-commit')
@run('uv run pre-commit run --all-files')
def pre_commit(context: Context) -> None:
    """Run all pre-commit hooks on the entire codebase."""


namespace = Collection('develop')
namespace.add_task(cast(Task, pre_commit_install), name='pre-commit-install')
namespace.add_task(cast(Task, pre_commit), name='pre-commit')
