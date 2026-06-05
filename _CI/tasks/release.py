"""Release task definitions."""

import os
import re
import shutil
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

from .build import build
from .configuration import OIDC_ENV_VARS, SBOM_FILE, UV_PUBLISH_SETTINGS
from .gitlab import create_release_pr, pr_create_url
from .shared import execute, logged


@task
@logged('release.validate')
def validate(context: Context) -> None:
    """Ensure the working tree is clean and in sync with origin before releasing.

    Fails if there are staged, unstaged, or untracked files; if the current
    branch has no upstream configured; or if the local branch is ahead of or
    behind origin after a fetch.
    """
    status = context.run('git status --porcelain', hide=True, warn=True)
    if status is None or status.failed:
        print('Could not determine git status.')
        raise SystemExit(1)
    if status.stdout.strip():
        print('Working tree is dirty. Commit, stash, or discard these changes before releasing:')
        print(status.stdout)
        raise SystemExit(1)

    fetch = context.run('git fetch --quiet origin', hide=True, warn=True)
    if fetch is None or fetch.failed:
        print('Could not fetch origin. Check your remote connection before releasing.')
        if fetch is not None and fetch.stderr:
            print(fetch.stderr.strip())
        raise SystemExit(1)

    upstream = context.run('git rev-parse --abbrev-ref @{upstream}', hide=True, warn=True)
    if upstream is None or upstream.failed:
        print('Current branch has no upstream configured. Set one with `git push -u origin <branch>` before releasing.')
        raise SystemExit(1)

    counts = context.run('git rev-list --left-right --count @{upstream}...HEAD', hide=True, warn=True)
    if counts is None or counts.failed:
        print('Could not compare local branch with upstream.')
        raise SystemExit(1)
    behind_str, ahead_str = counts.stdout.strip().split()
    behind, ahead = int(behind_str), int(ahead_str)

    if ahead > 0:
        ahead_log = context.run('git log --oneline @{upstream}..HEAD', hide=True, warn=True)
        print(f'You have {ahead} unpushed commit(s) on this branch. Push them before releasing:')
        if ahead_log is not None and ahead_log.stdout.strip():
            print(ahead_log.stdout.rstrip())
        raise SystemExit(1)

    if behind > 0:
        print(
            f'Your branch is {behind} commit(s) behind `{upstream.stdout.strip()}`. '
            'Pull the latest changes before releasing.'
        )
        raise SystemExit(1)


@task
@logged('release.bump')
def bump(context: Context, increment: str = '') -> None:
    """Bump the version and create a git tag.

    Args:
        context: Invoke context.
        increment: Version increment type — major, minor, patch, alpha, beta, or rc.
    """
    prerelease_types = ('alpha', 'beta', 'rc')
    semver_types = ('major', 'minor', 'patch')
    valid = semver_types + prerelease_types
    if increment not in valid:
        print('Usage: ./workflow.cmd release -i <increment>')
        print(f'  increment: {", ".join(valid)}')
        raise SystemExit(1)
    if increment in prerelease_types:
        execute(context, f'uv run cz bump --increment patch --prerelease {increment} --allow-no-commit --yes')
    else:
        execute(context, f'uv run cz bump --increment {increment} --allow-no-commit --yes')


@task
@logged('release.changelog')
def changelog(context: Context, write: bool = False) -> None:
    """Generate the changelog from all tags.

    By default prints the changelog to stdout. With --write, writes to
    docs/changelog.md and commits the result.

    Args:
        context: Invoke context.
        write: Write changelog to file and commit instead of printing to stdout.
    """
    if write:
        execute(context, 'uv run cz changelog')
        execute(context, 'git add docs/changelog.md')
        execute(context, 'git commit --no-gpg-sign -m "docs: update changelog"')
    else:
        execute(context, 'uv run cz changelog --dry-run')


@task
@logged('release.push')
def push(context: Context) -> None:
    """Push the bump commit and tag to the remote."""
    execute(context, 'git push')
    execute(context, 'git push --tags')


@task
@logged('release.publish')
def publish(context: Context) -> None:
    """Build, publish, and upload SBOM — the full post-release publishing pipeline."""
    if all(os.environ.get(var) for var in OIDC_ENV_VARS):
        print('PyPI trusted publishing (OIDC) detected — skipping legacy credential check.')
        # GH Actions injects `secrets.UV_PUBLISH_*` as empty strings when the
        # secret is undefined, and `uv publish` treats UV_PUBLISH_URL="" as an
        # explicit --publish-url with no base instead of falling back to PyPI.
        # Drop empty legacy vars so OIDC users get the default publish URL.
        for var in UV_PUBLISH_SETTINGS:
            if not os.environ.get(var, '').strip():
                os.environ.pop(var, None)
    else:
        missing = [v for v in UV_PUBLISH_SETTINGS if not os.environ.get(v)]
        if missing:
            print(
                f'Missing required environment variables: {", ".join(missing)}.\n'
                'Either provide them, or grant the publish job '
                '`permissions: id-token: write` so PyPI trusted publishing can mint a token.'
            )
            raise SystemExit(1)
    clean(context)
    build(context)
    execute(context, 'uv publish')
    clean(context)


