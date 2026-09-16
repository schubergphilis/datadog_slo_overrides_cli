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
"""datadog_slo_overrides_cli."""

__author__ = 'Yorick Hoorneman <yhoorneman@schubergphilis.com>'
__docformat__ = 'google'
__date__ = '05-06-2026'
__copyright__ = 'Copyright 2026, Yorick Hoorneman'
__credits__ = ['Yorick Hoorneman']
__license__ = 'Apache-2.0'
__maintainer__ = 'Yorick Hoorneman'
__email__ = '<yhoorneman@schubergphilis.com>'
__status__ = 'Development'

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import niquests
import pytest
from typer.testing import CliRunner

from datadog_slo_overrides_cli import datadog_slo_overrides_cli as cli
from datadog_slo_overrides_cli.datadog_slo_overrides_cli import (
    APP_NAME,
    CORRECTIONS_PAGE_SIZE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SKIP_IF_COVERED,
    SKIP_IF_EXACT,
    SKIP_IF_OVERLAP,
    Correction,
    DowntimeRow,
    Transition,
    _matches,
    app,
    build_config,
    classify_transition,
    command_tree,
    config_template,
    correction_intervals,
    default_window,
    event_monitor_id,
    filter_by_tags,
    format_duration,
    format_instant,
    format_net_summary,
    format_percentage,
    format_uptime,
    get_json,
    list_alert_events,
    load_config,
    load_direnv_env,
    merge_intervals,
    parse_transitions,
    print_slo_downtime,
    report_net_downtime,
    resolve_credentials,
    resolve_list_tags,
    resolve_tag_filter,
    resolve_window,
    slo_monitor_ids,
    slo_name,
    slo_observed_downtime,
    subtract_intervals,
    to_epoch,
    total_seconds,
    transitions_to_intervals,
    uptime_percentage,
)

runner = CliRunner()

DST_OFFSET_SECONDS = 7200
# What the corrections endpoint serves when no `page[limit]` is given.
_CORRECTIONS_DEFAULT_PAGE = 10


