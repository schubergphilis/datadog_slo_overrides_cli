## v0.3.0 (2026-09-25)

### Features

- authenticate with a Datadog personal or service access token

## v0.2.3 (2026-09-22)

### Bug Fixes

- retry Datadog server errors instead of aborting the run

## v0.2.2 (2026-09-16)

### Bug Fixes

- treat an `ok` alert_type as a recovery

## v0.2.1 (2026-09-16)

### Bug Fixes

- page SLO corrections with page[limit]/page[offset]

## v0.2.0 (2026-09-11)

### BREAKING CHANGE

- `list` reports observed monitor downtime net of corrections instead of uncovered Datadog downtime objects. Its output format and data source both change, and it now exits 1 when no monitor state change can be read. Adds a python-dateutil dependency.

### Bug Fixes

- upgrade msgpack, pip and pymdown-extensions past known vulnerabilities
- enable typer shell completion

### Features

- report monitor downtime net of SLO corrections

## v0.1.1 (2026-07-17)

### Bug Fixes

- init-envrc pre-fills DD_API_KEY/DD_APP_KEY from secret/audit

## v0.1.0 (2026-07-17)

### Features

- add list command for SLO downtime and a commands tree

## v0.0.7 (2026-06-17)

## v0.0.6 (2026-06-09)

### Features

- require --description when using --apply

## v0.0.5 (2026-06-08)

### Features

- list valid choices for --strategy/--category errors

## v0.0.4 (2026-06-08)

## v0.0.3 (2026-06-07)

### Features

- add --version option

## v0.0.2 (2026-06-05)

### Features

- surface direnv stderr when .envrc credential lookup fails

## v0.0.1 (2026-06-05)

### Features

- add Datadog SLO overrides CLI
