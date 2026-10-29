"""New projected kinds must make an explicit Procedure reference-path decision."""

import pytest

from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_core.authoring.lowering import _reference_path
from cruxible_core.compiler.compiler import ARTIFACT_KINDS

# Registry path kinds and wire identity kinds deliberately have different names.
# Each referenceable kind needs a real, canonical identity and its exact path.
REFERENCEABLE = {
    "approval-policy": ("ApprovalPolicy:instance", "governance/approval-policy.json"),
    "procedure-runtime-policy": (
        "ProcedureRuntimePolicy:instance",
        "governance/procedure-runtime-policy.json",
    ),
    "capture-contract": ("CaptureContract:example", "capture-contracts/example.json"),
    "claim": ("Claim:CLM-" + "a" * 32, "claims/aa/CLM-" + "a" * 32 + ".json"),
    "contract": ("Contract:example/v1", "contracts/example/v1.json"),
    "attestation": ("ClaimAttestation:sha256:" + "a" * 64, "attestations/aa/" + "a" * 64 + ".json"),
    "claim-type": ("ClaimType:example.title", "claim-types/example/title.json"),
    "document": ("document:example", "documents/example.json"),
    "exhaust-promotion": ("ExhaustPromotion:example", "exhaust-promotions/example.json"),
    "line": ("Line:example", "lines/example.json"),
    "procedure": ("Procedure:example", "procedures/example.json"),
    "procedure-mandate": ("ProcedureMandate:example", "procedure-mandates/example.json"),
    "provider": ("Provider:example/" + "a" * 64, "providers/example/" + "a" * 64 + ".json"),
    "query-definition": ("QueryDefinition:example", "query-definitions/example.json"),
    "resolution-contract": ("ResolutionContract:example", "resolution-contracts/example.json"),
    "subject": ("Subject:example/item", "subjects/example/item.json"),
    "subject-kind": ("SubjectKind:example", "subject-kinds/example.json"),
    "trigger": ("Trigger:example", "triggers/example.json"),
}
NOT_REFERENCEABLE = {
    "changeset": "Acceptance history records are not Procedure dependency artifacts.",
    "compiler-upgrade": "Compiler adoption is a ledger transition, not a graph dependency.",
    "principal": "Governance principals are not Procedure graph artifact references.",
}


def test_every_registered_projected_kind_has_a_reference_path_decision() -> None:
    registry = ARTIFACT_KINDS
    registered = {entry.kind for entry in registry.entries()}
    assert not REFERENCEABLE.keys() & NOT_REFERENCEABLE.keys()
    assert REFERENCEABLE.keys() | NOT_REFERENCEABLE.keys() == registered
    assert all(NOT_REFERENCEABLE.values())


@pytest.mark.parametrize("kind", sorted(REFERENCEABLE))
def test_referenceable_kind_resolves_its_exact_registered_path(kind: str) -> None:
    identity, expected = REFERENCEABLE[kind]
    path = _reference_path(parse_artifact_identity(identity))
    assert path == expected
    registry = ARTIFACT_KINDS
    assert registry.resolve_path(path) == kind
