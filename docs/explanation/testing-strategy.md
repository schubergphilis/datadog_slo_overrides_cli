# Testing strategy

The template layers three things to test your code: pytest for the test runner, coverage for what got executed, and tox for the multi-version matrix. Each does one job.

## Layer 1 — pytest

Why pytest and not unittest:

- **Fixtures**. Sharable, composable test setup.
- **Parametrization**. One test, many input rows.
- **Markers**. `@pytest.mark.slow`, `@pytest.mark.integration`, sliced via `-m`.
- **Plugin ecosystem**. xdist (parallel), coverage, hypothesis, asyncio, ...

The template ships pytest + a small set of plugins (coverage, xdist, html, env, metadata). Discover with `pytest --markers` after bootstrap.

## Layer 2 — Coverage

`pytest-cov` runs alongside pytest. The template tracks branch coverage (not just line coverage) and writes HTML + JSON reports under `reports/`.

`pyproject.toml`'s `[tool.coverage.report]` has `fail_under` set, and the test task **ratchets** this value upward after each green run: if your latest coverage was 87% and `fail_under` was 80%, the task bumps `fail_under` to 87%. Once engaged, the bar only goes up — lowering `fail_under` is a deliberate, reviewable change that shows up in the diff.

### Dormant during scaffold

The ratchet starts **dormant**. The smoke test (`def test_sanity` in `tests/test_<slug>.py`) drives the scaffolded project to 100% coverage on its very first run; if the ratchet engaged on that signal, your first real change would crash the build at a 100% floor. So the test task checks for the presence of `def test_sanity` and, while it's still there, prints a status line on every run explaining the dormant state and how to engage:

```
[ratchet] coverage=100% — scaffold still pristine (test_sanity present); ratchet dormant
[ratchet]   ratchet engages when you remove `test_sanity` in tests/test_<slug>.py
[ratchet]   to engage immediately, set [tool.test-ratchet] mode = "strict" in pyproject.toml
```

The moment you delete or rename `test_sanity` — i.e., the moment you start writing real tests — the ratchet engages and works exactly as described above. Once any non-zero `fail_under` is written, dormancy is over for good; re-adding `test_sanity` later doesn't reactivate it.

If you're seeding the template into a codebase that's already covered, set `[tool.test-ratchet] mode = "strict"` in `pyproject.toml` to bypass the dormancy check entirely.

Coverage regressions still can't slip in silently — they just can't slip in *or out* during the scaffold phase.

## Layer 3 — tox

tox + tox-uv runs the test suite against every Python version in the project's range. Configured in `pyproject.toml`'s `[tool.tox]`, generated from `min_python_version` / `max_python_version` at template-render time.

`./workflow.cmd test` runs only one Python version (whichever the active uv venv resolved to). The full matrix runs in CI per shipped workflow, or locally via `uv run tox`.

See [Run tests for one Python version](../how-to/run-tests-for-one-python-version.md) for the practical commands.

## What we don't ship

- **A testing pyramid.** The template doesn't pre-create unit/integration/e2e folders. The example smoke test lives directly in `tests/`. Structure your tests how your project warrants.
- **Hypothesis or other property-based tooling.** Add it as a `test` group dep if you want it.
- **Mutation testing.** Mutmut or cosmic-ray. Add them as a `quality` group concern if you reach for them.
- **A "tests" service in `docker-compose`.** The deps cache image (`Dockerfile.deps`) is for CI deps, not for app testing.

## Smoke tests vs. real tests

The template generates one smoke test per project — a `tests/test_<slug>.py` that exercises the example `hello()` function with two functions: `test_sanity` (an `assert True` placeholder) and `test_integration` (the actual call). Keep them or delete them; either is fine, but `test_sanity` doubles as the [ratchet dormancy marker](#dormant-during-scaffold), so removing it engages the ratchet.

## Parallel execution

`pytest-xdist` runs tests across CPU cores by default (`-n auto` in the template's pytest config). Some tests don't play well with parallelism — anything touching the filesystem in a fixed location, or relying on shared global state.

For those, add `@pytest.mark.serial` and a corresponding `-m "not serial"` / `-m serial` two-pass setup. The template doesn't ship this scaffolding because most projects don't need it.

## Coverage of `_CI/`

The template treats `_CI/` as part of your codebase for linting purposes but **not** for test coverage. The CI tooling isn't reasonably unit-testable — its job is to glue together external commands. Coverage of `_CI/` is implicitly via the workflow tasks running successfully end-to-end on every commit.

## See also

- [Run tests for one Python version](../how-to/run-tests-for-one-python-version.md) — the practical commands.
- [Dependency groups](../reference/dependency-groups.md) — what's in the `test` group.
