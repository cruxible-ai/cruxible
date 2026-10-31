"""Model-constructed decision-only examples for the point-of-use CLI surface."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Final

from cruxible_client.authoring.inputs import (
    ApprovalPolicyInput,
    AuthoringInput,
    CarriedContractInput,
    ChangeSetInput,
    ClaimInput,
    ClaimRetirementInput,
    ClaimTypeInput,
    ClaimTypeSuccessionInput,
    ExactContentObjectInput,
    ExistingCaptureInput,
    LineInput,
    LiteralObjectInput,
    ProcedureInput,
    ProcedureMandateInput,
    ProcedureRuntimePolicyInput,
    QueryDefinitionInput,
    SelfSourceInput,
    SubjectInput,
    SubjectKindInput,
    SubjectObjectInput,
    TriggerInput,
    WorkingSelectionInput,
)
from cruxible_client.contracts import AuthoringExampleName as AuthoringExampleName
from cruxible_client.contracts.approval_policy import ApprovalPolicy
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.artifacts import ArtifactLifecycle as _ArtifactLifecycle
from cruxible_client.contracts.authoring.models import ClaimTypeSuccessionDependent
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.cron import CRON_UTC_HINT
from cruxible_client.contracts.documents import DocumentLifecycle, DocumentShell
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import ProcedureHardCaps
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionSpec,
    QueryEvaluationPolicy,
)
from cruxible_client.contracts.query.grammar import (
    QueryArtifactsEntry,
    QueryBudgets,
    QueryClaimValueRef,
    QueryEntry,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
)
from cruxible_client.contracts.triggers import CronSchedule


def subject_example() -> SubjectInput:
    """Create one work item by its key; core assigns the id (here ``replace-me``).

    The kind needs an accepted SubjectKind first: ``authoring example subject-kind``.
    """

    return SubjectInput(
        kind="subject",
        subject_kind="project.work_item",
        key={"slug": "replace-me"},
        rationale="Replace with why this work item exists.",
    )


def subject_kind_example() -> ChangeSetInput:
    """A single-key unique kind whose key value is the readable slug.

    The key ClaimType and the SubjectKind land in one change. After it, a write
    that names a ``project.work_item/<slug>`` that does not exist yet creates it:
    the slug is its key value and, rendered, its id. The key ClaimType declares
    no evidence rule: a key Claim core writes when it creates the Subject is
    covered by the creation itself, so it is supported whatever it cites (the
    author's words, a write's ``because``, a run's Capture). A later revision of
    the slug is judged by these (absent) rules, so it lands ``uncovered``; add a
    rule if slugs are revised.
    """

    slug = ClaimType(
        artifact_format="playbill-claim-type-v8",
        identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.slug"),
        predicate="project.work_item.slug",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"minLength": 1, "type": "string"},
        cardinality="one",
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        description="The work item's readable name; its key.",
        default_role="normative",
        evidence_requirement="self",
        revision_evidence="replace",
        canonical_form=("nfkc", "trim", "collapse_whitespace", "casefold"),
    )
    return ChangeSetInput(
        kind="change_set",
        members=(
            ClaimTypeInput(kind="claim_type", claim_type=slug),
            SubjectKindInput(
                kind="subject_kind",
                name="project.work_item",
                description="One unit of planned work, named by its slug.",
                keys=("project.work_item.slug",),
                uniqueness="unique",
            ),
        ),
        rationale="Declare how work items are named before creating any.",
    )


def approval_policy_example() -> ApprovalPolicyInput:
    return ApprovalPolicyInput(
        kind="approval_policy",
        approval_policy=ApprovalPolicy(mode="independent_approval_required"),
    )


def procedure_runtime_policy_example() -> ProcedureRuntimePolicyInput:
    return ProcedureRuntimePolicyInput(
        kind="procedure_runtime_policy",
        procedure_runtime_policy=ProcedureRuntimePolicy(provider_output_bytes_cap=2_097_152),
    )


def change_set_example() -> ChangeSetInput:
    """Return one changeset carrying a mix of members that admit or refuse together.

    One authoring surface is one changeset: how work items are named (a key
    ClaimType and its SubjectKind), a Subject created by its key, the ClaimType
    that admits the statement, two Claims that read both, and one retirement
    all lower once and generate once.
    """

    return ChangeSetInput(
        kind="change_set",
        members=(
            *subject_kind_example().members,
            subject_example(),
            ClaimTypeInput(
                kind="claim_type",
                claim_type=ClaimType(
                    artifact_format="playbill-claim-type-v7",
                    identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
                    predicate="project.work_item.owner",
                    allowed_subject_kinds=("project.work_item",),
                    object_kind="literal",
                    literal_schema={"type": "string"},
                    cardinality="one",
                    permitted_roles=("normative", "observation"),
                    evidence_admission_policy=ClaimEvidenceAdmissionPolicy(),
                    admission_policy=ClaimAdmissionPolicy(),
                    resolution_policy=ClaimResolutionPolicy(
                        cardinality="one",
                        eligible_verdicts=("supported",),
                        selector="only_contender",
                    ),
                    evidence_requirement="self",
                    revision_evidence="replace",
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
                retires="CLM-" + "0" * 32,
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
                    artifact_format="playbill-claim-type-v7",
                    identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
                    predicate="project.work_item.owner",
                    allowed_subject_kinds=("project.work_item",),
                    object_kind="literal",
                    literal_schema={"type": "string", "enum": ["replace-me"]},
                    cardinality="one",
                    permitted_roles=("normative", "observation"),
                    evidence_admission_policy=ClaimEvidenceAdmissionPolicy(),
                    admission_policy=ClaimAdmissionPolicy(),
                    resolution_policy=ClaimResolutionPolicy(
                        cardinality="one",
                        eligible_verdicts=("supported",),
                        selector="only_contender",
                    ),
                    lifecycle=_ArtifactLifecycle(predecessor_digest="sha256:" + "0" * 64),
                    evidence_requirement="self",
                    revision_evidence="replace",
                ),
                dependents=(
                    ClaimTypeSuccessionDependent(
                        identity=ArtifactIdentity(kind="Claim", name="CLM-" + "0" * 32),
                        disposition="successor",
                    ),
                    ClaimTypeSuccessionDependent(
                        identity=ArtifactIdentity(kind="Claim", name="CLM-" + "1" * 32),
                        disposition="re_author",
                        successor_claim_id="CLM-" + "1" * 32,
                    ),
                    ClaimTypeSuccessionDependent(
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
                revises="CLM-" + "1" * 32,
            ),
        ),
    )


#: The `--example procedure` hard caps. A mandate's resource ceiling may narrow
#: but never widen its Procedure's caps, so the mandate example reuses these and
#: the two templates are accepted together. The wall-clock caps (and the
#: per-run default below) are a safety factor against an obviously bad run,
#: not compute management, so they stay generous: 10 minutes per run, 30
#: minutes overall.
_EXAMPLE_PROCEDURE_HARD_CAPS: Final = {
    "max_wall_clock": {"microseconds": 1_800_000_000},
    "max_provider_calls": 0,
    "max_capture_bytes": 0,
    "max_items": 200,
    "max_repeat_attempts": 1,
}


def procedure_mandate_example() -> ProcedureMandateInput:
    """A propose grant over the `--example line` Line, within its Procedure's caps.

    Only a Line that proposes or settles needs a mandate; an observe-only Line
    (like the example Procedure's) runs without one.
    """

    return ProcedureMandateInput(
        kind="procedure_mandate",
        name="replace-me",
        line_name="replace-me",
        grants="propose",
        resource_ceiling=ProcedureHardCaps.model_validate(_EXAMPLE_PROCEDURE_HARD_CAPS),
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


def line_example() -> LineInput:
    """A Line over the `--example procedure` Procedure.

    That Procedure has no Source nodes, so the Line names no acquisition
    policy, and its input contract is empty, so `parameters` is `{}`. It only
    observes, so it runs without a ProcedureMandate. It runs when run explicitly
    (`line run`); `--example trigger` schedules it, and the Trigger runs it only
    once the Line is enabled (`line enable`).
    """

    return LineInput(kind="line", name="replace-me", procedure_name="replace-me", parameters={})


def trigger_example() -> TriggerInput:
    """A Trigger that runs the `--example line` Line hourly, on the hour, in UTC.

    Cron expressions are evaluated in UTC; convert local times first (09:00 New
    York in winter is 14:00 UTC).

    Name `line_name` or `action` (a registered internal action such as
    `evidence.sweep`), never both. A schedule is `cadence` (`interval_seconds`),
    `cron` (a five-field UTC `expression`), `generation_accepted` (no fields),
    `capture_landing` (an exact CaptureContract `event`), or `window_close` (a
    `window`). Actions admit timed or generation-accepted schedules; a Line that binds
    its triggering Capture needs a schedule that fires on that exact event. Nothing
    fires before the Trigger is accepted, and a Trigger aimed at a Line does
    nothing until that Line is enabled (`line enable`): this one first runs at
    the top of the hour after both.
    """

    return TriggerInput(
        kind="trigger",
        name="replace-me",
        schedule=CronSchedule(expression="0 * * * *"),
        line_name="replace-me",
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


def claim_revision_example() -> ClaimInput:
    """Revise one accepted Claim: the same statement slot, a new generation of it.

    `revises` names the Claim ID the revision replaces. Omitting it states a new
    Claim instead, with its own freshly minted ID.
    """

    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale="Replace with why the accepted Claim needs this revision.",
        source=SelfSourceInput(kind="self_source", body="status: replace-me\n"),
        revises="CLM-" + "0" * 32,
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
    return ProcedureInput(
        kind="procedure",
        definition={
            "name": "replace-me",
            "description": (
                "Shape, filter, dedupe, join and count typed records with closed transforms."
            ),
            "contract_in": carried("empty-input", "contract-in"),
            "contract_out": carried("count-result", "contract-out"),
            "nodes": [
                {
                    "kind": "project",
                    "node_id": "adapt",
                    "contract_out": carried("collection", "contract-out"),
                    "literal": {
                        "items": [
                            {"id": "one", "keep": True},
                            {"id": "two", "keep": False},
                        ]
                    },
                    "as": "adapted",
                },
                {
                    "kind": "transform",
                    "node_id": "shape",
                    "transform_kind": "shape",
                    "spec": {
                        "tag": "cruxible-transform-shape-spec-v1",
                        "input": "$steps.adapted.items",
                        "fields": {
                            "id": {"from": "$item.fields.id"},
                            "keep": {"from": "$item.fields.keep"},
                            "label": {"from": "$item.fields.id"},
                        },
                    },
                    "as": "shaped",
                },
                {
                    "kind": "transform",
                    "node_id": "filter",
                    "transform_kind": "filter",
                    "spec": {
                        "tag": "cruxible-transform-filter-spec-v1",
                        "input": "$steps.shaped",
                        "where": "eq($item.fields.keep, true)",
                    },
                    "as": "filtered",
                },
                {
                    "kind": "transform",
                    "node_id": "dedupe",
                    "transform_kind": "dedupe",
                    "spec": {
                        "tag": "cruxible-transform-dedupe-spec-v1",
                        "input": "$steps.filtered",
                        "keys": ["id"],
                    },
                    "as": "deduped",
                },
                {
                    "kind": "transform",
                    "node_id": "join",
                    "transform_kind": "join",
                    "spec": {
                        "tag": "cruxible-transform-join-spec-v1",
                        "left": "$steps.deduped",
                        "right": [
                            {"id": "one", "rank": 1},
                            {"id": "two", "rank": 2},
                        ],
                        "left_field": "id",
                        "right_field": "id",
                        "right_fields": {"rank": "rank"},
                    },
                    "as": "joined",
                },
                {
                    "kind": "transform",
                    "node_id": "aggregate",
                    "transform_kind": "aggregate",
                    "spec": {
                        "tag": "cruxible-transform-aggregate-spec-v1",
                        "input": "$steps.joined",
                        "op": "count",
                    },
                    "as": "counted",
                },
                {
                    "kind": "project",
                    "node_id": "result",
                    "contract_out": carried("count-result", "contract-out"),
                    "fields": {"count": "$steps.counted.result"},
                    "as": "result",
                },
            ],
            "returns": "result",
            "budget": {
                "wall_clock": {"microseconds": 600_000_000},
                "max_provider_calls": 0,
                "max_capture_bytes": 0,
                "max_items": 100,
            },
            "hard_caps": _EXAMPLE_PROCEDURE_HARD_CAPS,
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
                name="count-result",
                fields={"count": PropertySchema(type="int")},
            ),
        ),
    )


def query_claims_by_type_example() -> QueryDefinitionInput:
    """Return a governed query template for current supported work-item status."""

    return QueryDefinitionInput(
        kind="query_definition",
        query_definition=QueryDefinitionSpec(
            identity=ArtifactIdentity(
                kind="QueryDefinition",
                name="project.work_items_by_status",
            ),
            description="List supported current status Claims for project work items.",
            entry=QueryEntry(binding="item", subject_kinds=("project.work_item",)),
            result_binding="item",
            result_shape="subject",
            result_cardinality="many",
            dedupe="subject",
            projection=QueryProjection(
                fields=(
                    QueryProjectionField(
                        name="item_id",
                        value=QuerySubjectFieldRef(binding="item", field="subject_id"),
                    ),
                    QueryProjectionField(
                        name="status",
                        value=QueryClaimValueRef(
                            binding="item",
                            predicate="project.work_item.status",
                        ),
                    ),
                )
            ),
            evaluation_policy=QueryEvaluationPolicy(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
            maximum_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
        ),
    )


def query_ontology_example() -> QueryDefinitionInput:
    """An exact namespace selector retains future additions, including from empty state."""
    return QueryDefinitionInput(
        kind="query_definition",
        query_definition=QueryDefinitionSpec(
            artifact_format="cruxible-query-definition-v3",
            identity=ArtifactIdentity(kind="QueryDefinition", name="security.ontology"),
            entry=QueryArtifactsEntry(
                artifact_kind="ClaimType",
                selection="namespaces",
                namespaces=("security.asset", "security.service"),
            ),
            result_binding="definition",
            result_shape="artifact_definition",
            result_cardinality="many",
            dedupe="artifact",
            evaluation_policy=QueryEvaluationPolicy(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
            maximum_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
        ),
    )


def query_procedures_example() -> QueryDefinitionInput:
    query = query_ontology_example().query_definition.model_dump(mode="json")
    query["identity"] = {"kind": "QueryDefinition", "name": "security.procedures"}
    query["entry"] = QueryArtifactsEntry(
        artifact_kind="Procedure", selection="name_prefixes", name_prefixes=("security.",)
    ).model_dump(mode="json")
    return QueryDefinitionInput(
        kind="query_definition", query_definition=QueryDefinitionSpec.model_validate(query)
    )


AuthoringExample = AuthoringInput

AUTHORING_EXAMPLE_FACTORIES: Final[dict[AuthoringExampleName, Callable[[], AuthoringExample]]] = {
    "claim-existing-capture": claim_existing_capture_example,
    "claim-flow-a": claim_flow_a_example,
    "claim-self-source": claim_self_source_example,
    "claim-subject-relation": claim_subject_relation_example,
    "claim-exact-content": claim_exact_content_example,
    "claim-revision": claim_revision_example,
    "procedure": procedure_example,
    "query-claims-by-type": query_claims_by_type_example,
    "query-ontology": query_ontology_example,
    "query-procedures": query_procedures_example,
    "subject": subject_example,
    "subject-kind": subject_kind_example,
    "approval-policy": approval_policy_example,
    "procedure-runtime-policy": procedure_runtime_policy_example,
    "procedure-mandate": procedure_mandate_example,
    "line": line_example,
    "trigger": trigger_example,
    "change-set": change_set_example,
    "claim-type-succession": claim_type_succession_example,
}

#: One line shown beside an example's payload, where the payload alone could mislead.
AUTHORING_EXAMPLE_NOTES: Final[dict[AuthoringExampleName, str]] = {
    "query-claims-by-type": (
        "result_shape, result_cardinality, dedupe, evaluation_policy and both budgets "
        "here are the defaults (subject / many / subject / supported, current, conflicts "
        "surfaced / 100 and 1000); a Subject query may leave them out."
    ),
    "trigger": (
        CRON_UTC_HINT + ' Generation floor refresh: {"kind":"trigger",'
        '"name":"floor-refresh","schedule":{"kind":"generation_accepted"},'
        '"action":"floor.refresh"}.'
    ),
}


def authoring_example_note(name: AuthoringExampleName) -> str | None:
    return AUTHORING_EXAMPLE_NOTES.get(name)


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
    "claim-revision",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
    "query-claims-by-type",
    "query-ontology",
    "query-procedures",
    "subject",
    "subject-kind",
    "approval-policy",
    "procedure-runtime-policy",
    "procedure-mandate",
    "line",
    "trigger",
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
        revises=claim_id,
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
    "AUTHORING_EXAMPLE_NOTES",
    "AuthoringExampleName",
    "authoring_example",
    "authoring_example_note",
    "change_set_example",
    "claim_type_succession_example",
    "claim_existing_capture_example",
    "claim_flow_a_example",
    "claim_exact_content_example",
    "claim_revision_example",
    "claim_self_source_example",
    "claim_subject_relation_example",
    "document_example",
    "approval_policy_example",
    "line_example",
    "procedure_example",
    "procedure_mandate_example",
    "query_claims_by_type_example",
    "subject_example",
]
