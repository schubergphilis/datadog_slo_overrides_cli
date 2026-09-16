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
from datetime import UTC, datetime
from enum import Enum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import niquests
import typer
import typer.core
from dateutil.rrule import rrulebase, rrulestr
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
# /api/v1/slo/{id}/corrections pages differently from the rest of the v1 API: it
# ignores `limit`/`offset` entirely (silently serving the 10 most recent
# corrections), takes JSON:API-style `page[limit]`/`page[offset]` instead, and
# caps a page at 25 however large a limit is asked for. Paging it with the
# wrong parameter names is invisible — the short page reads as "last page" —
# so an SLO with more corrections than a page silently loses the rest, and the
# report then charges already-excused downtime against it.
CORRECTIONS_PAGE_SIZE = 25

# Statuses worth turning into advice rather than a bare code.
HTTP_AUTH_FAILURES = frozenset({401, 403})
HTTP_RATE_LIMITED = 429

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

# Uptime is quoted to three decimals: at a month-long window one decimal cannot
# distinguish 99.95% from 99.99%, which is the whole span of a typical SLO target.
UPTIME_DECIMALS = 3
# The highest figure the report will print short of a clean 100%. A single
# second lost in a month rounds to 100.000% at three decimals, which would claim
# perfection directly above a listed outage; such a window reads as 99.999%
# instead, and only genuinely zero downtime prints as 100%.
FULL_UPTIME = 100.0
ALMOST_FULL_UPTIME = FULL_UPTIME - 10**-UPTIME_DECIMALS

# A half-open ``[start, end)`` interval in epoch seconds. Every interval this
# module works on is concrete: open-ended inputs are clipped to the report window
# before they reach the interval algebra, so there is no ``None``/+inf case there.
Interval = tuple[int, int]

# What the report header claims, kept honest about what the data supports. A
# monitor-based SLO is treated as impaired whenever any one of its monitors is
# alerting, so the per-monitor alert periods are unioned rather than
# intersected. Datadog aggregates a multi-monitor SLO's own SLI differently, so
# the rule says out loud that the two can disagree rather than implying this
# figure is what the SLO's status page shows.
DOWNTIME_RULE = (
    'Datadog monitor alert periods (union: any backing monitor alerting), minus SLO corrections'
    " — for a multi-monitor SLO this need not equal Datadog's own SLI"
)

# Monitor state changes are read from the v1 events stream, whose ``alert``
# source is what monitor transitions are published under.
EVENT_SOURCE = 'alert'
# The events endpoint groups related events by default and exposes the rest as
# children, which would hide a recovery behind its trigger; this asks for them
# individually instead.
EVENT_UNAGGREGATED = 'true'
# The events endpoint caps how many events one response carries and truncates
# rather than paginating, so a long window is requested in day-sized chunks
# instead of one call.
EVENT_CHUNK_SECONDS = SECONDS_PER_DAY
# A response carrying at least this many events is assumed to have been
# truncated, and the chunk is halved and retried. Set below the documented v1
# cap of 1000 so a cap that is actually lower is still caught.
EVENT_PAGE_CAP = 900
# The smallest chunk worth bisecting to. A slice this short that still looks
# truncated is reported rather than split further.
EVENT_MIN_CHUNK_SECONDS = 60
# How far before the window to look for the transition that established a
# monitor's state at ``window_start``. An outage that opened earlier still shows
# up without it — see ``transitions_to_intervals``, which reads a recovery with
# no preceding trigger as "was already down" — but the lookback is what lets the
# outage's real start be used instead of the window's.
EVENT_LOOKBACK_SECONDS = 7 * SECONDS_PER_DAY
# Parsed instants further than this outside the fetched range are treated as
# unusable rather than trusted. Guards against a field holding milliseconds,
# which would otherwise land in the year 57000 and be silently clipped away.
EVENT_TIME_SLACK_SECONDS = SECONDS_PER_DAY

# Monitor states meaning "currently failing", used to catch a monitor that has
# been red for longer than the event lookback and so emitted no transition.
ALERTING_MONITOR_STATES = frozenset({'alert', 'no data'})

# Event fields naming the monitor a transition belongs to. Monitor events have
# carried ``monitor_id`` at the top level for a long time, but the alert payload
# has grown nested shapes over the years, so the lookup tolerates several.
MONITOR_ID_KEYS = ('monitor_id', 'monitorId', 'id')
# Fields carrying the instant a transition happened, in epoch seconds.
EVENT_TIME_KEYS = ('date_happened', 'dateHappened', 'timestamp')
# Fields naming the transition itself. ``alert_transition`` is the precise one
# ("Triggered", "Recovered", ...); ``alert_type`` is the coarser severity that
# every alert event carries and is used as the fallback.
EVENT_TRANSITION_KEYS = ('alert_transition', 'alertTransition')
EVENT_TYPE_KEYS = ('alert_type', 'alertType')
# Fields naming the monitor group a transition belongs to. A grouped monitor
# reports each group's transitions separately, and collapsing them into one
# stream loses the tail of an overlapping outage.
EVENT_GROUP_KEYS = ('monitor_groups', 'monitorGroups', 'group', 'host')
# Field holding the sub-events of an aggregated event, walked in case the
# unaggregated request is honoured differently than expected.
EVENT_CHILDREN_KEYS = ('children', 'child_events')

# Transitions that put a monitor into a failing state, lowercased. "Re-Triggered"
# repeats an already-open outage and is treated as opening one, so a missed
# Triggered event cannot swallow the whole outage.
FAILURE_TRANSITIONS = frozenset(
    {'triggered', 're-triggered', 'renotify', 'escalated', 'no data', 're-no data'},
)
# Transitions that end a failing state. Missing a recovery spelling is the
# dangerous direction — the outage would then run to the end of the window — so
# the known variants are all listed.
RECOVERY_TRANSITIONS = frozenset(
    {'recovered', 'recovery', 'resolved', 'no data recovered', 'recovered from no data'},
)
# ``alert_type`` values meaning "failing", used when no explicit transition is
# present. ``warning`` is deliberately absent: a monitor in WARN is not a breach
# of a monitor-based SLO, and counting it would over-report downtime.
FAILURE_ALERT_TYPES = frozenset({'error'})
# ``alert_type`` values meaning "no longer failing".
RECOVERY_ALERT_TYPES = frozenset({'success', 'recovery'})


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


