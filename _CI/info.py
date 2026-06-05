"""Stdlib-only project info commands.

Runnable without uv/invoke/bootstrap: exposes pyproject.toml values for
pipeline bootstrap and local inspection.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = next(
    parent for parent in Path(__file__).resolve().parents if (parent / '_CI').is_dir()
)
PYPROJECT = ROOT / 'pyproject.toml'


def load() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))


def walk(cfg: dict, path: tuple[str, ...]) -> str:
    for part in path:
        cfg = cfg[part]
    return cfg


def python_version(cfg: dict) -> str:
    requires = cfg['project']['requires-python']
    match = re.search(r'>=\s*(\d+\.\d+)', requires)
    if not match:
        msg = f'cannot parse requires-python: {requires!r}'
        raise KeyError(msg)
    return match.group(1)


def uv_version(cfg: dict) -> str:
    required = cfg['tool']['uv']['required-version']
    match = re.search(r'(\d+\.\d+(?:\.\d+)?)', required)
    if not match:
        msg = f'cannot parse [tool.uv] required-version: {required!r}'
        raise KeyError(msg)
    return match.group(1)


DISPATCH = {
    'info.uv-version': uv_version,
    'info.base-image': lambda c: walk(c, ('tool', 'docker-versions', 'base-image')),
    'info.alpine-image': lambda c: walk(c, ('tool', 'docker-versions', 'alpine-image')),
    'info.project-name': lambda c: walk(c, ('project', 'name')),
    'info.project-version': lambda c: walk(c, ('project', 'version')),
    'info.python-version': python_version,
}


def read(command: str) -> str:
    """Return the value for a full `info.<key>` command.

    Raises:
        KeyError: if the command is unknown or the backing pyproject key is missing.
    """
    if command not in DISPATCH:
        raise KeyError(command)
    return DISPATCH[command](load())


def main(argv: list[str]) -> None:
    """Dispatch a single info.<key> CLI invocation to stdout."""
    if len(argv) != 2 or argv[1] not in DISPATCH:
        sys.stderr.write('Usage: info.<command>\n')
        sys.stderr.write('Available: ' + ', '.join(sorted(DISPATCH)) + '\n')
        sys.exit(2)
    try:
        sys.stdout.write(read(argv[1]) + '\n')
    except KeyError as exc:
        sys.stderr.write(f'missing key in pyproject.toml: {exc}\n')
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv)
