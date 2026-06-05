# Why uv

This template uses [uv](https://docs.astral.sh/uv/) for every Python operation: creating virtualenvs, installing dependencies, resolving the lockfile, publishing to PyPI, even fetching Python interpreters. This page explains why, and what we gave up to get there.

## What uv replaces

In an older lineage of this template, a fresh checkout required: a system Python, pipx, virtualenv, pipenv, pip-tools, tox-uv, and twine. Each had its own configuration surface, its own caching behaviour, and its own update cadence.

uv replaces all of them with a single Rust binary that:

- Installs and pins Python interpreters per project (no system-Python dependency).
- Resolves and locks dependencies (no `pip-compile`, no `pipenv`).
- Creates and manages virtualenvs (no `python -m venv`).
- Publishes to PyPI (no `twine`).
- Runs tools in ephemeral environments (`uvx`, no global `pipx`).

## Why this is worth the lock-in

**Speed.** uv resolves a typical lockfile in under a second. pipenv took tens of seconds — sometimes minutes on large dependency graphs. The dev loop genuinely feels different.

**One config surface.** `pyproject.toml` plus `uv.lock`. Two files. No `requirements.in`, no `requirements.txt`, no `Pipfile`, no `Pipfile.lock`.

**Reproducibility by default.** uv's lockfile is platform-portable and hash-locked. CI and your laptop get bit-identical environments without opt-in flags.

**Dependency groups, not extras-abuse.** uv first-classed PEP 735 dependency groups, which are exactly what we want for the dev/lint/test/document/security split. We previously used `[project.optional-dependencies]` and had to explain that those aren't really "optional," they're internal grouping.

## What we gave up

**Tool risk.** uv is one company's project (Astral). If they pivot or the project stalls, we're more exposed than if we'd stuck with the pip/build/twine stack maintained by the PyPA. We mitigate by:

- Keeping the generated `pyproject.toml` valid against PEP 517 / 518 / 621 — any PEP-compliant tool could build it.
- Not relying on uv-specific syntax beyond dependency groups (which are themselves standardised).

**Older Python support.** uv requires recent enough Python that the template's `min_python_version` floor of 3.11 isn't a constraint uv adds — but if you wanted to support 3.8, uv wouldn't stop you, it just isn't a configuration we test.

**Familiarity.** Some contributors will arrive expecting `pip install -r requirements.txt`. The README points them to `./workflow.cmd bootstrap` and the [first-run setup tutorial](../tutorials/first-run-setup.md).

## When this decision could be revisited

If two of these became true together:

1. uv's lockfile format incompatibly changed and broke existing projects.
2. A maintained alternative offered comparable speed and PEP 735 support.

Then the cost of migration would be one template revision and a `copier update`. The decision is reversible at the template level.

## See also

- [Dependency groups](../reference/dependency-groups.md) — how we structure deps using uv's features.