@dataclass(frozen=True)
class Transition:
    """One monitor state change: when it happened and whether it started an outage."""

    at: int
    failure: bool


@dataclass
class EventDrops:
    """How many events the parser could not use, by reason.

    Every field is a way the report can be wrong without looking wrong, so they
    are counted and surfaced rather than discarded.
    """

    usable: int = 0
    no_monitor: int = 0
    no_timestamp: int = 0
    unknown_transition: int = 0
    out_of_range: int = 0

    @property
    def dropped(self) -> int:
        """Return the total number of events that yielded no transition."""
        return self.no_monitor + self.no_timestamp + self.unknown_transition + self.out_of_range


@dataclass(frozen=True)
class EventDiagnostics:
    """What the event stream yielded, so an empty report can be told from a clean one.

    The API response shape is the one thing this report cannot verify for
    itself: if the fields are named differently than expected, every SLO reads
    as perfectly available. These counters are what make that visible.
    """

    fetched: int
    drops: EventDrops
    saturated_slices: int
    monitors_wanted: int
    monitors_seen: int
    monitors_silent: int


@dataclass(frozen=True)
class DowntimeRow:
    """One SLO with its observed and remaining downtime.

    ``raw`` is what the monitors reported and ``net`` is ``raw`` minus the
    windows the corrections excuse — the downtime that still counts against the
    SLO.
    """

    slo: dict
    raw: list[Interval]
    net: list[Interval]


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

    An end in the future is pulled back to now: the window is the denominator of
    every uptime percentage, so counting hours that have not happened yet as
    available would silently inflate the figure. A window whose end does not
    follow its start is an operator error rather than an empty report, because
    the uptime of a zero-length window reads as a clean 100%.

    Args:
        start: Window start (ISO 8601 or epoch) or None.
        end: Window end (ISO 8601 or epoch) or None.
        tz: Timezone applied to naive datetimes and to the defaults.

    Returns:
        A ``(start, end)`` pair as epoch seconds.
    """
    default_start, now = default_window(tz)
    resolved_start = to_epoch(start, tz) if start else default_start
    resolved_end = min(to_epoch(end, tz) if end else now, now)
    if resolved_start >= resolved_end:
        sys.exit(
            f'error: the window start ({format_instant(resolved_start, tz)}) must fall before '
            f'its end ({format_instant(resolved_end, tz)})',
        )
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


def get_json(session: niquests.Session, url: str, params: dict[str, str] | None = None) -> dict:
    """GET a Datadog endpoint and return its decoded JSON body.

    The read paths issue a request per SLO and per event slice, so a report over
    a large account makes hundreds of calls and a transport error or a 429 part
    way through is an expected outcome rather than a surprise. Those exit with a
    message naming the endpoint instead of a traceback; ``raise_for_status`` is
    avoided because its message embeds the full URL and query string.

    Args:
        session: Authenticated Datadog session.
        url: The full endpoint URL.
        params: Query parameters, if any.

    Returns:
        The decoded JSON body, or an empty dict when the body is not an object.
    """
    endpoint = url.partition('/api/')[2] or url
    try:
        resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    except niquests.RequestException as exc:
        sys.exit(f'error: could not reach Datadog /api/{endpoint}: {type(exc).__name__}')
    if not resp.ok:
        hint = ''
        if resp.status_code in HTTP_AUTH_FAILURES:
            hint = f'; check {API_KEY_ENV} / {APP_KEY_ENV} are valid and the app key has read scope'
        elif resp.status_code == HTTP_RATE_LIMITED:
            hint = '; the account is rate limited, retry with a shorter window'
        sys.exit(f'error: Datadog /api/{endpoint} returned HTTP {resp.status_code}{hint}')
    try:
        body = resp.json()
    except ValueError:
        sys.exit(f'error: Datadog /api/{endpoint} did not return JSON')
    return body if isinstance(body, dict) else {}


def slo_name(slo: dict) -> str:
    """Return an SLO's display name, falling back to a placeholder.

    ``dict.get`` with a default only covers an absent key, and the API can
    return an explicit null, which would otherwise print as ``None``.

    Args:
        slo: An SLO object from ``list_slos``.

    Returns:
        The name, or ``<unnamed>`` when it is missing or blank.
    """
    return (slo.get('name') or '').strip() or '<unnamed>'


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
        page = get_json(session, f'{base}/api/v1/slo', params).get('data') or []
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
    rows = [(s.get('id', ''), slo_name(s), ','.join(s.get('tags', []))) for s in slos]
    id_w = max(len('SLO ID'), *(len(r[0]) for r in rows))
    name_w = max(len('NAME'), *(len(r[1]) for r in rows))
    typer.echo(f'{"SLO ID".ljust(id_w)}  {"NAME".ljust(name_w)}  TAGS')
    typer.echo(f'{"-" * id_w}  {"-" * name_w}  {"-" * 4}')
    for slo_id, name, tags in rows:
        typer.echo(f'{slo_id.ljust(id_w)}  {name.ljust(name_w)}  {tags}')


def format_instant(epoch: int, tz: ZoneInfo) -> str:
    """Format an epoch-seconds instant as ``YYYY-MM-DD HH:MM:SS`` in ``tz``.

    Seconds are shown because monitor transitions land on arbitrary seconds, and
    a truncated ``17:51`` would make a 58-second outage read as a whole minute.

    Args:
        epoch: The instant in epoch seconds.
        tz: Timezone the instant is rendered in.

    Returns:
        The formatted wall-clock string.
    """
    return datetime.fromtimestamp(epoch, tz).strftime('%Y-%m-%d %H:%M:%S')


def format_duration(seconds: int) -> str:
    """Format a duration in seconds as a compact ``2d 3h 4m 5s`` string.

    Args:
        seconds: The duration in seconds.

    Returns:
        The formatted duration, or ``0s`` for a non-positive input.
    """
    if seconds <= 0:
        return '0s'
    remaining = seconds
    parts: list[str] = []
    for label, size in (('d', SECONDS_PER_DAY), ('h', SECONDS_PER_HOUR), ('m', SECONDS_PER_MINUTE)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f'{count}{label}')
    if remaining:
        parts.append(f'{remaining}s')
    return ' '.join(parts)


def uptime_percentage(downtime_seconds: int, window_seconds: int) -> float:
    """Return uptime as a percentage of the report window.

    The window is the denominator, so any time the monitors reported nothing
    counts as up: only an alerting period is downtime. A monitor created midway
    through the window therefore reads as available for the earlier part.

    Args:
        downtime_seconds: Downtime within the window.
        window_seconds: Length of the report window.

    Returns:
        The uptime percentage, 100.0 for an empty window.
    """
    if window_seconds <= 0:
        return 100.0
    up = max(0, window_seconds - max(0, downtime_seconds))
    return 100.0 * up / window_seconds


def total_seconds(intervals: list[Interval]) -> int:
    """Return the summed length of a list of intervals.

    Args:
        intervals: Non-overlapping ``(start, end)`` pairs.

    Returns:
        The total number of seconds covered.
    """
    return sum(end - start for start, end in intervals)


# --------------------------------------------------------------------------- #
# Interval algebra — pure, no I/O.
# --------------------------------------------------------------------------- #


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Return ``intervals`` sorted, with empty ones dropped and overlaps coalesced.

    Args:
        intervals: Possibly unsorted, overlapping ``(start, end)`` pairs.

    Returns:
        Sorted, non-overlapping, non-empty intervals.
    """
    ordered = sorted((start, end) for start, end in intervals if start < end)
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def clip_interval(start: int, end: int, window_start: int, window_end: int) -> Interval | None:
    """Return ``[start, end)`` trimmed to the window, or None if nothing remains.

    Args:
        start: Interval start (epoch seconds).
        end: Interval end (epoch seconds).
        window_start: Window start (epoch seconds).
        window_end: Window end (epoch seconds).

    Returns:
        The clipped interval, or None when the two do not overlap.
    """
    low = max(start, window_start)
    high = min(end, window_end)
    return (low, high) if low < high else None


