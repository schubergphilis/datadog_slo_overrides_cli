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
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from datadog_slo_overrides_cli import datadog_slo_overrides_cli as cli
from datadog_slo_overrides_cli.datadog_slo_overrides_cli import (
    APP_NAME,
    SKIP_IF_COVERED,
    SKIP_IF_EXACT,
    SKIP_IF_OVERLAP,
    Correction,
    _covered_by_correction,
    _matches,
    app,
    build_config,
    command_tree,
    config_template,
    corrections_attrs,
    default_window,
    downtime_monitor_id,
    downtime_window,
    filter_by_tags,
    format_downtime,
    index_downtimes_by_monitor,
    load_config,
    load_direnv_env,
    resolve_credentials,
    resolve_list_tags,
    resolve_tag_filter,
    resolve_window,
    slo_monitor_ids,
    to_epoch,
    uncovered_downtimes,
)

runner = CliRunner()

DST_OFFSET_SECONDS = 7200


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
    explicit_start, explicit_end = resolve_window('2026-07-05T00:00', '1777034190', tz)
    assert explicit_start == to_epoch('2026-07-05T00:00', tz)
    assert explicit_end == 1777034190


def test_slo_monitor_ids_only_for_monitor_slos() -> None:
    """Monitor IDs come from monitor_ids; metric/time_slice SLOs have none."""
    assert slo_monitor_ids({'type': 'monitor', 'monitor_ids': [12345, 67890]}) == [12345, 67890]
    assert slo_monitor_ids({'type': 'metric'}) == []
    assert slo_monitor_ids({'type': 'monitor'}) == []


def test_downtime_monitor_id_distinguishes_id_from_tag_scoped() -> None:
    """A monitor_id target yields its id; a monitor_tags target yields None."""
    assert downtime_monitor_id({'monitor_identifier': {'monitor_id': 12345}}) == 12345
    assert downtime_monitor_id({'monitor_identifier': {'monitor_tags': ['team:x']}}) is None
    assert downtime_monitor_id({}) is None


def test_downtime_window_one_time_and_recurring() -> None:
    """One-time downtimes read schedule.start/end; recurring read current_downtime."""
    one_time = downtime_window({'schedule': {'start': '2026-07-09T10:00:00+00:00', 'end': '2026-07-09T11:00:00+00:00'}})
    assert one_time is not None
    start, end = one_time
    assert end is not None
    assert end - start == 3600  # one hour
    # Open-ended one-time downtime.
    open_ended = downtime_window({'schedule': {'start': '2026-07-09T10:00:00+00:00', 'end': None}})
    assert open_ended is not None
    assert open_ended[1] is None
    # Recurring downtime uses the current occurrence.
    recurring = downtime_window(
        {
            'schedule': {
                'recurrences': [{'rrule': 'FREQ=DAILY', 'start': '2026-07-09T10:00'}],
                'current_downtime': {'start': '2026-07-09T10:00:00+00:00', 'end': '2026-07-09T10:30:00+00:00'},
            }
        }
    )
    assert recurring is not None
    # A downtime with no resolvable start returns None.
    assert downtime_window({'schedule': {}}) is None


def test_index_downtimes_by_monitor_counts_tag_scoped() -> None:
    """Downtimes are grouped by monitor_id; tag-scoped ones are counted, not indexed."""
    downtimes = [
        {'attributes': {'monitor_identifier': {'monitor_id': 1}, 'schedule': {'start': '2026-07-09T10:00:00+00:00'}}},
        {'attributes': {'monitor_identifier': {'monitor_id': 1}, 'schedule': {'start': '2026-07-10T10:00:00+00:00'}}},
        {'attributes': {'monitor_identifier': {'monitor_tags': ['team:x']}, 'schedule': {}}},
    ]
    index, tag_scoped = index_downtimes_by_monitor(downtimes)
    assert len(index[1]) == 2
    assert tag_scoped == 1


def test_covered_by_correction_only_fully_contained() -> None:
    """A downtime is overridden only when a non-recurring correction fully contains it."""
    corrections = [{'start': 100, 'end': 200}]
    assert _covered_by_correction(120, 180, corrections) is True
    assert _covered_by_correction(120, 250, corrections) is False  # extends past the correction
    # Recurring corrections never count as coverage.
    assert _covered_by_correction(120, 180, [{'start': 100, 'end': 200, 'rrule': 'FREQ=DAILY'}]) is False
    # Open-ended correction covers an open-ended downtime that starts within it.
    assert _covered_by_correction(150, None, [{'start': 100, 'end': None}]) is True


def test_corrections_attrs_extracts_payloads() -> None:
    """corrections_attrs reduces correction objects to their attributes dicts."""
    assert corrections_attrs([{'id': 'a', 'attributes': {'start': 1}}]) == [{'start': 1}]


def test_uncovered_downtimes_filters_window_and_overrides() -> None:
    """Only in-window downtimes that no correction covers survive."""

    def _downtime(start: str, end: str) -> dict:
        return {'monitor_identifier': {'monitor_id': 1}, 'schedule': {'start': start, 'end': end}}

    downtimes = [
        _downtime('2026-07-09T10:00:00+00:00', '2026-07-09T11:00:00+00:00'),  # in window, not covered -> kept
        _downtime('2026-07-05T22:00:00+00:00', '2026-07-05T23:00:00+00:00'),  # covered by correction -> dropped
        _downtime('2026-08-01T10:00:00+00:00', '2026-08-01T11:00:00+00:00'),  # outside window -> dropped
    ]
    window_start = to_epoch('2026-07-01T00:00', ZoneInfo('UTC'))
    window_end = to_epoch('2026-07-31T23:59', ZoneInfo('UTC'))
    covering = {
        'start': to_epoch('2026-07-05T21:00', ZoneInfo('UTC')),
        'end': to_epoch('2026-07-06T00:00', ZoneInfo('UTC')),
    }
    kept = uncovered_downtimes(downtimes, [covering], window_start, window_end)
    assert len(kept) == 1
    assert kept[0][1] == to_epoch('2026-07-09T10:00', ZoneInfo('UTC'))


def test_format_downtime_renders_window_and_target() -> None:
    """A downtime line shows its window, the target monitor, and any message."""
    tz = ZoneInfo('UTC')
    start = to_epoch('2026-07-09T10:00', tz)
    end = to_epoch('2026-07-09T11:00', tz)
    line = format_downtime({'monitor_identifier': {'monitor_id': 42}, 'message': 'deploy'}, start, end, tz)
    assert 'monitor 42' in line
    assert '→' in line
    assert '"deploy"' in line
    open_ended = format_downtime({'monitor_identifier': {'monitor_id': 42}}, start, None, tz)
    assert 'open-ended' in open_ended


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
