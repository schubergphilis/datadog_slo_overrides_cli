# Run tests for one Python version

`./workflow.cmd test` runs pytest against the active uv venv, which uses one Python version (the `requires-python` floor by default). To exercise the full version range or pin to a specific minor, use the tox path.

## All versions via tox

```bash
uv run tox
```

Runs every env declared in `pyproject.toml`'s `[tool.tox]` section. For a project generated with `min_python_version=3.13` and `max_python_version=3.14`, this is `py313` + `py314`.

## A single Python version

```bash
uv run tox -e py313
uv run tox -e py314
```

The env name is `py<major><minor>` with no dot.

## Pytest directly on the current venv

```bash
uv run pytest
uv run pytest tests/test_specific_file.py
uv run pytest -k "test_hello"
uv run pytest -m "not slow"
```

The first call uses whichever Python version uv resolved for the project. The other forms slice by file, name, or marker.

## In CI

The shipped CI matrix runs each Python version in parallel. To restrict locally what CI runs, edit the `matrix` block in `.github/workflows/continuous-integration.yaml` or the `parallel:` block in `.gitlab-ci.yml`.

## When you need a version uv hasn't fetched yet

```bash
uv python install 3.13
uv python install 3.14
```

uv fetches and pins it; tox picks it up automatically on the next run.

## See also

- [Reference: configuration files](../reference/configuration-files.md) — where pytest, tox, and coverage settings live.
