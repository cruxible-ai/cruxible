"""The workspace reads as a core built-in: behaviour, purity, identity and seeded bytes.

The behaviour golden (``tests/fixtures/workspace_file_parity.json``) records the
built-in's envelope for six registration fixtures (inherited from the provider
package the first revision was ported from) and a set of edge and refusal
vectors, each rendered the way the child harness renders it and digested without
its run id. The built-in must reproduce every digest.

The golden is the behaviour pin for ``WORKSPACE_FILE_BUILTIN_REVISION``: an
adapter change that moves any digest fails here, and is a semantic change that
needs a new revision (and so a new implementation digest and genesis seeds).
Revision 2 checks the handoff and returns an empty output: core commits the
bytes it read as the call's capture field.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import cruxible_core.providers.builtin_workspace_file as adapter_module
from cruxible_client.contracts.providers import (
    evaluate_provider_law,
    provider_path,
    render_provider,
)
from cruxible_client.contracts.shapes import Contract, contract_digest, parse_contract
from cruxible_client.contracts.workspace_file import (
    workspace_file_contracts,
    workspace_file_refusal_contracts,
)
from cruxible_core.governance.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_BUILTIN_DEPLOYMENT_DIGEST,
    WORKSPACE_FILE_BUILTIN_ENVIRONMENT_MANIFEST_DIGEST,
    WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
    WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST,
    WORKSPACE_FILE_BUILTIN_REVISION,
    workspace_file_builtin_identity,
    workspace_file_builtin_provider,
)
from cruxible_core.ledger.bootstrap import genesis_seed_files
from cruxible_core.providers.builtin_runtime import BUILTIN_IMPLEMENTATIONS
from cruxible_core.providers.builtin_workspace_file import WorkspaceFile
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgets,
    ProviderRuntimeRunContext,
)

GOLDEN = json.loads((Path(__file__).parents[1] / "fixtures/workspace_file_parity.json").read_text())


def _text_lines(count: int, *, newline: str, trailing: bool, bom: bool = False) -> bytes:
    lines = [
        f"line {index:05d}: the quick brown fox jumps over the lazy dog" for index in range(count)
    ]
    text = newline.join(lines) + (newline if trailing else "")
    return ("\ufeff" if bom else "").encode() + text.encode()


def _pseudo_random(length: int, *, seed: str) -> bytes:
    output = bytearray()
    block = seed.encode()
    while len(output) < length:
        block = hashlib.sha256(block).digest()
        output.extend(block)
    return bytes(output[:length])


#: The six registration fixtures (byte-equal to the 0.2.x package's): the
#: golden's registration-fixture inputs.
_FIXTURE_BYTES: dict[str, bytes] = {
    "workspace-file-binary-medium": _pseudo_random(70_000, seed="workspace-file-binary-medium"),
    "workspace-file-binary-small": _pseudo_random(5_000, seed="workspace-file-binary-small"),
    "workspace-file-binary-tiny": bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff61"
    ),
    "workspace-file-text-medium": _text_lines(1_200, newline="\n", trailing=False, bom=True),
    "workspace-file-text-small": _text_lines(600, newline="\r\n", trailing=True),
    "workspace-file-text-tiny": (
        "# Reach readings\n\nUpper reach: 4.1 mg/l nitrate — see the tide-gauge report.\n"
    ).encode(),
}


@dataclass(frozen=True)
class _ConformanceFixture:
    """One golden input: its id and its handoff."""

    fixture_id: str
    canonical_input: dict[str, object]


def _fixture(fixture_id: str, data: bytes) -> _ConformanceFixture:
    return _ConformanceFixture(
        fixture_id=fixture_id,
        canonical_input={
            "logical_source": f"fixtures/{fixture_id}",
            "commitment_digest": f"sha256:{'c0' * 32}",
            "content_encoding": "base64",
            "bytes": base64.b64encode(data).decode("ascii"),
            "byte_length": len(data),
            "bytes_digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
        },
    )


WORKSPACE_FILE_FIXTURES = tuple(
    _fixture(fixture_id, data)
    for fixture_id, data in sorted(_FIXTURE_BYTES.items(), key=lambda item: item[0].encode())
)


#: The built-in's run identity in these vectors (the digests leave it out).
_BUILD_DIGEST = "sha256:" + "4d" * 32
_SIGNATURE_DIGEST = "sha256:" + "3c" * 32


def _context(payload: object) -> ProviderRuntimeRunContext:
    return ProviderRuntimeRunContext.model_construct(
        protocol_version="2.0",
        run_id="parity",
        operation="workspace.file",
        operation_signature_digest=_SIGNATURE_DIGEST,
        build_digest=_BUILD_DIGEST,
        implementation_digest=WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
        entrypoint="cruxible_core.providers.builtin_workspace_file:WorkspaceFile",
        coordinates={},
        input=payload,
        capture_contract=None,
        budgets=ProviderRuntimeBudgets(wall_clock_seconds=5.0, output_bytes=1 << 24),
        declared_endpoints=(),
        secret_channel=None,
        additive={},
    )


def _envelope_digest(payload: object) -> tuple[str, dict[str, Any]]:
    """The envelope without its protocol version and run id, digested as the golden is."""

    document = json.loads(WorkspaceFile()(_context(payload)).to_json())
    document.pop("protocol_version")
    document.pop("run_id")
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest(), document


def test_the_built_in_answers_every_registration_fixture_as_recorded() -> None:
    fixtures = {fixture.fixture_id: fixture for fixture in WORKSPACE_FILE_FIXTURES}
    assert sorted(fixtures) == sorted(item["fixture_id"] for item in GOLDEN["fixtures"])
    for expected in GOLDEN["fixtures"]:
        fixture = fixtures[expected["fixture_id"]]
        digest, document = _envelope_digest(fixture.canonical_input)
        # The call's value is the Capture core commits: the built-in adds no field.
        assert document["status"] == "ok" and document["output"] == {}
        assert digest == expected["envelope_sha256"], expected["fixture_id"]


@pytest.mark.parametrize("vector", GOLDEN["vectors"], ids=lambda item: item["id"])
def test_the_built_in_answers_edge_and_refusal_vectors_as_recorded(
    vector: dict[str, Any],
) -> None:
    digest, document = _envelope_digest(vector["input"])
    assert document["status"] == vector["status"]
    if "refusal_code" in vector:
        assert document["refusal"]["code"] == vector["refusal_code"]
    assert digest == vector["envelope_sha256"]


def test_the_built_in_takes_a_file_above_one_mebibyte() -> None:
    """The structuring guard is gone: the CaptureKind's byte budget bounds a read."""

    data = b"a" * (1_048_576 + 1)
    payload = {
        "logical_source": "fixtures/large",
        "commitment_digest": f"sha256:{'c0' * 32}",
        "content_encoding": "base64",
        "bytes": base64.b64encode(data).decode("ascii"),
        "byte_length": len(data),
        "bytes_digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
    }
    _digest, document = _envelope_digest(payload)
    assert document["status"] == "ok" and document["output"] == {}


