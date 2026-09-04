"""Model-constructed decision-only examples for the point-of-use CLI surface."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Final, Literal

from cruxible_client.authoring.inputs import (
    ApprovalPolicyInput,
    AuthoringInputV1,
    CarriedContractInput,
    ChangeSetInput,
    ClaimInput,
    ClaimRetirementInput,
    ClaimTypeInput,
    ClaimTypeSuccessionInput,
    ExactContentObjectInput,
    ExistingCaptureInput,
    LiteralObjectInput,
    ProcedureInput,
    ProcedureMandateInputV1,
    ProcedureRuntimePolicyInput,
    QueryDefinitionInput,
    SelfSourceInput,
    SubjectInput,
    SubjectObjectInput,
    WorkingSelectionInput,
)
from cruxible_client.contracts.approval_policy import ApprovalPolicyV1
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.artifacts import ArtifactLifecycle as _ArtifactLifecycle
from cruxible_client.contracts.authoring.models import ClaimTypeSuccessionDependentV1
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.documents import DocumentLifecycle, DocumentShell
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicyV1
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import ProcedureHardCapsV3
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimValueRefV1,
    QueryEntryV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
    QuerySubjectFieldRefV1,
)
from cruxible_client.contracts.subjects import SubjectShell

AuthoringExampleName = Literal[
    "claim-existing-capture",
    "claim-flow-a",
    "claim-self-source",
    "claim-subject-relation",
    "claim-exact-content",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
    "query-claims-by-type",
    "subject",
    "approval-policy",
    "procedure-runtime-policy",
    "procedure-mandate",
    "change-set",
    "claim-type-succession",
]


def subject_example() -> SubjectInput:
    return SubjectInput(
        kind="subject",
        subject=SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name="project.work_item/replace-me"),
            subject_kind="project.work_item",
            subject_id="replace-me",
        ),
    )


def approval_policy_example() -> ApprovalPolicyInput:
    return ApprovalPolicyInput(
        kind="approval_policy",
        approval_policy=ApprovalPolicyV1(mode="independent_approval_required"),
    )


def procedure_runtime_policy_example() -> ProcedureRuntimePolicyInput:
    return ProcedureRuntimePolicyInput(
        kind="procedure_runtime_policy",
        procedure_runtime_policy=ProcedureRuntimePolicyV1(provider_output_bytes_cap=2_097_152),
    )


def change_set_example() -> ChangeSetInput:
    """Return one changeset carrying a mix of members that admit or refuse together.

    One authoring surface is one changeset: a Subject, the ClaimType that
    admits the statement, two Claims that read both, and one retirement all
    lower once and generate once.
    """

    return ChangeSetInput(
        kind="change_set",
        members=(
            subject_example(),
            ClaimTypeInput(
                kind="claim_type",
                claim_type=ClaimType(
                    identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
                    predicate="project.work_item.owner",
                    allowed_subject_kinds=("project.work_item",),
                    object_kind="literal",
                    literal_schema={"type": "string"},
                    cardinality="one",
                    permitted_roles=("normative", "observation"),
                    evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
                    admission_policy=ClaimAdmissionPolicyV1(),
                    resolution_policy=ClaimResolutionPolicyV1(
                        cardinality="one",
                        eligible_verdicts=("supported",),
                        selector="only_contender",
                    ),
                ),
            ),
            claim_self_source_example(),
            ClaimInput(
                kind="claim",
                subject="project.work_item/replace-me",
                predicate="project.work_item.owner",
                object=LiteralObjectInput(kind="literal", value="replace-me"),
                role="observation",
                rationale="Replace with why this work item has this owner.",
                source=SelfSourceInput(kind="self_source", body="owner: replace-me\n"),
            ),
            ClaimRetirementInput(
                kind="claim_retirement",
                claim_id="CLM-" + "0" * 32,
                reason="was-rescinded",
            ),
        ),
    )


def claim_type_succession_example() -> ChangeSetInput:
    """Return one changeset that evolves a committed vocabulary in one generation.

    The succession names the ClaimType it replaces and pins its exact current
    digest; every member of that ClaimType's reverse-pin closure is
    dispositioned in the same set -- one carried to the successor, one
    tombstoned, one re-authored as the sibling Claim member that says it again
    under the new vocabulary.
    """

    return ChangeSetInput(
        kind="change_set",
        members=(
            ClaimTypeSuccessionInput(
                kind="claim_type_succession",
                successor=ClaimType(
                    identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
                    predicate="project.work_item.owner",
                    allowed_subject_kinds=("project.work_item",),
                    object_kind="literal",
                    literal_schema={"type": "string", "enum": ["replace-me"]},
                    cardinality="one",
                    permitted_roles=("normative", "observation"),
                    evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
                    admission_policy=ClaimAdmissionPolicyV1(),
                    resolution_policy=ClaimResolutionPolicyV1(
                        cardinality="one",
                        eligible_verdicts=("supported",),
                        selector="only_contender",
                    ),
                    lifecycle=_ArtifactLifecycle(predecessor_digest="sha256:" + "0" * 64),
                ),
                dependents=(
                    ClaimTypeSuccessionDependentV1(
                        identity=ArtifactIdentity(kind="Claim", name="CLM-" + "0" * 32),
                        disposition="successor",
                    ),
                    ClaimTypeSuccessionDependentV1(
                        identity=ArtifactIdentity(kind="Claim", name="CLM-" + "1" * 32),
                        disposition="re_author",
                        successor_claim_id="CLM-" + "1" * 32,
                    ),
                    ClaimTypeSuccessionDependentV1(
                        identity=ArtifactIdentity(kind="Claim", name="CLM-" + "2" * 32),
                        disposition="retire",
                        claim_retirement_reason="was-rescinded",
                    ),
                ),
            ),
            ClaimInput(
                kind="claim",
                subject="project.work_item/replace-me",
                predicate="project.work_item.owner",
                object=LiteralObjectInput(kind="literal", value="replace-me"),
                role="observation",
                rationale="Replace with why this owner is right under the new vocabulary.",
                source=SelfSourceInput(kind="self_source", body="owner: replace-me\n"),
                claim_id="CLM-" + "1" * 32,
            ),
        ),
    )


def procedure_mandate_example() -> ProcedureMandateInputV1:
    return ProcedureMandateInputV1(
        kind="procedure_mandate",
        name="replace-me",
        procedure_name="replace-me",
        rung=2,
        authority_ceiling=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=60_000_000),
            max_provider_calls=100,
            max_capture_bytes=10_000_000,
            max_items=1000,
            max_repeat_attempts=5,
        ),
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


def document_example() -> DocumentShell:
    return DocumentShell(
        identity="document:replace-me",
        document_kind="reference",
        title="Replace with a governed document title",
        media_type="text/markdown",
        body_digest="sha256:" + "0" * 64,
        governance_scope=("project.replace-me",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def claim_existing_capture_example() -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale="Replace with why the accepted Capture supports this statement.",
        source=ExistingCaptureInput(
            kind="existing_capture",
            capture_digest="sha256:" + "0" * 64,
        ),
        citation_role="evidence",
    )


def claim_flow_a_example() -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale="Replace with why this source supports the statement.",
        source=WorkingSelectionInput(
            kind="working_selection",
            source_id="repo.replace-me",
        ),
        citation_role="evidence",
    )


def claim_self_source_example() -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale="Replace with why this new statement should be governed.",
        source=SelfSourceInput(kind="self_source", body="status: replace-me\n"),
    )


def claim_subject_relation_example() -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="sec.vulnerability/cve-replace-me",
        predicate="sec.vuln.affects_package",
        object=SubjectObjectInput(kind="subject", subject="sec.package/replace-me"),
        role="observation",
        rationale="Replace with why this vulnerability affects the accepted package.",
        source=SelfSourceInput(kind="self_source", body="affected package: replace-me\n"),
    )


def claim_exact_content_example() -> ClaimInput:
    """A Claim whose object IS the text, not a value that names it.

    Rulings and method laws are this shape: the statement is the wording, so the
    object carries the body rather than a literal the ClaimType admits. `text`
    is the ordinary spelling; `content_base64` is the same object for bytes that
    are not text.
    """

    return ClaimInput(
        kind="claim",
        subject="project.method/replace-me",
        predicate="project.method.law",
        object=ExactContentObjectInput(
            kind="exact_content",
            text="Replace with the ruling exactly as it was written.\n",
        ),
        role="normative",
        rationale="Replace with why this wording is the governed one.",
        source=SelfSourceInput(
            kind="self_source",
            body="Replace with the ruling exactly as it was written.\n",
        ),
    )


def procedure_example() -> ProcedureInput:
    def carried(name: str, role: str) -> dict[str, str]:
        return {"kind": "carried_contract", "name": name, "role": role}

    raw_item = {
        "id": PropertySchema(type="string"),
        "keep": PropertySchema(type="bool"),
    }
    shaped_item = {
        **raw_item,
        "label": PropertySchema(type="string"),
    }
    joined_item = {
        "id": PropertySchema(type="string"),
        "rank": PropertySchema(type="int"),
    }

    return ProcedureInput(
        kind="procedure",
        definition={
            "graph_format": 3,
            "name": "replace-me",
            "description": "Run all six deterministic compute kernels over typed collections.",
            "contract_in": carried("empty-input", "contract-in"),
            "contract_out": carried("count-result", "contract-out"),
            "nodes": [
                {
                    "kind": "transform",
                    "node_id": "adapt",
                    "transform_kind": "adapter",
                    "contract_in": carried("collection", "contract-in"),
                    "contract_out": carried("collection", "contract-out"),
                    "spec": {
                        "tag": "playbill-transform-adapter-spec-v1",
                        "value": {
                            "items": [
                                {"id": "one", "keep": True},
                                {"id": "two", "keep": False},
                            ]
                        },
                    },
                    "as": "adapted",
                },
                {
                    "kind": "transform",
                    "node_id": "shape",
                    "transform_kind": "shape_items",
                    "contract_in": carried("shape-spec", "contract-in"),
                    "contract_out": carried("shaped-result", "contract-out"),
                    "spec": {
                        "tag": "playbill-transform-shape-items-spec-v1",
                        "items": "$steps.adapted.items",
                        "fields": {"label": "$item.id"},
                        "include_input": True,
                    },
                    "as": "shaped",
                },
                {
                    "kind": "transform",
                    "node_id": "filter",
                    "transform_kind": "filter_items",
                    "contract_in": carried("filter-spec", "contract-in"),
                    "contract_out": carried("shaped-result", "contract-out"),
                    "spec": {
                        "tag": "playbill-transform-filter-items-spec-v1",
                        "items": "$steps.shaped.items",
                        "where": {"keep": True},
                    },
                    "as": "filtered",
                },
                {
                    "kind": "transform",
                    "node_id": "dedupe",
                    "transform_kind": "dedupe_items",
                    "contract_in": carried("dedupe-spec", "contract-in"),
                    "contract_out": carried("shaped-result", "contract-out"),
                    "spec": {
                        "tag": "playbill-transform-dedupe-items-spec-v1",
                        "items": "$steps.filtered.items",
                        "keys": ["id"],
                    },
                    "as": "deduped",
                },
                {
                    "kind": "transform",
                    "node_id": "join",
                    "transform_kind": "join_items",
                    "contract_in": carried("join-spec", "contract-in"),
                    "contract_out": carried("joined-result", "contract-out"),
                    "spec": {
                        "tag": "playbill-transform-join-items-spec-v1",
                        "left_items": "$steps.deduped.items",
                        "right_items": [
                            {"id": "one", "rank": 1},
                            {"id": "two", "rank": 2},
                        ],
                        "left_key": "id",
                        "right_key": "id",
                        "fields": {"id": "$item.left.id", "rank": "$item.right.rank"},
                    },
                    "as": "joined",
                },
                {
                    "kind": "transform",
                    "node_id": "aggregate",
                    "transform_kind": "aggregate_items",
                    "contract_in": carried("aggregate-spec", "contract-in"),
                    "contract_out": carried("count-result", "contract-out"),
                    "spec": {
                        "tag": "playbill-transform-aggregate-items-spec-v1",
                        "items": "$steps.joined.items",
                    },
                    "as": "result",
                },
            ],
            "returns": "result",
            "pin_slots": [],
            "budget": {
                "wall_clock": {"microseconds": 2_000_000},
                "max_provider_calls": 0,
                "max_capture_bytes": 0,
                "max_items": 100,
            },
            "hard_caps": {
                "max_wall_clock": {"microseconds": 4_000_000},
                "max_provider_calls": 0,
                "max_capture_bytes": 0,
                "max_items": 200,
                "max_repeat_attempts": 1,
            },
            "terminal_capability": 1,
        },
        activation_policy="snapshot",
        contracts=(
            CarriedContractInput(name="empty-input", fields={}),
            CarriedContractInput(
                name="collection",
                fields={"items": PropertySchema(type="list", item_fields=raw_item)},
            ),
            CarriedContractInput(
                name="shape-spec",
                fields={
                    "items": PropertySchema(type="list", item_fields=raw_item),
                    "fields": PropertySchema(type="json"),
                    "include_input": PropertySchema(type="bool"),
                },
            ),
            CarriedContractInput(
                name="shaped-result",
                fields={
                    "items": PropertySchema(type="list", item_fields=shaped_item),
                    "input_count": PropertySchema(type="int"),
                    "output_count": PropertySchema(type="int"),
                },
            ),
            CarriedContractInput(
                name="filter-spec",
                fields={
                    "items": PropertySchema(type="list", item_fields=shaped_item),
                    "where": PropertySchema(type="json"),
                },
            ),
            CarriedContractInput(
                name="dedupe-spec",
                fields={
                    "items": PropertySchema(type="list", item_fields=shaped_item),
                    "keys": PropertySchema(type="json"),
                },
            ),
            CarriedContractInput(
                name="join-spec",
                fields={
                    "left_items": PropertySchema(type="list", item_fields=shaped_item),
                    "right_items": PropertySchema(
                        type="list",
                        item_fields={
                            "id": PropertySchema(type="string"),
                            "rank": PropertySchema(type="int"),
                        },
                    ),
                    "left_key": PropertySchema(type="string"),
                    "right_key": PropertySchema(type="string"),
                    "fields": PropertySchema(type="json"),
                },
            ),
            CarriedContractInput(
                name="joined-result",
                fields={
                    "items": PropertySchema(type="list", item_fields=joined_item),
                    "output_count": PropertySchema(type="int"),
                },
            ),
            CarriedContractInput(
                name="aggregate-spec",
                fields={"items": PropertySchema(type="list", item_fields=joined_item)},
            ),
            CarriedContractInput(
                name="count-result",
                fields={"count": PropertySchema(type="int")},
            ),
        ),
    )


def query_claims_by_type_example() -> QueryDefinitionInput:
    """Return a governed query template for current supported work-item status."""

    return QueryDefinitionInput(
        kind="query_definition",
        query_definition=QueryDefinitionV1(
            identity=ArtifactIdentity(
                kind="QueryDefinition",
                name="project.work_items_by_status",
            ),
            description="List supported current status Claims for project work items.",
            entry=QueryEntryV1(binding="item", subject_kinds=("project.work_item",)),
            result_binding="item",
            result_shape="subject",
            result_cardinality="many",
            dedupe="subject",
            projection=QueryProjectionV1(
                fields=(
                    QueryProjectionFieldV1(
                        name="item_id",
                        value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                    ),
                    QueryProjectionFieldV1(
                        name="status",
                        value=QueryClaimValueRefV1(
                            binding="item",
                            predicate="project.work_item.status",
                        ),
                    ),
                )
            ),
            evaluation_policy=QueryEvaluationPolicyV1(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgetsV1(max_results=100, max_traversal_depth=0),
            maximum_budgets=QueryBudgetsV1(max_results=1000, max_traversal_depth=0),
            pins=(
                ArtifactPin(
                    role="claim-type",
                    target=ArtifactIdentity(
                        kind="ClaimType",
                        name="project.work_item.status",
                    ),
                    artifact_digest="sha256:" + "0" * 64,
                ),
            ),
        ),
    )


AuthoringExample = AuthoringInputV1

AUTHORING_EXAMPLE_FACTORIES: Final[dict[AuthoringExampleName, Callable[[], AuthoringExample]]] = {
    "claim-existing-capture": claim_existing_capture_example,
    "claim-flow-a": claim_flow_a_example,
    "claim-self-source": claim_self_source_example,
    "claim-subject-relation": claim_subject_relation_example,
    "claim-exact-content": claim_exact_content_example,
    "procedure": procedure_example,
    "query-claims-by-type": query_claims_by_type_example,
    "subject": subject_example,
    "approval-policy": approval_policy_example,
    "procedure-runtime-policy": procedure_runtime_policy_example,
    "procedure-mandate": procedure_mandate_example,
    "change-set": change_set_example,
    "claim-type-succession": claim_type_succession_example,
}

_DOOR_EXAMPLES = {
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
}
AUTHORING_EXAMPLE_NAMES: Final[tuple[AuthoringExampleName, ...]] = (
    "claim-existing-capture",
    "claim-flow-a",
    "claim-self-source",
    "claim-subject-relation",
    "claim-exact-content",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
    "query-claims-by-type",
    "subject",
    "approval-policy",
    "procedure-runtime-policy",
    "procedure-mandate",
    "change-set",
    "claim-type-succession",
)


def _door_example(
    name: AuthoringExampleName,
    *,
    claim_id: str,
    capture_digest: str,
) -> ClaimInput:
    rationale = {
        "claim-adjudicate-contradicting-evidence": (
            "Replace with the adjudication of this contradicting Capture."
        ),
        "claim-cite-supporting-evidence": (
            "Replace the statement fields, then cite this supporting Capture."
        ),
        "claim-adjudicate-unreviewed-evidence": (
            "Replace with the adjudication reached after reviewing this Capture."
        ),
    }[name]
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale=rationale,
        source=ExistingCaptureInput(kind="existing_capture", capture_digest=capture_digest),
        citation_role="evidence",
        claim_id=claim_id,
    )


def authoring_example(
    name: AuthoringExampleName,
    *,
    claim_id: str | None = None,
    capture_digest: str | None = None,
) -> AuthoringExample:
    door = name in _DOOR_EXAMPLES
    if door:
        if claim_id is None or capture_digest is None:
            raise ValueError("attestation-door examples require claim_id and capture_digest")
        return _door_example(name, claim_id=claim_id, capture_digest=capture_digest)
    if claim_id is not None or capture_digest is not None:
        raise ValueError("claim_id/capture_digest apply only to attestation-door examples")
    return AUTHORING_EXAMPLE_FACTORIES[name]()


__all__ = [
    "AUTHORING_EXAMPLE_FACTORIES",
    "AUTHORING_EXAMPLE_NAMES",
    "AuthoringExampleName",
    "authoring_example",
    "change_set_example",
    "claim_type_succession_example",
    "claim_existing_capture_example",
    "claim_flow_a_example",
    "claim_exact_content_example",
    "claim_self_source_example",
    "claim_subject_relation_example",
    "document_example",
    "approval_policy_example",
    "procedure_example",
    "procedure_mandate_example",
    "query_claims_by_type_example",
    "subject_example",
]
