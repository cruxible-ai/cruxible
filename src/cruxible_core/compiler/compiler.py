"""Versioned Cruxible compiler coordinates and their exact projection registries."""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactKindRegistry
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    P2_B0_ARTIFACT_CODEC,
    ArtifactCodec,
    canonical_digest,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    claim_extension_registry,
    claim_type_extension_registry,
    evidence_extension_registry,
    extension_registry,
    governance_extension_registry,
    p2c_extension_registry,
    procedure_extension_registry,
    provider_runtime_extension_registry,
    replay_extension_registry,
    runtime_extension_registry,
    subject_extension_registry,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.projection_artifacts import (
    ATTESTATION_ARTIFACT_KINDS,
    AUTHORITY_VERBS_ARTIFACT_KINDS,
    CLAIM_EVIDENCE_ARTIFACT_KINDS,
    GOVERNED_TRIGGERS_ARTIFACT_KINDS,
    ONTOLOGY_ARTIFACT_KINDS,
    P2_B0_ARTIFACT_KINDS,
    P2_B1_ARTIFACT_KINDS,
    P2_C_ARTIFACT_KINDS,
    PLAYBILL_ARTIFACT_KINDS,
    PROVIDER_BUILDS_ARTIFACT_KINDS,
    PROVIDER_CONTRACT_ARTIFACT_KINDS,
    PROVIDER_PACKAGE_ARTIFACT_KINDS,
    RESOLUTION_ARTIFACT_KINDS,
    RESOURCE_BUDGET_ARTIFACT_KINDS,
    SDK_SOURCE_ARTIFACT_KINDS,
    SOURCE_CHECKED_ARTIFACT_KINDS,
    SUBJECT_IDENTITY_ARTIFACT_KINDS,
    TRIGGER_CAPTURE_ARTIFACT_KINDS,
    UPGRADE_ARTIFACT_KINDS,
)
from cruxible_core.proposals.candidate_cards import CARD_RENDERER_DIGEST


def _coordinate(
    *,
    projection_content: str | None = None,
    semantic_revision: int | None = None,
    candidate_card_renderer_digest: str | None = None,
    revision_name: str | None = None,
) -> CompilerCoordinate:
    payload: dict[str, object] = {
        "implementation": "python-reference",
        "schema_version": 1,
    }
    if projection_content is not None:
        payload["projection_content"] = projection_content
    if semantic_revision is not None:
        payload["semantic_revision"] = semantic_revision
    if candidate_card_renderer_digest is not None:
        payload["candidate_card_renderer_digest"] = candidate_card_renderer_digest
    if revision_name is not None:
        payload["revision_name"] = revision_name
    return CompilerCoordinate(
        rule_digest=f"sha256:{canonical_digest('playbill-compiler-v1', payload)}"
    )


PB_B_COMPILER = _coordinate()
PB_C_COMPILER = _coordinate(projection_content="family-1-document-v1")
PB_D_COMPILER = _coordinate(projection_content="family-1-document-governance-v1")
PC_A1_COMPILER = _coordinate(projection_content="claims-procedures-subject-v1")
PC_A2_COMPILER = _coordinate(projection_content="claims-procedures-claim-type-v1")
PC_B_COMPILER = _coordinate(projection_content="claims-procedures-claim-v1")
PC_C_COMPILER = _coordinate(projection_content="claims-procedures-evidence-v1")
PC_D_COMPILER = _coordinate(projection_content="claims-procedures-procedure-v1")
PC_E1_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=10,
)
P2_B0_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=11,
)
PC_HR_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=12,
)
P2_B1_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=13,
)
P2_C_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=14,
)
PC_DF2_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=15,
)
P2_B2_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=16,
)
P2_B4_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=17,
)
P2_B4_UNIT2_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=18,
)
P2_B5_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=19,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
ATTESTATION_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=20,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
RESOLUTION_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=21,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
ONTOLOGY_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=22,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
UPGRADE_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=23,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
PROVIDER_CONTRACT_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=24,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
PROVIDER_PACKAGE_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=25,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
RESOURCE_BUDGET_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=26,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
SDK_SOURCE_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=27,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)
CLAIM_EVIDENCE_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=28,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)