def test_the_behaviour_revision_and_derived_identity_are_frozen() -> None:
    """Bump the revision only with a golden change; everything else derives from it."""

    assert WORKSPACE_FILE_BUILTIN_REVISION == (
        "sha256:04922994e7747b10f7a84726512d7b537d89d37655ca63a926bbb216dfdc25a5"
    )
    assert WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST == IMPLEMENTATION_DIGEST
    assert (
        len(
            {
                WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
                WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST,
                WORKSPACE_FILE_BUILTIN_DEPLOYMENT_DIGEST,
                WORKSPACE_FILE_BUILTIN_ENVIRONMENT_MANIFEST_DIGEST,
            }
        )
        == 4
    )


def test_the_seeded_provider_speaks_protocol_2_from_one_entrypoint() -> None:
    """The built-in's manifest is schema 2: one entrypoint row, no interface or buckets."""

    provider = workspace_file_builtin_provider()
    manifest = provider.runtime_artifact.manifest
    assert manifest.schema_version == 2
    assert manifest.supported_protocol_majors == (2,)
    (row,) = manifest.implementations
    assert set(row.model_dump()) == {
        "entrypoint",
        "backends",
        "declared_endpoints",
        "requires_extras",
        "deterministic",
    }
    assert WorkspaceFile.operations == ("workspace.file", "workspace.files")
    assert {item.entrypoint for item in provider.operations} == {row.entrypoint}


