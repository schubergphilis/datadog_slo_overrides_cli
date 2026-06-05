"""Compose a CycloneDX 1.7 SBOM covering runtime, dev, vendored, and pipeline components."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
import warnings
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, distribution, metadata, version
from pathlib import Path
from typing import NamedTuple

from cyclonedx.model import ExternalReference, ExternalReferenceType, HashAlgorithm, HashType, Property, XsUri
from cyclonedx.model.bom import Bom
from cyclonedx.model.component import Component, ComponentScope, ComponentType
from cyclonedx.model.contact import OrganizationalContact, OrganizationalEntity
from cyclonedx.model.license import DisjunctiveLicense, LicenseExpression, LicenseRepository
from cyclonedx.model.lifecycle import LifecyclePhase, LifecycleRepository, PredefinedLifecycle
from cyclonedx.model.tool import ToolRepository
from cyclonedx.output.json import JsonV1Dot7
from packageurl import PackageURL

from .configuration import PROJECT_NAME, SBOM_FILE, UV_LOCK, VENDOR_DIR, VENDOR_TXT
from .gitlab import iter_pipeline_components
from .shared import PipelineComponent

REQUIREMENT_PATTERN = re.compile(r'^([A-Za-z0-9._\-]+)==([A-Za-z0-9._\-+]+)')

# Text-based SPDX detection covers the licenses our runtime/dev/vendored sets
# actually ship under. Order matters: BSD-3 must precede BSD-2 (3-clause text
# is a superset).
SPDX_TEXT_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (('Apache License', 'Version 2.0'), 'Apache-2.0'),
    (('Permission is hereby granted, free of charge',), 'MIT'),
    (('Redistribution and use in source and binary forms', 'Neither the name'), 'BSD-3-Clause'),
    (('Redistribution and use in source and binary forms',), 'BSD-2-Clause'),
    # MPL appears as both "Version 2.0" (header pages) and "v. 2.0" (inline
    # licence-block snippets used in projects like certifi). Matching the
    # distinctive name alone is enough; the only widely-used MPL is 2.0.
    (('Mozilla Public License',), 'MPL-2.0'),
    (('Python Software Foundation License',), 'PSF-2.0'),
    (('ISC License',), 'ISC'),
)

# Trove classifier → SPDX mapping. Used as a last-resort fallback for packages
# that publish only a `License :: …` classifier (no License-Expression, no
# License header, no readable LICENSE-File). The mapping reflects the most
# common open-source intent; ambiguous classifiers (e.g. plain "BSD License")
# resolve to the most permissive plausible identifier.
CLASSIFIER_TO_SPDX: dict[str, str] = {
    'License :: OSI Approved :: Apache Software License': 'Apache-2.0',
    'License :: OSI Approved :: MIT License': 'MIT',
    'License :: OSI Approved :: BSD License': 'BSD-3-Clause',
    'License :: OSI Approved :: ISC License (ISCL)': 'ISC',
    'License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)': 'MPL-2.0',
    'License :: OSI Approved :: Python Software Foundation License': 'PSF-2.0',
    'License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)': 'LGPL-3.0',
    'License :: OSI Approved :: GNU Lesser General Public License v2.1 (LGPLv2.1)': 'LGPL-2.1',
    'License :: OSI Approved :: GNU General Public License v3 (GPLv3)': 'GPL-3.0',
    'License :: OSI Approved :: GNU General Public License v2 (GPLv2)': 'GPL-2.0',
}


class PinnedRequirement(NamedTuple):
    """A single `name==version` requirement, normalised to lowercase name."""

    name: str
    version: str


def uv_export(*extra_args: str) -> str:
    """Run `uv export` with the given extra flags and return its stdout."""
    result = subprocess.run(
        ['uv', 'export', '--format', 'requirements-txt', '--no-hashes', '--no-header', *extra_args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f'uv export failed: {result.stderr}', file=sys.stderr)
        raise SystemExit(1)
    return result.stdout


def iter_runtime_requirements() -> Iterator[PinnedRequirement]:
    """Yield runtime dependencies from `uv export --no-dev` (lockfile-pinned, dev groups excluded)."""
    yield from iter_requirements_lines(uv_export('--no-dev').splitlines())


def iter_dev_requirements() -> Iterator[PinnedRequirement]:
    """Yield dev-only dependencies (full lockfile minus the runtime set)."""
    runtime = set(iter_runtime_requirements())
    for req in iter_requirements_lines(uv_export('--all-groups').splitlines()):
        if req not in runtime:
            yield req


def iter_vendored_requirements() -> Iterator[PinnedRequirement]:
    """Yield vendored CI deps from `_CI/lib/vendor.txt` (pip-compile output)."""
    if not VENDOR_TXT.exists():
        return
    yield from iter_requirements_lines(VENDOR_TXT.read_text(encoding='utf-8').splitlines())


def iter_requirements_lines(lines: Iterable[str]) -> Iterator[PinnedRequirement]:
    """Parse `name==version` requirements out of a sequence of requirements.txt-style lines."""
    for raw in lines:
        clean = raw.split('#', 1)[0].strip()
        if not clean or clean.startswith('-'):
            continue
        match = REQUIREMENT_PATTERN.match(clean)
        if not match:
            continue
        yield PinnedRequirement(name=match.group(1).lower(), version=match.group(2))


def read_project_metadata() -> dict:
    """Return the parsed `[project]` block from pyproject.toml."""
    data = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
    return data['project']


def origin_repo_url() -> str | None:
    """Return the project's origin remote URL, or None when not in a git repo."""
    try:
        result = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    if url.startswith('git@'):
        url = url.replace(':', '/', 1).replace('git@', 'https://', 1)
    return url.removesuffix('.git') or None