SOURCE_CHECKED_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=29,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)

TRIGGER_CAPTURE_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=30,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)

AUTHORITY_VERBS_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=31,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
)

# Revision 32: every trigger is a governed Trigger artifact aimed at a Line or an
# internal action, and a Line (v6) no longer embeds its own. A reverted change once
# minted a revision-32 coordinate from the bare preimage; naming this revision in
# its preimage keeps the two from ever sharing a digest.
GOVERNED_TRIGGERS_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=32,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
    revision_name="governed-triggers-generation-v1",
)

# Revision 33: core names every new Subject (identity D1/D4/D6). SubjectKind
# artifacts declare each kind's rule, and the candidate boundary holds every new
# Subject to it: a declared kind, core's assignment binding, unique keys.
SUBJECT_IDENTITY_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=33,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
    revision_name="subject-identity-v1",
)

# Revision 34: several builds per provider name. A build is its own artifact at
# providers/<name>/<build>.json and a contract registration its own at
# provider-interfaces/<id>/<contract>.json, so installing a build or a changed
# contract adds an artifact and never replaces one. Single-path Providers and
# registrations stay readable as builds.
PROVIDER_BUILDS_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=34,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
    revision_name="provider-builds-v1",
)

# Revision 35: a Claim may be re-bound. Claim law v2 revision 10 lets a revision
# that keeps its statement replace a drifted citation with one of the text it
# rests on now (same role, origin and CaptureContract). The ledger grammar is
# revision 34's.
CLAIM_REBIND_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=35,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
    revision_name="claim-rebind-v1",
)

# One revision for the light-artifacts program; existing coordinates stay frozen.
LIGHT_ARTIFACTS_COMPILER = _coordinate(
    projection_content="claims-procedures-runtime-v1",
    semantic_revision=36,
    candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
    revision_name="light-artifacts-v1",
)

# The renderer a coordinate commits to is resolved from the coordinate itself,
# never from a second copy of its preimage. A duplicated preimage with its own
# revision literal silently returns None the moment the revision is succeeded,
# and cards then stop being derived, verified, or re-derived with every test
# still green.
_CARD_RENDERER_BY_COMPILER: dict[str, str] = {
    P2_B5_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    ATTESTATION_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    RESOLUTION_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    ONTOLOGY_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    UPGRADE_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    PROVIDER_CONTRACT_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    PROVIDER_PACKAGE_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    RESOURCE_BUDGET_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    SDK_SOURCE_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    CLAIM_EVIDENCE_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    SOURCE_CHECKED_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    TRIGGER_CAPTURE_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    AUTHORITY_VERBS_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    GOVERNED_TRIGGERS_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    SUBJECT_IDENTITY_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    PROVIDER_BUILDS_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    CLAIM_REBIND_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
    LIGHT_ARTIFACTS_COMPILER.rule_digest: CARD_RENDERER_DIGEST,
}


def admits_claim_rebind(compiler: CompilerCoordinate) -> bool:
    """Whether a compiler judges Claims under Claim law v2 revision 10 (re-bind)."""

    return compiler in {CLAIM_REBIND_COMPILER, LIGHT_ARTIFACTS_COMPILER}


def candidate_card_renderer_digest_for_compiler(
    compiler: CompilerCoordinate,
) -> str | None:
    """Resolve the renderer committed by an exact installed compiler coordinate."""

    return _CARD_RENDERER_BY_COMPILER.get(compiler.rule_digest)


