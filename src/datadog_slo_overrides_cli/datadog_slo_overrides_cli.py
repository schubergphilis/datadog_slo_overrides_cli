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
from typer.main import get_command

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


def resolve_timezone(name: str) -> ZoneInfo:
    """Return the ZoneInfo for a name, exiting with a clear error if it's unknown.

    Args:
        name: An IANA timezone name (e.g. ``Europe/Amsterdam``).

    Returns:
        The resolved timezone.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        sys.exit(f'error: unknown timezone {name!r}')


def default_window(tz: ZoneInfo) -> tuple[int, int]:
    """Return the default listing window: the start of the current month to now.

    Args:
        tz: Timezone in which "start of month" and "now" are anchored.

    Returns:
        A ``(start, end)`` pair as epoch seconds.
    """
    now = datetime.now(tz)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(month_start.timestamp()), int(now.timestamp())


def resolve_window(start: str | None, end: str | None, tz: ZoneInfo) -> tuple[int, int]:
    """Resolve the listing window, defaulting each unset bound.

    An unset start defaults to the first day of the current month; an unset end
    defaults to now. Both accept ISO 8601 or epoch seconds (see ``to_epoch``).

    Args:
        start: Window start (ISO 8601 or epoch) or None.
        end: Window end (ISO 8601 or epoch) or None.
        tz: Timezone applied to naive datetimes and to the defaults.

    Returns:
        A ``(start, end)`` pair as epoch seconds.
    """
    default_start, default_end = default_window(tz)
    resolved_start = to_epoch(start, tz) if start else default_start
    resolved_end = to_epoch(end, tz) if end else default_end
    return resolved_start, resolved_end


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


def resolve_list_tags(tags: list[str], tags_query: str | None) -> tuple[str, list[str]]:
    """Like ``resolve_tag_filter`` but allows an empty selection (lists all SLOs).

    The read/list path treats tag selection as an optional filter rather than a
    requirement, so with neither ``--tag`` nor ``--tags-query`` it returns an
    empty server query meaning "every SLO".

    Args:
        tags: Tags from repeated ``--tag``.
        tags_query: Raw single-tag query, used as-is when given.

    Returns:
        A ``(server_query, required_tags)`` pair; an empty query means "all SLOs".
    """
    if tags_query:
        return tags_query, []
    if tags:
        return tags[0], tags
    return '', []


def list_slos(session: niquests.Session, base: str, tags_query: str) -> list[dict]:
    """Return all SLOs matching the single-tag server query, following pagination.

    An empty ``tags_query`` omits the server-side filter, returning every SLO.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        tags_query: Single-tag Datadog query, or empty for all SLOs.

    Returns:
        The matching SLO objects.
    """
    slos: list[dict] = []
    offset = 0
    while True:
        params = {'limit': str(PAGE_SIZE), 'offset': str(offset)}
        if tags_query:
            params['tags_query'] = tags_query
        resp = session.get(
            f'{base}/api/v1/slo',
            params=params,
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


def format_instant(epoch: int, tz: ZoneInfo) -> str:
    """Format an epoch-seconds instant as ``YYYY-MM-DD HH:MM`` in ``tz``.

    Args:
        epoch: The instant in epoch seconds.
        tz: Timezone the instant is rendered in.

    Returns:
        The formatted wall-clock string.
    """
    return datetime.fromtimestamp(epoch, tz).strftime('%Y-%m-%d %H:%M')


def _iso_to_epoch(value: str) -> int:
    """Convert a Datadog ISO 8601 datetime to epoch seconds (naive treated as UTC).

    Args:
        value: An ISO 8601 datetime string, e.g. ``2026-07-09T10:00:00+00:00``.

    Returns:
        The instant as epoch seconds.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo('UTC'))
    return int(dt.timestamp())


