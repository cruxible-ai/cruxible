"""workspace.file as a core built-in: parity, purity, identity and seeded bytes.

The parity golden (``tests/fixtures/workspace_file_parity.json``) was produced by
running ``cruxible_provider_workspace.file.WorkspaceFile`` from cruxible-providers
db085204 (the commit CI pins) over the package's six registration fixtures and a
set of edge and refusal vectors, rendering each result the way the package's
child harness does, and digesting the envelope without its run id. Core's port
must reproduce every digest. When a checkout is configured
(``CRUXIBLE_PROVIDERS_CHECKOUT``) the package is also run live against the port.

The same golden is the behaviour pin for ``WORKSPACE_FILE_BUILTIN_REVISION``: an
adapter change that moves any digest fails here, and is a semantic change that
needs a new revision (and so a new implementation digest and genesis seed set).
"""

from __future__ import annotations

import ast
import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

import cruxible_core.providers.builtin_workspace_file as adapter_module
from cruxible_client.contracts.provider_interfaces import (
    evaluate_provider_interface_law,
    provider_bucket_fixture_digest,
    provider_external_interface_definition_digest,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    evaluate_provider_law,
    provider_path,
    render_provider,
)
from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_V2_DIGEST
from cruxible_core.governance.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_BUILTIN_DEPLOYMENT_DIGEST,
    WORKSPACE_FILE_BUILTIN_ENVIRONMENT_MANIFEST_DIGEST,
    WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
    WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST,
    WORKSPACE_FILE_BUILTIN_REVISION,
    WORKSPACE_FILE_FIXTURES,
    WorkspaceFileBucketClassifier,
    workspace_file_accepted_registration,
    workspace_file_builtin_provider,
)
from cruxible_core.ledger.bootstrap import GENESIS_SEED_SETS, genesis_seed_files
from cruxible_core.providers.builtin_workspace_file import WorkspaceFile, classify
from cruxible_core.providers.provider_classifiers import (
    PROVIDER_BUCKET_CLASSIFIER_REGISTRY,
    core_provider_bucket_conformance_fixtures,
)
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeRunContextV1,
)
from tests.support.provider_checkout import provider_checkout_path

GOLDEN = json.loads((Path(__file__).parents[1] / "fixtures/workspace_file_parity.json").read_text())
# sha256 of the package's contracts/workspace.file.json, as its registration.json pins it.
PACKAGE_DEFINITION_FILE_DIGEST = "fe0762d929330c30a3c307d645cfd08ef0f87038a1bc826945ce2fd8f317c9eb"


def _context(payload: object, bucket: str) -> ProviderRuntimeRunContextV1:
    return ProviderRuntimeRunContextV1.model_construct(
        protocol_version="1.0",
        run_id="parity",
        interface_id="workspace.file",
        interface_digest=WORKSPACE_FILE_INTERFACE_V2_DIGEST,
        implementation_digest=WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
        entrypoint="cruxible_core.providers.builtin_workspace_file:WorkspaceFile",
        coordinates={},
        input=payload,
        input_bucket=bucket,
        capture_contract=None,
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=5.0, output_bytes=1 << 24),
        declared_endpoints=(),
        secret_channel=None,
        additive={},
    )


def _envelope_digest(payload: object, bucket: str) -> tuple[str, dict[str, Any]]:
    document = json.loads(WorkspaceFile()(_context(payload, bucket)).to_json())
    document.pop("protocol_version")
    document.pop("run_id")
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest(), document


def test_the_port_reproduces_the_package_on_every_registration_fixture() -> None:
    fixtures = {fixture.fixture_id: fixture for fixture in WORKSPACE_FILE_FIXTURES}
    assert sorted(fixtures) == sorted(item["fixture_id"] for item in GOLDEN["fixtures"])
    for expected in GOLDEN["fixtures"]:
        fixture = fixtures[expected["fixture_id"]]
        assert classify(fixture.canonical_input) == expected["input_bucket"]  # type: ignore[arg-type]
        digest, document = _envelope_digest(fixture.canonical_input, expected["input_bucket"])
        assert document["status"] == "ok"
        assert digest == expected["envelope_sha256"], expected["fixture_id"]


@pytest.mark.parametrize("vector", GOLDEN["vectors"], ids=lambda item: item["id"])
def test_the_port_reproduces_the_package_on_edge_and_refusal_vectors(
    vector: dict[str, Any],
) -> None:
    assert classify(vector["input"]) == vector["measured_bucket"]
    digest, document = _envelope_digest(vector["input"], vector["input_bucket"])
    assert document["status"] == vector["status"]
    if "refusal_code" in vector:
        assert document["refusal"]["code"] == vector["refusal_code"]
    assert digest == vector["envelope_sha256"]


def test_the_behaviour_revision_and_derived_identity_are_frozen() -> None:
    """Bump the revision only with a golden change; everything else derives from it."""

    assert WORKSPACE_FILE_BUILTIN_REVISION == (
        "sha256:9010dd924943e5ba971d5326bfe4cb7673a90873d1a475e7b2651943d0091e0d"
    )
    assert WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST == (
        "sha256:67c337ac0a87cfe8ee42da30a21c4cce658a0ad6bb3944b39a81e921aaba843c"
    )
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