SUPPORTED_COMPILERS = (
    PB_B_COMPILER,
    PB_C_COMPILER,
    PB_D_COMPILER,
    PC_A1_COMPILER,
    PC_A2_COMPILER,
    PC_B_COMPILER,
    PC_C_COMPILER,
    PC_D_COMPILER,
    PC_E1_COMPILER,
    P2_B0_COMPILER,
    PC_HR_COMPILER,
    P2_B1_COMPILER,
    P2_C_COMPILER,
    PC_DF2_COMPILER,
    P2_B2_COMPILER,
    P2_B4_COMPILER,
    P2_B4_UNIT2_COMPILER,
    P2_B5_COMPILER,
    ATTESTATION_COMPILER,
    RESOLUTION_COMPILER,
    ONTOLOGY_COMPILER,
    UPGRADE_COMPILER,
    PROVIDER_CONTRACT_COMPILER,
    PROVIDER_PACKAGE_COMPILER,
    RESOURCE_BUDGET_COMPILER,
    SDK_SOURCE_COMPILER,
    CLAIM_EVIDENCE_COMPILER,
    SOURCE_CHECKED_COMPILER,
    TRIGGER_CAPTURE_COMPILER,
    AUTHORITY_VERBS_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    SUBJECT_IDENTITY_COMPILER,
    PROVIDER_BUILDS_COMPILER,
    CLAIM_REBIND_COMPILER,
    LIGHT_ARTIFACTS_COMPILER,
)
# Immutable human-facing revision labels.  The digest remains the authority;
# these labels are display metadata and must never be inferred from the moving
# current compiler alias or from tuple position.
COMPILER_REVISION_LABELS = {
    PB_B_COMPILER: "pb-b",
    PB_C_COMPILER: "pb-c",
    PB_D_COMPILER: "pb-d",
    PC_A1_COMPILER: "pc-a1",
    PC_A2_COMPILER: "pc-a2",
    PC_B_COMPILER: "pc-b",
    PC_C_COMPILER: "pc-c",
    PC_D_COMPILER: "pc-d",
    PC_E1_COMPILER: "pc-e1",
    P2_B0_COMPILER: "p2-b0",
    PC_HR_COMPILER: "pc-hr",
    P2_B1_COMPILER: "p2-b1",
    P2_C_COMPILER: "p2-c",
    PC_DF2_COMPILER: "pc-df2",
    P2_B2_COMPILER: "p2-b2",
    P2_B4_COMPILER: "p2-b4",
    P2_B4_UNIT2_COMPILER: "p2-b4-u2",
    P2_B5_COMPILER: "p2-b5",
    ATTESTATION_COMPILER: "accepted-attestations-v1",
    RESOLUTION_COMPILER: "independent-resolution-contracts-v1",
    ONTOLOGY_COMPILER: "ontology-queries-v2",
    UPGRADE_COMPILER: "governed-compiler-upgrade-v1",
    PROVIDER_CONTRACT_COMPILER: "provider-operation-contracts-v1",
    PROVIDER_PACKAGE_COMPILER: "provider-package-registration-v1",
    RESOURCE_BUDGET_COMPILER: "bounded-resource-budgets-v1",
    SDK_SOURCE_COMPILER: "typed-procedure-source-v1",
    CLAIM_EVIDENCE_COMPILER: "producer-independent-claim-types-v1",
    SOURCE_CHECKED_COMPILER: "checked-procedure-source-v2",
    TRIGGER_CAPTURE_COMPILER: "line-trigger-capture-input-v1",
    AUTHORITY_VERBS_COMPILER: "authority-verbs-settle-mandates-v1",
    GOVERNED_TRIGGERS_COMPILER: "governed-triggers-v1",
    SUBJECT_IDENTITY_COMPILER: "subject-identity-v1",
    PROVIDER_BUILDS_COMPILER: "provider-builds-v1",
    CLAIM_REBIND_COMPILER: "claim-rebind-v1",
    LIGHT_ARTIFACTS_COMPILER: "light-artifacts-v1",
}
PC_HR_ARTIFACT_CODEC_COMPILERS = frozenset(
    {
        PC_HR_COMPILER,
        P2_B1_COMPILER,
        P2_C_COMPILER,
        PC_DF2_COMPILER,
        P2_B2_COMPILER,
        P2_B4_COMPILER,
        P2_B4_UNIT2_COMPILER,
        P2_B5_COMPILER,
        ATTESTATION_COMPILER,
        RESOLUTION_COMPILER,
        ONTOLOGY_COMPILER,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        AUTHORITY_VERBS_COMPILER,
        GOVERNED_TRIGGERS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
        PROVIDER_BUILDS_COMPILER,
        CLAIM_REBIND_COMPILER,
        LIGHT_ARTIFACTS_COMPILER,
    }
)