def uv_version() -> str:
    """Return the version of the `uv` binary on PATH (best-effort)."""
    try:
        result = subprocess.run(['uv', '--version'], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 'unknown'
    if result.returncode != 0:
        return 'unknown'
    return result.stdout.strip().removeprefix('uv ').strip() or 'unknown'


def load_uv_lock_index() -> dict:
    """Return a `{name: package_dict}` index of `uv.lock`."""
    if not UV_LOCK.exists():
        return {}
    data = tomllib.loads(UV_LOCK.read_text(encoding='utf-8'))
    return {pkg['name'].lower(): pkg for pkg in data.get('package', [])}


UV_LOCK_INDEX = load_uv_lock_index()


def detect_spdx_id(text: str) -> str | None:
    """Best-effort SPDX identifier detection from license text."""
    for needles, spdx in SPDX_TEXT_HINTS:
        if all(needle in text for needle in needles):
            return spdx
    return None


def license_from_dist_files(name: str) -> str | None:
    """Read any LICENSE* file shipped in the package's dist-info and detect SPDX from its text."""
    try:
        dist = distribution(name)
    except PackageNotFoundError:
        return None
    for file in dist.files or []:
        if 'license' not in file.name.lower():
            continue
        try:
            text = file.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        spdx = detect_spdx_id(text or '')
        if spdx:
            return spdx
    return None


def license_from_classifiers(name: str) -> str | None:
    """Map any `License :: OSI Approved :: …` trove classifier to an SPDX id."""
    try:
        meta = metadata(name)
    except PackageNotFoundError:
        return None
    for classifier in meta.get_all('Classifier') or []:
        spdx = CLASSIFIER_TO_SPDX.get(classifier.strip())
        if spdx:
            return spdx
    return None


def lookup_pypi_license(name: str) -> LicenseRepository | None:
    """Declared license for an installed PyPI distribution.

    Tries, in order:
        1. ``License-Expression`` METADATA header (PEP 639).
        2. ``License`` METADATA header (legacy, free-form).
        3. The text of any ``LICENSE*`` file shipped in the dist-info — SPDX
           detected from the content.
        4. A ``License :: …`` Trove classifier mapped via ``CLASSIFIER_TO_SPDX``.
    """
    try:
        meta = metadata(name)
    except PackageNotFoundError:
        return None
    expression = meta.get('License-Expression')
    if expression:
        return LicenseRepository([LicenseExpression(value=expression)])
    name_value = meta.get('License')
    if name_value and name_value.strip().lower() != 'unknown':
        spdx = detect_spdx_id(name_value)
        if spdx:
            return LicenseRepository([DisjunctiveLicense(id=spdx)])
        return LicenseRepository([DisjunctiveLicense(name=name_value.splitlines()[0].strip())])
    spdx = license_from_dist_files(name) or license_from_classifiers(name)
    if spdx:
        return LicenseRepository([DisjunctiveLicense(id=spdx)])
    return None


def vendor_directory_for(name: str) -> Path | None:
    """Find the on-disk vendored directory for `name`.

    The vendoring tool stores packages under their importable Python module
    name, not their PyPI distribution name. The two diverge in several common
    ways: hyphens become underscores (``charset-normalizer`` →
    ``charset_normalizer``); some package names lose their ``-py`` suffix
    (``markdown-it-py`` → ``markdown_it``, ``rpds-py`` → ``rpds``); some lose
    every hyphen entirely (``pip-tools`` → ``piptools``). Try each variant in
    order.
    """
    candidates = []
    seen: set[str] = set()

    def push(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    push(name)
    push(name.replace('-', '_'))
    push(name.replace('-', ''))
    stripped = name.removesuffix('-py')
    push(stripped)
    push(stripped.replace('-', '_'))
    push(stripped.replace('-', ''))
    for candidate in candidates:
        path = VENDOR_DIR / candidate
        if path.is_dir():
            return path
    return None


def lookup_vendored_license(name: str) -> LicenseRepository | None:
    """SPDX identifier for a vendored package.

    Primary path: detect from any ``LICENSE*`` file in the vendored directory.
    Fallback path: many vendored packages are also installed as transitive
    dev/lint/test deps in the venv (e.g. ``referencing`` comes in through
    ``cyclonedx-python-lib``'s json-validation extra). When the vendored
    directory carries no LICENSE we defer to ``lookup_pypi_license`` — same
    name + version are pinned identically in both worlds.
    """
    vendor_dir = vendor_directory_for(name)
    if vendor_dir is not None:
        license_files = sorted(vendor_dir.glob('LICENSE*'))
        if license_files:
            spdx_ids = {detect_spdx_id(p.read_text(encoding='utf-8', errors='replace')) for p in license_files}
            spdx_ids.discard(None)
            if spdx_ids:
                if len(spdx_ids) == 1:
                    return LicenseRepository([DisjunctiveLicense(id=spdx_ids.pop())])
                return LicenseRepository([LicenseExpression(value=' OR '.join(sorted(spdx_ids)))])
    return lookup_pypi_license(name)


def lookup_pypi_hash(name: str, ver: str):
    """SHA-256 hash from `uv.lock` for the matching name+version."""
    entry = UV_LOCK_INDEX.get(name)
    if not entry or entry.get('version') != ver:
        return None
    for wheel in entry.get('wheels', []) or []:
        hash_value = wheel.get('hash')
        if hash_value and hash_value.startswith('sha256:'):
            return HashType(alg=HashAlgorithm.SHA_256, content=hash_value.removeprefix('sha256:'))
    sdist = entry.get('sdist')
    if sdist and isinstance(sdist, dict):
        hash_value = sdist.get('hash')
        if hash_value and hash_value.startswith('sha256:'):
            return HashType(alg=HashAlgorithm.SHA_256, content=hash_value.removeprefix('sha256:'))
    return None


def pypi_website_reference(name: str, ver: str):
    """`type=website` external reference pointing at the PyPI project page."""
    return ExternalReference(
        type=ExternalReferenceType.WEBSITE,
        url=XsUri(f'https://pypi.org/project/{name}/{ver}/'),
    )


def pypi_component(req: PinnedRequirement, scope: ComponentScope, *, license_source: str):
    """Build a PyPI-typed CycloneDX component with license, hash, and external_ref.

    license_source: 'pypi' (uses importlib.metadata) or 'vendored' (uses LICENSE-text detection).
    """
    if license_source == 'vendored':
        licenses = lookup_vendored_license(req.name)
        hashes = None
    else:
        licenses = lookup_pypi_license(req.name)
        hashes = lookup_pypi_hash(req.name, req.version)
    return Component(
        name=req.name,
        version=req.version,
        type=ComponentType.LIBRARY,
        scope=scope,
        purl=PackageURL(type='pypi', name=req.name, version=req.version),
        licenses=licenses,
        hashes=[hashes] if hashes else None,
        external_references=[pypi_website_reference(req.name, req.version)],
    )


def pipeline_external_reference(spec: PipelineComponent):
    """Return a CycloneDX external_reference pointing at the pipeline component's host page."""
    if spec.purl.startswith('pkg:github/'):
        return ExternalReference(type=ExternalReferenceType.VCS, url=XsUri(f'https://github.com/{spec.name}'))
    if spec.purl.startswith('pkg:docker/'):
        return ExternalReference(type=ExternalReferenceType.DISTRIBUTION, url=XsUri(f'https://{spec.name}'))
    return None


def pipeline_to_component(spec: PipelineComponent):
    """Build a CycloneDX component from a host-supplied pipeline-component spec."""
    references = [ref for ref in (pipeline_external_reference(spec),) if ref is not None]
    return Component(
        name=spec.name,
        version=spec.version,
        type=ComponentType.LIBRARY,
        scope=ComponentScope.EXCLUDED,
        purl=PackageURL.from_string(spec.purl),
        external_references=references or None,
    )


def project_authors(project_meta: dict):
    """Map `pyproject.toml [project.authors]` into OrganizationalContact objects."""
    return [
        OrganizationalContact(name=author.get('name'), email=author.get('email'))
        for author in project_meta.get('authors') or []
        if author.get('name') or author.get('email')
    ]


def project_supplier(project_meta: dict):
    """Derive a supplier OrganizationalEntity from the first author entry."""
    authors = project_meta.get('authors') or []
    if not authors:
        return None
    first = authors[0]
    name = first.get('name')
    if not name:
        return None
    return OrganizationalEntity(name=name, contacts=project_authors(project_meta))


def root_external_references():
    """Return a vcs external_reference pointing at the project's git remote, when reachable."""
    url = origin_repo_url()
    if not url:
        return []
    return [ExternalReference(type=ExternalReferenceType.VCS, url=XsUri(url))]


def build_root_component(project_meta: dict):
    """The project itself, with bom-ref + vcs external_ref + license."""
    name = project_meta['name']
    ver = project_meta['version']
    licenses = None
    license_text = project_meta.get('license')
    if isinstance(license_text, str) and license_text and license_text.lower() != 'none':
        licenses = LicenseRepository([DisjunctiveLicense(id=license_text)])
    return Component(
        name=name,
        version=ver,
        type=ComponentType.LIBRARY,
        scope=ComponentScope.REQUIRED,
        purl=PackageURL(type='pypi', name=name, version=ver),
        licenses=licenses,
        external_references=root_external_references() or None,
    )


def build_build_environment_component():
    """Synthetic component representing the build environment (vendored + pipeline)."""
    return Component(
        name=f'{PROJECT_NAME}-build-environment',
        version='0',
        type=ComponentType.PLATFORM,
        scope=ComponentScope.EXCLUDED,
        description=(
            'Composite of the vendored CI tooling (_CI/lib/vendor/) and the chosen-host '
            'pipeline components used to build, test, and publish this project. Not shipped '
            'in the wheel; tracked here so the SBOM carries the full build provenance.'
        ),
    )


def build_metadata_tools():
    """Tools that produced this SBOM: cyclonedx-python-lib + uv + the project's own generator."""

    def safe_pkg_version(pkg: str) -> str:
        try:
            return version(pkg)
        except PackageNotFoundError:
            return 'unknown'

    cyclonedx_version = safe_pkg_version('cyclonedx-python-lib')
    return ToolRepository(
        components=[
            Component(
                name='cyclonedx-python-lib',
                version=cyclonedx_version,
                type=ComponentType.LIBRARY,
                purl=PackageURL(type='pypi', name='cyclonedx-python-lib', version=cyclonedx_version),
            ),
            Component(
                name='uv',
                version=uv_version(),
                type=ComponentType.APPLICATION,
                purl=PackageURL(type='generic', name='uv', version=uv_version()),
            ),
            Component(
                name=f'{PROJECT_NAME}-sbom-generator',
                version=read_project_metadata().get('version', '0.0.0'),
                type=ComponentType.APPLICATION,
                purl=PackageURL(type='generic', name=f'{PROJECT_NAME}-sbom-generator'),
            ),
        ]
    )


def build_lifecycles():
    """Declare this SBOM was produced during the build phase."""
    return LifecycleRepository([PredefinedLifecycle(phase=LifecyclePhase.BUILD)])


def build_bom():
    """Assemble the CycloneDX 1.7 Bom with metadata, components, and a two-level dependency graph."""
    project_meta = read_project_metadata()
    root = build_root_component(project_meta)
    build_env = build_build_environment_component()

    bom = Bom()
    bom.metadata.timestamp = datetime.now(UTC)
    bom.metadata.component = root
    bom.metadata.tools = build_metadata_tools()
    bom.metadata.lifecycles = build_lifecycles()
    bom.metadata.authors = project_authors(project_meta)
    supplier = project_supplier(project_meta)
    if supplier is not None:
        bom.metadata.supplier = supplier
    bom.metadata.properties.add(Property(name='paleofuturistic:git_hosting_service', value='gitlab'))

    bom.components.add(build_env)
    runtime_components: list = []
    dev_components: list = []
    vendored_components: list = []
    pipeline_component_objects: list = []
    seen: set[tuple[str, str]] = set()

    def add(req: PinnedRequirement, scope: ComponentScope, license_source: str, target: list) -> None:
        if (req.name, req.version) in seen:
            return
        seen.add((req.name, req.version))
        component = pypi_component(req, scope, license_source=license_source)
        bom.components.add(component)
        target.append(component)

    for req in iter_runtime_requirements():
        add(req, ComponentScope.REQUIRED, 'pypi', runtime_components)
    for req in iter_dev_requirements():
        add(req, ComponentScope.OPTIONAL, 'pypi', dev_components)
    for req in iter_vendored_requirements():
        add(req, ComponentScope.EXCLUDED, 'vendored', vendored_components)
    for spec in iter_pipeline_components():
        component = pipeline_to_component(spec)
        bom.components.add(component)
        pipeline_component_objects.append(component)

    # Two-level dependency graph: the project depends on its runtime + dev deps
    # and on the build-environment; the build-environment depends on the
    # vendored + pipeline components.
    bom.register_dependency(root, [*runtime_components, *dev_components, build_env])
    bom.register_dependency(build_env, [*vendored_components, *pipeline_component_objects])
    return bom


def render_sbom() -> str:
    """Return the CycloneDX 1.7 JSON serialization of the assembled Bom."""
    with warnings.catch_warnings():
        # cyclonedx-python-lib warns when the root component declares zero
        # runtime dependencies — which is the legitimate case for a freshly
        # generated project with `dependencies = []`. The warning is
        # informational, not actionable for an empty-deps project.
        warnings.filterwarnings(
            'ignore',
            message='The Component this BOM is describing .* has no defined dependencies',
            category=UserWarning,
        )
        return JsonV1Dot7(build_bom()).output_as_string(indent=2)


def write_sbom(target: Path = SBOM_FILE) -> None:
    """Write the CycloneDX SBOM to `target`, creating parent dirs as needed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_sbom() + '\n', encoding='utf-8')


VALIDATE_SCRIPT = (
    'import sys\n'
    'from pathlib import Path\n'
    'from cyclonedx.validation.json import JsonStrictValidator\n'
    'from cyclonedx.schema import SchemaVersion\n'
    'validator = JsonStrictValidator(SchemaVersion.V1_7)\n'
    'error = validator.validate_str(Path(sys.argv[1]).read_text(encoding="utf-8"))\n'
    'if error is not None:\n'
    '    print(str(error), file=sys.stderr)\n'
    '    sys.exit(1)\n'
)


def validate_sbom(target: Path = SBOM_FILE) -> list[str]:
    """Validate the SBOM at `target` against the CycloneDX 1.7 JSON schema.

    Runs in a `uv run python` subprocess so the venv-installed
    cyclonedx-python-lib + jsonschema win over the vendored CI libs that the
    workflow.cmd launcher places earlier on sys.path. Returns a list of
    error messages (empty when the SBOM is valid).
    """
    result = subprocess.run(
        ['uv', 'run', 'python', '-c', VALIDATE_SCRIPT, str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return []
    return [(result.stderr or result.stdout).strip()]
