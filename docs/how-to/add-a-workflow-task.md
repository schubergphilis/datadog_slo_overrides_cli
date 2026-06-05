# Add a workflow task

The `./workflow.cmd <namespace>.<task>` interface is backed by Invoke. Adding a new task is one of:

- A new task inside an existing module (e.g. another step in `lint`).
- A new module entirely (e.g. `_CI/tasks/benchmarks.py`).

## A new task in an existing module

Open the relevant module under `_CI/tasks/`. Each module ends with a `namespace = Collection(...)` and a series of `namespace.add_task(...)` calls. To add an Invoke task:

```python
from invoke import Context, task

from .shared import execute, logged


@task
@logged('lint.spelling')
def spelling(context: Context) -> None:
    """Run codespell on src/ and tests/."""
    execute(context, 'uv run codespell src/ tests/')
```

The `@logged` decorator emits the pass/fail status line. The `execute` helper raises `SystemExit(1)` on non-zero exit; nested stdout is indented under the parent banner (the `IndentingStream` wiring in `_CI/tasks/shared.py`).

Then register the task at the bottom of the module:

```python
namespace.add_task(cast(Task, spelling))
```

If the module aggregates several subtasks into a default (e.g. `lint` running everything), add it to the `run_steps()` call.

## A new module entirely

1. Create `_CI/tasks/<name>.py` with at least:

   ```python
   from invoke import Collection, Context, Task, task

   from .shared import execute, logged

   @task
   @logged('<name>.all')
   def all_(context: Context) -> None:
       """Run everything in this namespace."""
       ...

   namespace = Collection('<name>')
   namespace.add_task(cast(Task, all_), default=True, name='all')
   ```

2. Register it in `_CI/tasks/__init__.py`:

   ```python
   from . import <name>

   namespace.add_collection(<name>.namespace)
   ```

3. Add it to the bootstrap-pre-task loop so the first invocation triggers bootstrap:

   ```python
   for module in (build, container, ..., <name>):
       for task in module.namespace.tasks.values():
           task.pre.insert(0, bootstrap_task)
   ```

## Calling from another task

Modules compose via direct function calls — they don't reach into each other's Invoke internals:

```python
from .build import build as build_task

@task
def my_thing(context):
    build_task(context)   # plain call
    ...
```

## Conventions

- One module per concern. Lint covers all linters; security covers all security tools. Don't sprawl.
- Side effects via `execute(context, '...')` so failures abort the run cleanly.
- Don't catch `SystemExit` — let the `run_steps()` runner accumulate failures.
- Don't use leading underscores on module-level function names; the project's pylint config disallows it.

## See also

- [Reference: Invoke task catalog](../reference/invoke-tasks.md) — what already exists.
- [The _CI tasks architecture](../explanation/the-ci-tasks-architecture.md) — design rationale for the `@logged` + `IndentingStream` plumbing.