def current_compiler_coordinate() -> CompilerCoordinate:
    return LIGHT_ARTIFACTS_COMPILER


def artifact_kinds_for_compiler(compiler: CompilerCoordinate) -> ArtifactKindRegistry:
    """Return the frozen ledger path grammar selected by one compiler."""

    if compiler in {PROVIDER_BUILDS_COMPILER, CLAIM_REBIND_COMPILER, LIGHT_ARTIFACTS_COMPILER}:
        return PROVIDER_BUILDS_ARTIFACT_KINDS
    if compiler == SUBJECT_IDENTITY_COMPILER:
        return SUBJECT_IDENTITY_ARTIFACT_KINDS
    if compiler == GOVERNED_TRIGGERS_COMPILER:
        return GOVERNED_TRIGGERS_ARTIFACT_KINDS
    if compiler == AUTHORITY_VERBS_COMPILER:
        return AUTHORITY_VERBS_ARTIFACT_KINDS
    if compiler == TRIGGER_CAPTURE_COMPILER:
        return TRIGGER_CAPTURE_ARTIFACT_KINDS
    if compiler == SOURCE_CHECKED_COMPILER:
        return SOURCE_CHECKED_ARTIFACT_KINDS
    if compiler == CLAIM_EVIDENCE_COMPILER:
        return CLAIM_EVIDENCE_ARTIFACT_KINDS
    if compiler == SDK_SOURCE_COMPILER:
        return SDK_SOURCE_ARTIFACT_KINDS
    if compiler == RESOURCE_BUDGET_COMPILER:
        return RESOURCE_BUDGET_ARTIFACT_KINDS
    if compiler == PROVIDER_PACKAGE_COMPILER:
        return PROVIDER_PACKAGE_ARTIFACT_KINDS
    if compiler == PROVIDER_CONTRACT_COMPILER:
        return PROVIDER_CONTRACT_ARTIFACT_KINDS
    if compiler == UPGRADE_COMPILER:
        return UPGRADE_ARTIFACT_KINDS
    if compiler == ONTOLOGY_COMPILER:
        return ONTOLOGY_ARTIFACT_KINDS
    if compiler == RESOLUTION_COMPILER:
        return RESOLUTION_ARTIFACT_KINDS
    if compiler == ATTESTATION_COMPILER:
        return ATTESTATION_ARTIFACT_KINDS

    if compiler in {
        P2_C_COMPILER,
        PC_DF2_COMPILER,
        P2_B2_COMPILER,
        P2_B4_COMPILER,
        P2_B4_UNIT2_COMPILER,
        P2_B5_COMPILER,
    }:
        return P2_C_ARTIFACT_KINDS
    if compiler == P2_B1_COMPILER:
        return P2_B1_ARTIFACT_KINDS
    if compiler == PC_HR_COMPILER:
        return PLAYBILL_ARTIFACT_KINDS
    if compiler in SUPPORTED_COMPILERS:
        return P2_B0_ARTIFACT_KINDS
    raise FormatError("compiler coordinate has no installed artifact codec")


def artifact_codec_for_compiler(compiler: CompilerCoordinate) -> ArtifactCodec:
    """Return the frozen byte codec selected by one compiler coordinate."""

    if compiler in PC_HR_ARTIFACT_CODEC_COMPILERS:
        return CURRENT_ARTIFACT_CODEC
    if compiler in SUPPORTED_COMPILERS:
        return P2_B0_ARTIFACT_CODEC
    raise FormatError("compiler coordinate has no installed artifact codec")


