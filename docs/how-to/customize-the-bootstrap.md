# Customize the bootstrap

The first-run setup that every workflow command depends on lives in `_CI/tasks/bootstrap.py`. It's a list of steps, each declarative — adding one is appending to the list.

## The shape of a step

```python
BootstrapStep(
    name='pre-commit hooks',
    action=install_pre_commit,
    prompt='Install pre-commit hooks? [y/N] ',
    ci_behavior='skip',
)
```

| Field | Purpose |
| --- | --- |
| `name` | Display label in the bootstrap output. |
| `action` | Callable invoked when the step runs. Receives the Invoke `Context`. |
| `prompt` | Shown interactively if `CI` env var is unset. Empty string means run unconditionally. |
| `ci_behavior` | `'run'` or `'skip'` — what to do under `CI=1`. |

## Adding a step

1. Define the action:

   ```python
   def install_dev_certificates(context):
       execute(context, 'mkcert -install')
   ```

2. Append to the `STEPS` list:

   ```python
   STEPS.append(
       BootstrapStep(
           name='dev certificates',
           action=install_dev_certificates,
           prompt='Install dev TLS certificates? [y/N] ',
           ci_behavior='skip',
       )
   )
   ```

3. Run `./workflow.cmd bootstrap --force` to test it.

## Idempotency

Every action must be idempotent. The bootstrap sentinel (`_CI/.bootstrapped`) skips the *whole* bootstrap on re-runs, but `--force` re-runs everything — and your action will fire again. Use marker files or `if not installed: install` checks.

## Skip in CI

Most one-time interactive setup (pre-commit hooks, certificate install, IDE config) doesn't make sense in CI. Set `ci_behavior='skip'` for those. Steps that genuinely need to run in CI (e.g. fetching submodules) get `'run'`.

## What not to do

- Don't write business logic into the bootstrap. It's for environment setup only.
- Don't depend on user state (env vars, working directory contents beyond the project) — the bootstrap may run as the first action on a fresh checkout.
- Don't catch failures and continue silently. If a step fails, bootstrap should fail. The next workflow command would otherwise run in a half-set-up environment.

## See also

- [The _CI tasks architecture](../explanation/the-ci-tasks-architecture.md) — why bootstrap is wired as a pre-task on every other task.
- [Reference: Invoke task catalog](../reference/invoke-tasks.md) — the full task list.
