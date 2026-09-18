"""Build a new provider solely from its package metadata for integration tests."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def build_local_call(
    directory: Path,
    repository: Path,
    *,
    increment: int = 1,
    name: str = "local-call",
    backends: tuple[str, ...] = ("local_env",),
) -> tuple[Path, Path]:
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
        [{"fixture_id": "integer", "canonical_input": {"n": 2}, "measured_bucket_id": "size=one"}],
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
                    "declared_input_buckets": ["size=one"],
                    "bucket_conformance": {"size=one": "integer"},
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
            "governed_definitions": [],
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
