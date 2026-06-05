"""Security task definitions."""

import os
from datetime import date
from typing import cast

from invoke import Collection, Context, Task, task

from .configuration import IGNORE_PATTERN, SBOM_FILE, SECURITY_OVERRIDE_ENV, SECURITY_OVERRIDES_FILE
from .sbom import render_sbom, validate_sbom, write_sbom
from .shared import execute, logged


def validate_override_entry(entry: str, source: str) -> None:
    """Abort with a clear message if `entry` is not a valid suppression.

    Format: ``<VULN_ID>[::YYYY-MM-DD]`` — the vulnerability id must match the
    project-wide ``IGNORE_PATTERN``, and the expiry, when present, must be a
    real calendar date. ``source`` is prepended to the error for context
    (e.g. ``.security-overrides:7``).

    Raises:
        SystemExit: with exit code 1 if the entry is malformed.
    """
    expected = (
        '<VULN_ID>[::YYYY-MM-DD] — e.g. CVE-2024-1234::2026-12-31 (expires, '
        'forces re-review on the date) or plain CVE-2024-1234 (permanent '
        'suppression, the audit will never flag this vulnerability again — '
        'prefer an expiry when you can)'
    )
    match = IGNORE_PATTERN.fullmatch(entry)
    if not match:
        print(f'{source}: invalid entry {entry!r}; expected {expected}')
        raise SystemExit(1)
    expiry = match.group('expiration_date')
    if not expiry:
        return
    try:
        date.fromisoformat(expiry)
    except ValueError as exc:
        print(f'{source}: invalid expiry {expiry!r}: {exc}; expected YYYY-MM-DD (or omit for a permanent suppression)')
        raise SystemExit(1) from None


def load_overrides_file() -> str:
    """Return comma-joined entries from `.security-overrides`, stripping `#` comments and blanks."""
    if not SECURITY_OVERRIDES_FILE.exists():
        return ''
    entries: list[str] = []
    for lineno, raw in enumerate(SECURITY_OVERRIDES_FILE.read_text(encoding='utf-8').splitlines(), start=1):
        entry = raw.split('#', 1)[0].strip()
        if not entry:
            continue
        validate_override_entry(entry, f'{SECURITY_OVERRIDES_FILE}:{lineno}')
        entries.append(entry)
    return ','.join(entries)


@task
@logged('secure.audit')
def audit(context: Context, ignore: str | None = None) -> None:
    """Run pip-audit security scan.

    Suppressions are sourced, in precedence order, from ``--ignore``, the
    ``DATADOG_SLO_OVERRIDES_CLI_SECURITY_OVERRIDE`` environment
    variable, and a ``.security-overrides`` file at the project root. All three
    are merged and deduplicated. Each entry is a vulnerability ID with an
    optional expiry (``CVE-2024-1234::2026-12-31``); entries whose expiry has
    passed are dropped so the audit fails until the suppression is reviewed.

    Args:
        context: Invoke context.
        ignore: Comma-separated vulnerability IDs to ignore.
    """
    today = date.today()  # noqa: DTZ011
    env_override = os.environ.get(SECURITY_OVERRIDE_ENV, '')
    file_override = load_overrides_file()
    combined = ','.join(filter(None, [ignore, env_override, file_override]))
    ignore_args = [
        f'--ignore-vuln {m.group("vulnerability_id")}'
        for m in IGNORE_PATTERN.finditer(combined)
        if not m.group('expiration_date') or date.fromisoformat(m.group('expiration_date')) > today
    ]
    ignore_opts = (' ' + ' '.join(ignore_args)) if ignore_args else ''
    execute(context, f'uv run pip-audit{ignore_opts}')


@task
@logged('secure.sbom-extract')
def sbom_extract(context: Context, write: bool = False) -> None:  # noqa: ARG001
    """Compose a CycloneDX SBOM covering runtime deps, vendored CI deps, and pipeline components.

    By default prints the SBOM to stdout. With --write, writes the SBOM to the
    package data path so `uv build` ships it inside the wheel.

    Args:
        context: Invoke context.
        write: Write SBOM to ``src/<package>/sbom.cdx.json`` instead of printing to stdout.
    """
    if write:
        write_sbom()
        print(f'Wrote SBOM to {SBOM_FILE}.')
    else:
        print(render_sbom())


@task
@logged('secure.sbom-validate')
def sbom_validate(context: Context) -> None:
    """Validate the generated SBOM against the CycloneDX 1.7 JSON schema.

    Re-runs ``sbom-extract --write`` if the SBOM file is missing so the
    validation has something to check.
    """
    if not SBOM_FILE.exists():
        sbom_extract(context, write=True)
    errors = validate_sbom()
    if errors:
        for err in errors:
            print(err)
        raise SystemExit(1)
    print(f'SBOM at {SBOM_FILE} validates against CycloneDX 1.7 schema.')


@task
@logged('secure.validate-overrides')
def validate_overrides(context: Context) -> None:  # noqa: ARG001
    """Validate every entry in .security-overrides without running the audit.

    Intended as a pre-commit hook: fails fast with a ``file:line: <reason>``
    message if any entry is malformed, the expiry is not a real date, or the
    file contains merge conflict markers. Silent no-op when the file is absent.
    """
    load_overrides_file()


@task
@logged('secure')
def secure(context: Context) -> None:
    """Run all security checks; reports all failures before exiting."""
    failed = False
    try:
        audit(context)
    except SystemExit:
        failed = True
    try:
        sbom_extract(context, write=True)
    except SystemExit:
        failed = True
    try:
        sbom_validate(context)
    except SystemExit:
        failed = True
    if failed:
        raise SystemExit(1)


namespace = Collection('secure')
namespace.add_task(cast(Task, secure), default=True, name='all')
namespace.add_task(cast(Task, audit))
namespace.add_task(cast(Task, sbom_extract))
namespace.add_task(cast(Task, sbom_validate))
namespace.add_task(cast(Task, validate_overrides))
