"""GitHub-specific helpers: container registry publish, release PR creation, pipeline SBOM components."""

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from invoke import Context

from _CI.info import read as read_info

from .shared import PipelineComponent, container_engine, execute

WORKFLOWS_DIR = Path('.github/workflows')
USES_PATTERN = re.compile(r'^\s*-?\s*uses:\s*([A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+)@([A-Za-z0-9._\-]+)')


class RegistrySettings(NamedTuple):
    """Container registry credentials and image-prefix for the chosen host."""

    url: str
    user: str
    password_env: str
    image_prefix: str


def registry_settings() -> RegistrySettings:
    """Return ghcr.io credentials sourced from GitHub Actions env vars."""
    # ghcr.io rejects uppercase in image paths, but GITHUB_REPOSITORY preserves org/repo case.
    repo = os.environ['GITHUB_REPOSITORY'].lower()
    return RegistrySettings(
        url='ghcr.io',
        user=os.environ['GITHUB_ACTOR'],
        password_env='GITHUB_TOKEN',
        image_prefix=f'ghcr.io/{repo}',
    )


def publish_deps_image(context: Context, tag: str) -> str:
    """Build and push the deps image to ghcr.io. Return the full image reference.

    Logs into the registry with the detected engine and builds+pushes only
    when the image is not already present.
    """
    settings = registry_settings()
    image = f'{settings.image_prefix}-deps:{tag}'
    engine = container_engine()
    context.run(
        f'echo "${settings.password_env}" | {engine} login {settings.url} -u "{settings.user}" --password-stdin',
        hide=True,
    )
    result = context.run(f'{engine} manifest inspect {image}', hide=True, warn=True)
    if result and not result.failed:
        print(f'Image already exists: {image}')
    else:
        base_image = read_info('info.base-image')
        execute(context, f'{engine} build --build-arg BASE_IMAGE={base_image} -f Dockerfile.deps -t {image} .')
        execute(context, f'{engine} push {image}')
    return image


def origin_slug(context: Context) -> str:
    """Return the ``owner/repo`` slug of the origin remote, or '' if not GitHub."""
    remote = context.run('git remote get-url origin', hide=True, warn=True)
    if remote is None or remote.failed:
        return ''
    url = remote.stdout.strip()
    match = re.match(
        r'(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$',
        url,
    )
    return match.group(1) if match else ''


def pr_create_url(context: Context, release_branch: str) -> str:
    """Compose the GitHub PR-create URL for a release branch, or '' if origin isn't GitHub."""
    slug = origin_slug(context)
    return f'https://github.com/{slug}/pull/new/{release_branch}' if slug else ''


def create_release_pr(context: Context, release_branch: str, new_version: str) -> str:
    """Create the release pull request via the GitHub REST API. Return PR URL, or '' on failure.

    Requires ``GITHUB_TOKEN`` in the environment (a PAT or fine-grained token
    with ``Contents: read/write`` and ``Pull requests: read/write`` on the repo).
    No external CLI dependencies — uses only stdlib.
    """
    token = os.environ.get('GITHUB_TOKEN', '').strip()
    if not token:
        print(
            'GITHUB_TOKEN not set — release PR will not be opened automatically. '
            'Export a token with Pull requests: read/write to have it created for you.'
        )
        return ''
    slug = origin_slug(context)
    if not slug:
        print('Origin remote is not GitHub; cannot open PR via API.')
        return ''
    payload = json.dumps(
        {
            'title': f'chore(release): v{new_version}',
            'body': (
                f'Release v{new_version} prepared by `./workflow.cmd release`. '
                'Merge this PR with a merge commit so the tag lands on main and '
                'publish fires in CI.'
            ),
            'head': release_branch,
            'base': 'main',
        }
    ).encode('utf-8')
    request = urllib.request.Request(
        f'https://api.github.com/repos/{slug}/pulls',
        data=payload,
        method='POST',
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'datadog_slo_overrides_cli-release',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:500]
        print(f'GitHub API returned {exc.code}: {detail}')
        return ''
    except OSError as exc:
        print(f'GitHub API request failed: {exc}')
        return ''
    return data.get('html_url', '')


def iter_pipeline_components() -> Iterator[PipelineComponent]:
    """Yield CI pipeline components for inclusion in the SBOM.

    Walks every workflow file under `.github/workflows/` and emits one entry per
    third-party `uses: <owner>/<repo>@<ref>` reference. Local workflow refs
    (those starting with `./`) and re-usable workflows in the same repo are
    skipped because they have no external version to record.
    """
    if not WORKFLOWS_DIR.is_dir():
        return
    seen: set[tuple[str, str]] = set()
    for workflow in sorted(WORKFLOWS_DIR.glob('*.y*ml')):
        for line in workflow.read_text(encoding='utf-8').splitlines():
            match = USES_PATTERN.match(line)
            if not match:
                continue
            name, ref = match.group(1), match.group(2)
            if (name, ref) in seen:
                continue
            seen.add((name, ref))
            yield PipelineComponent(name=name, version=ref, purl=f'pkg:github/{name}@{ref}')
