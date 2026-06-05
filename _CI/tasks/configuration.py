"""Centralized constants for CI task definitions."""

import re
from pathlib import Path

PATHS = 'src/ _CI/tasks/ tests/'
SECURITY_OVERRIDE_ENV = 'DATADOG_SLO_OVERRIDES_CLI_SECURITY_OVERRIDE'
SECURITY_OVERRIDES_FILE = Path('.security-overrides')

PYSCN_REPORTS_DIR = Path('reports')

IGNORE_PATTERN = re.compile(
    r'(?P<vulnerability_id>[A-Za-z0-9\-_]+)'
    r'(::(?P<expiration_date>\d{4}-\d{2}-\d{2}))?'
)

IMAGE_NAME = 'datadog_slo_overrides_cli-deps'

PROJECT_NAME = 'datadog_slo_overrides_cli'

# SBOM ships inside the wheel via the package data tree, so consumers of the
# released artefact can extract it via importlib.resources or by unzipping.
SBOM_FILE = Path('src') / PROJECT_NAME / 'sbom.cdx.json'
VENDOR_TXT = Path('_CI') / 'lib' / 'vendor.txt'
VENDOR_DIR = Path('_CI') / 'lib' / 'vendor'
UV_LOCK = Path('uv.lock')
UV_PUBLISH_SETTINGS = ('UV_PUBLISH_URL', 'UV_PUBLISH_PASSWORD', 'UV_PUBLISH_USERNAME')
# Presence of both signals PyPI Trusted Publishing (OIDC) — `uv publish`
# exchanges them for a short-lived token, so the legacy UV_PUBLISH_* creds
# become unnecessary. See release.publish for the branching logic.
OIDC_ENV_VARS = ('ACTIONS_ID_TOKEN_REQUEST_URL', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN')

SENTINEL = Path('_CI/.bootstrapped')
