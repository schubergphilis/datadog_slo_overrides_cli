# Add a dependency

## Add a runtime dependency

```bash
uv add httpx
```

This updates `pyproject.toml`'s `[project.dependencies]` and `uv.lock`. Commit both files.

## Add a development-only dependency

Dev dependencies live in named groups in `pyproject.toml`. The full group list is in [Reference: dependency groups](../reference/dependency-groups.md).

```bash
uv add --group test pytest-asyncio
uv add --group lint flake8-bugbear
uv add --group document mkdocs-glightbox
```

Use the group whose workflow task will actually run the package. A dep used only during `lint` should not be in `test`; the bootstrap will install it either way, but CI matrices and container layers will not.

## Pin or constrain a version

```bash
uv add 'httpx>=0.27,<0.28'
```

Pinning happens in `pyproject.toml`; uv records the exact resolved version in `uv.lock`.

## Remove a dependency

```bash
uv remove httpx
uv remove --group lint flake8-bugbear
```

## After any change

Re-run the bootstrap so every group's venv is in sync:

```bash
./workflow.cmd bootstrap --force
```

And re-run the affected stage:

```bash
./workflow.cmd lint    # if you changed lint or dev deps
./workflow.cmd test    # if you changed test or runtime deps
```

## What to commit

Always: `pyproject.toml` and `uv.lock`. Never: the `.venv*/` directories (already gitignored).
