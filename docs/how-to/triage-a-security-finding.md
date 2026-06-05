# Triage a security finding

`./workflow.cmd secure` runs [pip-audit](https://github.com/pypa/pip-audit) against the lockfile. When it reports a vulnerability you have three responses: upgrade, override with an expiry, or accept and document.

## 1. Upgrade if possible

The vast majority of findings have a fixed version available. Check the report:

```bash
./workflow.cmd secure.audit
```

If a fix exists upstream:

```bash
uv lock --upgrade-package <vulnerable-package>
./workflow.cmd secure.audit    # confirm clean
git commit -am "fix: bump <package> for <CVE-id>"
```

## 2. Override with an expiry (recommended for "can't upgrade yet")

The `.security-overrides` file at the project root lists allowed-for-now vulnerabilities with expiry dates. Each line is:

```
<VULN-ID> <YYYY-MM-DD> <justification>
```

For example:

```
PYSEC-2024-1234 2026-06-01 upstream fix pending in v2.3 — see https://github.com/pkg/issue/42
```

After the expiry date, `secure.audit` fails again, forcing a re-triage. Don't extend overrides without revisiting the underlying issue.

## 3. Accept and document (rare)

For findings that don't apply to your usage — e.g. the vulnerable code path is gated on a feature you don't use — write an override with an extra-long justification and a far-future expiry. Keep this rare; the next maintainer needs to trust the override list.

## Re-running after a triage

```bash
./workflow.cmd secure
```

This runs the full security pass: audit, SBOM generation, and (if configured) Dependency Track upload. Audit failures abort the run before SBOM upload.

## What gets uploaded to Dependency Track

If `integrate_dependency_track` was enabled at generation time, every successful `secure` run uploads a CycloneDX SBOM. Vulnerabilities you've overridden locally still appear in DT as known issues — DT tracks the full state of your project regardless of your local override list.

## See also

- [Upload an SBOM to Dependency Track](upload-an-sbom-to-dependency-track.md) — wiring the upload step.
- [SBOM and security model](../explanation/sbom-and-security-model.md) — why pip-audit + SBOM + DT layer together.
