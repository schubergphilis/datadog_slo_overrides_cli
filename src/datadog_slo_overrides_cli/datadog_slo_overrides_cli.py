#
# Copyright 2026 Yorick Hoorneman
#
# Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Set Datadog SLO corrections ("SLO overrides") on multiple SLOs, selected by tag.

Dry-run by default; pass ``--apply`` to write. See README.md for usage and details.
"""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import niquests
import typer
import typer.core
from typer._click.core import Context

__author__ = 'Yorick Hoorneman <yhoorneman@schubergphilis.com>'
__docformat__ = 'google'
__date__ = '05-06-2026'
__copyright__ = 'Copyright 2026, Yorick Hoorneman'
__credits__ = ['Yorick Hoorneman']
__license__ = 'Apache-2.0'
__maintainer__ = 'Yorick Hoorneman'
__email__ = '<yhoorneman@schubergphilis.com>'
__status__ = 'Development'


class Category(str, Enum):
    """Datadog correction categories accepted by ``--category``."""

    SCHEDULED_MAINTENANCE = 'Scheduled Maintenance'
    OUTSIDE_BUSINESS_HOURS = 'Outside Business Hours'
    DEPLOYMENT = 'Deployment'
    OTHER = 'Other'


VALID_CATEGORIES = tuple(c.value for c in Category)


# How an existing correction is judged to already satisfy a requested window.
# All three are idempotent (re-running the same command never duplicates).
class Strategy(str, Enum):
    """Skip policy accepted by ``--strategy`` (all idempotent)."""

    SKIP_IF_COVERED = 'skip-if-covered'  # skip only if request is fully inside an existing one
    SKIP_IF_OVERLAP = 'skip-if-overlap'  # skip on any overlap (may leave the request partly uncovered)
    SKIP_IF_EXACT = 'skip-if-exact'  # skip only on an identical window (create even when overlapping)


SKIP_IF_COVERED = Strategy.SKIP_IF_COVERED.value
SKIP_IF_OVERLAP = Strategy.SKIP_IF_OVERLAP.value
SKIP_IF_EXACT = Strategy.SKIP_IF_EXACT.value
STRATEGIES = tuple(s.value for s in Strategy)

# Built-in defaults for the non-secret settings a config file may override.
DEFAULT_SITE = 'datadoghq.eu'
DEFAULT_TIMEZONE = 'UTC'
DEFAULT_CATEGORY = 'Scheduled Maintenance'
DEFAULT_STRATEGY = SKIP_IF_COVERED
# Only these keys are honoured from a config file. Credentials are never read here.
CONFIG_KEYS = ('site', 'timezone', 'category', 'strategy')

# Datadog credential environment variables (also the names an optional .envrc exports).
API_KEY_ENV = 'DD_API_KEY'
APP_KEY_ENV = 'DD_APP_KEY'

APP_NAME = 'datadog-slo-overrides'
PACKAGE_NAME = 'datadog_slo_overrides_cli'


def config_dir() -> Path:
    """Return the tool's config directory: ``$XDG_CONFIG_HOME/<app>`` or ``~/.config/<app>``."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / APP_NAME


DEFAULT_CONFIG_PATH = config_dir() / 'config.toml'

HTTP_TIMEOUT = 30
PAGE_SIZE = 100


@dataclass
class Correction:
    """The settings shared by every correction this run creates.

    The per-SLO id is supplied separately at POST time.
    """

    category: str
    start: int
    end: int | None
    timezone: str
    description: str = ''
    rrule: str | None = None

    def attributes(self) -> dict[str, object]:
        """Return the correction as a Datadog ``attributes`` payload, omitting unset fields."""
        attrs: dict[str, object] = {
            'category': self.category,
            'start': self.start,
            'timezone': self.timezone,
        }
        if self.end is not None:
            attrs['end'] = self.end
        if self.description:
            attrs['description'] = self.description
        if self.rrule:
            attrs['rrule'] = self.rrule
        return attrs


