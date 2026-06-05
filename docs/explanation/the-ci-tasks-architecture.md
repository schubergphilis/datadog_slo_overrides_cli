# The _CI tasks architecture

The `_CI/` directory is the template's portable CI/CD framework — vendored [Invoke](https://www.pyinvoke.org/) plus a set of opinionated task modules. This page covers the design.

## Why Invoke, not make / just / nox

| Tool | Pro | Con (for this template) |
| --- | --- | --- |
| `make` | Universal | Whitespace-sensitive, shell-quoted args, awkward Python integration |
| `just` | Modern, fast | Extra binary to install; not Python-native |
| `nox` | Python-native, env-isolating | Template integration is messy; nox needs to BE installed before it can install anything |
| **Invoke** | Python-native, no extra install (vendored), composable | Less well-known than Make |

The deciding factor was vendoring: a fresh clone runs `./workflow.cmd …` immediately, no `pip install invoke` step. Invoke + its deps live committed under `_CI/lib/vendor/`.

## The polyglot launcher

`workflow.cmd` is a script that's simultaneously valid sh and Windows cmd:

```
@echo off & rem (Windows cmd interprets the rest; sh ignores the @ + rem prefix)
@uv run python -m _CI.invoke -- %*
:; uv run python -m _CI.invoke -- "$@"
```

Both shells route through `uv run python -m _CI.invoke -- <args>` — uv handles venv creation, invoke handles task dispatch. No global installs required; no shell-specific copy.

## Module structure

```
_CI/tasks/
├── __init__.py          # Namespace aggregation + bootstrap pre-task wiring
├── bootstrap.py         # First-run setup framework
├── configuration.py     # Shared constants (paths, env vars, registry settings)
├── shared.py            # @logged, @run, execute, run_steps, IndentingStream
├── github.py / gitlab.py  # Host-specific helpers (only one is present)
└── <feature>.py         # build, container, develop, document, format_, lint,
                         #   quality, release, secure, test
```

Each feature module:

- Imports `Collection`, `task` from invoke.
- Defines its tasks with `@task` (and optionally `@logged`).
- Builds its `namespace = Collection('<name>')` at module bottom.
- `add_task(...)` registers each task; one is `default=True` for the bare-namespace shortcut.

`__init__.py` aggregates all module namespaces into one `namespace` that Invoke discovers.

## `@logged` and indented output

The `@logged` decorator wraps a task to print a clean status line:

```
    ✅ lint.ruff passed 👍
```

For tasks that call other tasks (a "workflow task" like `release` that calls `validate`, `bump`, `changelog`, `push`), nested output is indented under the parent banner via the `IndentingStream` class in `shared.py`. The indent is applied at the stdout/stderr layer, so even commands invoked through `context.run()` get their output indented.

This single design choice does a lot of heavy lifting — terminal output for `./workflow.cmd release` is hierarchical and scannable instead of a flat dump.

## Bootstrap as a pre-task

Every top-level task has `bootstrap_task` inserted as its first `pre`. This means the first run of *any* workflow command triggers the bootstrap; subsequent runs see the sentinel file (`_CI/.bootstrapped`) and skip.

Wired in `__init__.py`:

```python
bootstrap_task = bootstrap.bootstrap
for module in (build, container, develop, ...):
    for task in module.namespace.tasks.values():
        task.pre.insert(0, bootstrap_task)
```

This is why a fresh clone "just works" — `./workflow.cmd test` on day one is bootstrap-then-test; on day two it's just test.

## `run_steps`: fail-last, no short-circuiting

A workflow task that runs N steps (e.g. `lint` runs ruff + pylint + ty + complexipy + commitizen) doesn't short-circuit on the first failure. The `run_steps()` helper in `shared.py` runs every step, accumulates failures, and raises `SystemExit(1)` at the end with all the failures reported.

This makes CI runs informative — you see every issue per run instead of fixing one at a time.

## Host-specific code isolation

The host (`github` or `gitlab`) chosen at generation time determines which of `_CI/tasks/github.py` or `_CI/tasks/gitlab.py` is shipped. Both expose the same contract:

- `registry_settings() -> RegistrySettings`
- `publish_deps_image(context, tag) -> str`
- `create_release_pr(context, branch, version) -> str`
- `pr_create_url(context, branch) -> str`

`container.py` and `release.py` import these via a Jinja-substituted relative import — `from .github import …` — which renders at generation time to a concrete `from .github import …` or `from .gitlab import …`. Because the unchosen module is omitted at generation time via a copier conditional filename (the file's rendered name is empty when not selected), there's exactly one code path and no runtime branching.

## See also

- [Add a workflow task](../how-to/add-a-workflow-task.md) — practical recipe for extending the framework.
- [Reference: Invoke task catalog](../reference/invoke-tasks.md) — what's already there.