def projection_registry_for_compiler(
    compiler: CompilerCoordinate,
) -> ProjectionExtensionRegistry:
    if compiler in {
        RESOLUTION_COMPILER,
        ONTOLOGY_COMPILER,
        UPGRADE_COMPILER,
        PROVIDER_CONTRACT_COMPILER,
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        AUTHORITY_VERBS_COMPILER,
    }:
        return p2c_extension_registry().with_artifact_kinds("attestation", "resolution-contract")
    if compiler in {
        SUBJECT_IDENTITY_COMPILER,
        PROVIDER_BUILDS_COMPILER,
        CLAIM_REBIND_COMPILER,
        LIGHT_ARTIFACTS_COMPILER,
    }:
        return p2c_extension_registry().with_artifact_kinds(
            "attestation", "resolution-contract", "trigger", "blueprint", "subject-kind"
        )
    if compiler == GOVERNED_TRIGGERS_COMPILER:
        return p2c_extension_registry().with_artifact_kinds(
            "attestation", "resolution-contract", "trigger", "blueprint"
        )
    if compiler == ATTESTATION_COMPILER:
        return p2c_extension_registry().with_artifact_kinds("attestation")
    if compiler == PB_B_COMPILER:
        return ProjectionExtensionRegistry(())
    if compiler == PB_C_COMPILER:
        return extension_registry()
    if compiler == PB_D_COMPILER:
        return governance_extension_registry()
    if compiler == PC_A1_COMPILER:
        return subject_extension_registry()
    if compiler == PC_A2_COMPILER:
        return claim_type_extension_registry()
    if compiler == PC_B_COMPILER:
        return claim_extension_registry()
    if compiler == PC_C_COMPILER:
        return evidence_extension_registry()
    if compiler == PC_D_COMPILER:
        return procedure_extension_registry()
    if compiler == PC_E1_COMPILER:
        return runtime_extension_registry()
    if compiler == P2_B0_COMPILER:
        return replay_extension_registry()
    if compiler == PC_HR_COMPILER:
        return replay_extension_registry()
    if compiler == P2_B1_COMPILER:
        return provider_runtime_extension_registry()
    if compiler in {
        P2_C_COMPILER,
        PC_DF2_COMPILER,
        P2_B2_COMPILER,
        P2_B4_COMPILER,
        P2_B4_UNIT2_COMPILER,
        P2_B5_COMPILER,
    }:
        return p2c_extension_registry()
    raise FormatError("compiler coordinate has no installed deterministic registry")


__all__ = [
    "ATTESTATION_COMPILER",
    "ONTOLOGY_COMPILER",
    "UPGRADE_COMPILER",
    "PROVIDER_CONTRACT_COMPILER",
    "PROVIDER_PACKAGE_COMPILER",
    "RESOURCE_BUDGET_COMPILER",
    "SDK_SOURCE_COMPILER",
    "CLAIM_EVIDENCE_COMPILER",
    "SOURCE_CHECKED_COMPILER",
    "TRIGGER_CAPTURE_COMPILER",
    "AUTHORITY_VERBS_COMPILER",
    "GOVERNED_TRIGGERS_COMPILER",
    "SUBJECT_IDENTITY_COMPILER",
    "PROVIDER_BUILDS_COMPILER",
    "CLAIM_REBIND_COMPILER",
    "LIGHT_ARTIFACTS_COMPILER",
    "RESOLUTION_COMPILER",
    "PB_B_COMPILER",
    "PB_C_COMPILER",
    "PB_D_COMPILER",
    "PC_A1_COMPILER",
    "PC_A2_COMPILER",
    "PC_B_COMPILER",
    "PC_C_COMPILER",
    "PC_D_COMPILER",
    "PC_E1_COMPILER",
    "P2_B0_COMPILER",
    "P2_B1_COMPILER",
    "P2_B2_COMPILER",
    "P2_B4_COMPILER",
    "P2_B4_UNIT2_COMPILER",
    "P2_B5_COMPILER",
    "P2_C_COMPILER",
    "PC_DF2_COMPILER",
    "PC_HR_COMPILER",
    "PC_HR_ARTIFACT_CODEC_COMPILERS",
    "COMPILER_REVISION_LABELS",
    "SUPPORTED_COMPILERS",
    "current_compiler_coordinate",
    "artifact_codec_for_compiler",
    "artifact_kinds_for_compiler",
    "projection_registry_for_compiler",
]
