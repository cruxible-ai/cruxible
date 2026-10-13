"""Build a new provider solely from its package metadata for integration tests."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

#: Bucket metadata the pre-removal installer refused three ways: the declared
#: selector names a class the vocabulary lacks, no selector names a fixture, and
#: the one fixture's measured bucket is not a bucket of the vocabulary. Core
#: carries it as inert data; installing, binding and running ignore it.
INERT_BUCKET_METADATA: dict[str, Any] = {
    "declared_input_buckets": ["size=two"],
    "bucket_conformance": {},
    "fixtures": [
        {"fixture_id": "integer", "canonical_input": {"n": 2}, "measured_bucket_id": "size=huge"}
    ],
}


def forbid_bucket_semantics(monkeypatch: Any) -> None:
    """Make the provider runtime's validating reader and bucket model raise.

    Core reads package metadata itself, with bucket data inert; if any path it
    runs reached one of these, the test would fail here.
    """

    from cruxible_provider_runtime import buckets, registration, registry, wheels

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a bucket-semantic provider-runtime routine was reached")

    for module, name in (
        (wheels, "wheel_registration"),
        (registration, "load_registration"),
        (registration, "registration_from_distribution"),
        (registration, "parse_bucket_id"),
        (registration, "load_bucket_vocabulary"),
        (buckets, "parse_bucket_id"),
        (registry, "load_bucket_vocabulary"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(buckets.BucketSelector, "parse", classmethod(forbidden))
    monkeypatch.setattr(buckets.BucketSelector, "matches", forbidden)


def build_local_call(
    directory: Path,
    repository: Path,
    *,
    increment: int = 1,
    name: str = "local-call",
    backends: tuple[str, ...] = ("local_env",),
    fixture_id: str = "integer",
    bucket_metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """A ``local.increment`` package. ``bucket_metadata`` replaces its declared
    selectors, selector -> fixture claims and fixtures as given, unchecked."""
    from cruxible_provider_runtime.canonical import domain_digest

    project = directory / f"{name}-{increment}"
    module = project / "src/local_call"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (project / "pyproject.toml").write_text("""[project]