def _subtract_from_one(start: int, end: int, blocked: list[Interval]) -> list[Interval]:
    """Return the parts of ``[start, end)`` left after removing ``blocked``.

    Args:
        start: Interval start (epoch seconds).
        end: Interval end (epoch seconds).
        blocked: Sorted, non-overlapping intervals to remove.

    Returns:
        The surviving pieces, in order (possibly more than one).
    """
    pieces: list[Interval] = []
    cursor = start
    for block_start, block_end in blocked:
        if block_end <= cursor:
            continue
        if block_start >= end:
            break
        if block_start > cursor:
            pieces.append((cursor, block_start))
        cursor = block_end
        if cursor >= end:
            return pieces
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def subtract_intervals(base: list[Interval], exclusions: list[Interval]) -> list[Interval]:
    """Return ``base`` with every part covered by ``exclusions`` removed.

    This is the core of the report: ``base`` is the downtime the monitors
    observed and ``exclusions`` are the windows the SLO corrections already
    excuse, so the result is the downtime that still counts. An exclusion landing
    in the middle of an interval splits it in two.

    Args:
        base: Intervals to subtract from (need not be sorted or disjoint).
        exclusions: Intervals to remove (need not be sorted or disjoint).

    Returns:
        Sorted, non-overlapping intervals covered by ``base`` but not ``exclusions``.
    """
    blocked = merge_intervals(exclusions)
    remaining: list[Interval] = []
    for start, end in merge_intervals(base):
        remaining.extend(_subtract_from_one(start, end, blocked))
    return remaining


# --------------------------------------------------------------------------- #
# Datadog corrections -> concrete exclusion intervals.
# --------------------------------------------------------------------------- #


