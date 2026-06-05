# Upload an SBOM to Dependency Track

If `integrate_dependency_track` was enabled at generation time, the release pipeline uploads a CycloneDX SBOM to your Dependency Track server on every release. This page covers wiring it up and verifying it works.

## The required environment

| Variable | Example | Source |
| --- | --- | --- |
| `OWASP_DT_URL` | `https://dt.example.com` | Your Dependency Track instance |
| `OWASP_DT_API_KEY` | `odt_…` | DT → Administration → Access Management → Teams |
| `OWASP_DT_PROJECT_UUID` | UUID | DT → Projects → your project |

In CI, set these as secrets:

- **GitHub Actions**: Settings → Secrets and variables → Actions → New repository secret.
- **GitLab CI**: Settings → CI/CD → Variables → Add variable, mark each as **Masked** and **Protected**.

Locally, export them in your shell before running the upload task.

## Running the upload

The release pipeline calls `secure.sbom-upload` after `release.publish` succeeds. To run it manually:

```bash
./workflow.cmd secure.sbom-upload
```

What it does:

1. Composes a fresh CycloneDX 1.7 SBOM via `./workflow.cmd secure.sbom-extract --write` (runtime deps + vendored CI tooling + chosen-host pipeline components).
2. Writes it to `src/<project_slug>/sbom.cdx.json` — the same file `uv build` later ships inside the wheel.
3. POSTs that file to `<OWASP_DT_URL>/api/v1/bom` with the API key and project UUID.
4. Prints the response.

## Verify

After upload, in the DT UI:

1. Open your project.
2. The **Components** tab should refresh within a few seconds; the count matches your lockfile.
3. **Audit Vulnerabilities** lists any CVEs DT knows about for your dependency set.

## Wasn't enabled at generation time

Re-run `uvx copier update --trust` and answer `y` to `integrate_dependency_track`. See [copier's update docs](https://copier.readthedocs.io/en/stable/updating/) for the full workflow.

## What if I don't have a Dependency Track server?

You still get a CycloneDX SBOM on every release — it's embedded **inside the wheel** at `<project_slug>/sbom.cdx.json`. Extract it with `unzip -p <wheel> <project_slug>/sbom.cdx.json` or read it via `importlib.resources.files('<project_slug>') / 'sbom.cdx.json'`. You can upload it manually elsewhere, or use any other SCA tool that consumes CycloneDX.

The Dependency Track integration is an optional automation, not a prerequisite for the security pipeline.

## See also

- [SBOM and security model](../explanation/sbom-and-security-model.md) — what an SBOM is for and why we ship one by default.
- [Triage a security finding](triage-a-security-finding.md) — what to do when DT (or pip-audit) finds something.