name = "local-call"
version = "0.2.0"
requires-python = ">=3.11"
dependencies = ["cruxible-provider-runtime>=0.2.0"]
[project.entry-points."cruxible.providers"]
"local.increment" = "local_call.operation:Increment"
[build-system]
requires = ["hatchling==1.32.1"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/local_call"]
""")
    resources = {}

    def resource(name, value):
        content = (
            value.encode() if isinstance(value, str) else json.dumps(value, sort_keys=True).encode()
        )
        (module / name).write_bytes(content)
        result = {"path": name, "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
        resources[name] = result
        return result

    code = """from cruxible_provider_runtime.provider_api import ProviderResult

def classify(value):
    return {"size":"one"}

class Increment:
    interface_id = "local.increment"
    def __call__(self, context):
        return ProviderResult.ok({"n":context.input["n"]+INCREMENT})
""".replace("INCREMENT", str(increment))
    resource("operation.py", code)
    schema = {"fields": {"n": {"type": "int"}}, "allow_extra": False}
    definition = {
        "interface_id": "local.increment",
        "version": 2,
        "effect_class": "pure",
        "contracts": {"input": schema, "output": schema},
    }
    digest = domain_digest("cruxible.interface.stub.v1", definition)
    resource("contract.json", definition)
    resource(
        "vocabulary.json",
        {
            "interface_id": "local.increment",
            "version": 1,
            "status": "draft",
            "description": "One integer operation",
            "dimensions": [
                {
                    "name": "size",
                    "description": "input shape",
                    "classes": [{"id": "one", "description": "one integer"}],
                }
            ],
        },
    )
    resource(
        "fixtures.json",
        [{"fixture_id": fixture_id, "canonical_input": {"n": 2}, "measured_bucket_id": "size=one"}]
        if bucket_metadata is None
        else bucket_metadata["fixtures"],
    )
    resource(
        "manifest.json",
        {
            "schema_version": 1,
            "provider_id": "local-call",
            "distribution": {"name": "local-call", "version": "0.2.0"},
            "entrypoint_group": "cruxible.providers",
            "supported_protocol_majors": [1],
            "implementations": [
                {
                    "interface_id": "local.increment",
                    "interface_digest": digest,
                    "entrypoint": "local_call.operation:Increment",
                    "backends": list(backends),
                    "requires_extras": [],
                    "declared_input_buckets": ["size=one"]
                    if bucket_metadata is None
                    else bucket_metadata["declared_input_buckets"],
                    "bucket_conformance": {"size=one": fixture_id}
                    if bucket_metadata is None
                    else bucket_metadata["bucket_conformance"],
                    "declared_endpoints": [],
                    "capture_contract_families": [],
                    "deterministic": True,
                    "side_effects": False,
                }
            ],
        },
    )
    resource(
        "registration.json",
        {
            "schema_version": 1,
            "manifest": resources["manifest.json"],
            "interfaces": [
                {
                    "interface_id": "local.increment",
                    "interface_digest": digest,
                    "definition": resources["contract.json"],
                    "vocabulary": resources["vocabulary.json"],
                    "classifier": {
                        "identity": "local.increment.input",
                        "version": 1,
                        "entrypoint": "local_call.operation:classify",
                        "source": resources["operation.py"],
                    },
                    "fixtures": resources["fixtures.json"],
                    "predecessors": [],
                }
            ],
            "runtime_requirements": [],
        },
    )
    lock = project / "uv.lock"
    lock.write_text(
        (repository / "packages/cruxible-provider-workspace/uv.lock")
        .read_text()
        .replace("cruxible-provider-workspace", "local-call")
    )
    if name != "local-call":
        for file in (project / "pyproject.toml", lock):
            file.write_text(file.read_text().replace("local-call", name))
        manifest = json.loads((module / "manifest.json").read_text())
        manifest["provider_id"] = name
        manifest["distribution"]["name"] = name
        reference = resource("manifest.json", manifest)
        descriptor = json.loads((module / "registration.json").read_text())
        descriptor["manifest"] = reference
        resource("registration.json", descriptor)
    uv = shutil.which("uv")
    assert uv is not None
    output = project / "dist"
    subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(output), str(project)],
        check=True,
        capture_output=True,
    )
    return next(output.glob("*.whl")), lock


#: The web.fetch buckets an alternative implementation claims by default: the one
#: static light selector of core's four, under the fixture id core's proof names.
STATIC_LIGHT = ("source_kind=static_html;access=*;page_weight=light", "web-fetch-static-light")

#: The four conformance fixtures core's web.fetch registration's proofs name. The
#: provider runtime 0.2.x package format still requires a package to export
#: fixtures and a classifier beside its definition; core treats them as inert data.
WEB_FETCH_PACKAGE_FIXTURES: tuple[dict[str, object], ...] = (
    {
        "fixture_id": "web-fetch-api-json",
        "canonical_input": {"url": "https://fixture.invalid/api/v1/measurements.json"},
        "measured_bucket_id": "source_kind=api_json;access=public;page_weight=light",
    },
    {
        "fixture_id": "web-fetch-rendered",
        "canonical_input": {"url": "https://fixture.invalid/dashboard", "render": True},
        "measured_bucket_id": "source_kind=js_rendered;access=public;page_weight=light",
    },
    {
        "fixture_id": "web-fetch-static-light",
        "canonical_input": {"url": "https://fixture.invalid/articles/tide-gauge-recalibration"},
        "measured_bucket_id": "source_kind=static_html;access=public;page_weight=light",
    },
    {
        "fixture_id": "web-fetch-static-medium",
        "canonical_input": {
            "url": "https://fixture.invalid/reports/water-quality",
            "max_bytes": 1048576,
        },
        "measured_bucket_id": "source_kind=static_html;access=public;page_weight=medium",
    },
)


def build_web_fetch_alternative(
    directory: Path,
    repository: Path,
    *,
    name: str = "fetch-alt",
    claims: tuple[tuple[str, str], ...] = (STATIC_LIGHT,),
) -> tuple[Path, Path]:
    """Another implementation of core's web.fetch v3 contract, as a third party ships it.

    It exports the exact v3 definition, core's vocabulary and the fixtures its
    claims name, with a classifier of its own (the package format requires one;
    nothing runs it), and embeds its lock so it also installs by name from an
    index. ``claims`` maps each declared selector to its fixture id.
    """

    from cruxible_provider_runtime.canonical import domain_digest

    from cruxible_core.providers.web_fetch import (
        WEB_FETCH_INTERFACE_DEFINITION,
        web_fetch_interface_registration,
    )

    # The lock is the workspace package's, renamed: the same version and runtime.
    version = "0.2.0"
    module_name = name.replace("-", "_")
    project = directory / name
    module = project / "src" / module_name
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (project / "pyproject.toml").write_text(f"""[project]
name = "{name}"
version = "{version}"
requires-python = ">=3.11"
dependencies = ["cruxible-provider-runtime>=0.2.0"]
[project.entry-points."cruxible.providers"]
"web.fetch" = "{module_name}.operation:Fetch"
[build-system]
requires = ["hatchling==1.32.1"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/{module_name}"]
[tool.hatch.build.targets.wheel.extra-metadata]
"uv.lock" = "uv.lock"
""")
    resources: dict[str, dict[str, str]] = {}

    def resource(file: str, value: object) -> dict[str, str]:
        content = (
            value.encode() if isinstance(value, str) else json.dumps(value, sort_keys=True).encode()
        )
        (module / file).write_bytes(content)
        resources[file] = {"path": file, "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
        return resources[file]

    resource(
        "operation.py",
        """from cruxible_provider_runtime.provider_api import ProviderResult


def classify(value):
    return {"source_kind": "static_html", "access": "public", "page_weight": "light"}


class Fetch:
    interface_id = "web.fetch"

    def __call__(self, context):
        return ProviderResult.failed("not_implemented", "a test double never fetches")
""",
    )
    definition = dict(WEB_FETCH_INTERFACE_DEFINITION)
    digest = domain_digest("cruxible.interface.stub.v1", definition)
    resource("contract.json", definition)
    # Core's vocabulary, read off its registration's inert bytes.
    vocabulary = dict(web_fetch_interface_registration().vocabulary_document)
    vocabulary["status"] = "draft"
    resource("vocabulary.json", vocabulary)
    claimed = {fixture_id for _selector, fixture_id in claims}
    resource(
        "fixtures.json",
        [fixture for fixture in WEB_FETCH_PACKAGE_FIXTURES if fixture["fixture_id"] in claimed],
    )
    resource(
        "manifest.json",
        {
            "schema_version": 1,
            "provider_id": name,
            "distribution": {"name": name, "version": version},
            "entrypoint_group": "cruxible.providers",
            "supported_protocol_majors": [1],
            "implementations": [
                {
                    "interface_id": "web.fetch",
                    "interface_digest": digest,
                    "entrypoint": f"{module_name}.operation:Fetch",
                    "backends": ["local_env"],
                    "requires_extras": [],
                    "declared_input_buckets": sorted(selector for selector, _id in claims),
                    "bucket_conformance": dict(claims),
                    "declared_endpoints": ["dynamic:target-from-run-input"],
                    "capture_contract_families": [],
                    "deterministic": False,
                    "side_effects": False,
                }
            ],
        },
    )
    resource(
        "registration.json",
        {
            "schema_version": 1,
            "manifest": resources["manifest.json"],
            "interfaces": [
                {
                    "interface_id": "web.fetch",
                    "interface_digest": digest,
                    "definition": resources["contract.json"],
                    "vocabulary": resources["vocabulary.json"],
                    "classifier": {
                        "identity": f"{name}.web.fetch",
                        "version": 1,
                        "entrypoint": f"{module_name}.operation:classify",
                        "source": resources["operation.py"],
                    },
                    "fixtures": resources["fixtures.json"],
                    "predecessors": [],
                }
            ],
            "runtime_requirements": [],
        },
    )
    lock = project / "uv.lock"
    lock.write_text(
        (repository / "packages/cruxible-provider-workspace/uv.lock")
        .read_text()
        .replace("cruxible-provider-workspace", name)
    )
    uv = shutil.which("uv")
    assert uv is not None
    output = project / "dist"
    subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(output), str(project)],
        check=True,
        capture_output=True,
    )
    return next(output.glob("*.whl")), lock


def write_file_index(root: Path, wheels: tuple[Path, ...]) -> tuple[str, ...]:
    """A PEP 503 file index listing ``wheels``; the index URLs an operator configures
    to install them by name (registry dependencies still resolve from PyPI)."""

    from packaging.utils import parse_wheel_filename

    simple = root / "simple"
    store = root / "files"
    store.mkdir(parents=True)
    for wheel in wheels:
        target = store / wheel.name
        shutil.copy(wheel, target)
        page = simple / str(parse_wheel_filename(wheel.name)[0])
        page.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with (page / "index.html").open("a") as stream:
            stream.write(
                f'<a href="{target.resolve().as_uri()}#sha256={digest}">{wheel.name}</a>\n'
            )
    return (
        simple.resolve().as_uri() + "/",
        store.resolve().as_uri() + "/",
        "https://pypi.org/simple",
        "https://files.pythonhosted.org/",
    )