def _interval_overlaps(start: int, end: int | None, window_start: int, window_end: int) -> bool:
    """Return True if ``[start, end)`` intersects ``[window_start, window_end)`` (None end = +inf).

    Args:
        start: Interval start (epoch seconds).
        end: Interval end (epoch seconds) or None for open-ended.
        window_start: Window start (epoch seconds).
        window_end: Window end (epoch seconds).

    Returns:
        Whether the two half-open intervals overlap.
    """
    real_end = float('inf') if end is None else end
    return start < window_end and window_start < real_end


def slo_monitor_ids(slo: dict) -> list[int]:
    """Return the monitor IDs backing an SLO (empty for metric/time_slice SLOs).

    Only ``type: monitor`` SLOs link to monitors via ``monitor_ids``; metric and
    time_slice SLOs have no monitor linkage and thus no monitor downtime.

    Args:
        slo: An SLO object from ``list_slos``.

    Returns:
        The integer monitor IDs, or an empty list when the SLO has none.
    """
    return [int(mid) for mid in (slo.get('monitor_ids') or []) if isinstance(mid, int)]


def list_downtimes(session: niquests.Session, base: str) -> list[dict]:
    """Return all downtimes (Downtimes v2), following pagination.

    The v2 endpoint has no server-side monitor/tag filter, so every downtime is
    fetched and matched to SLOs client-side.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.

    Returns:
        The downtime objects (each a dict carrying an ``attributes`` payload).
    """
    downtimes: list[dict] = []
    offset = 0
    while True:
        resp = session.get(
            f'{base}/api/v2/downtime',
            params={'page[limit]': str(PAGE_SIZE), 'page[offset]': str(offset)},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json().get('data', [])
        downtimes.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return downtimes


def downtime_monitor_id(attrs: dict) -> int | None:
    """Return the single monitor ID a downtime targets, or None if tag/scope-based.

    Args:
        attrs: A downtime's attributes.

    Returns:
        The targeted ``monitor_id``, or None when the downtime targets by
        ``monitor_tags``/scope (which names no specific monitor).
    """
    identifier = attrs.get('monitor_identifier') or {}
    monitor_id = identifier.get('monitor_id')
    return monitor_id if isinstance(monitor_id, int) else None


def index_downtimes_by_monitor(downtimes: list[dict]) -> tuple[dict[int, list[dict]], int]:
    """Group downtimes by the monitor ID they target.

    Downtimes that target monitors by tag/scope rather than a specific
    ``monitor_id`` cannot be linked to a monitor from the list response alone, so
    they are counted (for a caveat message) rather than indexed.

    Args:
        downtimes: Downtime objects from ``list_downtimes``.

    Returns:
        A ``(monitor_id -> [downtime attributes], tag_scoped_count)`` pair.
    """
    index: dict[int, list[dict]] = {}
    tag_scoped = 0
    for downtime in downtimes:
        attrs = downtime.get('attributes', {})
        monitor_id = downtime_monitor_id(attrs)
        if monitor_id is None:
            tag_scoped += 1
            continue
        index.setdefault(monitor_id, []).append(attrs)
    return index, tag_scoped


def downtime_window(attrs: dict) -> tuple[int, int | None] | None:
    """Return a downtime's concrete ``(start, end)`` window in epoch seconds.

    One-time downtimes use ``schedule.start``/``end``; recurring downtimes use
    ``schedule.current_downtime`` (the current or next occurrence). ``end`` is
    None for an open-ended downtime; None is returned when no start resolves.

    Args:
        attrs: A downtime's attributes.

    Returns:
        A ``(start, end)`` epoch pair, or None if the window is undetermined.
    """
    schedule = attrs.get('schedule') or {}
    if schedule.get('recurrences'):
        occurrence = schedule.get('current_downtime') or {}
        start_raw, end_raw = occurrence.get('start'), occurrence.get('end')
    else:
        start_raw, end_raw = schedule.get('start'), schedule.get('end')
    if not start_raw:
        return None
    return _iso_to_epoch(start_raw), (_iso_to_epoch(end_raw) if end_raw else None)


def corrections_attrs(corrections: list[dict]) -> list[dict]:
    """Return just the ``attributes`` payloads of correction objects.

    Args:
        corrections: Correction objects as returned by ``get_corrections``.

    Returns:
        The ``attributes`` dict of each correction.
    """
    return [c.get('attributes', {}) for c in corrections]


def _covered_by_correction(start: int, end: int | None, corrections: list[dict]) -> bool:
    """Return True if a non-recurring correction fully covers ``[start, end)``.

    A downtime is "overridden" when a correction's window entirely contains it,
    so the downtime is already excluded from the SLO's error budget. Recurring
    corrections (with an ``rrule``) are skipped: their coverage can't be reasoned
    about from a single interval.

    Args:
        start: Downtime start (epoch seconds).
        end: Downtime end (epoch seconds) or None for open-ended.
        corrections: The SLO's correction attribute dicts.

    Returns:
        Whether some correction fully covers the downtime window.
    """
    downtime_end = float('inf') if end is None else end
    for attrs in corrections:
        correction_start = attrs.get('start')
        if attrs.get('rrule') or correction_start is None:
            continue
        correction_end = float('inf') if attrs.get('end') is None else attrs['end']
        if correction_start <= start and downtime_end <= correction_end:
            return True
    return False


def slo_downtime_attrs(slo: dict, index: dict[int, list[dict]]) -> list[dict]:
    """Return the attribute dicts of downtimes targeting any of the SLO's monitors.

    Args:
        slo: An SLO object from ``list_slos``.
        index: The ``monitor_id -> [downtime attributes]`` index.

    Returns:
        The downtime attribute dicts for every monitor backing the SLO.
    """
    collected: list[dict] = []
    for monitor_id in slo_monitor_ids(slo):
        collected.extend(index.get(monitor_id, []))
    return collected


def uncovered_downtimes(
    downtime_attrs: list[dict],
    corrections: list[dict],
    window_start: int,
    window_end: int,
) -> list[tuple[dict, int, int | None]]:
    """Return the in-window downtimes not overridden by a correction.

    Each downtime is resolved to a concrete window, kept only if it intersects
    ``[window_start, window_end)``, and dropped when a correction fully covers it.

    Args:
        downtime_attrs: Attribute dicts of downtimes targeting the SLO's monitors.
        corrections: The SLO's correction attribute dicts.
        window_start: Window start (epoch seconds).
        window_end: Window end (epoch seconds).

    Returns:
        ``(attrs, start, end)`` triples for each surviving downtime.
    """
    kept: list[tuple[dict, int, int | None]] = []
    for attrs in downtime_attrs:
        window = downtime_window(attrs)
        if window is None:
            continue
        start, end = window
        if not _interval_overlaps(start, end, window_start, window_end):
            continue
        if _covered_by_correction(start, end, corrections):
            continue
        kept.append((attrs, start, end))
    return kept


def format_downtime(attrs: dict, start: int, end: int | None, tz: ZoneInfo) -> str:
    """Render one downtime as a ``window  (target)  "message"`` line.

    Args:
        attrs: The downtime's attributes.
        start: The downtime start (epoch seconds).
        end: The downtime end (epoch seconds) or None for open-ended.
        tz: Timezone used to render the instants.

    Returns:
        The formatted line (without a leading bullet).
    """
    window = f'{format_instant(start, tz)} → {"open-ended" if end is None else format_instant(end, tz)}'
    monitor_id = downtime_monitor_id(attrs)
    line = f'{window}  (monitor {monitor_id})'
    message = attrs.get('message') or ''
    if message:
        line += f'  "{message}"'
    return line


def print_downtime_header(
    label: str,
    matched: int,
    window_start: int,
    window_end: int,
    tz: ZoneInfo,
    tz_name: str,
) -> None:
    """Print the listing header: the tag label, the window, and the match count.

    Args:
        label: The tag selection shown to the user.
        matched: Number of matched SLOs.
        window_start: Window start (epoch seconds).
        window_end: Window end (epoch seconds).
        tz: Timezone the window is rendered in.
        tz_name: The timezone's name, shown alongside the window.
    """
    window = f'{format_instant(window_start, tz)} → {format_instant(window_end, tz)} {tz_name}'
    typer.echo(f'\nTags query : {label}')
    typer.echo(f'Window     : {window}')
    typer.echo(f'Matched    : {matched} SLO(s)')


def print_slo_downtime(slo: dict, entries: list[tuple[dict, int, int | None]], tz: ZoneInfo) -> None:
    """Print one SLO and its uncovered downtime (or an explanatory note).

    Args:
        slo: The SLO object.
        entries: ``(attrs, start, end)`` downtime triples not overridden.
        tz: Timezone used to render each downtime.
    """
    typer.echo(f'\n{slo.get("name", "<unnamed>")}  ({slo.get("id", "")})')
    monitor_ids = slo_monitor_ids(slo)
    if not monitor_ids:
        typer.echo(f'  (no monitor-based downtime; SLO type: {slo.get("type", "?")})')
        return
    typer.echo(f'  monitors: {", ".join(str(m) for m in monitor_ids)}')
    if not entries:
        typer.echo('  (no uncovered downtime in window)')
        return
    for attrs, start, end in entries:
        typer.echo(f'  • {format_downtime(attrs, start, end, tz)}')


def list_downtime(
    session: niquests.Session,
    base: str,
    tags_query: str,
    required_tags: list[str],
    window_start: int,
    window_end: int,
    tz: ZoneInfo,
    tz_name: str,
) -> int:
    """List SLOs matching the tags and their downtime not covered by an override.

    For each matched SLO, its monitors' downtimes (from the Downtimes v2 API) are
    intersected with the window, and any downtime fully covered by an SLO
    correction (override) is excluded, leaving only the uncovered downtime.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        tags_query: Single-tag server query, or empty for all SLOs.
        required_tags: Tags ANDed client-side (empty for a raw or empty query).
        window_start: Window start (epoch seconds).
        window_end: Window end (epoch seconds).
        tz: Timezone used to render instants.
        tz_name: The timezone's name, shown in the header.

    Returns:
        A process exit code (always 0).
    """
    slos = filter_by_tags(list_slos(session, base, tags_query), required_tags)
    label = ' AND '.join(required_tags) if required_tags else (tags_query or '(all SLOs)')
    print_downtime_header(label, len(slos), window_start, window_end, tz, tz_name)
    if not slos:
        typer.echo('\nNothing to list.')
        return 0
    index, tag_scoped = index_downtimes_by_monitor(list_downtimes(session, base))
    for slo in slos:
        corrections = corrections_attrs(get_corrections(session, base, slo.get('id', '')))
        entries = uncovered_downtimes(slo_downtime_attrs(slo, index), corrections, window_start, window_end)
        print_slo_downtime(slo, entries, tz)
    if tag_scoped:
        typer.echo(
            f'\nNote: {tag_scoped} tag/scope-targeted downtime(s) name no specific monitor '
            'and were not matched to any SLO.',
            err=True,
        )
    return 0


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


def get_corrections(session: niquests.Session, base: str, slo_id: str) -> list[dict]:
    """Return all corrections currently on an SLO.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        slo_id: The SLO whose corrections to fetch.

    Returns:
        The correction objects (each a dict carrying an ``attributes`` payload).
    """
    resp = session.get(f'{base}/api/v1/slo/{slo_id}/corrections', timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get('data', [])


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
    for correction in get_corrections(session, base, slo_id):
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
        '# Fetch from Vault (needs a valid token; run `vault login` first):\n'
        'export DD_API_KEY="$(vault kv get -field=value secret/audit/datadog-api-key)"\n'
        'export DD_APP_KEY="$(vault kv get -field=value secret/audit/datadog-application-key)"\n'
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
    tz = resolve_timezone(timezone)

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


@app.command('set', no_args_is_help=True, cls=ChoiceHintCommand)
def set_command(
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


@app.command('list')
def list_command(
    tag: list[str] = typer.Option(
        None,
        '--tag',
        metavar='KEY:VALUE',
        help='Tag to match; repeat to require several (ANDed). One is sent to Datadog, '
        'the rest filtered client-side. Omit --tag and --tags-query to list every SLO.',
    ),
    tags_query: str = typer.Option(
        None,
        '--tags-query',
        help='Raw single-tag Datadog tags_query, used as-is instead of --tag.',
    ),
    start: str = typer.Option(
        None,
        help='Window start: ISO 8601 (2026-07-01T00:00) or epoch. Default: start of the current month.',
    ),
    end: str = typer.Option(
        None,
        help='Window end: ISO 8601 or epoch. Default: now.',
    ),
    timezone: str = typer.Option(
        None,
        help=f'IANA timezone for the window (config/default: {DEFAULT_TIMEZONE}).',
    ),
    site: str = typer.Option(None, help=f'Datadog site (config/default: {DEFAULT_SITE}).'),
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
) -> None:
    """List SLOs matching the tags and their monitor downtime in a time window.

    Shows each SLO's Datadog downtimes (from the Downtimes API) that fall in the
    window, excluding any downtime already covered by an SLO correction (override).
    The window defaults to the start of the current month through now; override it
    with --start/--end. Tag selection is optional: with neither --tag nor
    --tags-query, every SLO is listed.
    """
    settings = load_config(config)
    resolved_api_key, resolved_app_key = resolve_credentials(api_key, app_key, config.parent)
    if not resolved_api_key or not resolved_app_key:
        sys.exit('error: provide --api-key/--app-key or set DD_API_KEY / DD_APP_KEY')
    tz_name = timezone or settings.get('timezone') or DEFAULT_TIMEZONE
    tz = resolve_timezone(tz_name)
    server_query, required_tags = resolve_list_tags(list(tag or []), tags_query)
    window_start, window_end = resolve_window(start, end, tz)
    session = build_session(resolved_api_key, resolved_app_key)
    base = f'https://api.{site or settings.get("site") or DEFAULT_SITE}'
    code = list_downtime(session, base, server_query, required_tags, window_start, window_end, tz, tz_name)
    raise typer.Exit(code)


def command_tree_lines(command: object, prefix: str = '') -> list[str]:
    """Return the tree lines for a Click command's children (empty for a leaf).

    Args:
        command: A Click command or group whose children to render.
        prefix: The indentation/branch prefix carried from parent levels.

    Returns:
        One string per descendant command, with ├──/└── branch connectors.
    """
    subcommands = getattr(command, 'commands', {})
    lines: list[str] = []
    names = sorted(subcommands)
    for index, name in enumerate(names):
        last = index == len(names) - 1
        connector = '└── ' if last else '├── '
        lines.append(f'{prefix}{connector}{name}')
        extension = '    ' if last else '│   '
        lines.extend(command_tree_lines(subcommands[name], prefix + extension))
    return lines


def command_tree(root_name: str) -> str:
    """Return a text tree of every command this CLI exposes, rooted at ``root_name``.

    Args:
        root_name: The label shown at the root of the tree (the program name).

    Returns:
        The rendered tree, one command per line.
    """
    return '\n'.join([root_name, *command_tree_lines(get_command(app))])


commands_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help='Introspect the CLI itself.',
)
app.add_typer(commands_app, name='commands')


@commands_app.command('list')
def commands_list() -> None:
    """Print a tree of every command this CLI provides."""
    typer.echo(command_tree(APP_NAME))


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
    typer.echo(f'Next: review it and adjust the credential source if needed, then run: direnv allow {directory}')


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == '__main__':
    main()