def _completed(stdout: str = '', stderr: str = '', returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build a fake CompletedProcess for mocking ``direnv export json``."""
    return subprocess.CompletedProcess(
        args=['direnv', 'export', 'json'], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_sanity() -> None:
    """Sanity check."""
    assert True


def test_to_epoch_passthrough_and_timezone() -> None:
    """Epoch strings pass through; naive datetimes are read in the given timezone."""
    assert to_epoch('1777034190', ZoneInfo('UTC')) == 1777034190
    utc = to_epoch('2026-06-10T22:00', ZoneInfo('UTC'))
    amsterdam = to_epoch('2026-06-10T22:00', ZoneInfo('Europe/Amsterdam'))
    assert utc - amsterdam == DST_OFFSET_SECONDS


def test_resolve_tag_filter() -> None:
    """A raw query passes through; repeated tags pick a server tag and keep the AND set."""
    assert resolve_tag_filter([], 'app:gitlab AND env:prod') == ('app:gitlab AND env:prod', [])
    assert resolve_tag_filter(['app:gitlab', 'customer:sbp'], None) == ('app:gitlab', ['app:gitlab', 'customer:sbp'])


def test_resolve_tag_filter_requires_a_selection() -> None:
    """With neither tags nor a query, the resolver exits."""
    with pytest.raises(SystemExit):
        resolve_tag_filter([], None)


def test_filter_by_tags_requires_every_tag() -> None:
    """Only SLOs carrying every required tag survive the client-side AND."""
    slos = [
        {'id': '1', 'tags': ['app:gitlab', 'customer:sbp']},
        {'id': '2', 'tags': ['app:gitlab']},
    ]
    assert [s['id'] for s in filter_by_tags(slos, ['app:gitlab', 'customer:sbp'])] == ['1']
    assert filter_by_tags(slos, []) == slos


def test_correction_attributes_omits_unset_fields() -> None:
    """Optional fields only appear in the payload when set."""
    minimal = Correction(category='Deployment', start=1, end=None, timezone='UTC')
    assert minimal.attributes() == {'category': 'Deployment', 'start': 1, 'timezone': 'UTC'}
    full = Correction(category='Deployment', start=1, end=2, timezone='UTC', description='x', rrule='FREQ=DAILY')
    assert full.attributes()['end'] == 2
    assert full.attributes()['rrule'] == 'FREQ=DAILY'


def test_matches_one_off_strategies() -> None:
    """The three one-off strategies differ on partial overlap but agree on an exact window."""
    existing = {'start': 100, 'end': 200, 'rrule': None}
    # Request fully inside the existing window.
    assert _matches(existing, 120, 180, None, SKIP_IF_COVERED) is True
    assert _matches(existing, 120, 180, None, SKIP_IF_EXACT) is False
    # 50% overlap: covered says create, overlap says skip.
    assert _matches(existing, 150, 300, None, SKIP_IF_COVERED) is False
    assert _matches(existing, 150, 300, None, SKIP_IF_OVERLAP) is True
    # Identical window is satisfied under every strategy (idempotent re-run).
    for strategy in (SKIP_IF_COVERED, SKIP_IF_OVERLAP, SKIP_IF_EXACT):
        assert _matches(existing, 100, 200, None, strategy) is True


def test_matches_recurring_requires_identical_rule() -> None:
    """Recurring corrections match only on identical start and rrule."""
    existing = {'start': 100, 'rrule': 'FREQ=DAILY'}
    assert _matches(existing, 100, None, 'FREQ=DAILY', SKIP_IF_COVERED) is True
    assert _matches(existing, 100, None, 'FREQ=WEEKLY', SKIP_IF_COVERED) is False


def test_build_config_requires_credentials() -> None:
    """Missing credentials abort before any network work."""
    with pytest.raises(SystemExit):
        build_config(
            api_key=None,
            app_key=None,
            site='datadoghq.eu',
            timezone='UTC',
            category='Scheduled Maintenance',
            strategy=SKIP_IF_COVERED,
            description='',
            tags=['app:gitlab'],
            tags_query=None,
            start=None,
            end=None,
            rrule=None,
            apply=False,
        )


def test_build_config_dry_run_has_no_correction() -> None:
    """A dry run resolves a config with no Correction attached."""
    cfg = build_config(
        api_key='key',
        app_key='app',
        site='datadoghq.eu',
        timezone='UTC',
        category='Scheduled Maintenance',
        strategy=SKIP_IF_COVERED,
        description='',
        tags=['app:gitlab'],
        tags_query=None,
        start=None,
        end=None,
        rrule=None,
        apply=False,
    )
    assert cfg.correction is None
    assert cfg.base == 'https://api.datadoghq.eu'
    assert cfg.required_tags == ['app:gitlab']


def test_build_config_requires_description_with_apply() -> None:
    """Applying without a (non-blank) description aborts, even with a valid window."""
    with pytest.raises(SystemExit):
        build_config(
            api_key='key',
            app_key='app',
            site='datadoghq.eu',
            timezone='UTC',
            category='Scheduled Maintenance',
            strategy=SKIP_IF_COVERED,
            description='   ',
            tags=['app:gitlab'],
            tags_query=None,
            start='2026-06-10T22:00',
            end='2026-06-10T23:00',
            rrule=None,
            apply=True,
        )


def test_load_config_ignores_credentials(tmp_path: Path) -> None:
    """Only non-secret keys are honoured; credential-like keys are dropped."""
    config = tmp_path / 'config.toml'
    config.write_text('site = "datadoghq.com"\nstrategy = "skip-if-overlap"\napi_key = "leak"\napp_key = "leak"\n')
    loaded = load_config(config)
    assert loaded == {'site': 'datadoghq.com', 'strategy': 'skip-if-overlap'}
    assert load_config(tmp_path / 'missing.toml') == {}


def test_init_config_writes_template_and_guards_overwrite(tmp_path: Path) -> None:
    """init-config writes the starter file once and refuses to clobber it."""
    config = tmp_path / 'config.toml'
    first = runner.invoke(app, ['init-config', '--config', str(config)])
    assert first.exit_code == 0
    assert config.read_text() == config_template()

    second = runner.invoke(app, ['init-config', '--config', str(config)])
    assert second.exit_code != 0


def test_version_option() -> None:
    """--version prints the app name and version, then exits 0."""
    result = runner.invoke(app, ['--version'])
    assert result.exit_code == 0
    assert 'datadog-slo-overrides' in result.output


def test_set_missing_choice_value_lists_choices() -> None:
    """A choice option given without a value reports the accepted values."""
    result = runner.invoke(app, ['set', '--strategy'])
    assert result.exit_code != 0
    normalized = ' '.join(result.output.split())
    assert 'Choose from' in normalized
    assert SKIP_IF_COVERED in normalized


def test_set_missing_nonchoice_value_keeps_bare_message() -> None:
    """A non-choice option given without a value keeps Click's plain message."""
    result = runner.invoke(app, ['set', '--start'])
    assert result.exit_code != 0
    assert 'Choose from' not in ' '.join(result.output.split())


def test_resolve_list_tags_allows_empty_selection() -> None:
    """The list path allows no tags (lists all SLOs), unlike the write path."""
    assert resolve_list_tags([], None) == ('', [])
    assert resolve_list_tags([], 'app:gitlab AND env:prod') == ('app:gitlab AND env:prod', [])
    assert resolve_list_tags(['app:gitlab', 'customer:sbp'], None) == ('app:gitlab', ['app:gitlab', 'customer:sbp'])


def test_default_window_is_month_start_to_now() -> None:
    """The default window runs from midnight on the 1st of the month to now."""
    start, end = default_window(ZoneInfo('UTC'))
    start_dt = datetime.fromtimestamp(start, ZoneInfo('UTC'))
    assert (start_dt.day, start_dt.hour, start_dt.minute, start_dt.second) == (1, 0, 0, 0)
    assert start <= end


def test_resolve_window_defaults_and_overrides() -> None:
    """An unset bound defaults; an explicit bound is parsed via to_epoch."""
    tz = ZoneInfo('UTC')
    default_start, _ = resolve_window(None, None, tz)
    month_start = datetime.fromtimestamp(default_start, tz)
    assert month_start.day == 1
    # An epoch end in the past is honoured as given.
    past_end = to_epoch('2026-07-20T00:00', tz)
    explicit_start, explicit_end = resolve_window('2026-07-05T00:00', str(past_end), tz)
    assert explicit_start == to_epoch('2026-07-05T00:00', tz)
    assert explicit_end == past_end


def test_resolve_window_clamps_a_future_end_to_now() -> None:
    """Hours that have not happened must not pad the uptime denominator."""
    tz = ZoneInfo('UTC')
    _start, now = default_window(tz)
    _clamped_start, clamped_end = resolve_window('2026-01-01T00:00', '2099-01-01T00:00', tz)
    # `now` moves between the two calls, so allow a second of slack.
    assert abs(clamped_end - now) <= 1


def test_resolve_window_rejects_a_reversed_window() -> None:
    """A backwards window is operator error, not a clean 100% uptime report."""
    tz = ZoneInfo('UTC')
    with pytest.raises(SystemExit) as exc:
        resolve_window('2026-07-20T00:00', '2026-07-05T00:00', tz)
    assert 'must fall before' in str(exc.value)


def test_slo_monitor_ids_only_for_monitor_slos() -> None:
    """Monitor IDs come from monitor_ids; metric/time_slice SLOs have none."""
    assert slo_monitor_ids({'type': 'monitor', 'monitor_ids': [12345, 67890]}) == [12345, 67890]
    assert not slo_monitor_ids({'type': 'metric'})
    assert not slo_monitor_ids({'type': 'monitor'})


def test_format_instant_includes_seconds() -> None:
    """Instants render to the second, so a sub-minute outage is not rounded away."""
    tz = ZoneInfo('UTC')
    assert format_instant(to_epoch('2026-08-24T17:47:00', tz), tz) == '2026-08-24 17:47:00'
    assert format_instant(to_epoch('2026-08-24T17:51:58', tz), tz) == '2026-08-24 17:51:58'


def test_format_duration_compact_units() -> None:
    """Durations render as compact d/h/m/s, omitting zero units."""
    assert format_duration(0) == '0s'
    assert format_duration(-5) == '0s'
    assert format_duration(58) == '58s'
    assert format_duration(298) == '4m 58s'
    assert format_duration(3600) == '1h'
    assert format_duration(90061) == '1d 1h 1m 1s'


def test_uptime_percentage_uses_window_as_denominator() -> None:
    """Uptime is the window minus downtime, over the window."""
    assert uptime_percentage(0, 3600) == 100.0
    assert uptime_percentage(36, 3600) == 99.0
    # An empty window cannot be less than fully available.
    assert uptime_percentage(10, 0) == 100.0
    # Downtime exceeding the window floors at zero rather than going negative.
    assert uptime_percentage(7200, 3600) == 0.0


def test_merge_intervals_sorts_and_coalesces() -> None:
    """Overlapping and touching intervals coalesce; empty ones are dropped."""
    assert merge_intervals([(10, 20), (15, 30)]) == [(10, 30)]
    assert merge_intervals([(20, 30), (10, 20)]) == [(10, 30)]  # touching
    assert merge_intervals([(10, 20), (30, 40)]) == [(10, 20), (30, 40)]
    assert not merge_intervals([(10, 10), (5, 4)])


def test_total_seconds_sums_intervals() -> None:
    """The total is the summed length of every interval."""
    assert total_seconds([]) == 0
    assert total_seconds([(10, 20), (30, 45)]) == 25


def test_subtract_intervals_splits_and_clears() -> None:
    """An exclusion inside an interval splits it; a covering one removes it."""
    assert subtract_intervals([(0, 100)], [(40, 60)]) == [(0, 40), (60, 100)]
    assert not subtract_intervals([(0, 100)], [(0, 100)])
    assert subtract_intervals([(0, 100)], [(100, 200)]) == [(0, 100)]  # no overlap
    assert subtract_intervals([(0, 100)], []) == [(0, 100)]
    # Partial overlap trims rather than dropping the whole interval, which is
    # what the old fully-contained check got wrong.
    assert subtract_intervals([(0, 100)], [(80, 200)]) == [(0, 80)]


def test_correction_intervals_clips_one_off_and_open_ended() -> None:
    """A one-off correction is clipped to the window; a missing end runs to its close."""
    corrections = [{'attributes': {'start': 50, 'end': 150}}]
    assert correction_intervals(corrections, 100, 200) == [(100, 150)]
    open_ended = [{'attributes': {'start': 120, 'end': None}}]
    assert correction_intervals(open_ended, 100, 200) == [(120, 200)]
    # A correction entirely outside the window excuses nothing.
    assert not correction_intervals([{'attributes': {'start': 500, 'end': 600}}], 100, 200)
    # A correction with no usable start is ignored.
    assert not correction_intervals([{'attributes': {}}], 100, 200)


def test_correction_intervals_expands_recurring_occurrences() -> None:
    """A recurring correction contributes every occurrence inside the window."""
    tz = ZoneInfo('UTC')
    start = to_epoch('2026-08-01T02:00', tz)
    window_start = to_epoch('2026-08-01T00:00', tz)
    window_end = to_epoch('2026-08-04T00:00', tz)
    corrections = [
        {'attributes': {'start': start, 'duration': 1800, 'rrule': 'FREQ=DAILY'}},
    ]
    occurrences = correction_intervals(corrections, window_start, window_end)
    # Three nightly half-hours: 1, 2 and 3 August.
    assert len(occurrences) == 3
    assert occurrences[0] == (start, start + 1800)
    assert total_seconds(occurrences) == 3 * 1800


def test_correction_intervals_recurring_needs_a_duration() -> None:
    """A recurring correction with no resolvable occurrence length is skipped."""
    assert not correction_intervals([{'attributes': {'start': 100, 'rrule': 'FREQ=DAILY'}}], 0, 10**6)


def test_event_monitor_id_prefers_the_nested_monitor() -> None:
    """The monitor's own id wins over the event's top-level id."""
    assert event_monitor_id({'id': 999, 'monitor': {'id': 42}}) == 42
    assert event_monitor_id({'monitor_id': 42}) == 42
    # A bare event id is the event's, not a monitor's, so it is not used.
    assert event_monitor_id({'id': 999}) is None
    assert event_monitor_id({}) is None


def test_classify_transition_reads_transition_then_alert_type() -> None:
    """alert_transition decides when present; alert_type is the fallback."""
    assert classify_transition({'alert_transition': 'Triggered'}) is True
    assert classify_transition({'alert_transition': 'Re-Triggered'}) is True
    assert classify_transition({'alert_transition': 'Recovered'}) is False
    # An unrecognised transition is ignored rather than guessed at, even when a
    # coarser alert_type is also present.
    assert classify_transition({'alert_transition': 'Muted', 'alert_type': 'error'}) is None
    assert classify_transition({'alert_type': 'error'}) is True
    assert classify_transition({'alert_type': 'success'}) is False
    # A Synthetics recovery carries no alert_transition and spells its
    # alert_type `ok`; not reading it as a recovery leaves the outage open to
    # the end of the window.
    assert classify_transition({'alert_type': 'ok'}) is False
    assert classify_transition({'alert_type': 'OK'}) is False
    assert classify_transition({}) is None


def test_parse_transitions_groups_by_monitor_and_counts_drops() -> None:
    """Transitions are grouped and sorted; every unusable event is counted by reason."""
    events = [
        {'monitor_id': 1, 'date_happened': 300, 'alert_transition': 'Recovered'},
        {'monitor_id': 1, 'date_happened': 100, 'alert_transition': 'Triggered'},
        {'monitor_id': 2, 'date_happened': 200, 'alert_transition': 'Triggered'},
        {'date_happened': 200, 'alert_transition': 'Triggered'},  # no monitor
        {'monitor_id': 3, 'alert_transition': 'Triggered'},  # no timestamp
        {'monitor_id': 4, 'date_happened': 200, 'alert_transition': 'Muted'},  # not a state change
        {'monitor_id': 5, 'date_happened': 200_000, 'alert_transition': 'Triggered'},  # out of range
    ]
    per_key, drops = parse_transitions(events, (0, 1000))
    assert sorted(per_key) == [(1, ''), (2, '')]
    assert per_key[(1, '')] == [Transition(at=100, failure=True), Transition(at=300, failure=False)]
    assert drops.usable == 3
    assert (drops.no_monitor, drops.no_timestamp, drops.unknown_transition, drops.out_of_range) == (1, 1, 1, 1)
    assert drops.dropped == 4


def test_parse_transitions_breaks_same_second_ties_failure_first() -> None:
    """A same-second trigger and recovery must not depend on API arrival order.

    The API returns events newest-first, so without an explicit tie-break the
    recovery would be read first, discarded, and the outage left open to the end
    of the window — turning a zero-second blip into weeks of downtime.
    """
    newest_first = [
        {'monitor_id': 1, 'date_happened': 100, 'alert_transition': 'Recovered'},
        {'monitor_id': 1, 'date_happened': 100, 'alert_transition': 'Triggered'},
    ]
    per_key, _drops = parse_transitions(newest_first, (0, 1000))
    assert not transitions_to_intervals(per_key[(1, '')], 0, 1000)


def test_parse_transitions_keeps_monitor_groups_apart() -> None:
    """A grouped monitor's overlapping group outages must not truncate each other."""
    events = [
        {'monitor_id': 1, 'monitor_groups': ['host:a'], 'date_happened': 100, 'alert_transition': 'Triggered'},
        {'monitor_id': 1, 'monitor_groups': ['host:b'], 'date_happened': 200, 'alert_transition': 'Triggered'},
        {'monitor_id': 1, 'monitor_groups': ['host:a'], 'date_happened': 300, 'alert_transition': 'Recovered'},
        {'monitor_id': 1, 'monitor_groups': ['host:b'], 'date_happened': 400, 'alert_transition': 'Recovered'},
    ]
    per_key, _drops = parse_transitions(events, (0, 1000))
    assert sorted(per_key) == [(1, 'host:a'), (1, 'host:b')]
    spans = [span for transitions in per_key.values() for span in transitions_to_intervals(transitions, 0, 1000)]
    # Unioned per monitor, the two staggered group outages cover 100 to 400.
    assert merge_intervals(spans) == [(100, 400)]


def test_flatten_events_pulls_up_children() -> None:
    """A recovery nested under an aggregated parent is still read."""
    events = [
        {
            'monitor_id': 1,
            'date_happened': 100,
            'alert_transition': 'Triggered',
            'children': [{'monitor_id': 1, 'date_happened': 200, 'alert_transition': 'Recovered'}],
        },
    ]
    per_key, _drops = parse_transitions(events, (0, 1000))
    assert transitions_to_intervals(per_key[(1, '')], 0, 1000) == [(100, 200)]


def test_transitions_to_intervals_pairs_trigger_with_recovery() -> None:
    """Each trigger is closed by the next recovery, at second precision."""
    tz = ZoneInfo('UTC')
    opened = to_epoch('2026-08-24T17:47:00', tz)
    closed = to_epoch('2026-08-24T17:51:58', tz)
    window_start = to_epoch('2026-08-01T00:00', tz)
    window_end = to_epoch('2026-09-01T00:00', tz)
    intervals = transitions_to_intervals(
        [Transition(at=opened, failure=True), Transition(at=closed, failure=False)],
        window_start,
        window_end,
    )
    assert intervals == [(opened, closed)]
    assert total_seconds(intervals) == 298


def test_transitions_to_intervals_absorbs_repeated_triggers() -> None:
    """A re-trigger while already alerting does not start a second interval."""
    intervals = transitions_to_intervals(
        [
            Transition(at=100, failure=True),
            Transition(at=150, failure=True),
            Transition(at=200, failure=False),
        ],
        0,
        1000,
    )
    assert intervals == [(100, 200)]


def test_transitions_to_intervals_handles_open_and_straddling_outages() -> None:
    """An outage open at the window end is closed there; one opened earlier is clipped."""
    # Never recovered: runs to the end of the window.
    assert transitions_to_intervals([Transition(at=500, failure=True)], 0, 1000) == [(500, 1000)]
    # Opened before the window (found via the lookback) and recovered inside it.
    assert transitions_to_intervals(
        [Transition(at=-500, failure=True), Transition(at=200, failure=False)],
        0,
        1000,
    ) == [(0, 200)]
    # A recovery with no preceding trigger proves the monitor was already down
    # before the fetched range began, so the outage counts from the window start
    # rather than vanishing.
    assert transitions_to_intervals([Transition(at=200, failure=False)], 0, 1000) == [(0, 200)]


def test_slo_observed_downtime_unions_its_monitors() -> None:
    """An SLO is down when any backing monitor is alerting, so intervals union."""
    per_monitor = {1: [(100, 200)], 2: [(150, 300)], 3: [(900, 950)]}
    slo = {'type': 'monitor', 'monitor_ids': [1, 2]}
    assert slo_observed_downtime(slo, per_monitor) == [(100, 300)]
    # A monitor with no events contributes nothing.
    assert not slo_observed_downtime({'type': 'monitor', 'monitor_ids': [4]}, per_monitor)
    # A metric SLO has no monitors at all.
    assert not slo_observed_downtime({'type': 'metric'}, per_monitor)


def test_list_alert_events_chunks_the_window() -> None:
    """A window longer than one chunk is fetched in slices, since the API truncates."""
    calls: list[tuple[int, int]] = []

    class _Resp:
        ok = True
        status_code = 200

        def json(self) -> dict:
            """Return one event per chunk, tagged with the chunk start."""
            return {'events': [{'monitor_id': 1, 'date_happened': calls[-1][0]}]}

    class _Session:
        def get(self, url: str, params: dict, timeout: int) -> _Resp:  # noqa: ARG002 - signature parity
            """Record the requested slice and return a single-event page."""
            calls.append((int(params['start']), int(params['end'])))
            return _Resp()

    session = cast('niquests.Session', _Session())
    events, saturated = list_alert_events(session, 'https://api.example', 0, int(2.5 * cli.EVENT_CHUNK_SECONDS))
    assert len(calls) == 3
    assert calls[0] == (0, cli.EVENT_CHUNK_SECONDS)
    # The final slice stops at the window end rather than overshooting.
    assert calls[-1][1] == int(2.5 * cli.EVENT_CHUNK_SECONDS)
    assert len(events) == 3
    assert saturated == 0


def test_format_net_summary_charges_only_the_overlap() -> None:
    """The excluded figure is raw minus net, not the corrections' own length."""
    summary = format_net_summary([(0, 100)], [(0, 40)])
    assert summary == 'raw 1m 40s - excluded 1m = net 40s'


def test_format_uptime_notes_the_pre_correction_figure() -> None:
    """The raw percentage is appended only when corrections changed it."""
    plain = format_uptime([(0, 36)], [(0, 36)], 3600)
    assert plain == 'uptime 99.000%'
    corrected = format_uptime([(0, 36)], [], 3600)
    assert corrected == 'uptime 100% (99.000% before corrections)'


def test_format_percentage_reserves_100_for_zero_downtime() -> None:
    """A clean 100% drops its decimals; a figure that only rounds to 100 does not."""
    assert format_percentage(100.0) == '100%'
    assert format_percentage(99.0) == '99.000%'
    # One second lost in a 30-day month: it must not read as perfect.
    month = 30 * SECONDS_PER_DAY
    assert format_percentage(uptime_percentage(1, month)) == '99.999%'


def test_print_slo_downtime_distinguishes_clean_from_fully_excused(capsys: pytest.CaptureFixture) -> None:
    """A fully corrected SLO reads differently from one that never failed."""
    tz = ZoneInfo('UTC')
    window = (0, 3600)
    slo = {'name': 'svc', 'id': 'abc', 'type': 'monitor', 'monitor_ids': [1]}

    print_slo_downtime(DowntimeRow(slo=slo, raw=[(0, 60)], net=[]), window, tz)
    excused = capsys.readouterr().out
    assert 'all observed downtime excluded by corrections' in excused

    print_slo_downtime(DowntimeRow(slo=slo, raw=[], net=[]), window, tz)
    clean = capsys.readouterr().out
    assert 'no net downtime in window' in clean

    print_slo_downtime(DowntimeRow(slo=slo, raw=[(0, 60)], net=[(0, 60)]), window, tz)
    down = capsys.readouterr().out
    assert '1970-01-01 00:00:00 → 1970-01-01 00:01:00' in down
    assert '(1m)' in down


def test_print_slo_downtime_notes_non_monitor_slos(capsys: pytest.CaptureFixture) -> None:
    """An SLO with no monitors cannot have monitor downtime, and says so."""
    print_slo_downtime(
        DowntimeRow(slo={'name': 'svc', 'id': 'abc', 'type': 'metric'}, raw=[], net=[]),
        (0, 3600),
        ZoneInfo('UTC'),
    )
    assert 'no monitor-based downtime' in capsys.readouterr().out


def test_correction_intervals_keeps_wall_clock_across_dst() -> None:
    """A nightly correction keeps its local time of day over a DST change.

    Anchoring the recurrence in UTC would shift every occurrence after the
    switch by an hour, charging excused maintenance against the SLO.
    """
    ams = ZoneInfo('Europe/Amsterdam')
    start = to_epoch('2026-10-24T22:00', ams)
    corrections = [
        {'attributes': {'start': start, 'duration': 3600, 'rrule': 'FREQ=DAILY', 'timezone': 'Europe/Amsterdam'}},
    ]
    window = (to_epoch('2026-10-24T00:00', ams), to_epoch('2026-10-27T00:00', ams))
    hours = [datetime.fromtimestamp(begin, ams).hour for begin, _end in correction_intervals(corrections, *window)]
    # 25 October is the switch; every occurrence still starts at 22:00 local.
    assert hours == [22, 22, 22]


def test_correction_intervals_bounds_a_recurring_series_by_its_end() -> None:
    """An `end` alongside an rrule bounds the series, not just one occurrence."""
    tz = ZoneInfo('UTC')
    start = to_epoch('2026-08-01T02:00', tz)
    corrections = [
        {
            'attributes': {
                'start': start,
                'end': to_epoch('2026-08-02T03:00', tz),
                'duration': 1800,
                'rrule': 'FREQ=DAILY',
            },
        },
    ]
    occurrences = correction_intervals(corrections, to_epoch('2026-08-01T00:00', tz), to_epoch('2026-08-05T00:00', tz))
    # Only the 1 and 2 August occurrences; the series ended before the third.
    assert len(occurrences) == 2


def test_correction_intervals_warns_rather_than_inferring_a_duration(
    capsys: pytest.CaptureFixture,
) -> None:
    """A recurring correction with no duration is skipped loudly, never inferred.

    Inferring `end - start` would treat a series-long span as one occurrence and
    excuse the whole window.
    """
    attrs = {'start': 100, 'end': 100 + 90 * SECONDS_PER_DAY, 'rrule': 'FREQ=DAILY'}
    assert not correction_intervals([{'attributes': attrs}], 0, 10**7)
    assert 'no usable duration' in capsys.readouterr().err


def test_list_alert_events_bisects_a_saturated_slice() -> None:
    """A slice returned at the page cap is halved and retried, not trusted."""
    widths: list[int] = []

    class _Resp:
        ok = True
        status_code = 200

        def __init__(self, count: int) -> None:
            self.count = count

        def json(self) -> dict:
            """Return a full page for the first (widest) slice only."""
            return {'events': [{'monitor_id': 1, 'date_happened': 1}] * self.count}

    class _Session:
        def get(self, url: str, params: dict, timeout: int) -> _Resp:  # noqa: ARG002 - signature parity
            """Saturate any slice wider than a quarter day, so bisection must recurse."""
            width = int(params['end']) - int(params['start'])
            widths.append(width)
            return _Resp(cli.EVENT_PAGE_CAP if width > SECONDS_PER_DAY // 4 else 1)

    session = cast('niquests.Session', _Session())
    events, saturated = list_alert_events(session, 'https://api.example', 0, SECONDS_PER_DAY)
    assert saturated == 0
    # One full day, halved, then quartered: the quarter-day slices are accepted.
    assert widths[0] == SECONDS_PER_DAY
    assert min(widths) == SECONDS_PER_DAY // 4
    assert len(events) == 4


def test_list_alert_events_reports_an_irreducibly_saturated_slice() -> None:
    """A slice still at the cap when it cannot be split further is reported."""

    class _Resp:
        ok = True
        status_code = 200

        def json(self) -> dict:
            """Always return a full page, however narrow the slice."""
            return {'events': [{'monitor_id': 1, 'date_happened': 1}] * cli.EVENT_PAGE_CAP}

    class _Session:
        def get(self, url: str, params: dict, timeout: int) -> _Resp:  # noqa: ARG002 - signature parity
            """Return a saturated page for every slice."""
            return _Resp()

    session = cast('niquests.Session', _Session())
    _events, saturated = list_alert_events(session, 'https://api.example', 0, cli.EVENT_MIN_CHUNK_SECONDS)
    assert saturated == 1


def test_get_json_turns_an_auth_failure_into_advice() -> None:
    """A 403 names the endpoint and the keys to check, never the URL with its query."""

    class _Resp:
        ok = False
        status_code = 403

    class _Session:
        def get(self, url: str, params: dict, timeout: int) -> _Resp:  # noqa: ARG002 - signature parity
            """Return a forbidden response."""
            return _Resp()

    with pytest.raises(SystemExit) as exc:
        get_json(cast('niquests.Session', _Session()), 'https://api.example/api/v1/events', {'sources': 'alert'})
    message = str(exc.value)
    assert 'v1/events' in message
    assert 'DD_APP_KEY' in message
    assert 'sources=alert' not in message


def test_slo_name_handles_an_explicit_null() -> None:
    """A null or blank name renders as a placeholder, not `None`."""
    assert slo_name({'name': 'svc'}) == 'svc'
    assert slo_name({'name': None}) == '<unnamed>'
    assert slo_name({'name': '  '}) == '<unnamed>'
    assert slo_name({}) == '<unnamed>'


def _fake_datadog_session(
    events: list[dict],
    corrections: dict[str, list[dict]],
    slos: list[dict],
) -> niquests.Session:
    """Return a fake session serving fixed SLO, event and correction payloads."""

    class _Resp:
        ok = True
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            """Return the canned payload."""
            return self.payload

    class _Session:
        def get(self, url: str, params: dict | None = None, timeout: int | None = None) -> _Resp:  # noqa: ARG002
            """Route the request to the matching canned payload."""
            params = params or {}
            if url.endswith('/api/v1/slo'):
                return _Resp({'data': slos if params.get('offset') == '0' else []})
            if url.endswith('/api/v1/events'):
                start, end = int(params['start']), int(params['end'])
                return _Resp({'events': [e for e in events if start <= e.get('date_happened', start) < end]})
            if '/corrections' in url:
                slo_id = url.split('/slo/')[1].split('/')[0]
                # Mirrors the real endpoint: `limit`/`offset` are ignored, only
                # `page[limit]`/`page[offset]` page it, and a page never exceeds
                # 25 however large a limit is asked for.
                offset = int(params.get('page[offset]', 0))
                limit = min(int(params.get('page[limit]', _CORRECTIONS_DEFAULT_PAGE)), CORRECTIONS_PAGE_SIZE)
                found = corrections.get(slo_id, [])
                return _Resp({'data': found[offset : offset + limit]})
            unexpected = f'unexpected URL {url}'
            raise AssertionError(unexpected)

    return cast('niquests.Session', _Session())


def test_report_closes_an_outage_recovered_with_an_ok_alert_type(capsys: pytest.CaptureFixture) -> None:
    """An `ok` recovery ends the outage instead of running it to the window end.

    Synthetics monitors emit no `alert_transition` and recover with
    `alert_type: ok`. Treating that as unreadable left the outage open, turning
    a minutes-long blip into days of reported downtime.
    """
    tz = ZoneInfo('UTC')
    window = (to_epoch('2026-09-15T00:00', tz), to_epoch('2026-09-17T00:00', tz))
    outage_start = to_epoch('2026-09-15T11:57:11', tz)
    outage_end = to_epoch('2026-09-15T12:01:41', tz)
    events = [
        {'monitor_id': 1, 'date_happened': outage_start, 'alert_type': 'error'},
        {'monitor_id': 1, 'date_happened': outage_end, 'alert_type': 'ok'},
    ]
    slos = [{'id': 'slo1', 'name': 'vault website', 'type': 'monitor', 'monitor_ids': [1], 'tags': []}]
    session = _fake_datadog_session(events, {}, slos)

    code = report_net_downtime(session, 'https://api.example', '', [], window, tz, 'UTC')
    out = capsys.readouterr().out
    assert code == 0
    assert '2026-09-15 11:57:11 → 2026-09-15 12:01:41  (4m 30s)' in out
    assert 'unrecognised transition' not in out


def test_report_net_downtime_pages_past_the_first_page_of_corrections(capsys: pytest.CaptureFixture) -> None:
    """Corrections beyond the endpoint's first page still excuse their downtime.

    The endpoint serves only 10 corrections unless paged with `page[limit]` /
    `page[offset]`, so an SLO with more than a page of them used to have the
    rest silently dropped and its excused downtime charged against it.
    """
    tz = ZoneInfo('UTC')
    window = (to_epoch('2026-08-01T00:00', tz), to_epoch('2026-09-01T00:00', tz))
    # One outage a day, each fully excused by its own correction. With 30 of
    # them, no single page of the endpoint holds them all.
    outages = [to_epoch('2026-08-01T00:00', tz) + day * SECONDS_PER_DAY + SECONDS_PER_HOUR for day in range(30)]
    events = []
    for start in outages:
        events.append({'monitor_id': 1, 'date_happened': start, 'alert_transition': 'Triggered'})
        events.append({'monitor_id': 1, 'date_happened': start + SECONDS_PER_HOUR, 'alert_transition': 'Recovered'})
    slos = [{'id': 'slo1', 'name': 'gitlab web', 'type': 'monitor', 'monitor_ids': [1], 'tags': []}]
    corrections = {'slo1': [{'attributes': {'start': s, 'end': s + SECONDS_PER_HOUR}} for s in outages]}
    assert len(corrections['slo1']) > CORRECTIONS_PAGE_SIZE
    session = _fake_datadog_session(events, corrections, slos)

    code = report_net_downtime(session, 'https://api.example', '', [], window, tz, 'UTC')
    out = capsys.readouterr().out
    assert code == 0
    assert 'all observed downtime excluded by corrections' in out
    assert 'raw 1d 6h - excluded 1d 6h = net 0s' in out


def test_report_net_downtime_end_to_end(capsys: pytest.CaptureFixture) -> None:
    """The whole report resolves, subtracts a partly-overlapping correction, and totals up."""
    tz = ZoneInfo('UTC')
    window = (to_epoch('2026-08-01T00:00', tz), to_epoch('2026-08-31T00:00', tz))
    outage_start = to_epoch('2026-08-24T17:47:00', tz)
    outage_end = to_epoch('2026-08-24T17:51:58', tz)
    excused_start = to_epoch('2026-08-10T02:00:00', tz)
    events = [
        {'monitor_id': 1, 'date_happened': outage_start, 'alert_transition': 'Triggered'},
        {'monitor_id': 1, 'date_happened': outage_end, 'alert_transition': 'Recovered'},
        {'monitor_id': 2, 'date_happened': excused_start, 'alert_transition': 'Triggered'},
        {'monitor_id': 2, 'date_happened': excused_start + 2 * SECONDS_PER_HOUR, 'alert_transition': 'Recovered'},
    ]
    slos = [
        {'id': 'slo1', 'name': 'gitlab web', 'type': 'monitor', 'monitor_ids': [1, 2], 'tags': []},
        {'id': 'slo2', 'name': 'metric thing', 'type': 'metric', 'tags': []},
    ]
    # A correction covering only the first hour of the two-hour outage.
    corrections = {'slo1': [{'attributes': {'start': excused_start, 'end': excused_start + SECONDS_PER_HOUR}}]}
    session = _fake_datadog_session(events, corrections, slos)

    code = report_net_downtime(session, 'https://api.example', '', [], window, tz, 'UTC')
    out = capsys.readouterr().out
    assert code == 0
    # The correction trims the excused hour and leaves the rest, rather than
    # excusing or keeping the whole outage.
    assert 'raw 2h 4m 58s - excluded 1h = net 1h 4m 58s' in out
    assert '2026-08-24 17:47:00 → 2026-08-24 17:51:58  (4m 58s)' in out
    assert '2026-08-10 03:00:00 → 2026-08-10 04:00:00  (1h)' in out
    assert 'Events     : 4 fetched, 4 usable transition(s)' in out
    # The metric SLO has no monitors and is reported as such.
    assert 'no monitor-based downtime' in out


def test_report_net_downtime_fails_when_no_transition_is_readable(capsys: pytest.CaptureFixture) -> None:
    """Events that yield no transitions must fail loudly, not certify 100% uptime."""
    tz = ZoneInfo('UTC')
    window = (to_epoch('2026-08-01T00:00', tz), to_epoch('2026-08-31T00:00', tz))
    # A payload whose fields are shaped differently than expected.
    events = [{'unexpected_id': 1, 'when': to_epoch('2026-08-10T00:00', tz), 'state': 'down'}]
    slos = [{'id': 'slo1', 'name': 'svc', 'type': 'monitor', 'monitor_ids': [1], 'tags': []}]
    session = _fake_datadog_session(events, {}, slos)

    code = report_net_downtime(session, 'https://api.example', '', [], window, tz, 'UTC')
    captured = capsys.readouterr()
    assert code == 1
    assert 'uptime 100%' in captured.out
    assert 'none could be read as a monitor state change' in captured.err


def test_report_net_downtime_only_downtime_hides_and_counts(capsys: pytest.CaptureFixture) -> None:
    """--only-downtime suppresses clean SLOs and reports how many were hidden."""
    tz = ZoneInfo('UTC')
    window = (to_epoch('2026-08-01T00:00', tz), to_epoch('2026-08-31T00:00', tz))
    outage = to_epoch('2026-08-24T17:47:00', tz)
    events = [
        {'monitor_id': 1, 'date_happened': outage, 'alert_transition': 'Triggered'},
        {'monitor_id': 1, 'date_happened': outage + 60, 'alert_transition': 'Recovered'},
    ]
    slos = [
        {'id': 'slo1', 'name': 'broken', 'type': 'monitor', 'monitor_ids': [1], 'tags': []},
        {'id': 'slo2', 'name': 'healthy', 'type': 'monitor', 'monitor_ids': [2], 'tags': []},
    ]
    session = _fake_datadog_session(events, {}, slos)

    code = report_net_downtime(session, 'https://api.example', '', [], window, tz, 'UTC', only_downtime=True)
    out = capsys.readouterr().out
    assert code == 0
    assert 'Hidden     : 1 SLO(s)' in out
    assert 'broken' in out
    assert 'healthy' not in out
    # Monitor 2 produced no transitions at all, which the header states rather
    # than leaving its 100% looking evidence-based.
    assert 'had no transitions and are reported as fully up' in out


def test_command_tree_lists_all_commands() -> None:
    """The command tree names every top-level command and the nested commands.list."""
    tree = command_tree(APP_NAME)
    assert tree.splitlines()[0] == APP_NAME
    for name in ('set', 'list', 'init-config', 'init-envrc', 'commands'):
        assert name in tree
    # commands.list appears nested under commands with a branch connector.
    assert '└── list' in tree or '├── list' in tree


def test_commands_list_command_prints_tree() -> None:
    """`commands list` prints the tree rooted at the app name."""
    result = runner.invoke(app, ['commands', 'list'])
    assert result.exit_code == 0
    assert APP_NAME in result.output
    assert 'set' in result.output
    assert 'commands' in result.output


def test_list_requires_credentials() -> None:
    """`list` aborts when no credentials are resolvable."""
    result = runner.invoke(app, ['list', '--tag', 'app:gitlab', '--config', '/nonexistent/config.toml'], env={})
    assert result.exit_code != 0


def test_load_direnv_env_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful ``direnv export json`` is parsed into the exported variables."""
    (tmp_path / '.envrc').write_text('export DD_API_KEY=k\n')
    monkeypatch.setattr(cli.shutil, 'which', lambda _: '/usr/bin/direnv')
    payload = json.dumps({'DD_API_KEY': 'key-123', 'DD_APP_KEY': 'app-456', 'DIRENV_DIFF': 'x'})
    monkeypatch.setattr(cli.subprocess, 'run', lambda *_args, **_kwargs: _completed(stdout=payload))

    loaded, _stderr = load_direnv_env(tmp_path)
    assert loaded['DD_API_KEY'] == 'key-123'
    assert loaded['DD_APP_KEY'] == 'app-456'


def test_load_direnv_env_unallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unapproved .envrc yields {} and an actionable 'direnv allow' hint."""
    (tmp_path / '.envrc').write_text('export DD_API_KEY=k\n')
    monkeypatch.setattr(cli.shutil, 'which', lambda _: '/usr/bin/direnv')
    blocked = _completed(stderr=f'direnv: error {tmp_path}/.envrc is blocked. Run `direnv allow`.', returncode=1)
    monkeypatch.setattr(cli.subprocess, 'run', lambda *_args, **_kwargs: blocked)

    env, _stderr = load_direnv_env(tmp_path)
    assert env == {}
    assert 'direnv allow' in capsys.readouterr().err


def test_load_direnv_env_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When direnv isn't installed, loading is skipped silently without spawning a process."""
    (tmp_path / '.envrc').write_text('export DD_API_KEY=k\n')
    monkeypatch.setattr(cli.shutil, 'which', lambda _: None)

    def _fail(*_args: object, **_kwargs: object) -> object:
        msg = 'subprocess.run should not be called when direnv is absent'
        raise AssertionError(msg)

    monkeypatch.setattr(cli.subprocess, 'run', _fail)
    env, _stderr = load_direnv_env(tmp_path)
    assert env == {}


def test_resolve_credentials_prefers_explicit_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Given both credentials, direnv is never consulted."""

    def _fail(_directory: Path) -> dict[str, str]:
        msg = 'direnv must not be consulted when both credentials are already present'
        raise AssertionError(msg)

    monkeypatch.setattr(cli, 'load_direnv_env', _fail)
    assert resolve_credentials('flag-key', 'flag-app', tmp_path) == ('flag-key', 'flag-app')


def test_resolve_credentials_fills_missing_from_direnv_without_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing keys are filled from direnv; an explicit value is never overridden."""
    monkeypatch.setattr(
        cli, 'load_direnv_env', lambda _d: ({'DD_API_KEY': 'direnv-key', 'DD_APP_KEY': 'direnv-app'}, '')
    )
    assert resolve_credentials('flag-key', None, tmp_path) == ('flag-key', 'direnv-app')


def test_resolve_credentials_surfaces_direnv_reason_when_keys_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An evaluated .envrc that yields no usable keys names them and echoes direnv's stderr."""
    # direnv ran (non-empty dict) but the Vault lookups failed, so the keys are absent
    # and the reason is on stderr.
    vault_error = 'Error making API request.\nCode: 403. Errors:\n* permission denied'
    monkeypatch.setattr(cli, 'load_direnv_env', lambda _d: ({'DIRENV_DIFF': 'x'}, vault_error))
    assert resolve_credentials(None, None, tmp_path) == (None, None)
    err = capsys.readouterr().err
    assert 'DD_API_KEY' in err
    assert 'DD_APP_KEY' in err
    assert 'did not provide' in err
    assert 'direnv reported:' in err
    assert '403' in err
    assert 'permission denied' in err
