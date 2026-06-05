"""Formatting task definitions."""

from typing import cast

from invoke import Collection, Context, Task, task

from .configuration import PATHS
from .shared import execute, logged, run_steps


@task
@logged('format.ruff')
def ruff_format(context: Context) -> None:
    """Format code and sort imports with ruff."""
    execute(context, f'uv run ruff check --select I --fix {PATHS}')
    execute(context, f'uv run ruff format {PATHS}')


@task
@logged('format')
def format_(context: Context) -> None:
    """Run all formatting steps; reports all failures before exiting."""
    run_steps(ruff_format)(context)


namespace = Collection('format')
namespace.add_task(cast(Task, format_), default=True, name='all')
namespace.add_task(cast(Task, ruff_format), name='ruff')
