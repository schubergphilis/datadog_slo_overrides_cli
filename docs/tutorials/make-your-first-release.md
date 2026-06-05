# Make your first release

Picks up after [First-run setup](first-run-setup.md): you have a green dev cycle. This tutorial cuts a real release — branch, bump, changelog, tag, push.

## Step 1 — Write a feature

Open `src/datadog_slo_overrides_cli/datadog_slo_overrides_cli.py` and edit `hello()`:

```python
def hello(someone: str = 'you') -> str:
    """Greet `someone` and announce the project."""
    return f'Hello {someone} from datadog_slo_overrides_cli!'
```

Update the smoke test under `tests/` to match. Confirm the dev cycle is still green:

```bash
./workflow.cmd format && ./workflow.cmd lint && ./workflow.cmd test
```

## Step 2 — Commit with Conventional Commits

```bash
git add -A
git commit -m "feat: greet someone by name"
```

The prefix does **not** drive the version bump — you'll pass that explicitly in the next step. What it drives is the **release notes**: when commitizen generates the changelog, it groups commits by prefix (`feat:` under "Features", `fix:` under "Bug Fixes", etc.). The lint step rejects any message that doesn't parse as Conventional Commits.

## Step 3 — Cut the release

```bash
./workflow.cmd release -i minor
```

`-i` is your explicit version-increment choice (`major`, `minor`, `patch`, `alpha`, `beta`, or `rc`). The template does not infer it from commit messages.

The `release` task is the orchestrator. In order, it:

1. **Validates** — working tree is clean and synced with origin, you're on `main`.
2. **Resolves the next version** — runs `cz bump --dry-run` to project the new version.
3. **Checks for collisions** — fails if the target tag or `release/<version>` branch already exists locally or on origin.
4. **Branches** — `git checkout -b release/<version>`.
5. **Bumps** — `cz bump` writes the new version into `pyproject.toml` and creates the tag.
6. **Updates the changelog** — `cz changelog` regenerates `docs/changelog.md` and commits it.
7. **Pushes** — branch and tag both go to origin.
8. **Opens the PR/MR** — GitHub: via API if `GITHUB_TOKEN` is set, otherwise prints a manual URL. GitLab: prints a manual URL.

Pass `--no-push` to keep the branch and tag local for inspection.

## Step 4 — Approve and merge

On your git host, approve the release PR/MR and merge with a merge commit (not a squash). The tag and bump land on `main`. If CI is configured for trusted publishing, `release.publish` fires automatically when the tag appears on `main` and your wheel lands on PyPI.

## Verify

- `pip install <your_project_slug>==<new_version>` from a fresh venv succeeds.
- `pypi.org/project/<your_project_slug>/` shows the new version.
- For Dependency Track users (see [Upload an SBOM to Dependency Track](../how-to/upload-an-sbom-to-dependency-track.md)): the project's Components tab reflects the new release.

## You're done

You've cut a release. Next:

- [Triage a security finding](../how-to/triage-a-security-finding.md) — what to do when pip-audit flags something.
- [SBOM and security model](../explanation/sbom-and-security-model.md) — what the release shipped beyond the wheel.
