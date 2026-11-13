"""Build a new provider package (protocol 2.0) solely from its metadata for integration tests."""

import hashlib
import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

#: The one Contract ``local.increment`` reads and returns: ``{n: int}``. An
#: install pins it, so a world accepts it (``local_call_contract_files``) before
#: installing the package.
LOCAL_CALL_VALUE = {"name": "local.increment.value", "version": 1, "fields": {"n": {"type": "int"}}}
LOCAL_CALL_IN = "local.increment.value@1"
LOCAL_CALL_OUT = "local.increment.value@1"
#: The capability a Procedure's slot names to call ``local.increment`` (pure).
LOCAL_CALL_CAPABILITY = "local.increment@1"
#: The authored slot naming it.
LOCAL_CALL_SLOT = {"capability": LOCAL_CALL_CAPABILITY}

#: A REFUSAL Contract an operation's own registration pins (``build_local_call``'s
#: ``refusal=``), never a Procedure slot's in/out: outside every namespace a
#: ``local.increment`` consumer owns, to exercise D-R48 review fix round 3, F1
#: (``build_kit`` must sweep a bundled package's refusal Contracts too, whatever
#: namespace they live in, since nothing else pins them into the owned closure).
OTHER_DECLINED_VALUE = {"name": "other.declined", "version": 1, "fields": {}}
OTHER_DECLINED_REF = "other.declined@1"


def local_call_contract_files() -> dict[str, bytes]:
    """The accepted-tree member of ``local.increment``'s Contract."""

    from cruxible_client.contracts.shapes import Contract, contract_path, render_contract

    contract = Contract.model_validate(LOCAL_CALL_VALUE)
    return {contract_path(contract.name, contract.version): render_contract(contract)}


def local_call_capability_files() -> dict[str, bytes]:
    """The accepted-tree member of the capability a slot names to call ``local.increment``.

    Accepted beside or after ``local_call_contract_files``: it pins that Contract.
    """

    from cruxible_client.contracts.shapes import Contract
    from tests.support.capabilities import capability, capability_files

    contract = Contract.model_validate(LOCAL_CALL_VALUE)
    return capability_files(
        capability("local.increment", in_=contract, out=contract, effect="pure")
    )


def other_declined_contract_files() -> dict[str, bytes]:
    """The accepted-tree member of ``other.declined``'s Contract (``OTHER_DECLINED_REF``)."""

    from cruxible_client.contracts.shapes import Contract, contract_path, render_contract

    contract = Contract.model_validate(OTHER_DECLINED_VALUE)
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


def _load_checkout_module(checkout: Path, name: str, relative_package: str) -> Any:
    """A providers-checkout package, loaded by file location (never via sys.path).

    Mirrors ``tests/test_e2e/test_skeleton_kev_epss.py``'s ``_web_fetch``: the
    module is imported by its exact file location and cached in ``sys.modules``
    under its real name, so its own internal imports (and any absolute import of
    a sibling package already on the ambient path, e.g. ``cruxible_provider_runtime``)
    resolve normally.
    """

    import importlib.util
    import sys

    if name in sys.modules:
        return sys.modules[name]
    package = checkout / relative_package
    if not package.is_dir():
        pytest.fail(f"{checkout} carries no {name} package")
    spec = importlib.util.spec_from_file_location(
        name, package / "__init__.py", submodule_search_locations=[str(package)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def noop_contract_files() -> dict[str, bytes]:
    """The reference noop operation's actual typed Contracts, straight from the package.

    ``cruxible_provider_noop.contract_members()`` out of the pinned providers
    checkout (the same one ``provider_checkout`` builds wheels from), so this
    is exactly the tree the package itself ships under ``contracts/``
    (including ``noop.captures``), never a hand-maintained duplicate that can
    drift from it.
    """
    from tests.support.provider_checkout import require_provider_checkout

    checkout = require_provider_checkout()
    noop = _load_checkout_module(
        checkout,
        "cruxible_provider_noop",
        "packages/cruxible-provider-noop/src/cruxible_provider_noop",
    )
    return noop.contract_members()


def _lock_root_version(lock_path: Path) -> str:
    """The version of the package a ``uv.lock`` is the root lock for (its own entry)."""

    lock = tomllib.loads(lock_path.read_text())
    roots = [row for row in lock["package"] if row.get("source") == {"editable": "."}]
    assert len(roots) == 1, f"expected one editable root package in {lock_path}"
    return str(roots[0]["version"])


def build_local_call(
    directory: Path,
    repository: Path,
    *,
    increment: int = 1,
    name: str = "local-call",
    backends: tuple[str, ...] = ("local_env",),
    operations: tuple[str, ...] = ("local.increment",),
    runtime_requirements: tuple[dict[str, Any], ...] = (),
    refusal: str | None = None,
) -> tuple[Path, Path]:
    """A provider runtime protocol 2.0 package: registration and manifest schema 2.

    One entrypoint, ``local_call.operation:Increment``, serves every name in
    ``operations``; the wheel's ``cruxible.providers`` entry points are one per
    operation, valued by that entrypoint. ``refusal`` (a ``name@N`` Contract
    ref) declares one typed refusal code (``declined``) on every operation,
    for a package that pins a REFUSAL Contract never a plain in/out slot pins
    (D-R48 review fix round 3, F1): a kit-build regression exercising it.
    """

    module_name = "local_call"
    entrypoint = f"{module_name}.operation:Increment"
    project = directory / f"{name}-{increment}"
    module = project / f"src/{module_name}"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    entry_points = "".join(f'"{operation}" = "{entrypoint}"\n' for operation in operations)
    # The lock below is copied verbatim from the workspace package's own lock
    # (its root), so this package's own version must match that root's version
    # exactly, or resolve_provider_package's lock-root identity check fails.
    version = _lock_root_version(repository / "packages/cruxible-provider-workspace/uv.lock")
    (project / "pyproject.toml").write_text(f"""[project]
name = "{name}"
version = "{version}"
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
            "distribution": {"name": name, "version": version},
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
                    **(
                        {} if refusal is None else {"refusals": {"declined": {"contract": refusal}}}
                    ),
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