def test_the_seeded_artifacts_are_the_builders_bytes_and_pass_their_laws() -> None:
    """A builder change that moves seeded bytes moves ``GENESIS_SEEDS`` with it."""

    seeds = genesis_seed_files()
    provider = workspace_file_builtin_provider()
    path = provider_path(provider.identity.name)
    assert seeds[path] == render_provider(provider)
    # The builtin carries its handoff, its copies of the base kit's two result
    # Contracts and its three refusal Contracts, so its law needs no instance
    # Contract.
    carried = {
        (contract.name, contract.version): contract_digest(contract).tagged
        for contract in (*workspace_file_contracts(), *workspace_file_refusal_contracts())
        if isinstance(contract, Contract)
    }
    assert provider.declares is not None
    assert {
        (item.name, item.version): contract_digest(item).tagged
        for item in provider.declares.contracts
    } == carried
    # Two operations, each serving the capability of its name (ruling O2), each
    # returning its copy of the base kit's result, byte for byte.
    one, glob = provider.operations
    assert (one.name, one.serves, glob.name, glob.serves) == (
        "workspace.file",
        ("workspace.file",),
        "workspace.files",
        ("workspace.files",),
    )
    base = Path(__file__).parents[2] / "src/cruxible_core/kits/base/artifacts"
    for operation in (one, glob):
        assert operation.in_.name == "workspace.file.handoff"
        kit_path = f"contracts/{operation.out.name}/v{operation.out.version}.json"
        kit_copy = parse_contract((base / kit_path).read_bytes(), path=kit_path)
        assert operation.out.digest == contract_digest(kit_copy).tagged
        assert operation.description
        assert operation.source.path.endswith("builtin_workspace_file.py")
    provider_law = evaluate_provider_law(
        provider, path=path, predecessor=None, serves_reserved=True
    )
    assert provider_law.verdict == "accepted", provider_law.diagnostics
    # Any other Provider serving a workspace. capability is refused.
    refused = evaluate_provider_law(provider, path=path, predecessor=None)
    assert refused.diagnostics[0].code == "cruxible.provider.capability_reserved"
    (record,) = provider.implementations
    assert record.implementation_digest == WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST


#: Every module the adapter may import, by full dotted name, and nothing else:
#: no file, network, process or clock access is reachable from it.
_ALLOWED_IMPORTS = {
    "__future__",
    "base64",
    "collections.abc",
    "cruxible_core.providers.provider_runtime_contract",
    "hashlib",
    "re",
    "typing",
}


def test_the_adapter_module_is_pure() -> None:
    source = Path(adapter_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None, "relative imports are not allowed"
            imported.add(node.module)
        elif isinstance(node, ast.Name) and node.id in {"open", "__import__", "eval", "exec"}:
            raise AssertionError(f"the adapter names {node.id!r}")
    assert imported == _ALLOWED_IMPORTS, imported ^ _ALLOWED_IMPORTS


#: The frozen prefix of BUILTIN_IMPLEMENTATIONS: implementation digest (the
#: ``cruxible.provider.implementation.v2`` identity) and the revision it was
#: derived from. A revision bump appends a line; an old line must never change
#: or disappear, because seeded instances pin it forever.
IMPLEMENTATION_DIGEST = "sha256:18e9bddcd464531c616b69e2899c1e548215480ee5beae723aa6bf4d24a84ac1"
FROZEN_BUILTIN_IMPLEMENTATIONS = (
    (
        IMPLEMENTATION_DIGEST,
        "sha256:04922994e7747b10f7a84726512d7b537d89d37655ca63a926bbb216dfdc25a5",
    ),
)


def test_built_in_implementations_are_append_only_and_keep_their_adapters() -> None:
    entries = [(digest, item.identity.revision) for digest, item in BUILTIN_IMPLEMENTATIONS.items()]
    assert entries[: len(FROZEN_BUILTIN_IMPLEMENTATIONS)] == list(FROZEN_BUILTIN_IMPLEMENTATIONS)
    assert len(entries) == len(FROZEN_BUILTIN_IMPLEMENTATIONS), (
        "pin the new revision in FROZEN_BUILTIN_IMPLEMENTATIONS"
    )
    for digest, item in BUILTIN_IMPLEMENTATIONS.items():
        assert item.identity == workspace_file_builtin_identity(
            item.identity.revision, entrypoint=item.identity.entrypoint
        )
        assert digest == item.identity.implementation_digest
        assert callable(item.adapter)
    # The revision new instances seed is one of them.
    assert WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST in BUILTIN_IMPLEMENTATIONS


def test_provider_list_names_the_providers_installed_here(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """provider list starts with what is installed, not only the repository (C3)."""

    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
    from cruxible_core.service.procedures.provider_installation import service_provider_catalog
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    catalog = service_provider_catalog(
        ProviderRuntimeOperator(tmp_path / "operator-state"), instance
    )

    (builtin,) = catalog.installed
    assert builtin.provider == "cruxible-builtin"
    assert builtin.operations == ("workspace.file", "workspace.files")
    assert catalog.packages == ()
    assert catalog.detail is not None and "No provider repository configured" in catalog.detail
