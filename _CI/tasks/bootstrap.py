"""Bootstrap task definitions for initial development environment setup."""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from invoke import Collection, Context, Task, task

from .configuration import SENTINEL
from .shared import execute, is_ci, logged


@dataclass
class BootstrapStep:
    """A single bootstrap step with CI-aware execution behavior.

    Attributes:
        name: Display name for the step.
        action: Callable that performs the step.
        prompt: Question to ask locally. If empty, the step always runs.
        ci_behavior: What to do in CI — 'run' (auto-execute) or 'skip' (silently skip).
    """

    name: str
    action: Callable[[Context], None]
    prompt: str = ''
    ci_behavior: str = 'skip'


@task
def ensure_git_repo(context: Context) -> None:
    """Initialise a git repository in the current directory if one does not exist."""
    result = context.run('git rev-parse --git-dir', hide=True, warn=True)
    if result is None or result.failed:
        execute(context, 'git init')


@task(pre=[ensure_git_repo])
def install_pre_commit(context: Context) -> None:
    """Install and activate pre-commit hooks."""
    execute(context, 'uv run pre-commit install')


# Register steps here — add new ones as needed
STEPS: list[BootstrapStep] = [
    BootstrapStep(
        name='pre-commit hooks',
        action=cast(Callable[[Context], None], install_pre_commit),
        prompt='Install pre-commit hooks? [y/N] ',
        ci_behavior='skip',
    ),
]


def run_action(action: Callable[[Context], None], context: Context) -> None:
    """Execute an action, walking invoke pre-task chains if present."""
    for pre in getattr(action, 'pre', []):
        run_action(pre, context)
    action(context)


def run_steps(context: Context) -> None:
    """Execute all registered bootstrap steps, respecting CI and TTY context."""
    non_interactive = is_ci() or not sys.stdin.isatty()
    for step in STEPS:
        if non_interactive:
            if step.ci_behavior == 'run':
                print(f'  Running {step.name}...')
                run_action(step.action, context)
            else:
                print(f'  Skipping {step.name} (non-interactive mode)')
        elif step.prompt:
            if input(step.prompt).strip().lower() in ('y', 'yes'):
                run_action(step.action, context)
        else:
            run_action(step.action, context)


@task
@logged('bootstrap')
def bootstrap(context: Context, force: bool = False) -> None:
    """Set up the development environment (runs once).

    Args:
        context: Invoke context.
        force: Force re-bootstrap even if already done.
    """
    if SENTINEL.exists() and not force:
        return
    run_steps(context)
    SENTINEL.touch()


namespace = Collection('bootstrap')
namespace.add_task(cast(Task, bootstrap), default=True, name='all')