@dataclass
class RunConfig:
    """A fully resolved, validated run.

    Holds everything ``execute()`` needs, with no knowledge of the CLI, config
    file, or environment left to untangle.
    """

    session: niquests.Session
    base: str
    tags_query: str
    required_tags: list[str]
    start: int | None
    end: int | None
    rrule: str | None
    strategy: str
    # Set only for an --apply run with a valid window; None means dry run.
    correction: Correction | None


def to_epoch(value: str, tz: ZoneInfo) -> int:
    """Convert an epoch-seconds string or ISO 8601 datetime to epoch seconds.

    A naive datetime is interpreted in ``tz`` (the ``--timezone`` the user gave),
    so the absolute instant sent to Datadog matches the wall-clock time they meant.
    An explicit offset in the string is honoured as-is.

    Args:
        value: Epoch seconds (digits) or an ISO 8601 datetime string.
        tz: Timezone applied to a naive datetime.

    Returns:
        The instant as epoch seconds.
    """
    value = value.strip()
    if value.isdigit():
        return int(value)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def resolve_tag_filter(tags: list[str], tags_query: str | None) -> tuple[str, list[str]]:
    """Split the tag selection into a server query and a client-side AND filter.

    Datadog's ``tags_query`` filters on a single tag only, so one tag is sent to
    the server to narrow the result set and the rest are ANDed client-side. A raw
    ``tags_query`` is passed through untouched with no extra client-side filtering.

    Args:
        tags: Tags from repeated ``--tag`` options.
        tags_query: Raw single-tag query, used as-is when given.

    Returns:
        A ``(server_query, required_tags)`` pair.
    """
    if tags_query:
        return tags_query, []
    if not tags:
        sys.exit('error: provide --tag (one or more) or --tags-query')
    return tags[0], tags