def resolve_next_version(context: Context, increment: str) -> str:
    """Project the next version via ``cz bump --dry-run`` and parse it out.

    Raises SystemExit if the increment is invalid or cz output cannot be parsed.
    """
    prerelease_types = ('alpha', 'beta', 'rc')
    semver_types = ('major', 'minor', 'patch')
    valid = semver_types + prerelease_types
    if increment not in valid:
        print('Usage: ./workflow.cmd release -i <increment>')
        print(f'  increment: {", ".join(valid)}')
        raise SystemExit(1)
    if increment in prerelease_types:
        cmd = f'uv run cz bump --increment patch --prerelease {increment} --allow-no-commit --yes --dry-run'
    else:
        cmd = f'uv run cz bump --increment {increment} --allow-no-commit --yes --dry-run'
    result = context.run(cmd, hide=True, warn=True)
    if result is None or result.failed:
        print('Could not determine the next version (cz bump --dry-run failed).')
        if result is not None:
            print(result.stdout)
            print(result.stderr)
        raise SystemExit(1)
    output = f'{result.stdout}\n{result.stderr}'
    match = re.search(r'tag to create:\s*v?(\S+)', output)
    if match is None:
        match = re.search(r'bump:\s*version\s*\S+\s*\S+\s*(\S+)', output)
    if match is None:
        print('Could not parse the next version from cz bump --dry-run output:')
        print(output)
        raise SystemExit(1)
    return match.group(1)


def ensure_refs_are_free(context: Context, new_version: str, release_branch: str) -> None:
    """Abort before any git mutation if the target tag or branch already exists.

    Checks both local and origin copies so a stale ref on either side halts
    the release cleanly — rather than failing partway through and leaving a
    bump commit behind without a tag.
    """
    tag_ref = f'v{new_version}'
    local_tag = context.run(f'git tag --list {tag_ref}', hide=True, warn=True)
    remote_tag = context.run(
        f'git ls-remote --tags origin refs/tags/{tag_ref}',
        hide=True,
        warn=True,
    )
    local_has = local_tag is not None and local_tag.stdout.strip()
    remote_has = remote_tag is not None and remote_tag.stdout.strip()
    if local_has or remote_has:
        locations = []
        if local_has:
            locations.append('locally')
        if remote_has:
            locations.append('on origin')
        print(
            f'Tag `{tag_ref}` already exists {" and ".join(locations)}. '
            'Bump to a different version, or remove the tag everywhere before retrying:'
        )
        if remote_has:
            print(f'  git push origin :refs/tags/{tag_ref}')
        if local_has:
            print(f'  git tag -d {tag_ref}')
        raise SystemExit(1)

    local_branch = context.run(
        f'git show-ref --verify --quiet refs/heads/{release_branch}',
        hide=True,
        warn=True,
    )
    remote_branch = context.run(
        f'git ls-remote --heads origin refs/heads/{release_branch}',
        hide=True,
        warn=True,
    )
    local_branch_has = local_branch is not None and not local_branch.failed
    remote_branch_has = remote_branch is not None and remote_branch.stdout.strip()
    if local_branch_has or remote_branch_has:
        locations = []
        if local_branch_has:
            locations.append('locally')
        if remote_branch_has:
            locations.append('on origin')
        print(
            f'Branch `{release_branch}` already exists {" and ".join(locations)}. Remove it everywhere before retrying:'
        )
        if remote_branch_has:
            print(f'  git push origin --delete {release_branch}')
        if local_branch_has:
            print(f'  git branch -D {release_branch}')
        raise SystemExit(1)


@task
@logged('release')
def release(context: Context, increment: str = '', no_push: bool = False) -> None:
    """Prepare a release on a new ``release/<version>`` branch.

    Validates a clean tree on ``main``, branches off, bumps the version,
    commits the changelog, and pushes both the branch and the new tag so
    the resulting pull request carries the full release snapshot for review.
    Publish fires from CI once the PR is merged into main.

    Args:
        context: Invoke context.
        increment: Version increment type — major, minor, patch, alpha, beta, or rc.
        no_push: Skip the push step (branch + tag stay local).
    """
    validate(context)

    current = context.run('git rev-parse --abbrev-ref HEAD', hide=True, warn=True)
    if current is None or current.failed:
        print('Could not determine the current branch.')
        raise SystemExit(1)
    current_branch = current.stdout.strip()
    if current_branch != 'main':
        print(f'Releases must start from `main` (currently on `{current_branch}`).')
        raise SystemExit(1)

    new_version = resolve_next_version(context, increment)
    release_branch = f'release/{new_version}'
    ensure_refs_are_free(context, new_version, release_branch)

    execute(context, f'git checkout -b {release_branch}')
    bump(context, increment=increment)
    changelog(context, write=True)

    if no_push:
        print(f'Skipping push. Branch `{release_branch}` and tag `v{new_version}` stay local.')
        return

    execute(context, f'git push -u origin {release_branch}')
    execute(context, f'git push origin v{new_version}')

    pr_url = create_release_pr(context, release_branch, new_version)
    if pr_url:
        print()
        print(f'Release pull request opened: {pr_url}')
        return
    manual_url = pr_create_url(context, release_branch)
    if manual_url:
        print()
        print(f'Open the release pull request manually: {manual_url}')


@task
@logged('release.clean')
def clean(context: Context) -> None:  # noqa: ARG001
    """Remove build artifacts (``dist/`` and the package-data SBOM)."""
    if Path('dist').exists():
        shutil.rmtree('dist')
        print('Removed dist/')
    if SBOM_FILE.exists():
        SBOM_FILE.unlink()
        print(f'Removed {SBOM_FILE}')


namespace = Collection('release')
namespace.add_task(cast(Task, release), default=True, name='all')
namespace.add_task(cast(Task, validate))
namespace.add_task(cast(Task, bump))
namespace.add_task(cast(Task, changelog))
namespace.add_task(cast(Task, push))
namespace.add_task(cast(Task, publish))
namespace.add_task(cast(Task, clean))
