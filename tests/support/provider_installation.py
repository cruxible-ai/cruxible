"""Build a new provider package (protocol 2.0) solely from its metadata for integration tests."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

#: The one Contract ``local.increment`` reads and returns: ``{n: int}``. An
#: install pins it, so a world accepts it (``local_call_contract_files``) before
#: installing the package.
LOCAL_CALL_VALUE = {"name": "local.increment.value", "version": 1, "fields": {"n": {"type": "int"}}}
LOCAL_CALL_IN = "local.increment.value@1"
LOCAL_CALL_OUT = "local.increment.value@1"


def local_call_contract_files() -> dict[str, bytes]:
    """The accepted-tree member of ``local.increment``'s Contract."""

    from cruxible_client.contracts.shapes import Contract, contract_path, render_contract

    contract = Contract.model_validate(LOCAL_CALL_VALUE)
    return {contract_path(contract.name, contract.version): render_contract(contract)}


def accept_local_call_contract(instance_id: str, approve: Any) -> None:
    """Accept ``local.increment``'s Contract in a world before any local-call install.

    Every world that installs a package from ``build_local_call`` calls this
    first: an install pins the operation's Contracts to the accepted ones.
    ``approve(proposal_id)`` approves and activates the proposal the way the
    world's policy requires. A world that already holds the Contract is left
    as it is.
    """

    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from tests.core_support._candidate_support import submit_member_candidate

    instance = get_playbill_manager().get(instance_id)
    members = local_call_contract_files()
    if set(members) <= set(instance.tree_at(instance.accepted_coordinate().git_oid)):
        return
    proposal = submit_member_candidate(
        instance,
        members=members,
        actor_id="operator",
        proposal_name="local-call-contract",
        proposal_family="procedure",
        timestamp="2026-10-29T11:00:00.000000Z",
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    approve(proposal.admission.proposal_id)


def build_local_call(
    directory: Path,
    repository: Path,
    *,
    increment: int = 1,
    name: str = "local-call",
    backends: tuple[str, ...] = ("local_env",),
    operations: tuple[str, ...] = ("local.increment",),
    runtime_requirements: tuple[dict[str, Any], ...] = (),
) -> tuple[Path, Path]:
    """A provider runtime protocol 2.0 package: registration and manifest schema 2.

    One entrypoint, ``local_call.operation:Increment``, serves every name in
    ``operations``; the wheel's ``cruxible.providers`` entry points are one per
    operation, valued by that entrypoint.
    """

    module_name = "local_call"
    entrypoint = f"{module_name}.operation:Increment"
    project = directory / f"{name}-{increment}"
    module = project / f"src/{module_name}"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    entry_points = "".join(f'"{operation}" = "{entrypoint}"\n' for operation in operations)
    (project / "pyproject.toml").write_text(f"""[project]
name = "{name}"
version = "0.3.0"
requires-python = ">=3.11"
dependencies = ["cruxible-provider-runtime>=0.3.0"]
[project.entry-points."cruxible.providers"]
{entry_points}[build-system]
requires = ["hatchling==1.32.1"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/{module_name}"]
""")

    def resource(file: str, value: object) -> dict[str, str]:
        content = (
            value.encode() if isinstance(value, str) else json.dumps(value, sort_keys=True).encode()
        )
        (module / file).write_bytes(content)
        return {"path": file, "digest": "sha256:" + hashlib.sha256(content).hexdigest()}

    resource(
        "operation.py",
        f"""from cruxible_provider_runtime.provider_api import ProviderResult


class Increment:
    operations = {tuple(operations)!r}

    def __call__(self, context):
        return ProviderResult.ok({{"n": context.input["n"] + {increment}}})
""",
    )
    manifest = resource(
        "manifest.json",
        {
            "schema_version": 2,
            "provider_id": name,
            "distribution": {"name": name, "version": "0.3.0"},
            "entrypoint_group": "cruxible.providers",
            "supported_protocol_majors": [2],
            "implementations": [
                {
                    "entrypoint": entrypoint,
                    "backends": list(backends),
                    "requires_extras": [],
                    "declared_endpoints": [],
                    "deterministic": True,
                }
            ],
        },
    )
    resource(
        "registration.json",
        {
            "schema_version": 2,
            "manifest": manifest,
            "operations": [
                {
                    "name": operation,
                    "in": LOCAL_CALL_IN,
                    "out": LOCAL_CALL_OUT,
                    "effect": "pure",
                    "entrypoint": entrypoint,
                }
                for operation in sorted(operations)
            ],
            "runtime_requirements": list(runtime_requirements),
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
