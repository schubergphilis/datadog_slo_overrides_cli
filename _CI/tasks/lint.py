"""Linting task definitions."""

from typing import cast

from invoke import Collection, Context, Task, task

from .configuration import PATHS
from .shared import execute, logged, run, run_steps


@task
@logged('lint.ruff')
@run(f'uv run ruff check {PATHS}')
def ruff_lint(context: Context) -> None:
    """Run ruff linter."""


@task
@logged('lint.format')
@run(f'uv run ruff format --check {PATHS}')
def format_check(context: Context) -> None:
    """Report code that is not correctly formatted, without modifying any files."""


@task
@logged('lint.pylint')
@run(f'uv run pylint {PATHS}')
def pylint(context: Context) -> None:
    """Run pylint."""


@task
@logged('lint.ty')
@run(f'uv run ty check {PATHS}')
def ty(context: Context) -> None:
    """Run ty type checker."""


@task
@logged('lint.complexipy')
@run('uv run complexipy src/')
def complexipy(context: Context) -> None:
    """Run complexipy cognitive complexity checker."""


@task
@logged('lint.commitizen')
def commitizen(context: Context, commit_msg_file: str | None = None) -> None:
    """Lint commit messages using commitizen conventional commits.

    Args:
        context: Invoke context.
        commit_msg_file: Path to a commit message file (used by commit-msg hooks).
            When omitted, checks the last committed message.
    """
    if commit_msg_file:
        execute(context, f'uv run cz check --commit-msg-file {commit_msg_file}')
    elif context.run('git rev-parse HEAD', hide=True, warn=True):
        execute(context, 'uv run cz check --rev-range HEAD')
    else:
        print('No commits yet — skipping commitizen check.')


@task
@logged('lint')
def lint(context: Context) -> None:
    """Run all linting steps; reports all failures before exiting."""
    run_steps(ruff_lint, format_check, pylint, ty, complexipy, commitizen)(context)


namespace = Collection('lint')
namespace.add_task(cast(Task, lint), default=True, name='all')
namespace.add_task(cast(Task, ruff_lint), name='ruff')
namespace.add_task(cast(Task, format_check), name='format-check')
namespace.add_task(cast(Task, pylint))
namespace.add_task(cast(Task, ty))
namespace.add_task(cast(Task, complexipy))
namespace.add_task(cast(Task, commitizen))