def test_the_registration_keeps_the_package_definition_and_fixtures_exactly() -> None:
    definition = files("cruxible_core.governance.seed_artifacts").joinpath(
        "workspace-file-interface.json"
    )
    assert hashlib.sha256(definition.read_bytes()).hexdigest() == PACKAGE_DEFINITION_FILE_DIGEST
    accepted = workspace_file_accepted_registration()
    registration = accepted.registration
    assert registration.interface_digest == WORKSPACE_FILE_INTERFACE_V2_DIGEST
    assert (
        provider_external_interface_definition_digest(
            registration.interface_bytes_hex, domain="cruxible.interface.stub.v1"
        )
        == WORKSPACE_FILE_INTERFACE_V2_DIGEST
    )
    assert registration.effect_class == "none"
    catalog = core_provider_bucket_conformance_fixtures()
    for proof in registration.conformance_proofs:
        assert provider_bucket_fixture_digest(catalog[proof.fixture_id]) == proof.fixture_digest
    assert (
        registration.classifier_digest
        in PROVIDER_BUCKET_CLASSIFIER_REGISTRY.installed_classifier_digests
    )


def test_the_compiler_owned_classifier_agrees_with_the_adapter() -> None:
    classifier = WorkspaceFileBucketClassifier()
    for fixture in WORKSPACE_FILE_FIXTURES:
        measured = classifier.classify(fixture.canonical_input, deadline=None)
        assert measured == fixture.measured_bucket_id == classify(fixture.canonical_input)  # type: ignore[arg-type]


def test_the_seeded_artifacts_are_the_builders_bytes_and_pass_their_laws() -> None:
    """A builder change that moves seeded bytes needs a new genesis seed set."""

    seeded = next(item for item in GENESIS_SEED_SETS if item.set_id == "triggers-4-workspace-file")
    seeds = genesis_seed_files(seeded)
    accepted = workspace_file_accepted_registration()
    provider = workspace_file_builtin_provider(interface_artifact_digest=accepted.artifact_digest)
    assert seeds[accepted.path] == render_provider_interface(accepted.registration)
    assert seeds[provider_path(provider.identity.name)] == render_provider(provider)
    law = evaluate_provider_interface_law(
        accepted.registration,
        path=accepted.path,
        predecessor=None,
        conformance_fixtures=core_provider_bucket_conformance_fixtures(),
    )
    assert law.verdict == "accepted"
    provider_law = evaluate_provider_law(
        provider,
        path=provider_path(provider.identity.name),
        predecessor=None,
        interface_registrations={accepted.registration.identity.qualified: accepted},
    )
    assert provider_law.verdict == "accepted"
    (record,) = provider.implementations
    assert record.implementation_digest == WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST


_FORBIDDEN_MODULES = {"os", "io", "socket", "subprocess", "pathlib", "shutil", "time", "datetime"}


def test_the_adapter_module_is_pure() -> None:
    """No file, network, process or clock access is even importable from the adapter."""

    source = Path(adapter_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Name) and node.id in {"open", "__import__", "eval", "exec"}:
            raise AssertionError(f"the adapter names {node.id!r}")
    assert not imported & _FORBIDDEN_MODULES, imported & _FORBIDDEN_MODULES
    assert {name for name in imported if "." not in name} <= {
        "__future__",
        "base64",
        "collections",
        "cruxible_core",
        "hashlib",
        "re",
        "typing",
    }


def test_the_port_matches_the_package_live_when_a_checkout_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = provider_checkout_path()
    if checkout is None:
        pytest.skip("set CRUXIBLE_PROVIDERS_CHECKOUT to run the package side by side")
    monkeypatch.syspath_prepend(str(checkout / "packages/cruxible-provider-workspace/src"))
    from cruxible_provider_runtime.egress import EgressRecorder
    from cruxible_provider_runtime.protocol import Budgets
    from cruxible_provider_runtime.provider_api import ProviderRunContext
    from cruxible_provider_workspace.file import WorkspaceFile as PackageWorkspaceFile

    inputs = [
        (fixture.canonical_input, fixture.measured_bucket_id) for fixture in WORKSPACE_FILE_FIXTURES
    ]
    inputs += [(vector["input"], vector["input_bucket"]) for vector in GOLDEN["vectors"]]
    for payload, bucket in inputs:
        result = PackageWorkspaceFile()(
            ProviderRunContext(
                run_id="parity",
                interface_id="workspace.file",
                interface_digest=WORKSPACE_FILE_INTERFACE_V2_DIGEST,
                implementation_digest="sha256:" + "0" * 64,
                input_bucket=bucket,
                input=payload,  # type: ignore[arg-type]
                coordinates={},
                budgets=Budgets(wall_clock_seconds=5.0, output_bytes=1 << 24),
                declared_endpoints=(),
                capture_contract=None,
                secrets={},
                egress=EgressRecorder(),
            )
        )
        _digest, ours = _envelope_digest(payload, bucket)
        assert ours["status"] == result.status
        assert ours["output"] == result.output
        assert ours["trace"]["metrics"] == result.metrics
        if result.refusal is not None:
            assert ours["refusal"] == json.loads(result.refusal.model_dump_json())