def _occurrence_epoch(moment: datetime) -> int:
    """Return an rrule occurrence as epoch seconds, treating a naive value as UTC."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return int(aware.timestamp())


def _build_rule(rrule: str, start: int, tz: ZoneInfo) -> tuple[rrulebase | None, bool]:
    """Return a parsed dateutil rule for ``rrule`` anchored at ``start``, and its awareness.

    The anchor is built in the correction's own timezone so each occurrence keeps
    its wall-clock time of day. Anchoring in UTC instead would shift a nightly
    maintenance window by an hour on the far side of a DST change, charging an
    hour of excused maintenance against the SLO and excusing an unrelated hour.

    A recurrence carrying ``UNTIL`` must agree with the anchor on timezone
    awareness, and which one Datadog emits varies, so an aware anchor is tried
    first and a naive one second rather than guessing. The flag is returned
    because ``rrulebase.between`` compares its bounds against the generated
    occurrences, and mixing aware and naive datetimes there raises TypeError.

    Args:
        rrule: An iCal RRULE string.
        start: The correction's start (epoch seconds), used as ``DTSTART``.
        tz: The correction's timezone.

    Returns:
        A ``(rule, is_aware)`` pair; the rule is None when the string cannot be
        parsed either way.
    """
    anchor = datetime.fromtimestamp(start, tz)
    for dtstart in (anchor, anchor.replace(tzinfo=None)):
        try:
            return rrulestr(rrule, dtstart=dtstart), dtstart.tzinfo is not None
        except (ValueError, TypeError):
            continue
    return None, False


def _recurring_occurrences(
    start: int,
    duration: int,
    rrule: str,
    window: Interval,
    tz: ZoneInfo,
) -> list[Interval]:
    """Expand a recurring correction into its concrete occurrences inside the window.

    The search starts one duration before the window so an occurrence that began
    earlier but still runs into the window is not missed.

    Args:
        start: The correction's ``DTSTART`` (epoch seconds).
        duration: How long each occurrence lasts (seconds).
        rrule: The correction's iCal RRULE.
        window: The search bounds as ``(start, end)`` epoch seconds, already
            narrowed to the report window and the recurrence's own series end.
        tz: The correction's timezone, in which occurrences keep their time of day.

    Returns:
        The clipped occurrence intervals.
    """
    window_start, window_end = window
    rule, aware = _build_rule(rrule, start, tz)
    if rule is None:
        return []
    search_from = datetime.fromtimestamp(window_start - duration, tz)
    search_to = datetime.fromtimestamp(window_end, tz)
    if not aware:
        search_from = search_from.replace(tzinfo=None)
        search_to = search_to.replace(tzinfo=None)
    occurrences: list[Interval] = []
    for moment in rule.between(search_from, search_to, inc=True):
        occurrence_start = _occurrence_epoch(moment)
        clipped = clip_interval(occurrence_start, occurrence_start + duration, window_start, window_end)
        if clipped:
            occurrences.append(clipped)
    return occurrences


def _correction_timezone(attrs: dict) -> ZoneInfo:
    """Return the timezone a correction's recurrence is anchored in.

    Args:
        attrs: The correction's attributes.

    Returns:
        The correction's timezone, or UTC when it names none or an unknown one.
    """
    name = attrs.get('timezone')
    if not isinstance(name, str) or not name.strip():
        return ZoneInfo('UTC')
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo('UTC')


def _correction_occurrences(attrs: dict, window_start: int, window_end: int) -> list[Interval]:
    """Return one correction's exclusion intervals inside the window.

    Args:
        attrs: The correction's attributes.
        window_start: Report window start (epoch seconds).
        window_end: Report window end (epoch seconds).

    Returns:
        The clipped intervals this correction excuses.
    """
    start = attrs.get('start')
    if not isinstance(start, int):
        return []
    end = attrs.get('end')
    rrule = attrs.get('rrule') or None
    if rrule:
        duration = attrs.get('duration')
        if not isinstance(duration, int) or duration <= 0:
            # Deliberately no `end - start` fallback: on a recurring correction
            # `end` can bound the whole series rather than the first occurrence,
            # and inferring a months-long "occurrence" from it would excuse the
            # entire window. Skipping under-excuses, which is the safe direction.
            typer.echo(
                f'warning: skipping a recurring correction with no usable duration (rrule {rrule})',
                err=True,
            )
            return []
        # An `end` alongside an rrule bounds the series, so no occurrence may
        # start after it.
        series_end = min(end, window_end) if isinstance(end, int) else window_end
        return _recurring_occurrences(
            start,
            duration,
            str(rrule),
            (window_start, series_end),
            _correction_timezone(attrs),
        )
    # A one-off correction with no end is open-ended, so it excuses the rest of
    # the window.
    finish = end if isinstance(end, int) else window_end
    clipped = clip_interval(start, finish, window_start, window_end)
    return [clipped] if clipped else []


def correction_intervals(corrections: list[dict], window_start: int, window_end: int) -> list[Interval]:
    """Return the exclusion intervals of an SLO's corrections, clipped to the window.

    Args:
        corrections: Correction objects as returned by ``get_corrections``.
        window_start: Report window start (epoch seconds).
        window_end: Report window end (epoch seconds).

    Returns:
        Sorted, non-overlapping intervals excused by the corrections.
    """
    intervals: list[Interval] = []
    for correction in corrections:
        intervals.extend(_correction_occurrences(correction.get('attributes', {}), window_start, window_end))
    return merge_intervals(intervals)


# --------------------------------------------------------------------------- #
# Datadog monitor transitions -> observed downtime intervals.
# --------------------------------------------------------------------------- #


def slo_monitor_ids(slo: dict) -> list[int]:
    """Return the monitor IDs backing an SLO (empty for metric/time_slice SLOs).

    Only ``type: monitor`` SLOs link to monitors via ``monitor_ids``; metric and
    time_slice SLOs have no monitor linkage, so no monitor transition can be
    attributed to them.

    Args:
        slo: An SLO object from ``list_slos``.

    Returns:
        The integer monitor IDs, or an empty list when the SLO has none.
    """
    # `bool` is a subclass of `int`, so it needs excluding explicitly.
    return [mid for mid in (slo.get('monitor_ids') or []) if isinstance(mid, int) and not isinstance(mid, bool)]


def _first_int(record: dict, keys: tuple[str, ...]) -> int | None:
    """Return the first key in ``keys`` holding an int-like value, coerced to int.

    Args:
        record: The mapping to read.
        keys: Candidate key names, in order of preference.

    Returns:
        The coerced value, or None when no key holds one.
    """
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip('-').isdigit():
            return int(value.strip())
    return None


def _first_str(record: dict, keys: tuple[str, ...]) -> str | None:
    """Return the first key in ``keys`` holding a non-empty string.

    Only real strings are accepted: coercing an arbitrary value with ``str()``
    would turn a nested object into a nonsense transition name that then matches
    nothing, which is harder to notice than an outright miss.

    Args:
        record: The mapping to read.
        keys: Candidate key names, in order of preference.

    Returns:
        The stripped value, or None when no key holds one.
    """
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def event_monitor_id(event: dict) -> int | None:
    """Return the monitor ID an alert event belongs to, or None if it names none.

    The nested ``monitor`` object is preferred over the event's own ``id``, which
    on some payload shapes is the event ID rather than the monitor's.

    Args:
        event: One event from the events stream.

    Returns:
        The monitor ID, or None for an alert event not tied to a monitor.
    """
    monitor = event.get('monitor')
    if isinstance(monitor, dict):
        nested = _first_int(monitor, MONITOR_ID_KEYS)
        if nested is not None:
            return nested
    for key in MONITOR_ID_KEYS:
        if key == 'id':
            # Only trust a bare `id` when it is the monitor's, which the nested
            # object above already covers; a top-level `id` is the event's.
            continue
        value = _first_int(event, (key,))
        if value is not None:
            return value
    return None


def event_group(event: dict) -> str:
    """Return the monitor group a transition belongs to, or an empty string for none.

    A grouped monitor (one alerting per host, region, or other tag) reports each
    group's transitions independently, so the group is part of the state
    machine's identity. Collapsing every group of a monitor into one stream
    loses the tail of any outage that overlaps another group's — and it loses it
    in the direction that makes the SLO look healthier.

    Args:
        event: One event from the events stream.

    Returns:
        A stable group key, empty when the monitor is ungrouped.
    """
    for key in EVENT_GROUP_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            names = sorted(str(item).strip() for item in value if str(item).strip())
            if names:
                return ','.join(names)
    return ''


def classify_transition(event: dict) -> bool | None:
    """Return True if an event starts an outage, False if it ends one, None if neither.

    ``alert_transition`` is used when present because it distinguishes a recovery
    from an informational re-notification; ``alert_type`` is the fallback. An
    unrecognised value yields None so an unknown transition is ignored rather
    than guessed into an outage boundary.

    Args:
        event: One event from the events stream.

    Returns:
        True (failure), False (recovery), or None (not a state change).
    """
    transition = _first_str(event, EVENT_TRANSITION_KEYS)
    if transition:
        lowered = transition.lower()
        if lowered in FAILURE_TRANSITIONS:
            return True
        if lowered in RECOVERY_TRANSITIONS:
            return False
        return None
    alert_type = _first_str(event, EVENT_TYPE_KEYS)
    if alert_type:
        lowered = alert_type.lower()
        if lowered in FAILURE_ALERT_TYPES:
            return True
        if lowered in RECOVERY_ALERT_TYPES:
            return False
    return None


def flatten_events(events: list[dict]) -> list[dict]:
    """Return ``events`` with the children of any aggregated event pulled up.

    The fetch asks for unaggregated events, but if that is honoured differently
    than expected a trigger could be returned with its recovery buried in a
    ``children`` array — which would leave the outage looking like it never
    ended.

    Args:
        events: Events as returned by the API.

    Returns:
        The events plus any nested child events, parents first.
    """
    flattened: list[dict] = []
    for event in events:
        flattened.append(event)
        for key in EVENT_CHILDREN_KEYS:
            children = event.get(key)
            if isinstance(children, list):
                flattened.extend(child for child in children if isinstance(child, dict))
    return flattened


def parse_transitions(
    events: list[dict],
    fetched_window: Interval,
) -> tuple[dict[tuple[int, str], list[Transition]], EventDrops]:
    """Group monitor state changes by ``(monitor id, group)``, in chronological order.

    Ties are broken with failures before recoveries. The API returns events
    newest-first, so a monitor that triggered and recovered inside the same
    second would otherwise keep that arrival order, and the state machine would
    read the recovery first, discard it, then leave the outage open to the end
    of the window — a zero-second blip inflated into weeks.

    Args:
        events: Alert events from the events stream.
        fetched_window: The ``(start, end)`` range actually requested, used to
            reject instants that cannot be seconds (a millisecond field, say).

    Returns:
        A ``(mapping, drops)`` pair; each transition list is sorted by time.
    """
    fetch_start, fetch_end = fetched_window
    earliest = fetch_start - EVENT_TIME_SLACK_SECONDS
    latest = fetch_end + EVENT_TIME_SLACK_SECONDS
    per_key: dict[tuple[int, str], list[Transition]] = {}
    drops = EventDrops()
    for event in flatten_events(events):
        monitor_id = event_monitor_id(event)
        at = _first_int(event, EVENT_TIME_KEYS)
        failure = classify_transition(event)
        if monitor_id is None:
            drops.no_monitor += 1
            continue
        if at is None:
            drops.no_timestamp += 1
            continue
        if not earliest <= at <= latest:
            # Outside the range that was asked for, so the field is not epoch
            # seconds (milliseconds land in the year 57000). Trusting it would
            # silently drop the outage during clipping.
            drops.out_of_range += 1
            continue
        if failure is None:
            drops.unknown_transition += 1
            continue
        per_key.setdefault((monitor_id, event_group(event)), []).append(Transition(at=at, failure=failure))
        drops.usable += 1
    for transitions in per_key.values():
        transitions.sort(key=lambda t: (t.at, 0 if t.failure else 1))
    return per_key, drops


def transitions_to_intervals(
    transitions: list[Transition],
    window_start: int,
    window_end: int,
) -> list[Interval]:
    """Convert one monitor group's state changes into its alerting intervals.

    A failure transition opens an interval and the next recovery closes it;
    repeated failures while already open are absorbed rather than nesting. An
    outage still open at ``window_end`` is closed there. A recovery arriving
    with nothing open means the monitor was *already* failing before the fetched
    range began — its trigger predates even the lookback — so the outage is
    counted from the start of the window rather than discarded, which is what
    keeps a monitor that has been red for months from reporting 100% uptime.

    Args:
        transitions: The group's transitions, chronologically ordered.
        window_start: Report window start (epoch seconds).
        window_end: Report window end (epoch seconds).

    Returns:
        The clipped, non-overlapping alerting intervals.
    """
    intervals: list[Interval] = []
    opened_at: int | None = None
    for transition in _with_implied_opening(transitions, window_start):
        if transition.failure:
            opened_at = transition.at if opened_at is None else opened_at
            continue
        if opened_at is not None:
            _append_clipped(intervals, opened_at, transition.at, window_start, window_end)
            opened_at = None
    if opened_at is not None:
        # Still alerting when the window closed (or when the data ran out).
        _append_clipped(intervals, opened_at, window_end, window_start, window_end)
    return merge_intervals(intervals)


def _with_implied_opening(transitions: list[Transition], window_start: int) -> list[Transition]:
    """Return ``transitions`` with a synthetic trigger when the first one is a recovery.

    A recovery arriving before any trigger means the monitor was already failing
    when the fetched range began. Materialising that as an opening at
    ``window_start`` lets the state machine stay a plain trigger/recovery pairing
    instead of carrying a special case, and it is what stops a monitor that has
    been red for longer than the lookback from reporting full availability.

    Args:
        transitions: The group's transitions, chronologically ordered.
        window_start: Report window start (epoch seconds).

    Returns:
        The transitions, prefixed with an implied opening where one is needed.
    """
    if transitions and not transitions[0].failure:
        return [Transition(at=window_start, failure=True), *transitions]
    return transitions


def _append_clipped(
    intervals: list[Interval],
    start: int,
    end: int,
    window_start: int,
    window_end: int,
) -> None:
    """Append ``[start, end)`` to ``intervals``, trimmed to the window and dropped if empty.

    Args:
        intervals: The list to append to, modified in place.
        start: Interval start (epoch seconds).
        end: Interval end (epoch seconds).
        window_start: Window start (epoch seconds).
        window_end: Window end (epoch seconds).
    """
    clipped = clip_interval(start, end, window_start, window_end)
    if clipped:
        intervals.append(clipped)


def list_alert_events(session: niquests.Session, base: str, start: int, end: int) -> tuple[list[dict], int]:
    """Return every monitor alert event in ``[start, end)``, fetched in chunks.

    The events endpoint has no monitor filter and caps how many events a single
    response carries, truncating rather than paginating, so the range is walked
    in ``EVENT_CHUNK_SECONDS`` slices. A slice that comes back at the cap is
    halved and retried, because day-chunking alone is not a truncation guard on
    a busy account — and silently losing events reads as uptime.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        start: Fetch start (epoch seconds), typically before the report window.
        end: Fetch end (epoch seconds).

    Returns:
        An ``(events, saturated_slices)`` pair, where the count is of slices
        that still looked truncated at the smallest size worth splitting.
    """
    events: list[dict] = []
    saturated = 0
    # Slices still to fetch, newest last so the walk stays chronological.
    pending: list[Interval] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + EVENT_CHUNK_SECONDS, end)
        pending.append((cursor, chunk_end))
        cursor = chunk_end
    while pending:
        slice_start, slice_end = pending.pop(0)
        page = (
            get_json(
                session,
                f'{base}/api/v1/events',
                {
                    'start': str(slice_start),
                    'end': str(slice_end),
                    'sources': EVENT_SOURCE,
                    'unaggregated': EVENT_UNAGGREGATED,
                },
            ).get('events')
            or []
        )
        usable = [item for item in page if isinstance(item, dict)]
        if len(page) >= EVENT_PAGE_CAP and slice_end - slice_start > EVENT_MIN_CHUNK_SECONDS:
            midpoint = slice_start + (slice_end - slice_start) // 2
            pending[:0] = [(slice_start, midpoint), (midpoint, slice_end)]
            continue
        if len(page) >= EVENT_PAGE_CAP:
            saturated += 1
        events.extend(usable)
    return events, saturated


def fetch_monitor_intervals(
    session: niquests.Session,
    base: str,
    window: Interval,
    monitor_ids: set[int],
) -> tuple[dict[int, list[Interval]], EventDiagnostics]:
    """Return the alerting intervals of the monitors that back an SLO.

    Events are fetched from ``EVENT_LOOKBACK_SECONDS`` before the window so an
    outage already in progress at the window start is attributed to its real
    beginning, then the intervals are clipped to the window itself. Only
    monitors in ``monitor_ids`` are processed: the account-wide alert stream can
    dwarf the handful of monitors a report actually needs.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        window: The report window as ``(start, end)`` epoch seconds.
        monitor_ids: The monitors backing the matched SLOs.

    Returns:
        A ``(monitor_id -> [Interval], diagnostics)`` pair.
    """
    window_start, window_end = window
    fetch_start = window_start - EVENT_LOOKBACK_SECONDS
    events, saturated = list_alert_events(session, base, fetch_start, window_end)
    per_key, drops = parse_transitions(events, (fetch_start, window_end))
    per_monitor: dict[int, list[Interval]] = {}
    for (monitor_id, _group), transitions in per_key.items():
        if monitor_id not in monitor_ids:
            continue
        # One state machine per group, unioned per monitor: the monitor is
        # alerting while any of its groups is.
        per_monitor.setdefault(monitor_id, []).extend(
            transitions_to_intervals(transitions, window_start, window_end),
        )
    per_monitor = {monitor_id: merge_intervals(spans) for monitor_id, spans in per_monitor.items()}
    diagnostics = EventDiagnostics(
        fetched=len(events),
        drops=drops,
        saturated_slices=saturated,
        monitors_wanted=len(monitor_ids),
        monitors_seen=len([mid for mid, spans in per_monitor.items() if spans]),
        monitors_silent=len(monitor_ids - {mid for (mid, _group) in per_key}),
    )
    return per_monitor, diagnostics


def slo_observed_downtime(
    slo: dict,
    per_monitor: dict[int, list[Interval]],
) -> list[Interval]:
    """Return an SLO's observed downtime: the union of its monitors' alerting time.

    A monitor-based SLO with several monitors is treated as impaired whenever
    *any* of them is alerting, so the per-monitor intervals are unioned. (This is
    the opposite of a multi-location probe, where agreement across locations is
    what makes a failure real.) Datadog aggregates its own multi-monitor SLI
    differently, so this figure answers "was the service impaired" rather than
    reproducing the SLO's status page.

    Args:
        slo: An SLO object from ``list_slos``.
        per_monitor: A ``monitor_id -> [Interval]`` mapping.

    Returns:
        Sorted, non-overlapping observed downtime intervals.
    """
    collected: list[Interval] = []
    for monitor_id in slo_monitor_ids(slo):
        collected.extend(per_monitor.get(monitor_id, []))
    return merge_intervals(collected)


# --------------------------------------------------------------------------- #
# Reporting.
# --------------------------------------------------------------------------- #


def print_report_header(
    label: str,
    matched: int,
    hidden: int,
    window: Interval,
    tz: ZoneInfo,
    tz_name: str,
) -> None:
    """Print the net-downtime report header.

    Args:
        label: The tag selection shown to the user.
        matched: Number of matched SLOs.
        hidden: Number suppressed by ``--only-downtime``.
        window: The report window as ``(start, end)`` epoch seconds.
        tz: Timezone the window is rendered in.
        tz_name: The timezone's name, shown alongside the window.
    """
    window_start, window_end = window
    rendered = f'{format_instant(window_start, tz)} → {format_instant(window_end, tz)} {tz_name}'
    typer.echo(f'\nTags query : {label}')
    typer.echo(f'Window     : {rendered}')
    typer.echo(f'Rule       : {DOWNTIME_RULE}')
    typer.echo(f'Matched    : {matched} SLO(s)')
    if hidden:
        typer.echo(f'Hidden     : {hidden} SLO(s) with no net downtime (--only-downtime)')


def print_event_diagnostics(diagnostics: EventDiagnostics) -> None:
    """Print what the event stream yielded, and warn about anything that limits it.

    Args:
        diagnostics: The counters gathered while reading the events.
    """
    drops = diagnostics.drops
    typer.echo(f'Events     : {diagnostics.fetched} fetched, {drops.usable} usable transition(s)')
    if diagnostics.monitors_silent:
        typer.echo(
            f'Monitors   : {diagnostics.monitors_silent} of {diagnostics.monitors_wanted} '
            'backing monitor(s) had no transitions and are reported as fully up',
        )
    if drops.dropped:
        typer.echo(
            f'warning: ignored {drops.dropped} event(s) — '
            f'{drops.no_monitor} named no monitor, {drops.no_timestamp} had no timestamp, '
            f'{drops.unknown_transition} had an unrecognised transition, '
            f'{drops.out_of_range} had an out-of-range timestamp',
            err=True,
        )
    if diagnostics.saturated_slices:
        typer.echo(
            f'warning: {diagnostics.saturated_slices} time slice(s) returned a full page even at '
            f'{EVENT_MIN_CHUNK_SECONDS}s wide, so some events were almost certainly dropped by the '
            'API and this report understates downtime',
            err=True,
        )


def warn_if_no_transitions(diagnostics: EventDiagnostics) -> bool:
    """Warn when the event stream yielded nothing usable, and say whether it did.

    An empty result is indistinguishable from a flawless month in the report
    body, so it is called out explicitly and makes the command exit non-zero —
    a scheduled report that starts returning nothing should fail rather than
    quietly certify 100% uptime.

    Args:
        diagnostics: The counters gathered while reading the events.

    Returns:
        True when no usable transition was found.
    """
    if diagnostics.drops.usable:
        return False
    if diagnostics.fetched:
        typer.echo(
            f'error: {diagnostics.fetched} alert event(s) were fetched but none could be read as a '
            'monitor state change, so every SLO below reads as fully available. The event payload '
            'is probably shaped differently than expected — check one event against '
            'MONITOR_ID_KEYS / EVENT_TIME_KEYS / EVENT_TRANSITION_KEYS before trusting these figures.',
            err=True,
        )
    else:
        typer.echo(
            'error: no alert events were returned for this window, so every SLO below reads as '
            f'fully available. Verify that "{EVENT_SOURCE}" is the right event source and that the '
            'app key may read events before trusting these figures.',
            err=True,
        )
    return True


def format_net_summary(raw: list[Interval], net: list[Interval]) -> str:
    """Return the one-line subtraction accounting for an SLO.

    The excluded figure is ``raw - net`` rather than the corrections' own total,
    because a correction may extend well beyond the observed downtime and only
    the overlapping part actually excuses anything.

    Args:
        raw: Observed downtime intervals.
        net: Downtime left after subtraction.

    Returns:
        A ``raw X - excluded Y = net Z`` string.
    """
    raw_total = total_seconds(raw)
    net_total = total_seconds(net)
    return (
        f'raw {format_duration(raw_total)} - '
        f'excluded {format_duration(raw_total - net_total)} = '
        f'net {format_duration(net_total)}'
    )


def format_percentage(value: float) -> str:
    """Format an uptime percentage, without decimals when it is a clean 100%.

    Trailing zeros on ``100.000%`` carry no information, and a figure that only
    *rounds* to 100 is held back to ``99.999%`` so the number never contradicts
    an outage listed beneath it.

    Args:
        value: The percentage to render.

    Returns:
        ``100%`` for full availability, else the value to ``UPTIME_DECIMALS``.
    """
    if value >= FULL_UPTIME:
        return '100%'
    return f'{min(value, ALMOST_FULL_UPTIME):.{UPTIME_DECIMALS}f}%'


def format_uptime(raw: list[Interval], net: list[Interval], window_seconds: int) -> str:
    """Return the uptime line for an SLO, noting the pre-correction figure when it differs.

    Args:
        raw: Observed downtime intervals.
        net: Downtime left after subtraction.
        window_seconds: Length of the report window.

    Returns:
        An ``uptime 99.583%`` string, with the raw percentage appended when
        corrections changed it.
    """
    net_seconds = total_seconds(net)
    raw_seconds = total_seconds(raw)
    line = f'uptime {format_percentage(uptime_percentage(net_seconds, window_seconds))}'
    if raw_seconds != net_seconds:
        raw_pct = uptime_percentage(raw_seconds, window_seconds)
        line += f' ({format_percentage(raw_pct)} before corrections)'
    return line


def print_slo_downtime(row: DowntimeRow, window: Interval, tz: ZoneInfo) -> None:
    """Print one SLO, its monitors, and the downtime that still counts against it.

    Args:
        row: The resolved SLO downtime figures.
        window: The report window as ``(start, end)`` epoch seconds.
        tz: Timezone used to render the instants.
    """
    window_seconds = window[1] - window[0]
    typer.echo(f'\n{slo_name(row.slo)}  ({row.slo.get("id", "")})')
    monitor_ids = slo_monitor_ids(row.slo)
    if not monitor_ids:
        typer.echo(f'  (no monitor-based downtime; SLO type: {row.slo.get("type", "?")})')
        return
    typer.echo(f'  monitors: {", ".join(str(m) for m in monitor_ids)}')
    if row.net:
        typer.echo(f'  {format_net_summary(row.raw, row.net)}')
        typer.echo(f'  {format_uptime(row.raw, row.net, window_seconds)}')
        for start, end in row.net:
            span = f'{format_instant(start, tz)} → {format_instant(end, tz)}'
            typer.echo(f'  • {span}  ({format_duration(end - start)})')
    elif row.raw:
        # Distinguish "nothing went wrong" from "everything was excused".
        typer.echo(f'  {format_net_summary(row.raw, row.net)}')
        typer.echo(f'  {format_uptime(row.raw, row.net, window_seconds)}')
        typer.echo('  (all observed downtime excluded by corrections)')
    else:
        typer.echo(f'  {format_uptime(row.raw, row.net, window_seconds)}')
        typer.echo('  (no net downtime in window)')


def build_downtime_rows(
    session: niquests.Session,
    base: str,
    slos: list[dict],
    per_monitor: dict[int, list[Interval]],
    window: Interval,
) -> list[DowntimeRow]:
    """Resolve every SLO's observed and remaining downtime.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        slos: The matched SLOs, in report order.
        per_monitor: A ``monitor_id -> [Interval]`` mapping of alerting time.
        window: The report window as ``(start, end)`` epoch seconds.

    Returns:
        One row per SLO.
    """
    window_start, window_end = window
    rows: list[DowntimeRow] = []
    for slo in slos:
        raw = slo_observed_downtime(slo, per_monitor)
        corrections = get_corrections(session, base, slo.get('id', ''))
        excluded = correction_intervals(corrections, window_start, window_end)
        rows.append(DowntimeRow(slo=slo, raw=raw, net=subtract_intervals(raw, excluded)))
    return rows


def report_net_downtime(
    session: niquests.Session,
    base: str,
    tags_query: str,
    required_tags: list[str],
    window: Interval,
    tz: ZoneInfo,
    tz_name: str,
    *,
    only_downtime: bool = False,
) -> int:
    """Report each matched SLO's monitor downtime net of its Datadog corrections.

    Args:
        session: Authenticated Datadog session.
        base: API base URL.
        tags_query: Single-tag server query, or empty for all SLOs.
        required_tags: Tags ANDed client-side (empty for a raw or empty query).
        window: The report window as ``(start, end)`` epoch seconds.
        tz: Timezone used to render instants.
        tz_name: The timezone's name, shown in the header.
        only_downtime: Show only SLOs with net downtime left.

    Returns:
        A process exit code: 1 when no monitor state change could be read at
        all (making the figures meaningless), 0 otherwise.
    """
    slos = filter_by_tags(list_slos(session, base, tags_query), required_tags)
    label = ' AND '.join(required_tags) if required_tags else (tags_query or '(all SLOs)')
    if not slos:
        print_report_header(label, 0, 0, window, tz, tz_name)
        typer.echo('\nNothing to list.')
        return 0
    monitor_ids = {monitor_id for slo in slos for monitor_id in slo_monitor_ids(slo)}
    per_monitor, diagnostics = fetch_monitor_intervals(session, base, window, monitor_ids)
    rows = build_downtime_rows(session, base, slos, per_monitor, window)
    shown = [row for row in rows if row.net] if only_downtime else rows
    print_report_header(label, len(rows), len(rows) - len(shown), window, tz, tz_name)
    if monitor_ids:
        print_event_diagnostics(diagnostics)
    for row in shown:
        print_slo_downtime(row, window, tz)
    return 1 if monitor_ids and warn_if_no_transitions(diagnostics) else 0


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
    corrections: list[dict] = []
    offset = 0
    while True:
        page = (
            get_json(
                session,
                f'{base}/api/v1/slo/{slo_id}/corrections',
                {'page[limit]': str(CORRECTIONS_PAGE_SIZE), 'page[offset]': str(offset)},
            ).get('data')
            or []
        )
        corrections.extend(page)
        if len(page) < CORRECTIONS_PAGE_SIZE:
            break
        offset += CORRECTIONS_PAGE_SIZE
    return corrections


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
        name = slo_name(slo)
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
    names = {s.get('id', ''): slo_name(s) for s in slos}
    report_existing(existing, names, cfg.strategy)

    if cfg.correction is None:
        report_dry_run(len(slos), len(existing), window_given=cfg.start is not None)
        return 0

    typer.echo(f'\nApplying {cfg.correction.category} correction to {len(slos)} SLO(s)...\n')
    created, skipped, failed = apply_corrections(cfg.session, cfg.base, slos, existing, cfg.correction)
    typer.echo(f'\nDone. {created} created, {skipped} skipped, {failed} failed.')
    return 1 if failed else 0


app = typer.Typer(
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
    only_downtime: bool = typer.Option(
        False,
        '--only-downtime',
        help='List only SLOs with net downtime left; the header reports how many were hidden.',
    ),
) -> None:
    """Report monitor downtime net of the SLO corrections that excuse it.

    For each matched SLO, its monitors' alerting periods are reconstructed from
    their Datadog alert/recovery events, the windows of every correction on the
    SLO are subtracted, and what remains is the downtime that still counts —
    reported with its uptime percentage for the window. The window defaults to
    the start of the current month through now; override it with --start/--end.
    Tag selection is optional: with neither --tag nor --tags-query, every SLO is
    reported.
    """
    settings = load_config(config)
    resolved_api_key, resolved_app_key = resolve_credentials(api_key, app_key, config.parent)
    if not resolved_api_key or not resolved_app_key:
        sys.exit('error: provide --api-key/--app-key or set DD_API_KEY / DD_APP_KEY')
    tz_name = timezone or settings.get('timezone') or DEFAULT_TIMEZONE
    tz = resolve_timezone(tz_name)
    server_query, required_tags = resolve_list_tags(list(tag or []), tags_query)
    session = build_session(resolved_api_key, resolved_app_key)
    base = f'https://api.{site or settings.get("site") or DEFAULT_SITE}'
    code = report_net_downtime(
        session,
        base,
        server_query,
        required_tags,
        resolve_window(start, end, tz),
        tz,
        tz_name,
        only_downtime=only_downtime,
    )
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