def list_slos(session: niquests.Session, base: str, tags_query: str) -> list[dict]:
    """Return all SLOs matching the single-tag server query, following pagination.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        tags_query: Single-tag Datadog query.

    Returns:
        The matching SLO objects.
    """
    slos: list[dict] = []
    offset = 0
    while True:
        resp = session.get(
            f'{base}/api/v1/slo',
            params={'tags_query': tags_query, 'limit': str(PAGE_SIZE), 'offset': str(offset)},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json().get('data', [])
        slos.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return slos


def filter_by_tags(slos: list[dict], required_tags: list[str]) -> list[dict]:
    """Keep only SLOs that carry every one of ``required_tags`` (client-side AND).

    Args:
        slos: SLO objects to filter.
        required_tags: Tags that must all be present.

    Returns:
        The SLOs carrying every required tag.
    """
    if not required_tags:
        return slos
    wanted = set(required_tags)
    return [s for s in slos if wanted <= set(s.get('tags', []))]


def print_preview(slos: list[dict], tags_query: str) -> None:
    """Print the matched SLOs as an aligned ID/name/tags table.

    Args:
        slos: SLO objects to display.
        tags_query: The query label shown in the header.
    """
    typer.echo(f'\nTags query : {tags_query}')
    typer.echo(f'Matched    : {len(slos)} SLO(s)\n')
    if not slos:
        return
    rows = [(s.get('id', ''), s.get('name', '<unnamed>'), ','.join(s.get('tags', []))) for s in slos]
    id_w = max(len('SLO ID'), *(len(r[0]) for r in rows))
    name_w = max(len('NAME'), *(len(r[1]) for r in rows))
    typer.echo(f'{"SLO ID".ljust(id_w)}  {"NAME".ljust(name_w)}  TAGS')
    typer.echo(f'{"-" * id_w}  {"-" * name_w}  {"-" * 4}')
    for slo_id, name, tags in rows:
        typer.echo(f'{slo_id.ljust(id_w)}  {name.ljust(name_w)}  {tags}')


def create_correction(session: niquests.Session, base: str, slo_id: str, correction: Correction) -> niquests.Response:
    """POST a single SLO correction.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        slo_id: The SLO to correct.
        correction: The correction settings to apply.

    Returns:
        The HTTP response.
    """
    attributes = {'slo_id': slo_id, **correction.attributes()}
    body = {'data': {'type': 'correction', 'attributes': attributes}}
    return session.post(f'{base}/api/v1/slo/correction', json=body, timeout=HTTP_TIMEOUT)


def _matches(attrs: dict, start: int, end: int | None, rrule: str | None, strategy: str) -> bool:
    """Return True if an existing correction already satisfies the request under ``strategy``.

    Recurring corrections (rrule on either side) only ever match an identical
    start plus rrule; their occurrences can't be reasoned about by an interval
    check. For one-off corrections a missing end means open-ended (+inf):

    - skip-if-exact: identical start and end.
    - skip-if-overlap: the two half-open intervals overlap at all.
    - skip-if-covered: the requested window lies entirely within the existing one.

    Args:
        attrs: The existing correction's attributes.
        start: Requested start (epoch seconds).
        end: Requested end (epoch seconds) or None for open-ended.
        rrule: Requested recurrence rule, if any.
        strategy: One of ``STRATEGIES``.

    Returns:
        Whether the request is already satisfied.
    """
    existing_rrule = attrs.get('rrule') or None
    if rrule or existing_rrule:
        return attrs.get('start') == start and existing_rrule == (rrule or None)

    existing_start = attrs.get('start')
    if existing_start is None:
        return False
    if strategy == SKIP_IF_EXACT:
        return existing_start == start and attrs.get('end') == end

    existing_end = float('inf') if attrs.get('end') is None else attrs['end']
    requested_end = float('inf') if end is None else end
    if strategy == SKIP_IF_OVERLAP:
        return existing_start < requested_end and start < existing_end
    # skip-if-covered: the request lies entirely within the existing window.
    return existing_start <= start and requested_end <= existing_end


def find_matching_correction(
    session: niquests.Session,
    base: str,
    slo_id: str,
    start: int,
    end: int | None,
    rrule: str | None,
    strategy: str,
) -> str | None:
    """Return the id of an existing correction satisfying the request under ``strategy``.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        slo_id: The SLO to inspect.
        start: Requested start (epoch seconds).
        end: Requested end (epoch seconds) or None.
        rrule: Requested recurrence rule, if any.
        strategy: One of ``STRATEGIES``.

    Returns:
        The matching correction id, or None.
    """
    resp = session.get(f'{base}/api/v1/slo/{slo_id}/corrections', timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    for correction in resp.json().get('data', []):
        if _matches(correction.get('attributes', {}), start, end, rrule, strategy):
            return correction.get('id')
    return None


def find_existing_corrections(
    session: niquests.Session,
    base: str,
    slos: list[dict],
    start: int,
    end: int | None,
    rrule: str | None,
    strategy: str,
) -> dict[str, str]:
    """Map slo_id to an existing correction id for SLOs already satisfied under ``strategy``.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        slos: Matched SLO objects.
        start: Requested start (epoch seconds).
        end: Requested end (epoch seconds) or None.
        rrule: Requested recurrence rule, if any.
        strategy: One of ``STRATEGIES``.

    Returns:
        A mapping of slo_id to the satisfying correction id.
    """
    existing: dict[str, str] = {}
    for slo in slos:
        slo_id = slo.get('id', '')
        match = find_matching_correction(session, base, slo_id, start, end, rrule, strategy)
        if match:
            existing[slo_id] = match
    return existing


def build_session(api_key: str, app_key: str) -> niquests.Session:
    """Return a Datadog session pre-loaded with the auth headers.

    Args:
        api_key: Datadog API key.
        app_key: Datadog application key.

    Returns:
        The configured session.
    """
    session = niquests.Session()
    session.headers.update(
        {
            'DD-API-KEY': api_key,
            'DD-APPLICATION-KEY': app_key,
            'Content-Type': 'application/json',
        },
    )
    return session


def report_existing(existing: dict[str, str], names: dict[str, str], strategy: str) -> None:
    """Print the SLOs that will be skipped because they are already satisfied.

    Args:
        existing: Mapping of slo_id to satisfying correction id.
        names: Mapping of slo_id to SLO name.
        strategy: The active skip strategy.
    """
    if not existing:
        return
    typer.echo(f'\nAlready satisfied under --strategy {strategy} (will skip): {len(existing)}')
    for slo_id, corr_id in existing.items():
        name = names.get(slo_id, '<unnamed>')
        typer.echo(f'  {name}  ({slo_id})  -> correction {corr_id}')


def report_dry_run(matched: int, skipping: int, *, window_given: bool) -> None:
    """Print the dry-run summary.

    Args:
        matched: Number of matched SLOs.
        skipping: Number already satisfied.
        window_given: Whether a start/end window was supplied.
    """
    if not window_given:
        typer.echo('\nDRY RUN — no corrections created. Re-run with --apply (and --start/--end) to write them.')
    else:
        typer.echo(
            f'\nDRY RUN — would create {matched - skipping}, skip {skipping} '
            'already present. Re-run with --apply to write.',
        )


def apply_corrections(
    session: niquests.Session,
    base: str,
    slos: list[dict],
    existing: dict[str, str],
    correction: Correction,
) -> tuple[int, int, int]:
    """Create the correction on each SLO not already satisfied.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        slos: Matched SLO objects.
        existing: Mapping of slo_id to satisfying correction id (skipped).
        correction: The correction settings to apply.

    Returns:
        A ``(created, skipped, failed)`` count tuple.
    """
    created = skipped = failed = 0
    for slo in slos:
        slo_id = slo.get('id', '')
        name = slo.get('name', '<unnamed>')
        if slo_id in existing:
            skipped += 1
            typer.echo(f'  skip {slo_id}  {name}  -> already satisfied by correction {existing[slo_id]}')
            continue
        resp = create_correction(session, base, slo_id, correction)
        if resp.ok:
            created += 1
            corr_id = resp.json().get('data', {}).get('id', '?')
            typer.echo(f'  ok   {slo_id}  {name}  -> correction {corr_id}')
        else:
            failed += 1
            typer.echo(f'  FAIL {slo_id}  {name}  -> {resp.status_code} {resp.text}')
    return created, skipped, failed


def load_config(path: Path) -> dict:
    """Read non-secret defaults from a TOML config, or {} if it doesn't exist.

    Credentials are never read: only ``CONFIG_KEYS`` are returned.

    Args:
        path: Path to the TOML config file.

    Returns:
        The honoured (non-secret) settings.
    """
    if not path.is_file():
        return {}
    try:
        with path.open('rb') as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.exit(f'error: could not read config {path}: {exc}')
    return {k: v for k, v in data.items() if k in CONFIG_KEYS}


def config_template() -> str:
    """Return the starter config file contents (non-secret defaults only)."""
    return (
        f'# {APP_NAME} config — non-secret defaults only.\n'
        '# Credentials are NEVER read from here: pass --api-key/--app-key or set\n'
        '# DD_API_KEY / DD_APP_KEY (e.g. via direnv + Vault).\n'
        '\n'
        f'site = "{DEFAULT_SITE}"\n'
        f'timezone = "{DEFAULT_TIMEZONE}"\n'
        f'category = "{DEFAULT_CATEGORY}"\n'
        f'strategy = "{DEFAULT_STRATEGY}"\n'
    )


def envrc_template() -> str:
    """Return the starter ``.envrc`` contents (loaded on demand by direnv)."""
    return (
        f'# Credentials for {APP_NAME}, loaded on demand by direnv — never sourced by the tool.\n'
        '# After editing, approve it:  direnv allow <this directory>\n'
        '# Read only as a fallback: an explicit --api-key/--app-key or an already-set\n'
        '# DD_API_KEY / DD_APP_KEY in your shell always take precedence.\n'
        '\n'
        '# Example — fetch from Vault (needs a valid token; run `vault login` first):\n'
        '# export DD_API_KEY="$(vault kv get -field=value secret/datadog/api-key)"\n'
        '# export DD_APP_KEY="$(vault kv get -field=value secret/datadog/app-key)"\n'
    )


def _envrc_is_blocked(stderr: str) -> bool:
    """Return True if direnv's stderr indicates the .envrc is not approved."""
    lowered = stderr.lower()
    return 'blocked' in lowered or 'not allowed' in lowered or 'direnv allow' in lowered


def _run_direnv_export(directory: Path) -> subprocess.CompletedProcess[str] | None:
    """Run ``direnv export json`` in ``directory``, or None if direnv/.envrc is absent.

    Args:
        directory: Directory whose ``.envrc`` direnv should evaluate.

    Returns:
        The completed process, or None when direnv isn't installed or no .envrc exists.
    """
    direnv = shutil.which('direnv')
    if direnv is None or not (directory / '.envrc').is_file():
        return None
    return subprocess.run(  # noqa: S603 - fixed args, executable resolved via shutil.which
        [direnv, 'export', 'json'],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


def load_direnv_env(directory: Path) -> tuple[dict[str, str], str]:
    """Return the variables an optional ``.envrc`` produces via direnv, plus direnv's stderr.

    Returns ``({}, '')`` silently when direnv isn't installed or there's no
    ``.envrc``. When direnv refuses an unapproved ``.envrc``, prints an actionable
    hint and returns ``({}, <stderr>)`` rather than crashing. The stderr is
    returned so callers can surface *why* the ``.envrc`` produced no usable values
    (e.g. a Vault command inside it failing), since direnv runs the file itself.

    Args:
        directory: Directory whose ``.envrc`` should be evaluated.

    Returns:
        A ``(exported vars, direnv stderr)`` pair.
    """
    result = _run_direnv_export(directory)
    if result is None:
        return {}, ''
    if result.returncode != 0 or _envrc_is_blocked(result.stderr):
        typer.echo(
            f'direnv could not load {directory}/.envrc (not approved?). Run: direnv allow {directory}',
            err=True,
        )
        return {}, result.stderr
    stdout = result.stdout.strip()
    if not stdout:
        return {}, result.stderr
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}, result.stderr
    return {key: value for key, value in data.items() if isinstance(value, str)}, result.stderr


def _warn_if_envrc_lacks_keys(directory: Path, direnv_env: dict[str, str], missing: list[str], stderr: str) -> None:
    """Warn when an evaluated ``.envrc`` didn't provide the credential keys still needed.

    ``direnv_env`` is non-empty only when direnv actually evaluated an approved
    ``.envrc`` (it always includes direnv's own bookkeeping vars), which lets us
    tell "loaded but missing the keys" apart from "blocked" or "no .envrc". When
    the file ran but the keys are unset/empty, direnv's stderr usually explains
    why (e.g. a failed Vault lookup), so it is echoed back as the reason.

    Args:
        directory: Directory whose ``.envrc`` was evaluated.
        direnv_env: Variables direnv exported (empty if it didn't run/was blocked).
        missing: Required credential variables still unset after the merge.
        stderr: direnv's stderr from evaluating the ``.envrc``.
    """
    if not (direnv_env and missing):
        return
    typer.echo(
        f'{directory}/.envrc was loaded via direnv but did not provide: {", ".join(missing)}',
        err=True,
    )
    diagnostic = stderr.strip()
    if diagnostic:
        typer.echo('direnv reported:', err=True)
        for line in diagnostic.splitlines():
            typer.echo(f'  {line}', err=True)


def resolve_credentials(api_key: str | None, app_key: str | None, directory: Path) -> tuple[str | None, str | None]:
    """Fill missing credentials from a direnv-loaded ``.envrc``, without overriding given values.

    Precedence (highest first): an explicit ``--flag`` or real env var (already
    folded into ``api_key``/``app_key`` by the CLI layer), then variables loaded
    from ``directory/.envrc`` via direnv. direnv is only consulted when a key is
    still missing, so it can never override a flag or a real environment variable.
    If the ``.envrc`` is evaluated but doesn't export the still-missing keys, an
    actionable hint naming them is printed.

    Args:
        api_key: API key from --api-key or the DD_API_KEY env var, if any.
        app_key: App key from --app-key or the DD_APP_KEY env var, if any.
        directory: Directory whose ``.envrc`` provides the fallback.

    Returns:
        The resolved ``(api_key, app_key)`` pair (either may still be None).
    """
    if api_key and app_key:
        return api_key, app_key
    direnv_env, stderr = load_direnv_env(directory)
    api_key = api_key or direnv_env.get(API_KEY_ENV)
    app_key = app_key or direnv_env.get(APP_KEY_ENV)
    missing = [name for name, value in ((API_KEY_ENV, api_key), (APP_KEY_ENV, app_key)) if not value]
    _warn_if_envrc_lacks_keys(directory, direnv_env, missing, stderr)
    return api_key, app_key


def build_config(
    *,
    api_key: str | None,
    app_key: str | None,
    site: str,
    timezone: str,
    category: str,
    strategy: str,
    description: str,
    tags: list[str],
    tags_query: str | None,
    start: str | None,
    end: str | None,
    rrule: str | None,
    apply: bool,
) -> RunConfig:
    """Validate already-resolved settings into a RunConfig (exits on bad input).

    Args:
        api_key: Datadog API key (required).
        app_key: Datadog application key (required).
        site: Datadog site, e.g. ``datadoghq.eu``.
        timezone: IANA timezone for the window.
        category: Correction category (see ``VALID_CATEGORIES``).
        strategy: Skip strategy (see ``STRATEGIES``).
        description: Free-text description for the correction.
        tags: Tags from repeated ``--tag``.
        tags_query: Raw single-tag query, used instead of ``tags``.
        start: Window start (ISO 8601 or epoch) or None.
        end: Window end (ISO 8601 or epoch) or None.
        rrule: Recurrence rule, if any.
        apply: Whether this is a writing run.

    Returns:
        The validated run configuration.
    """
    if not api_key or not app_key:
        sys.exit('error: provide --api-key/--app-key or set DD_API_KEY / DD_APP_KEY')
    if category not in VALID_CATEGORIES:
        sys.exit(f'error: invalid category {category!r}; choose from {", ".join(VALID_CATEGORIES)}')
    if strategy not in STRATEGIES:
        sys.exit(f'error: invalid strategy {strategy!r}; choose from {", ".join(STRATEGIES)}')
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        sys.exit(f'error: unknown timezone {timezone!r}')

    # Window is optional in dry run, required for --apply.
    start_epoch = to_epoch(start, tz) if start else None
    end_epoch = to_epoch(end, tz) if end else None

    correction = None
    if apply:
        if not description.strip():
            sys.exit('error: --description is required with --apply')
        if start_epoch is None:
            sys.exit('error: --start is required with --apply')
        if end_epoch is None and not rrule:
            sys.exit('error: provide --end (or --rrule for an indefinite recurrence)')
        correction = Correction(
            category=category,
            start=start_epoch,
            end=end_epoch,
            timezone=timezone,
            description=description,
            rrule=rrule,
        )

    server_query, required_tags = resolve_tag_filter(tags, tags_query)
    return RunConfig(
        session=build_session(api_key, app_key),
        base=f'https://api.{site}',
        tags_query=server_query,
        required_tags=required_tags,
        start=start_epoch,
        end=end_epoch,
        rrule=rrule,
        strategy=strategy,
        correction=correction,
    )


def execute(cfg: RunConfig) -> int:
    """Run a resolved config: list, preview, idempotency-skip, then apply.

    Args:
        cfg: The validated run configuration.

    Returns:
        A process exit code (0 success, 1 if any correction failed).
    """
    slos = filter_by_tags(list_slos(cfg.session, cfg.base, cfg.tags_query), cfg.required_tags)
    label = ' AND '.join(cfg.required_tags) if cfg.required_tags else cfg.tags_query
    print_preview(slos, label)
    if not slos:
        typer.echo('\nNothing to do.')
        return 0

    # Idempotency: which matched SLOs are already satisfied under cfg.strategy?
    existing: dict[str, str] = {}
    if cfg.start is not None:
        existing = find_existing_corrections(cfg.session, cfg.base, slos, cfg.start, cfg.end, cfg.rrule, cfg.strategy)
    names = {s.get('id', ''): s.get('name', '<unnamed>') for s in slos}
    report_existing(existing, names, cfg.strategy)

    if cfg.correction is None:
        report_dry_run(len(slos), len(existing), window_given=cfg.start is not None)
        return 0

    typer.echo(f'\nApplying {cfg.correction.category} correction to {len(slos)} SLO(s)...\n')
    created, skipped, failed = apply_corrections(cfg.session, cfg.base, slos, existing, cfg.correction)
    typer.echo(f'\nDone. {created} created, {skipped} skipped, {failed} failed.')
    return 1 if failed else 0


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help='Set Datadog SLO corrections on multiple SLOs, selected by tag. '
    'Dry-run by default; --apply to write. See README.md.',
)


class ChoiceHintCommand(typer.core.TyperCommand):
    """A command that lists valid values when a choice option is given without one.

    Click reports a bare ``Option '--x' requires an argument.`` for a missing value,
    raised while parsing before any option callback runs. This intercepts that error
    and, when ``--x`` is a choice option, appends the accepted values so the message
    is as helpful as the one shown for an *invalid* value.

    Detection is by duck typing (``option_name``/``message`` on the error, ``choices``
    on the param type) so it survives Typer vendoring its own copy of Click.
    """

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        """Parse args, enriching a choice option's missing-value error with its choices."""
        try:
            return super().parse_args(ctx, args)
        except Exception as exc:  # re-raised unchanged unless it's a choice option
            err: Any = exc
            option_name = getattr(err, 'option_name', None)
            message = getattr(err, 'message', None)
            if option_name and message:
                choices = self._choices_for(ctx, option_name)
                if choices is not None:
                    err.message = f'{message} Choose from {", ".join(map(repr, choices))}.'
            raise

    def _choices_for(self, ctx: Context, option_name: str) -> tuple[str, ...] | None:
        """Return the choices of the choice-typed param exposing ``option_name``, or None."""
        for param in self.get_params(ctx):
            if option_name in (*param.opts, *param.secondary_opts):
                choices = getattr(param.type, 'choices', None)
                return tuple(choices) if choices is not None else None
        return None


def _version_callback(value: bool) -> None:
    """Print the package version and exit when ``--version`` is passed."""
    if not value:
        return
    try:
        installed = package_version(PACKAGE_NAME)
    except PackageNotFoundError:
        installed = 'unknown'
    typer.echo(f'{APP_NAME} {installed}')
    raise typer.Exit


@app.callback()
def _root(
    _version: bool = typer.Option(
        False,
        '--version',
        callback=_version_callback,
        is_eager=True,
        help='Show the version and exit.',
    ),
) -> None:
    """Set Datadog SLO corrections on multiple SLOs, selected by tag.

    Dry-run by default; --apply to write. See README.md.
    """


@app.command(no_args_is_help=True, cls=ChoiceHintCommand)
def run(
    tag: list[str] = typer.Option(
        None,
        '--tag',
        metavar='KEY:VALUE',
        help='Tag to match; repeat to require several (ANDed). One is sent to Datadog, '
        'the rest filtered client-side. Mutually exclusive with --tags-query.',
    ),
    tags_query: str = typer.Option(
        None,
        '--tags-query',
        help='Raw single-tag Datadog tags_query, used as-is instead of --tag.',
    ),
    start: str = typer.Option(
        None,
        help='Correction start: ISO 8601 (2026-06-10T22:00) or epoch. Required with --apply.',
    ),
    end: str = typer.Option(
        None,
        help='Correction end: ISO 8601 or epoch. Required with --apply unless --rrule.',
    ),
    category: Category = typer.Option(
        None,
        help=f'Correction category (config/default: {DEFAULT_CATEGORY}).',
    ),
    description: str = typer.Option('', help='Free-text description stored on the correction. Required with --apply.'),
    timezone: str = typer.Option(
        None,
        help=f'IANA timezone for start/end (config/default: {DEFAULT_TIMEZONE}).',
    ),
    rrule: str = typer.Option(
        None,
        help="iCal RRULE for a recurring correction (e.g. 'FREQ=DAILY;INTERVAL=1').",
    ),
    site: str = typer.Option(None, help=f'Datadog site (config/default: {DEFAULT_SITE}).'),
    strategy: Strategy = typer.Option(
        None,
        help=f'Skip policy (config/default: {DEFAULT_STRATEGY}). All are idempotent.',
    ),
    api_key: str = typer.Option(
        None,
        '--api-key',
        envvar=API_KEY_ENV,
        help='Datadog API key (or env DD_API_KEY, or a direnv-loaded .envrc).',
    ),
    app_key: str = typer.Option(
        None,
        '--app-key',
        envvar=APP_KEY_ENV,
        help='Datadog application key (or env DD_APP_KEY, or a direnv-loaded .envrc).',
    ),
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        '--config',
        help='TOML config of non-secret defaults (never credentials).',
    ),
    apply: bool = typer.Option(
        False,
        '--apply',
        help='Create the corrections. Without it, dry-run preview only.',
    ),
) -> None:
    """Preview (default) or --apply SLO corrections to every SLO matching the tags."""
    settings = load_config(config)
    # Fill missing credentials from an optional direnv-managed .envrc next to the config.
    resolved_api_key, resolved_app_key = resolve_credentials(api_key, app_key, config.parent)
    cfg = build_config(
        api_key=resolved_api_key,
        app_key=resolved_app_key,
        site=site or settings.get('site') or DEFAULT_SITE,
        timezone=timezone or settings.get('timezone') or DEFAULT_TIMEZONE,
        category=(category.value if category else None) or settings.get('category') or DEFAULT_CATEGORY,
        strategy=(strategy.value if strategy else None) or settings.get('strategy') or DEFAULT_STRATEGY,
        description=description,
        tags=list(tag or []),
        tags_query=tags_query,
        start=start,
        end=end,
        rrule=rrule,
        apply=apply,
    )
    raise typer.Exit(execute(cfg))


@app.command('init-config')
def init_config(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, '--config', help='Where to write the config file.'),
    force: bool = typer.Option(False, '--force', help='Overwrite an existing file.'),
) -> None:
    """Write a starter config of non-secret defaults (never credentials)."""
    if config.exists() and not force:
        sys.exit(f'error: {config} already exists (use --force to overwrite)')
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(config_template())
    typer.echo(f'Wrote {config}')


@app.command('init-envrc')
def init_envrc(
    directory: Path = typer.Option(
        DEFAULT_CONFIG_PATH.parent, '--dir', help='Config directory to write the .envrc into.'
    ),
    force: bool = typer.Option(False, '--force', help='Overwrite an existing file.'),
) -> None:
    """Write a starter ``.envrc`` for optional, direnv-managed credential loading."""
    envrc = directory / '.envrc'
    if envrc.exists() and not force:
        sys.exit(f'error: {envrc} already exists (use --force to overwrite)')
    directory.mkdir(parents=True, exist_ok=True)
    envrc.write_text(envrc_template())
    typer.echo(f'Wrote {envrc}')
    typer.echo(f'Next: edit it to export DD_API_KEY / DD_APP_KEY, then run: direnv allow {directory}')


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == '__main__':
    main()
