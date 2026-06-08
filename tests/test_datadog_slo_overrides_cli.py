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
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from datadog_slo_overrides_cli import datadog_slo_overrides_cli as cli
from datadog_slo_overrides_cli.datadog_slo_overrides_cli import (
    SKIP_IF_COVERED,
    SKIP_IF_EXACT,
    SKIP_IF_OVERLAP,
    Correction,
    _matches,
    app,
    build_config,
    config_template,
    filter_by_tags,
    load_config,
    load_direnv_env,
    resolve_credentials,
    resolve_tag_filter,
    to_epoch,
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


def test_run_missing_choice_value_lists_choices() -> None:
    """A choice option given without a value reports the accepted values."""
    result = runner.invoke(app, ['run', '--strategy'])
    assert result.exit_code != 0
    normalized = ' '.join(result.output.split())
    assert 'Choose from' in normalized
    assert SKIP_IF_COVERED in normalized


def test_run_missing_nonchoice_value_keeps_bare_message() -> None:
    """A non-choice option given without a value keeps Click's plain message."""
    result = runner.invoke(app, ['run', '--start'])
    assert result.exit_code != 0
    assert 'Choose from' not in ' '.join(result.output.split())


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
