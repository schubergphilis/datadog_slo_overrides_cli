# First-run setup

This is the first thing to do after this project was generated. By the end of it you'll have a green test run and a built wheel.

## You need

- [`uv`](https://docs.astral.sh/uv/) on your PATH.
- A shell. macOS/Linux: bash or zsh. Windows: PowerShell or git-bash.

You do *not* need to pre-install Python — uv will fetch the versions declared in `.python-version`.

## This step is a `pre` command of all the other commands so running it manually is not actually required. 
### It is safe to skip to step 2 immediately.

## Step 1 — Bootstrap

```bash
./workflow.cmd bootstrap
```

This:

1. Creates uv-managed virtualenvs for the `dev`, `lint`, `test`, `document`, `quality`, and `security` dependency groups (see [Reference: dependency groups](../reference/dependency-groups.md)).
2. Asks to install pre-commit hooks into `.git/hooks/`.
3. Drops a sentinel file (`_CI/.bootstrapped`) so re-runs are no-ops.

Re-running is safe and fast. Pass `--force` to repeat the setup.

## Step 2 — Format, then lint

```bash
./workflow.cmd format
./workflow.cmd lint
```

`format` runs `ruff format` and `ruff check --select I --fix` (import sort). `lint` runs ruff, pylint, ty (the type checker), complexipy, and commitizen. On a freshly-generated project all checks should be clean and pylint should rate the codebase 10.00/10.

## Step 3 — Test

```bash
./workflow.cmd test
```

This runs pytest with coverage and parallel execution via xdist. The default project ships one smoke test for the example `hello()` function — it should pass and report 100% coverage.

## Step 4 — Build

```bash
./workflow.cmd build
```

Produces a wheel and an sdist under `dist/`. You can `uv pip install dist/<your-package>-*.whl` into a throwaway venv to confirm it imports.

## You're ready

You now have the loop you'll run hundreds of times: format → lint → test → build. Three places to go next:

- **Make a real change** — write a function, then [Make your first release](make-your-first-release.md).
- **Add dependencies** — [Add a dependency](../how-to/add-a-dependency.md).
- **Understand what just happened** — [The _CI tasks architecture](../explanation/the-ci-tasks-architecture.md) explains how `workflow.cmd` dispatches into Invoke tasks.
