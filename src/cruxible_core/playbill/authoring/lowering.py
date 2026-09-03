"""Lower ergonomic authoring payloads onto ordinary governed artifact wires."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn, TypeAlias

from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    approval_policy_digest,
    render_approval_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    ApprovalPolicyAuthoringPayloadV1,
    AuthoringArtifactReferenceV1,
    AuthoringCandidateReferenceV1,
    AuthoringChangeSetMemberV1,
    AuthoringExactContentObjectV1,
    AuthoringIntentV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimAuthoringPayloadV3,
    ClaimRetirementMemberV1,
    ClaimTypeAuthoringPayloadV1,
    ClaimTypeSuccessionMemberV1,
    ExistingCaptureCitationSourceV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
    ProcedureMandateAuthoringPayloadV1,
    ProcedureRuntimePolicyAuthoringPayloadV1,
    QueryDefinitionAuthoringPayloadV1,
    RepairAlternativeV1,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
    WorkingSelectionObservationV1,
    authoring_member_identity,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    CaptureBuildResult,
    DirectCaptureBuildResult,
    build_coordinator_self_source_capture,
    build_working_selection_capture,
    capture_contract_digest,
    capture_contract_path,
    classify_capture_reuse,
    parse_capture_contract,
    parse_capture_envelope,
    render_capture_contract,
    verify_capture,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    CitationOrigin,
    CitationRole,
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimArtifactV3,
    ClaimBackingV2,
    ClaimReferentContext,
    ClaimRetireDependentV1,
    ClaimStatement,
    ExactContentClaimObject,
    LiteralClaimObject,
    SubjectClaimObject,
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_referent_context_digest,
    claim_statement_address,
    claim_statement_digest,
    evaluate_capture_evidence_admissions,
    merge_claim_citations,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    parse_procedure_mandate,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_PATH,
    procedure_runtime_policy_digest,
    render_procedure_runtime_policy,
)
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactAny,
    ProcedureArtifactV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    parse_procedure,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest_v3,
)
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV3, iter_pin_bindings
from cruxible_client.contracts.providers import parse_provider, provider_digest, provider_path
from cruxible_client.contracts.query.definitions import (
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.source_references import LedgerSourceReferenceV1
from cruxible_client.contracts.subjects import (
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_core.playbill.citation_relations import (
    RELATION_CONTRACT_SCHEMA,
    capture_contract_relation_subject,
)
from cruxible_core.playbill.claim_retirement import (
    ClaimRetireError,
    build_claim_retirement_candidate,
    claim_retirement_inventory,
)
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    ClaimTypeMigrationError,
    build_claim_type_migration_candidate,
    claim_type_migration_inventory,
    resolve_claim_type_succession,
)
from cruxible_core.playbill.compiler import (
    artifact_codec_for_compiler,
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.producer_receipts import local_producer_receipt_resolver
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_artifacts import parse_projection_tree


@dataclass(eq=False)
class AuthoringLoweringError(ValueError):
    code: str
    offending_element: str
    message: str
    repairs: tuple[RepairAlternativeV1, ...]

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class LoweredAuthoring:
    proposed_tree: dict[str, bytes]
    resolved_authoring: dict[str, object]
    changed_members: tuple[tuple[str, bytes], ...]
    idempotent: bool = False
    member_by_path: Mapping[str, int] = field(default_factory=dict)
    """Which change-set member authored each path this lowering wrote.

    A change set admits or refuses whole, typed to the offending member, and
    the compiler laws that run after lowering -- succession, permitted roles,
    cardinality, evidence admission -- address the artifact path they refused,
    not the member the author wrote. This is the map back. Empty for a singular
    intent, which owns every path it writes."""


@dataclass(frozen=True)
class _TreeLedgerResolver:
    tree: Mapping[str, bytes]
    coordinate: AcceptedCoordinate

    def read_ledger_source(self, source: LedgerSourceReferenceV1) -> bytes:
        if source.coordinate != self.coordinate:
            raise ValueError("ledger Capture source names another accepted coordinate")
        content = self.tree.get(source.address.artifact_path)
        if content is None:
            raise ValueError("ledger Capture source is absent at its accepted coordinate")
        return content


def _capture_contract_at_base(
    instance: PlaybillInstance,
    *,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    contract_digest: str,
) -> AcceptedCaptureContract:
    with instance.bind_accepted_projection(base) as projection:
        facts = projection.semantic_facts(
            RELATION_CONTRACT_SCHEMA,
            subject_identity=capture_contract_relation_subject(contract_digest),
        )
    if len(facts) != 1 or not isinstance(facts[0].value, dict):
        _refuse(
            "playbill.authoring.capture_contract_unresolved",
            "source.capture_digest",
            "The existing Capture's exact CaptureContract is not accepted at this base.",
            repair_kind="replace_capture",
            repair_description=(
                "Choose a Capture whose exact contract is accepted at the intent base."
            ),
        )
    value = facts[0].value
    path_value = value.get("path")
    path = path_value.get("$path") if isinstance(path_value, dict) else None
    if not isinstance(path, str) or not isinstance(base_tree.get(path), bytes):
        _refuse(
            "playbill.authoring.capture_contract_unresolved",
            "source.capture_digest",
            "The CaptureContract relation does not resolve an accepted artifact.",
            repair_kind="replace_capture",
            repair_description=(
                "Choose a Capture whose exact contract is accepted at the intent base."
            ),
        )
    contract = parse_capture_contract(base_tree[path], path=path)
    accepted = AcceptedCaptureContract(
        path=path,
        contract=contract,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    if accepted.artifact_digest != contract_digest:
        _refuse(
            "playbill.authoring.capture_contract_unresolved",
            "source.capture_digest",
            "The indexed CaptureContract differs from the Capture's exact contract digest.",
            repair_kind="replace_capture",
            repair_description=(
                "Choose a Capture whose exact contract is accepted at the intent base."
            ),
        )
    return accepted


def _producer_digests_for_capture(
    tree: Mapping[str, bytes],
    *,
    producer: ArtifactIdentity,
    executable: ArtifactIdentity,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for identity in {producer, executable}:
        path = (
            provider_path(identity.name)
            if identity.kind == "Provider"
            else procedure_path(identity.name)
            if identity.kind == "Procedure"
            else None
        )
        if path is None:
            continue
        content = tree.get(path)
        if content is None:
            continue
        if identity.kind == "Provider":
            provider = parse_provider(content, path=path)
            result[identity.qualified] = provider_digest(provider).tagged
        else:
            procedure = parse_procedure(content, path=path)
            result[identity.qualified] = procedure_artifact_digest(procedure).tagged
    return result


def _refuse(
    code: str,
    offending_element: str,
    message: str,
    *,
    repair_kind: str,
    repair_description: str,
    replacement: object | None = None,
) -> NoReturn:
    raise AuthoringLoweringError(
        code=code,
        offending_element=offending_element,
        message=message,
        repairs=(
            RepairAlternativeV1(
                kind=repair_kind,
                description=repair_description,
                replacement=replacement,
            ),
        ),
    )


def _observed_at(timestamp: str) -> datetime:
    raw = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    return datetime.fromisoformat(raw)


def _referent(
    tree: dict[str, bytes],
    address: SemanticAddress,
    *,
    descriptor: bool,
) -> tuple[ArtifactIdentity, str]:
    if address.selector.scheme != "artifact-v1":
        _refuse(
            "playbill.authoring.referent_not_whole_artifact",
            "statement.subject",
            "Claim referents must name a whole accepted Subject artifact.",
            repair_kind="replace_subject",
            repair_description="Use the Subject's whole-artifact semantic address.",
        )
    content = tree.get(address.artifact_path)
    if content is None:
        _refuse(
            "playbill.authoring.referent_not_found",
            "statement.subject",
            "The Claim referent does not exist at the intent base.",
            repair_kind="replace_subject",
            repair_description="Choose a Subject accepted at the intent base.",
        )
    if address.artifact_path.startswith("subjects/"):
        shell = parse_subject(content, path=address.artifact_path)
        return shell.identity, subject_digest(shell).tagged
    if descriptor and address.artifact_path.startswith("claim-types/"):
        claim_type = parse_claim_type(content, path=address.artifact_path)
        return claim_type.identity, claim_type_digest(claim_type).tagged
    _refuse(
        "playbill.authoring.referent_kind_not_admitted",
        "statement.subject",
        "The selected artifact kind is not admitted as this Claim's referent.",
        repair_kind="replace_subject",
        repair_description="Choose an accepted Subject of an admitted kind.",
    )


def _exact_object(
    instance: PlaybillInstance,
    value: object,
) -> LiteralClaimObject | SubjectClaimObject | ExactContentClaimObject:
    if not isinstance(value, AuthoringExactContentObjectV1):
        assert isinstance(value, LiteralClaimObject | SubjectClaimObject)
        return value
    content = value.content
    stored = instance.body_store().store(content)
    return ExactContentClaimObject(
        content_digest=stored.digest,
        span=ContentSpan(
            content_digest=stored.digest,
            start_byte=0,
            end_byte=len(content),
        ),
    )


def _same_predicate_claims(
    tree: dict[str, bytes], statement: ClaimStatement
) -> tuple[ClaimArtifactAny, ...]:
    """Return every live Claim on this statement's (subject, predicate).

    Wider than the contended slot: these are the Claims an author may
    disposition voluntarily, not the ones the law demands.
    """
    claims: list[ClaimArtifactAny] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if (
            claim.lifecycle.state == "live"
            and claim.statement.subject == statement.subject
            and claim.statement.predicate == statement.predicate
        ):
            claims.append(claim)
    return tuple(claims)


def _same_slot_claims(
    tree: dict[str, bytes], statement: ClaimStatement
) -> tuple[ClaimArtifactAny, ...]:
    """Return the live Claims contending for this statement's own slot.

    The slot is ``(subject, predicate, qualifier)`` — the same partition the
    resolution policy and the curation detectors already use. Matching on
    subject+predicate alone made every distinct qualifier and every
    cardinality=many sibling demand a disposition it does not contend with.
    """
    claims: list[ClaimArtifactAny] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if (
            claim.lifecycle.state == "live"
            and claim.statement.subject == statement.subject
            and claim.statement.predicate == statement.predicate
            and claim.statement.qualifier == statement.qualifier
        ):
            claims.append(claim)
    return tuple(claims)


def _merge_pins(*groups: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
    by_key: dict[tuple[str, str], ArtifactPin] = {}
    for pin in (item for group in groups for item in group):
        by_key[(pin.role, pin.target.qualified)] = pin
    return tuple(
        by_key[key] for key in sorted(by_key, key=lambda item: (item[0].encode(), item[1].encode()))
    )


def _merge_mappings(*groups: tuple[SourceMapping, ...]) -> tuple[SourceMapping, ...]:
    by_wire = {
        canonical_bytes(item.model_dump(mode="json")): item for group in groups for item in group
    }
    return tuple(by_wire[key] for key in sorted(by_wire))


def _install_claim_dependencies(
    payload: ClaimAuthoringPayloadV1,
    *,
    base_tree: dict[str, bytes],
) -> tuple[dict[str, bytes], set[str]]:
    """Install a Claim's Subject and ClaimType drafts against the tree it lowers on.

    `base_tree` is the STAGED tree, so a sibling member that defines the Subject
    or the ClaimType earlier in the same change set already satisfies the draft.
    The withdrawn `dependency_not_one_claim` refusal read a succession in a draft
    as proof that the author meant a second change; a change set IS one change,
    so the staged tree decides instead: a draft that differs from what is already
    staged or accepted at that identity still refuses, and one that agrees with
    it installs nothing.
    """

    candidate_tree = dict(base_tree)
    changed_paths: set[str] = set()
    if not isinstance(payload, ClaimAuthoringPayloadV2 | ClaimAuthoringPayloadV3):
        return candidate_tree, changed_paths

    drafts = payload.dependency_drafts
    subject_artifact_path = payload.statement.subject.artifact_path
    subject_draft = drafts.subject
    if subject_draft is not None:
        draft_path = subject_path(subject_draft.subject_kind, subject_draft.subject_id)
        if (
            payload.statement.subject.selector.scheme != "artifact-v1"
            or draft_path != subject_artifact_path
        ):
            _refuse(
                "playbill.authoring.dependency_subject_mismatch",
                "dependency_drafts.subject",
                "The Subject draft does not equal this Claim's exact subject.",
                repair_kind="replace_dependency_subject",
                repair_description="Use the Subject named by statement.subject.",
            )
        rendered = render_subject(subject_draft)
        accepted = base_tree.get(draft_path)
        if accepted is not None and accepted != rendered:
            _refuse(
                "playbill.authoring.dependency_conflicts_with_accepted",
                "dependency_drafts.subject",
                "The Subject dependency conflicts with accepted bytes at this identity.",
                repair_kind="use_accepted_subject",
                repair_description="Refresh the Subject ref from the accepted coordinate.",
            )
        if accepted is None:
            candidate_tree[draft_path] = rendered
            changed_paths.add(draft_path)
    elif subject_artifact_path not in base_tree:
        _refuse(
            "playbill.authoring.dependency_subject_required",
            "dependency_drafts.subject",
            "This Claim's Subject is absent at the intent base.",
            repair_kind="provide_dependency_subject",
            repair_description="Supply the exact Subject draft for statement.subject.",
        )

    type_artifact_path = claim_type_path(payload.statement.predicate)
    claim_type_draft = drafts.claim_type
    if claim_type_draft is not None:
        draft_path = claim_type_path(claim_type_draft.predicate)
        if draft_path != type_artifact_path:
            _refuse(
                "playbill.authoring.dependency_claim_type_mismatch",
                "dependency_drafts.claim_type",
                "The ClaimType draft does not equal this Claim's predicate.",
                repair_kind="replace_dependency_claim_type",
                repair_description="Use the ClaimType named by statement.predicate.",
            )
        rendered = render_claim_type(claim_type_draft)
        accepted = base_tree.get(draft_path)
        if accepted is not None and accepted != rendered:
            _refuse(
                "playbill.authoring.dependency_conflicts_with_accepted",
                "dependency_drafts.claim_type",
                "The ClaimType dependency conflicts with accepted bytes at this identity.",
                repair_kind="use_accepted_claim_type",
                repair_description="Refresh the ClaimType ref from the accepted coordinate.",
            )
        if accepted is None:
            candidate_tree[draft_path] = rendered
            changed_paths.add(draft_path)
    elif type_artifact_path not in base_tree:
        _refuse(
            "playbill.authoring.dependency_claim_type_required",
            "dependency_drafts.claim_type",
            "This Claim's ClaimType is absent at the intent base.",
            repair_kind="provide_dependency_claim_type",
            repair_description="Supply the exact ClaimType draft for statement.predicate.",
        )

    return candidate_tree, changed_paths


def _lower_claim(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    base_tree: dict[str, bytes],
    payload: ClaimAuthoringPayloadV1 | None = None,
    claim_identity: str | None = None,
) -> LoweredAuthoring:
    """Lower one authored Claim against the tree it is being written onto.

    `payload` and `claim_identity` are explicit so a change-set member lowers the
    same way a singular Claim intent does: the member carries its own payload and
    its own minted Claim ID, and `base_tree` is the staged tree its siblings have
    already written into, so dependency installs, the slot law and predecessor
    lookup all see the rest of the same change.
    """

    authored = intent.payload if payload is None else payload
    assert isinstance(authored, ClaimAuthoringPayloadV1)
    payload = authored
    claim_id = intent.semantic_identity if claim_identity is None else claim_identity
    type_path = claim_type_path(payload.statement.predicate)
    candidate_base_tree, dependency_paths = _install_claim_dependencies(
        payload,
        base_tree=base_tree,
    )
    type_content = candidate_base_tree.get(type_path)
    if type_content is None:
        _refuse(
            "playbill.authoring.claim_type_not_found",
            "statement.predicate",
            "The predicate has no accepted ClaimType at the intent base.",
            repair_kind="replace_predicate",
            repair_description="Choose an accepted ClaimType predicate.",
        )
    claim_type = parse_claim_type(type_content, path=type_path)
    descriptor = payload.statement.predicate in {
        "semantic.alias",
        "semantic.distinct_from",
        "semantic.related_to",
        "semantic.tag",
    }
    subject_identity, subject_digest_value = _referent(
        candidate_base_tree, payload.statement.subject, descriptor=descriptor
    )
    statement_object = _exact_object(instance, payload.statement.object)
    if statement_object.kind != claim_type.object_kind:
        _refuse(
            "playbill.claim.object_kind_mismatch",
            "statement.object",
            "The Claim object kind differs from its accepted ClaimType.",
            repair_kind="replace_object",
            repair_description=(
                f"Use a {claim_type.object_kind} object for ClaimType {claim_type.predicate!r}."
            ),
            replacement={"required_object_kind": claim_type.object_kind},
        )
    qualifier = payload.statement.qualifier
    object_referent: tuple[ArtifactIdentity, str] | None = None
    if isinstance(statement_object, SubjectClaimObject):
        object_path = statement_object.address.artifact_path
        object_content = candidate_base_tree.get(object_path)
        object_name = object_path.removeprefix("subjects/").removesuffix(".json")
        if object_content is None:
            _refuse(
                "playbill.authoring.object_subject_not_found",
                "statement.object.address",
                f"The object Subject {object_name!r} is not accepted at the intent base.",
                repair_kind="propose_subject",
                repair_description=(
                    f"Propose Subject {object_name!r}, accept it, and preflight again."
                ),
                replacement={"subject": object_name},
            )
        if not object_path.startswith("subjects/"):
            _refuse(
                "playbill.claim.object_subject_kind_forbidden",
                "statement.object.address",
                "A subject-valued Claim object must name an accepted Subject artifact.",
                repair_kind="replace_object_subject",
                repair_description="Choose an accepted Subject of an admitted kind.",
            )
        object_subject = parse_subject(object_content, path=object_path)
        if object_subject.subject_kind not in claim_type.allowed_object_subject_kinds:
            _refuse(
                "playbill.claim.object_subject_kind_forbidden",
                "statement.object.address",
                f"Subject kind {object_subject.subject_kind!r} is not admitted by "
                f"ClaimType {claim_type.predicate!r}.",
                repair_kind="replace_object_subject",
                repair_description=(
                    "Choose an accepted Subject whose kind is listed by the ClaimType."
                ),
                replacement={
                    "allowed_subject_kinds": list(claim_type.allowed_object_subject_kinds)
                },
            )
        object_referent = (
            object_subject.identity,
            subject_digest(object_subject).tagged,
        )
    observed_at = _observed_at(intent.canonical_timestamp)
    context = ClaimReferentContext(
        subject_content_digest=subject_digest_value,
        object_content_digest=None if object_referent is None else object_referent[1],
        observed_at=observed_at,
    )
    statement = ClaimStatement(
        subject=payload.statement.subject,
        claim_type=claim_type.identity,
        claim_type_digest=claim_type_digest(claim_type).tagged,
        predicate=payload.statement.predicate,
        qualifier=qualifier,
        object=statement_object,
        role=payload.statement.role,
        effective_from=payload.statement.effective_from,
        effective_until=payload.statement.effective_until,
        shell_context_digest=(
            claim_referent_context_digest(context).tagged
            if claim_type.referent_sensitivity == "shell"
            else None
        ),
    )
    # Demanded: the claims contending for this exact slot. Accepted-if-offered:
    # every live claim on the same (subject, predicate), so an author may still
    # take a position on a sibling in another qualifier's slot voluntarily.
    slot_claims = _same_slot_claims(candidate_base_tree, statement)
    existing = _same_predicate_claims(candidate_base_tree, statement)
    expected = {item.identity.name for item in slot_claims}
    dispositionable = {item.identity.name for item in existing}
    supplied = {item.claim_id for item in payload.existing_claim_dispositions}
    inferred = {payload.claim_ref} if payload.claim_ref in expected else set()
    required_ids = expected - inferred
    if not required_ids.issubset(supplied) or not supplied.issubset(dispositionable):
        required = tuple(sorted(slot_claims, key=lambda item: item.identity.name.encode("ascii")))
        _refuse(
            "playbill.authoring.existing_claim_dispositions_incomplete",
            "existing_claim_dispositions",
            "Every live Claim in this statement's (subject, predicate, qualifier) "
            "slot must receive an explicit disposition.",
            repair_kind="replace_dispositions",
            repair_description="Disposition exactly the listed existing Claim IDs.",
            replacement={
                "required_claims": [
                    {"claim_id": claim.identity.name, "status": claim.lifecycle.state}
                    for claim in required
                ],
                "missing_claims": [
                    {"claim_id": claim.identity.name, "status": claim.lifecycle.state}
                    for claim in required
                    if claim.identity.name in required_ids and claim.identity.name not in supplied
                ],
                "unexpected_claim_ids": sorted(
                    supplied - dispositionable,
                    key=lambda identity: identity.encode("ascii"),
                ),
            },
        )

    path = claim_path(claim_id)
    predecessor: ClaimArtifactAny | None = None
    if path in candidate_base_tree:
        predecessor = parse_claim(candidate_base_tree[path], path=path)
        if isinstance(predecessor, ClaimArtifactV3):
            _refuse(
                "playbill.authoring.claim_terminal",
                "claim_ref",
                "An attributed retired Claim is terminal and cannot be authored again.",
                repair_kind="omit_claim_ref",
                repair_description="Mint a new Claim lineage instead of reviving a retired one.",
                replacement=None,
            )
    elif payload.claim_ref is not None:
        _refuse(
            "playbill.authoring.claim_predecessor_not_found",
            "claim_ref",
            "The requested Claim lineage does not exist at the intent base.",
            repair_kind="omit_claim_ref",
            repair_description="Omit claim_ref to mint a new Claim lineage.",
            replacement=None,
        )

    public_base = AcceptedCoordinate.from_internal(base)
    built_capture: CaptureBuildResult | DirectCaptureBuildResult | None = None
    accepted_contract: AcceptedCaptureContract | None = None
    accepted_producer_digests: dict[str, str] = {}
    install_contract = True
    if isinstance(payload.source, SelfSourceBodyV1):
        built_capture = build_coordinator_self_source_capture(
            store=instance.body_store(),
            actor_id=actor_id,
            claim_id=claim_id,
            body=payload.source.content,
            observed_at=observed_at,
            accepted_coordinate=public_base,
        )
        contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
        citation_role: CitationRole = "copy"
        citation_origin: CitationOrigin = "self_source"
    elif isinstance(payload.source, ExistingCaptureCitationSourceV1):
        install_contract = False
        store = instance.body_store()
        try:
            raw_envelope = store.read(
                payload.source.capture_digest,
                access=BodyAccessContext(
                    principal_id="playbill-authoring",
                    can_read_body=True,
                ),
            )
        except PlaybillError:
            _refuse(
                "playbill.authoring.existing_capture_not_found",
                "source.capture_digest",
                "The existing Capture digest is not present in the daemon CAS.",
                repair_kind="replace_capture",
                repair_description="Use a Capture digest returned by a typed Claim/evidence read.",
            )
        try:
            envelope = parse_capture_envelope(raw_envelope)
        except (PlaybillError, ValueError):
            _refuse(
                "playbill.authoring.existing_capture_invalid",
                "source.capture_digest",
                "The referenced bytes are not a canonical Capture envelope.",
                repair_kind="replace_capture",
                repair_description="Use a Capture digest returned by a typed Claim/evidence read.",
            )
        accepted_contract = _capture_contract_at_base(
            instance,
            base=base,
            base_tree=candidate_base_tree,
            contract_digest=envelope.capture_contract_digest,
        )
        try:
            accepted_producer_digests = _producer_digests_for_capture(
                candidate_base_tree,
                producer=envelope.producer,
                executable=envelope.run_coordinate.executable_identity,
            )
            envelope = verify_capture(
                payload.source.capture_digest,
                store=store,
                contract=accepted_contract.contract,
                ledger_resolver=_TreeLedgerResolver(
                    candidate_base_tree,
                    AcceptedCoordinate.from_internal(base),
                ),
                producer_artifact_digests=accepted_producer_digests,
                producer_receipt_resolver=local_producer_receipt_resolver(
                    exhaust_root=instance.root / instance.descriptor.storage.exhaust,
                    instance_id=instance.descriptor.instance_id,
                    bodies=store,
                ),
            )
        except (PlaybillError, ValueError):
            _refuse(
                "playbill.authoring.existing_capture_invalid",
                "source.capture_digest",
                "The existing Capture does not verify against its exact accepted contract.",
                repair_kind="replace_capture",
                repair_description="Use a verified Capture returned at this accepted coordinate.",
            )
        classification = classify_capture_reuse(
            envelope,
            contract=accepted_contract.contract,
            store=store,
            claim_id=claim_id,
        )
        if classification == "claim_bound_mismatch":
            _refuse(
                "playbill.claim.self_source_capture_unbound",
                "source.capture_digest",
                "The Claim-bound Capture belongs to another Claim or has mismatched "
                "family signals.",
                repair_kind="replace_capture",
                repair_description="Use a shareable observed Capture or one bound to this Claim.",
            )
        if classification == "not_shareable":
            _refuse(
                "playbill.authoring.capture_not_shareable",
                "source.capture_digest",
                "Only verified observed Captures are shareable across Claims.",
                repair_kind="replace_capture",
                repair_description="Use an observed Capture or author new source evidence.",
            )
        assert payload.citation_role is not None
        if classification == "claim_bound" and payload.citation_role != "copy":
            _refuse(
                "playbill.authoring.existing_capture_not_admitted",
                "citation_role",
                "Claim-bound self-source Captures remain non-evidentiary copies.",
                repair_kind="replace_citation_role",
                repair_description="Use citation_role=copy for this Claim-bound Capture.",
                replacement="copy",
            )
        contract = accepted_contract.contract
        citation_role = payload.citation_role
        citation_origin = "self_source" if classification == "claim_bound" else "independent"
    else:
        source = payload.source
        assert isinstance(source, WorkingSelectionObservationV1)
        built_selection = build_working_selection_capture(
            store=instance.body_store(),
            actor_id=actor_id,
            claim_id=claim_id,
            rationale=payload.rationale,
            observed_at=observed_at,
            accepted_coordinate=public_base,
            source_id=source.source_id,
            coordinate=source.coordinate.model_dump(mode="json"),
            selector=source.selector.model_dump(mode="json"),
            selected_content=source.selected_content,
        )
        built_capture = built_selection
        contract = built_selection.contract
        assert payload.citation_role is not None
        citation_role = payload.citation_role
        citation_origin = "independent"

    capture_digest_value = (
        payload.source.capture_digest
        if isinstance(payload.source, ExistingCaptureCitationSourceV1)
        else built_capture.capture_digest
    )
    capture_envelope = (
        envelope
        if isinstance(payload.source, ExistingCaptureCitationSourceV1)
        else built_capture.envelope
    )

    identity = ArtifactIdentity(kind="Claim", name=claim_id)
    citation = build_claim_citation(
        identity,
        capture_digest=capture_digest_value,
        role=citation_role,
        origin=citation_origin,
    )
    predecessor_citations = (
        predecessor.backing.citations
        if predecessor is not None and isinstance(predecessor.backing, ClaimBackingV2)
        else ()
    )
    capture_digests = tuple(
        sorted(
            {
                *(() if predecessor is None else predecessor.backing.capture_digests),
                capture_digest_value,
            },
            key=lambda item: item.encode("ascii"),
        )
    )
    source_mapping = SourceMapping(
        subject=claim_statement_address(path),
        spans=(
            ContentSpan(
                content_digest=capture_envelope.commitment.digest,
                start_byte=0,
                end_byte=capture_envelope.commitment.byte_length or 0,
            ),
        ),
    )
    pins = [
        ArtifactPin(
            role="capture-contract",
            target=contract.identity,
            artifact_digest=capture_contract_digest(contract).tagged,
        ),
        ArtifactPin(
            role="claim-type",
            target=claim_type.identity,
            artifact_digest=claim_type_digest(claim_type).tagged,
        ),
        ArtifactPin(
            role="subject",
            target=subject_identity,
            artifact_digest=subject_digest_value,
        ),
    ]
    for producer_identity in {
        capture_envelope.producer,
        capture_envelope.run_coordinate.executable_identity,
    }:
        producer_digest_value = accepted_producer_digests.get(producer_identity.qualified)
        if producer_digest_value is not None and producer_identity.kind in {
            "Provider",
            "Procedure",
        }:
            pins.append(
                ArtifactPin(
                    role=producer_identity.kind.casefold(),
                    target=producer_identity,
                    artifact_digest=producer_digest_value,
                )
            )
    if object_referent is not None:
        pins.append(
            ArtifactPin(
                role="object-subject",
                target=object_referent[0],
                artifact_digest=object_referent[1],
            )
        )
    claim = ClaimArtifactV2(
        identity=identity,
        statement=statement,
        backing=ClaimBackingV2(
            referent_context=context,
            capture_digests=capture_digests,
            citations=merge_claim_citations(predecessor_citations, (citation,)),
            attestation_digests=(
                () if predecessor is None else predecessor.backing.attestation_digests
            ),
            input_claim_digests=(
                () if predecessor is None else predecessor.backing.input_claim_digests
            ),
            reducer_digest=None if predecessor is None else predecessor.backing.reducer_digest,
            source_mappings=_merge_mappings(
                () if predecessor is None else predecessor.backing.source_mappings,
                (source_mapping,),
            ),
        ),
        pins=_merge_pins(() if predecessor is None else predecessor.pins, tuple(pins)),
        lifecycle=ArtifactLifecycle(
            predecessor_digest=(
                None if predecessor is None else claim_artifact_digest(predecessor).tagged
            )
        ),
    )
    if isinstance(payload.source, ExistingCaptureCitationSourceV1) and citation_role == "evidence":
        assert accepted_contract is not None
        admissions = evaluate_capture_evidence_admissions(
            claim,
            claim_type=claim_type,
            capture_digest=capture_digest_value,
            capture_contract=accepted_contract,
            envelope=capture_envelope,
            verified_attestations=(),
        )
        if not any(decision.trace.result.verdict == "eligible" for decision in admissions):
            _refuse(
                "playbill.authoring.existing_capture_not_admitted",
                "source.capture_digest",
                "The existing Capture satisfies no evidence admission rule for this Claim.",
                repair_kind="replace_capture_or_role",
                repair_description=(
                    "Use a Capture admitted by the ClaimType or cite it as a non-evidentiary copy."
                ),
                replacement={
                    "closest_rule_ids": sorted(
                        {
                            decision.trace.closest_rule_id
                            for decision in admissions
                            if decision.trace.closest_rule_id is not None
                        },
                        key=lambda item: item.encode("utf-8"),
                    ),
                    "underlying_refusal_codes": sorted(
                        {
                            decision.trace.result.refusal_code
                            for decision in admissions
                            if decision.trace.result.refusal_code is not None
                        },
                        key=lambda item: item.encode("utf-8"),
                    ),
                },
            )
    candidate_tree = dict(candidate_base_tree)
    contract_member_path = capture_contract_path(contract.identity.name)
    contract_bytes = render_capture_contract(contract)
    claim_bytes = render_claim(claim)
    if install_contract:
        candidate_tree[contract_member_path] = contract_bytes
    candidate_tree[path] = claim_bytes
    member_paths = {path}
    if install_contract:
        member_paths.add(contract_member_path)
    member_paths.update(dependency_paths)
    changed = tuple(
        (member_path, candidate_tree[member_path])
        for member_path in sorted(
            member_paths,
            key=lambda item: item.encode("utf-8"),
        )
        if base_tree.get(member_path) != candidate_tree[member_path]
    )
    idempotent = (
        isinstance(payload.source, ExistingCaptureCitationSourceV1)
        and predecessor is not None
        and claim.statement == predecessor.statement
        and citation in predecessor_citations
        and not payload.existing_claim_dispositions
        and not dependency_paths
    )
    if idempotent:
        candidate_tree = dict(candidate_base_tree)
        changed = ()
    resolved_artifact_digest = (
        claim_artifact_digest(predecessor).tagged
        if idempotent and predecessor is not None
        else claim_artifact_digest(claim).tagged
    )
    return LoweredAuthoring(
        proposed_tree=candidate_tree,
        resolved_authoring={
            "artifact_digest": resolved_artifact_digest,
            "capture_digest": capture_digest_value,
            "citation_id": citation.citation_id,
            **(
                {
                    "outcome": "playbill.authoring.existing_capture_already_associated",
                }
                if idempotent
                else {}
            ),
            "changed_members": _encoded_members(changed),
            "existing_claim_dispositions": [
                {
                    **item.model_dump(mode="json"),
                    "statement_digest": claim_statement_digest(
                        next(
                            claim.statement
                            for claim in existing
                            if claim.identity.name == item.claim_id
                        )
                    ).tagged,
                }
                for item in payload.existing_claim_dispositions
            ],
            "identity": identity.model_dump(mode="json"),
            "predecessor_digest": claim.lifecycle.predecessor_digest,
            "statement": statement.model_dump(mode="json"),
        },
        changed_members=changed,
        idempotent=idempotent,
    )


def _validation_error_lines(exc: ValidationError, *, root: str) -> tuple[str, ...]:
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_context=False):
        location = root + "".join(
            f"[{member}]" if isinstance(member, int) else f".{member}" for member in error["loc"]
        )
        offending = error.get("input")
        try:
            rendered = json.dumps(
                offending,
                allow_nan=False,
                default=lambda value: f"<{type(value).__name__}>",
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except Exception:  # noqa: BLE001 - error reporting must be total over arbitrary inputs
            rendered = f"<{type(offending).__name__}>"
        lines.append(f"{location}: {error['msg']}; offending element: {rendered}")
    return tuple(lines)


def _resolve_authoring_references(
    value: object,
    *,
    accepted: dict[str, tuple[str, str]],
    candidates: dict[str, tuple[str, str]] | None = None,
    candidate_identities: frozenset[str] = frozenset(),
    owned_contracts: dict[str, ProcedureOwnedContractV1] | None = None,
    location: str = "definition",
) -> object:
    if isinstance(value, dict):
        if value.get("tag") == "playbill-authoring-artifact-reference-v1":
            try:
                reference = AuthoringArtifactReferenceV1.model_validate(value)
            except ValidationError as exc:
                _refuse(
                    "playbill.authoring.artifact_reference_invalid",
                    location,
                    "The procedure authoring reference is invalid: "
                    + " | ".join(_validation_error_lines(exc, root=location)),
                    repair_kind="replace_reference",
                    repair_description="Use a valid accepted-at-intent-base artifact reference.",
                )
            target_identity = reference.target.qualified
            resolved = accepted.get(target_identity)
            if resolved is None:
                _refuse(
                    "playbill.authoring.artifact_reference_unresolved",
                    location,
                    "The referenced artifact is not uniquely accepted at the intent base.",
                    repair_kind="replace_with_pin_slot",
                    repair_description="Use an explicit Procedure pin-slot reference.",
                )
            if target_identity in candidate_identities:
                # Both maps derive from the same staged non-Procedure members.
                resolved = (candidates or {})[target_identity]
            _path, digest = resolved
            return ArtifactPin(
                role=reference.role,
                target=reference.target,
                artifact_digest=digest,
            ).model_dump(mode="json")
        if value.get("tag") == "playbill-authoring-candidate-reference-v1":
            try:
                candidate_reference = AuthoringCandidateReferenceV1.model_validate(value)
            except ValidationError as exc:
                _refuse(
                    "playbill.authoring.candidate_reference_invalid",
                    location,
                    "The change-set candidate reference is invalid: "
                    + " | ".join(_validation_error_lines(exc, root=location)),
                    repair_kind="replace_reference",
                    repair_description="Use a valid candidate member reference.",
                )
            if candidate_reference.target.kind == "Procedure":
                _refuse(
                    "playbill.authoring.candidate_procedure_reference_forbidden",
                    location,
                    "A Procedure cannot candidate-reference another Procedure in change-set v1.",
                    repair_kind="replace_reference",
                    repair_description="Reference a non-Procedure member of this change set.",
                )
            if candidate_reference.target.qualified not in candidate_identities:
                _refuse(
                    "playbill.authoring.candidate_reference_outside_change_set",
                    location,
                    "The candidate reference does not name a member of this change set.",
                    repair_kind="add_or_replace_member",
                    repair_description="Add the referenced artifact or use an accepted reference.",
                )
            resolved = (candidates or {}).get(candidate_reference.target.qualified)
            if resolved is None:  # pragma: no cover - staged-tree invariant
                raise ValueError("candidate member was not present in its staged tree")
            _path, digest = resolved
            return ArtifactPin(
                role=candidate_reference.role,
                target=candidate_reference.target,
                artifact_digest=digest,
            ).model_dump(mode="json")
        if value.get("kind") == "carried_contract" and set(value) == {
            "kind",
            "name",
            "role",
        }:
            name = value["name"]
            role = value["role"]
            contract = None if not isinstance(name, str) else (owned_contracts or {}).get(name)
            if contract is None or not isinstance(role, str):
                _refuse(
                    "playbill.authoring.carried_contract_unresolved",
                    location,
                    "The carried Contract reference has no matching owned declaration.",
                    repair_kind="replace_reference",
                    repair_description="Declare the Contract once and reference its exact name.",
                )
            return ArtifactPin(
                role=role,
                target=contract.identity,
                artifact_digest=procedure_owned_contract_digest(contract).tagged,
            ).model_dump(mode="json")
        if set(value) == {"role", "target", "artifact_digest"}:
            _refuse(
                "playbill.authoring.caller_artifact_digest_forbidden",
                location,
                "Procedure authoring cannot supply an exact artifact digest.",
                repair_kind="replace_reference",
                repair_description=(
                    "Use an accepted-at-intent-base authoring reference or an explicit pin slot."
                ),
            )
        return {
            key: _resolve_authoring_references(
                member,
                accepted=accepted,
                candidates=candidates,
                candidate_identities=candidate_identities,
                owned_contracts=owned_contracts,
                location=f"{location}.{key}",
            )
            for key, member in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_authoring_references(
                member,
                accepted=accepted,
                candidates=candidates,
                candidate_identities=candidate_identities,
                owned_contracts=owned_contracts,
                location=f"{location}[{index}]",
            )
            for index, member in enumerate(value)
        ]
    return value


def _lower_procedure(
    *,
    intent: AuthoringIntentV1,
    base: AcceptedProjectionCoordinate,
    base_tree: dict[str, bytes],
    accepted_reference_tree: dict[str, bytes] | None = None,
    candidate_identities: frozenset[str] = frozenset(),
) -> LoweredAuthoring:
    payload = intent.payload
    assert isinstance(payload, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2)
    parsed = parse_projection_tree(
        base_tree if accepted_reference_tree is None else accepted_reference_tree,
        registry=projection_registry_for_compiler(base.compiler),
        artifact_kinds=artifact_kinds_for_compiler(base.compiler),
        artifact_codec=artifact_codec_for_compiler(base.compiler),
    )
    accepted: dict[str, tuple[str, str]] = {}
    duplicates: set[str] = set()
    for envelope in parsed.envelopes:
        if envelope.identity in accepted:
            duplicates.add(envelope.identity)
        accepted[envelope.identity] = (envelope.path, envelope.artifact_digest)
    for duplicate_identity in duplicates:
        accepted.pop(duplicate_identity, None)
    candidate_artifacts: dict[str, tuple[str, str]] = {}
    if candidate_identities:
        candidate_parsed = parse_projection_tree(
            base_tree,
            registry=projection_registry_for_compiler(base.compiler),
            artifact_kinds=artifact_kinds_for_compiler(base.compiler),
            artifact_codec=artifact_codec_for_compiler(base.compiler),
        )
        for envelope in candidate_parsed.envelopes:
            if envelope.identity in candidate_identities:
                candidate_artifacts[envelope.identity] = (
                    envelope.path,
                    envelope.artifact_digest,
                )
    owned_contracts = (
        {contract.identity.name: contract for contract in payload.owned_contracts}
        if isinstance(payload, ProcedureAuthoringPayloadV2)
        else None
    )
    resolved_definition = _resolve_authoring_references(
        payload.definition,
        accepted=accepted,
        candidates=candidate_artifacts,
        candidate_identities=candidate_identities,
        owned_contracts=owned_contracts,
    )
    if isinstance(resolved_definition, dict) and resolved_definition.get("graph_format") == 4:
        _refuse(
            "playbill.authoring.procedure_definition_invalid",
            "definition.graph_format",
            "Graph-v4 Procedure authoring is not supported by the graph-v3 lowering path.",
            repair_kind="replace_definition",
            repair_description=(
                "Use a graph-v3 definition; graph-v4 authoring requires a future dedicated "
                "lowering path."
            ),
        )
    try:
        definition = ProcedureDefinitionV3.model_validate(resolved_definition)
    except (ProcedureGraphFormatError, ValidationError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ProcedureGraphFormatError)
            else " | ".join(_validation_error_lines(exc, root="definition"))
        )
        _refuse(
            "playbill.authoring.procedure_definition_invalid",
            "definition",
            "The lowered graph-v3 Procedure definition is invalid: " + message,
            repair_kind="replace_definition",
            repair_description="Repair the indicated graph-v3 definition field.",
        )
    identity = ArtifactIdentity(kind="Procedure", name=definition.name)
    path = procedure_path(definition.name)
    predecessor: ProcedureArtifactAny | None = None
    if path in base_tree:
        predecessor = parse_procedure(base_tree[path], path=path)
    pins = tuple(
        sorted(
            {
                binding
                for binding in iter_pin_bindings(definition)
                if isinstance(binding, ArtifactPin)
            },
            key=lambda item: (
                item.role.encode("utf-8"),
                item.target.qualified.encode("utf-8"),
                item.artifact_digest.encode("ascii"),
            ),
        )
    )
    if isinstance(payload, ProcedureAuthoringPayloadV2):
        referenced_contract_digests = {
            pin.artifact_digest for pin in pins if pin.target.kind == "Contract"
        }
        unreferenced_contracts = tuple(
            sorted(
                (
                    contract.identity.qualified
                    for contract in payload.owned_contracts
                    if procedure_owned_contract_digest(contract).tagged
                    not in referenced_contract_digests
                ),
                key=lambda item: item.encode("utf-8"),
            )
        )
        if unreferenced_contracts:
            _refuse(
                "playbill.authoring.procedure_definition_invalid",
                "owned_contracts",
                "The Procedure declares owned Contracts that its graph does not reference: "
                + ", ".join(unreferenced_contracts),
                repair_kind="replace_contracts_or_definition",
                repair_description=(
                    "Remove each unused owned Contract or reference it from the Procedure graph."
                ),
            )

    if isinstance(payload, ProcedureAuthoringPayloadV2) and definition.budget.max_items is not None:

        def carries_list(contract: ProcedureOwnedContractV1) -> bool:
            pending = list(contract.contract_schema.fields.values())
            while pending:
                field = pending.pop()
                if field.type == "list":
                    return True
                if field.item_fields is not None:
                    pending.extend(field.item_fields.values())
            return False

        if not any(
            procedure_owned_contract_digest(contract).tagged in referenced_contract_digests
            and carries_list(contract)
            for contract in payload.owned_contracts
        ):
            _refuse(
                "playbill.authoring.procedure_definition_invalid",
                "definition.budget.max_items",
                "The lowered graph-v3 Procedure definition declares max_items but none of "
                "its pinned Contracts declares a list field.",
                repair_kind="replace_definition",
                repair_description=(
                    "Declare a list field on a pinned Contract or remove budget.max_items."
                ),
            )
    lifecycle = ArtifactLifecycle(
        state="retired" if payload.retire else "live",
        predecessor_digest=(
            None if predecessor is None else procedure_artifact_digest(predecessor).tagged
        ),
    )
    try:
        if isinstance(payload, ProcedureAuthoringPayloadV2):
            procedure: ProcedureArtifactAny = ProcedureArtifactV2(
                identity=identity,
                definition=definition,
                definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
                pins=pins,
                owned_contracts=payload.owned_contracts,
                activation_policy=payload.activation_policy,
                lifecycle=lifecycle,
            )
        else:
            procedure = ProcedureArtifactV1(
                identity=identity,
                definition=definition,
                definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
                pins=pins,
                activation_policy=payload.activation_policy,
                lifecycle=lifecycle,
            )
    except ValidationError as exc:
        _refuse(
            "playbill.authoring.procedure_definition_invalid",
            "procedure",
            "The lowered Procedure artifact is invalid: "
            + " | ".join(_validation_error_lines(exc, root="procedure")),
            repair_kind="replace_definition_or_contracts",
            repair_description=(
                "Repair the Procedure definition or its owned Contract declarations."
            ),
        )
    candidate_tree = dict(base_tree)
    procedure_bytes = render_procedure(procedure)
    candidate_tree[path] = procedure_bytes
    changed = () if base_tree.get(path) == procedure_bytes else ((path, procedure_bytes),)
    return LoweredAuthoring(
        proposed_tree=candidate_tree,
        resolved_authoring={
            "artifact_digest": procedure_artifact_digest(procedure).tagged,
            "changed_members": _encoded_members(changed),
            "definition": definition.model_dump(mode="json", by_alias=True),
            "definition_digest": procedure.definition_digest,
            "identity": identity.model_dump(mode="json"),
            "pins": [pin.model_dump(mode="json") for pin in pins],
            "predecessor_digest": procedure.lifecycle.predecessor_digest,
        },
        changed_members=changed,
    )


def _render_non_procedure_member(
    payload: SubjectAuthoringPayloadV1
    | QueryDefinitionAuthoringPayloadV1
    | ClaimTypeAuthoringPayloadV1
    | ApprovalPolicyAuthoringPayloadV1
    | ProcedureRuntimePolicyAuthoringPayloadV1,
) -> tuple[str, bytes, str]:
    if isinstance(payload, ClaimTypeAuthoringPayloadV1):
        definition = payload.claim_type
        return (
            claim_type_path(definition.predicate),
            render_claim_type(definition),
            claim_type_digest(definition).tagged,
        )
    if isinstance(payload, SubjectAuthoringPayloadV1):
        shell = payload.subject
        return (
            subject_path(shell.subject_kind, shell.subject_id),
            render_subject(shell),
            subject_digest(shell).tagged,
        )
    if isinstance(payload, QueryDefinitionAuthoringPayloadV1):
        query = payload.query_definition
        return (
            query_definition_path(query.identity.name),
            render_query_definition(query),
            query_definition_digest(query).tagged,
        )
    if isinstance(payload, ApprovalPolicyAuthoringPayloadV1):
        return (
            APPROVAL_POLICY_PATH,
            render_approval_policy(payload.approval_policy),
            approval_policy_digest(payload.approval_policy).tagged,
        )
    return (
        PROCEDURE_RUNTIME_POLICY_PATH,
        render_procedure_runtime_policy(payload.procedure_runtime_policy),
        procedure_runtime_policy_digest(payload.procedure_runtime_policy).tagged,
    )


def _render_procedure_mandate_member(
    payload: ProcedureMandateAuthoringPayloadV1,
    *,
    tree: Mapping[str, bytes],
) -> tuple[str, bytes, str]:
    target_path = procedure_path(payload.procedure_name)
    target_content = tree.get(target_path)
    if target_content is None:
        _refuse(
            "playbill.authoring.procedure_mandate_procedure_missing",
            "procedure_name",
            "ProcedureMandate authoring requires the named accepted or same-ChangeSet Procedure.",
            repair_kind="replace_procedure_name",
            repair_description="Use a Procedure name present at the authoring coordinate.",
        )
    procedure = parse_procedure(target_content, path=target_path)
    path = procedure_mandate_path(payload.name)
    previous_content = tree.get(path)
    predecessor_digest = None
    if previous_content is not None:
        previous = parse_procedure_mandate(previous_content, path=path)
        predecessor_digest = procedure_mandate_digest(previous).tagged
    mandate = ProcedureMandateV1(
        identity=ArtifactIdentity(kind="ProcedureMandate", name=payload.name),
        procedure=ArtifactPin(
            role="procedure",
            target=procedure.identity,
            artifact_digest=procedure_artifact_digest(procedure).tagged,
        ),
        rung=payload.rung,
        authority_ceiling=payload.authority_ceiling,
        namespace=payload.namespace,
        valid_from=payload.valid_from,
        expires_at=payload.expires_at,
        lifecycle=ArtifactLifecycle(
            state="retired" if payload.retire else "live",
            predecessor_digest=predecessor_digest,
        ),
    )
    return path, render_procedure_mandate(mandate), procedure_mandate_digest(mandate).tagged


def _lower_non_procedure(
    *,
    payload: SubjectAuthoringPayloadV1
    | QueryDefinitionAuthoringPayloadV1
    | ApprovalPolicyAuthoringPayloadV1
    | ProcedureRuntimePolicyAuthoringPayloadV1,
    base_tree: dict[str, bytes],
) -> LoweredAuthoring:
    path, content, digest = _render_non_procedure_member(payload)
    candidate_tree = dict(base_tree)
    candidate_tree[path] = content
    changed = () if base_tree.get(path) == content else ((path, content),)
    return LoweredAuthoring(
        proposed_tree=candidate_tree,
        resolved_authoring={
            "artifact_digest": digest,
            "changed_members": _encoded_members(changed),
            "identity": authoring_member_identity(payload),
        },
        changed_members=changed,
    )


MEMBER_STAGING_ORDER = (
    "definition",
    "claim_type_succession",
    "claim",
    "procedure",
    "procedure_mandate",
    "claim_retirement",
)


def _member_stage(member: AuthoringChangeSetMemberV1) -> str:
    """Say which staging pass writes this member into the change set's tree.

    Members are byte-sorted by semantic identity on the wire so one intent has
    one canonical form, which is the wrong order to *lower* in: a Claim sorts
    before the ClaimType that admits it. The passes below are the dependency
    order instead -- definitions, then the Claims that read them, then the
    Procedures and mandates that pin those, then the retirements that withdraw
    Claims the same set may just have written.

    A ClaimType succession sits between the definitions and the Claims: it
    reads the definitions, and every Claim after it -- the ones it re-authors
    included -- is lowered against the successor vocabulary rather than the one
    it replaces. Defining a ClaimType and succeeding it in one set is not one of
    the shapes this order admits: both members author the same artifact path, so
    member-path ownership refuses the set before any of these passes run.
    """

    if isinstance(member, ClaimAuthoringPayloadV1):
        return "claim"
    if isinstance(member, ClaimTypeSuccessionMemberV1):
        return "claim_type_succession"
    if isinstance(member, ClaimRetirementMemberV1):
        return "claim_retirement"
    if isinstance(member, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2):
        return "procedure"
    if isinstance(member, ProcedureMandateAuthoringPayloadV1):
        return "procedure_mandate"
    return "definition"


def _member_primary_path(
    member: AuthoringChangeSetMemberV1,
    *,
    claim_identities: Mapping[str, str],
) -> str:
    """Return the one artifact path this member is authoring."""

    if isinstance(member, ClaimAuthoringPayloadV1):
        return claim_path(claim_identities[authoring_member_identity(member)])
    if isinstance(member, ClaimTypeSuccessionMemberV1):
        return claim_type_path(member.predicate)
    if isinstance(member, ClaimRetirementMemberV1):
        return claim_path(member.claim_id)
    if isinstance(member, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2):
        return procedure_path(str(member.definition["name"]))
    if isinstance(member, ProcedureMandateAuthoringPayloadV1):
        return procedure_mandate_path(member.name)
    path, _content, _digest = _render_non_procedure_member(member)
    return path


def _rescope_member_error(
    error: AuthoringLoweringError,
    *,
    index: int,
    sibling_member_by_claim_id: Mapping[str, int],
) -> AuthoringLoweringError:
    """Re-address one member's refusal to the member that owns it.

    A change set admits or refuses whole, so the caller needs the offending
    member's index, not just the field path inside it. The slot law additionally
    names the sibling members whose Claims contend for the slot, because when the
    contender is another member of the same submission "disposition this Claim
    ID" is not yet a repair the author can carry out.

    That door is closed by construction, not merely unrepaired: the disposition
    needs the sibling's Claim ID, and Claim IDs are minted at create from a
    payload that is frozen by then. Two sibling Claims contending for one
    cardinality-one slot are therefore un-authorable in a single set, and the
    repair says so -- merge the two decisions, or split the set.
    """

    repairs = tuple(
        repair.model_copy(
            update={
                "replacement": {
                    **repair.replacement,
                    "sibling_members": sorted(
                        (
                            {"claim_id": claim_id, "member": member_index}
                            for claim_id, member_index in sibling_member_by_claim_id.items()
                            if any(
                                claim_id == entry.get("claim_id")
                                for entry in repair.replacement.get("required_claims", [])
                                if isinstance(entry, dict)
                            )
                        ),
                        key=lambda item: str(item["claim_id"]).encode("ascii"),
                    ),
                }
            }
        )
        if isinstance(repair.replacement, dict) and "required_claims" in repair.replacement
        else repair
        for repair in error.repairs
    )
    return AuthoringLoweringError(
        code=error.code,
        offending_element=f"members[{index}].{error.offending_element}",
        message=error.message,
        repairs=repairs,
    )


@dataclass(frozen=True)
class ChangeSetSingletonOnlyMember:
    """One member kind a change set refuses, whatever else the set carries.

    The member union parses these kinds, so nothing before lowering rejects
    them, and every surface that advertises the member kind family reads this
    table rather than repeating the list. A kind added here therefore leaves
    the CLI help and the references in the same change that starts refusing it.
    """

    payload: type[BaseModel]
    kind: str
    artifact: str

    @property
    def code(self) -> str:
        return f"playbill.authoring.{self.kind}_singleton_required"


CHANGE_SET_SINGLETON_ONLY_MEMBERS: tuple[ChangeSetSingletonOnlyMember, ...] = (
    ChangeSetSingletonOnlyMember(
        payload=ApprovalPolicyAuthoringPayloadV1,
        kind="approval_policy",
        artifact="ApprovalPolicy",
    ),
    ChangeSetSingletonOnlyMember(
        payload=ProcedureRuntimePolicyAuthoringPayloadV1,
        kind="procedure_runtime_policy",
        artifact="ProcedureRuntimePolicy",
    ),
)


def _lower_change_set(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    base_tree: dict[str, bytes],
) -> LoweredAuthoring:
    payload = intent.payload
    assert isinstance(payload, ChangeSetAuthoringPayloadV1)
    for singleton in CHANGE_SET_SINGLETON_ONLY_MEMBERS:
        if any(isinstance(member, singleton.payload) for member in payload.members):
            _refuse(
                singleton.code,
                "members",
                f"{singleton.artifact} authoring must be submitted as a singleton payload.",
                repair_kind="split_change_set",
                repair_description=(
                    f"Author and accept the {singleton.artifact} separately from the change set."
                ),
            )
    claim_identities = {
        item.member_identity: item.claim_id for item in intent.change_set_claim_identities
    }
    sibling_member_by_claim_id = {
        claim_identities[authoring_member_identity(member)]: index
        for index, member in enumerate(payload.members)
        if isinstance(member, ClaimAuthoringPayloadV1)
    }
    owner_by_path: dict[str, int] = {}
    primary_paths: list[str] = []
    for index, member in enumerate(payload.members):
        path = _member_primary_path(member, claim_identities=claim_identities)
        primary_paths.append(path)
        owner = owner_by_path.get(path)
        if owner is not None:
            _refuse(
                "playbill.authoring.change_set_member_path_collision",
                f"members[{index}]",
                f"Members {owner} and {index} both author {path!r}.",
                repair_kind="drop_or_merge_member",
                repair_description=(
                    "Keep one member per authored artifact path, or merge the two decisions."
                ),
                replacement={"members": [owner, index], "path": path},
            )
        owner_by_path[path] = index
    re_author_siblings = _resolve_re_author_siblings(
        payload.members,
        base_tree=base_tree,
        sibling_member_by_claim_id=sibling_member_by_claim_id,
    )
    consumed = {
        sibling_index
        for siblings in re_author_siblings.values()
        for sibling_index in siblings.values()
    }
    sibling_resolved: dict[int, dict[str, object]] = {}

    staged_tree = dict(base_tree)
    member_paths: set[str] = set()
    installed_by: dict[str, set[int]] = {}
    resolved: list[dict[str, object]] = []
    candidate_identities = frozenset(
        authoring_member_identity(member)
        for member in payload.members
        if not isinstance(member, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2)
    )
    for stage in MEMBER_STAGING_ORDER:
        for index, member in enumerate(payload.members):
            if _member_stage(member) != stage:
                continue
            path = primary_paths[index]
            member_paths.add(path)
            if index in consumed:
                # A re-authored Claim is lowered by the succession that names it,
                # under the successor vocabulary; staging it again here would
                # lower it a second time against a tree it has already changed.
                member_resolved = sibling_resolved[index]
            else:
                try:
                    (
                        staged_tree,
                        member_resolved,
                        extra_paths,
                        staged_siblings,
                    ) = _stage_change_set_member(
                        instance,
                        member=member,
                        intent=intent,
                        actor_id=actor_id,
                        base=base,
                        base_tree=base_tree,
                        staged_tree=staged_tree,
                        path=path,
                        members=payload.members,
                        claim_identities=claim_identities,
                        candidate_identities=candidate_identities,
                        re_author_siblings=re_author_siblings.get(index, {}),
                    )
                except AuthoringLoweringError as error:
                    raise _rescope_member_error(
                        error,
                        index=index,
                        sibling_member_by_claim_id=sibling_member_by_claim_id,
                    ) from error
                if isinstance(member, ClaimTypeSuccessionMemberV1):
                    _refuse_contended_succession_paths(
                        index=index,
                        changed_paths=extra_paths,
                        owner_by_path=owner_by_path,
                        re_authored=frozenset(re_author_siblings.get(index, {}).values()),
                        members=payload.members,
                    )
                sibling_resolved.update(staged_siblings)
                member_paths.update(extra_paths)
                for extra_path in extra_paths:
                    installed_by.setdefault(extra_path, set()).add(index)
            member_resolved["identity"] = authoring_member_identity(member)
            member_resolved["member"] = index
            member_resolved["path"] = path
            resolved.append(member_resolved)
    changed = tuple(
        (path, staged_tree[path])
        for path in sorted(member_paths, key=lambda item: item.encode("utf-8"))
        if base_tree.get(path) != staged_tree[path]
    )
    # A dependency draft or a Claim's own card may be installed by two members at
    # once (they are equal bytes or lowering would have refused); only a path one
    # member alone wrote can be addressed back to that member.
    member_by_path = dict(owner_by_path)
    for extra_path, owners in installed_by.items():
        if extra_path not in member_by_path and len(owners) == 1:
            member_by_path[extra_path] = next(iter(owners))
    return LoweredAuthoring(
        proposed_tree=staged_tree,
        resolved_authoring={
            "changed_members": _encoded_members(changed),
            "members": sorted(
                resolved,
                key=lambda item: str(item["identity"]).encode("utf-8"),
            ),
        },
        changed_members=changed,
        member_by_path=member_by_path,
    )


StagedMember: TypeAlias = tuple[
    dict[str, bytes],
    dict[str, object],
    set[str],
    dict[int, dict[str, object]],
]
"""The staged tree, what this member resolved to, the extra paths it wrote, and
what any sibling member it consumed resolved to."""


def _stage_change_set_member(
    instance: PlaybillInstance,
    *,
    member: AuthoringChangeSetMemberV1,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    base_tree: dict[str, bytes],
    staged_tree: dict[str, bytes],
    path: str,
    members: tuple[AuthoringChangeSetMemberV1, ...],
    claim_identities: Mapping[str, str],
    candidate_identities: frozenset[str],
    re_author_siblings: Mapping[str, int],
) -> StagedMember:
    """Write one member into the staged tree and report what it resolved to."""

    if isinstance(member, ClaimAuthoringPayloadV1):
        claim_id = claim_identities[authoring_member_identity(member)]
        lowered = _lower_claim(
            instance,
            intent=intent,
            actor_id=actor_id,
            base=base,
            base_tree=staged_tree,
            payload=member,
            claim_identity=claim_id,
        )
        member_resolved = dict(lowered.resolved_authoring)
        member_resolved["claim_id"] = claim_id
        extra = {member_path for member_path, _content in lowered.changed_members}
        return dict(lowered.proposed_tree), member_resolved, extra, {}
    if isinstance(member, ClaimTypeSuccessionMemberV1):
        return _stage_claim_type_succession(
            instance,
            member=member,
            intent=intent,
            actor_id=actor_id,
            base=base,
            staged_tree=staged_tree,
            path=path,
            members=members,
            claim_identities=claim_identities,
            re_author_siblings=re_author_siblings,
        )
    if isinstance(member, ClaimRetirementMemberV1):
        tree, resolved, extra = _stage_claim_retirement(
            instance,
            member=member,
            base=base,
            staged_tree=staged_tree,
            path=path,
        )
        return tree, resolved, extra, {}
    if isinstance(member, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2):
        lowered = _lower_procedure(
            intent=intent.model_copy(update={"payload": member}),
            base=base,
            base_tree=staged_tree,
            accepted_reference_tree=base_tree,
            candidate_identities=candidate_identities,
        )
        return dict(lowered.proposed_tree), dict(lowered.resolved_authoring), set(), {}
    if isinstance(member, ProcedureMandateAuthoringPayloadV1):
        mandate_path, content, digest = _render_procedure_mandate_member(member, tree=staged_tree)
        staged_tree = dict(staged_tree)
        staged_tree[mandate_path] = content
        return staged_tree, {"artifact_digest": digest}, set(), {}
    _path, content, digest = _render_non_procedure_member(member)
    staged_tree = dict(staged_tree)
    staged_tree[path] = content
    return staged_tree, {"artifact_digest": digest}, set(), {}


def _resolve_re_author_siblings(
    members: tuple[AuthoringChangeSetMemberV1, ...],
    *,
    base_tree: Mapping[str, bytes],
    sibling_member_by_claim_id: Mapping[str, int],
) -> dict[int, dict[str, int]]:
    """Bind every `re_author` disposition to the sibling Claim member it names.

    Resolved once, before anything is staged, so a set whose successions and
    Claims disagree about who re-authors what refuses before it writes a byte,
    naming both the succession member and the member it pointed at.

    A re-authored Claim keeps the slot it re-authors: the successor of a
    dependent is that dependent, said again under the new vocabulary, about the
    same Subject, under the same predicate, with its exact predecessor digest.
    A sibling that revises some other Claim, or moves the one it revises to
    another Subject, would be a different decision wearing this disposition.
    """

    resolved: dict[int, dict[str, int]] = {}
    owner: dict[int, int] = {}
    for index, member in enumerate(members):
        if not isinstance(member, ClaimTypeSuccessionMemberV1):
            continue
        bound: dict[str, int] = {}
        for position, dependent in enumerate(member.dependents):
            if dependent.disposition != "re_author":
                continue
            element = f"members[{index}].dependents[{position}].successor_claim_id"
            named = str(dependent.successor_claim_id)
            # Every repair names the key the payload actually carries, at the
            # value that key may hold: a re-authoring sibling revises the
            # dependent itself, so the only admissible spelling is the
            # dependent's own Claim ID. The map below is keyed by the sibling's
            # minted Claim ID, so binding it also settles whose Claim it is.
            required = dependent.identity.name
            sibling_index = sibling_member_by_claim_id.get(named)
            if sibling_index is None:
                _refuse(
                    "playbill.authoring.claim_type_succession_re_author_invalid",
                    element,
                    "No Claim member of this change set revises the Claim this dependent "
                    "is re-authored as.",
                    repair_kind="replace_re_author_member",
                    repair_description=(
                        f"Name {required} here, and author the sibling Claim member that "
                        "revises it."
                    ),
                    replacement={
                        "member": index,
                        "named_claim_id": named,
                        "reason": "member_not_found",
                        "successor_claim_id": required,
                    },
                )
            sibling = members[sibling_index]
            # Only Claim members are in the map above, so the sibling is one.
            assert isinstance(sibling, ClaimAuthoringPayloadV1)
            if sibling.statement.predicate != member.predicate:
                _refuse(
                    "playbill.authoring.claim_type_succession_re_author_invalid",
                    element,
                    "A re-authoring sibling Claim member lowers under the succeeded "
                    "ClaimType, not another one.",
                    repair_kind="replace_re_author_member",
                    repair_description=(f"Author the sibling Claim under {member.predicate!r}."),
                    replacement={
                        "expected_predicate": member.predicate,
                        "member": index,
                        "named_claim_id": named,
                        "predicate": sibling.statement.predicate,
                        "reason": "predicate_mismatch",
                        "sibling_member": sibling_index,
                        "successor_claim_id": required,
                    },
                )
            if named != required:
                _refuse(
                    "playbill.authoring.claim_type_succession_re_author_invalid",
                    element,
                    "A re-authored dependent keeps its own Claim identity.",
                    repair_kind="replace_re_author_member",
                    repair_description=f"Name {required}, the Claim this dependent re-authors.",
                    replacement={
                        "member": index,
                        "named_claim_id": named,
                        "reason": "identity_mismatch",
                        "sibling_member": sibling_index,
                        "successor_claim_id": required,
                    },
                )
            accepted_content = base_tree.get(claim_path(required))
            if accepted_content is not None:
                accepted = parse_claim(accepted_content, path=claim_path(required))
                if sibling.statement.subject != accepted.statement.subject:
                    _refuse(
                        "playbill.authoring.claim_type_succession_re_author_invalid",
                        f"members[{sibling_index}].statement.subject",
                        "A re-authored dependent keeps the Subject it was accepted about: "
                        "moving it would state a different Claim under this one's identity.",
                        repair_kind="replace_subject",
                        repair_description=(
                            "State this revision about the Subject the Claim it re-authors "
                            "is about, or drop the re_author disposition."
                        ),
                        replacement={
                            "expected_subject": accepted.statement.subject.model_dump(mode="json"),
                            "member": index,
                            "reason": "subject_mismatch",
                            "sibling_member": sibling_index,
                            "subject": sibling.statement.subject.model_dump(mode="json"),
                        },
                    )
            claimed_by = owner.get(sibling_index)
            if claimed_by is not None:
                _refuse(
                    "playbill.authoring.claim_type_succession_re_author_invalid",
                    element,
                    f"Members {claimed_by} and {index} both re-author member {sibling_index}.",
                    repair_kind="replace_re_author_member",
                    repair_description="Re-author one Claim member from one succession.",
                    replacement={
                        "claimed_by": claimed_by,
                        "member": index,
                        "named_claim_id": named,
                        "reason": "member_already_re_authored",
                        "sibling_member": sibling_index,
                        "successor_claim_id": required,
                    },
                )
            owner[sibling_index] = index
            bound[dependent.identity.qualified] = sibling_index
        resolved[index] = bound
    return resolved


def _refuse_contended_succession_paths(
    *,
    index: int,
    changed_paths: set[str],
    owner_by_path: Mapping[str, int],
    re_authored: frozenset[int],
    members: tuple[AuthoringChangeSetMemberV1, ...],
) -> None:
    """Refuse a set that settles one artifact twice: in a succession and as a member.

    A succession rewrites every dependent of the ClaimType it succeeds. Another
    member that authors one of those same paths -- a `ClaimRetirementMemberV1`
    withdrawing a carried Claim, say -- chains off a version this generation
    never accepts, and the compiler answers with a raw `stale_predecessor`
    diagnostic rather than a refusal an author can act on. Both members are
    named here instead, before the tree is compiled.

    Two overlaps are legitimate and exempt: the re-authoring siblings this
    succession lowered itself, and the definition-stage members it re-pins --
    re-pinning what the same set just defined is what the staged tree is for.
    """

    for path in sorted(changed_paths, key=lambda item: item.encode("utf-8")):
        other = owner_by_path.get(path)
        if other is None or other == index or other in re_authored:
            continue
        if _member_stage(members[other]) == "definition":
            continue
        _refuse(
            "playbill.authoring.change_set_member_path_collision",
            f"members[{index}].dependents",
            f"Members {index} and {other} both change {path!r}: this succession already "
            "dispositions it.",
            repair_kind="drop_or_merge_member",
            repair_description=(
                "Drop the sibling member and say what it says through this dependent's "
                "disposition instead."
            ),
            replacement={"members": sorted((index, other)), "path": path},
        )


def _stage_claim_type_succession(
    instance: PlaybillInstance,
    *,
    member: ClaimTypeSuccessionMemberV1,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    staged_tree: dict[str, bytes],
    path: str,
    members: tuple[AuthoringChangeSetMemberV1, ...],
    claim_identities: Mapping[str, str],
    re_author_siblings: Mapping[str, int],
) -> StagedMember:
    """Succeed one ClaimType and settle its whole closure inside this change set.

    The closure is computed over the STAGED tree -- the accepted tree as this
    set's definition members left it -- so a definition this set writes is read
    at its staged bytes. A sibling Claim member is never in it: successions
    stage before Claims, so a Claim of the succeeded predicate authored in this
    same set lowers under the SUCCESSOR and lands as an ordinary member. The
    candidate itself is built by the same function the standalone
    `/claim-types/proposals` migration builds it with: what a succession does to
    its dependents is one law, and this road only supplies one more way to
    answer it -- `re_author`, where a sibling Claim member of this intent is the
    dependent's successor.
    """

    for position, dependent in enumerate(member.dependents):
        if dependent.disposition != "invalidation":
            continue
        _refuse(
            "playbill.authoring.claim_type_succession_disposition_deprecated",
            f"dependents[{position}].disposition",
            "`invalidation` is the standalone migration route's deprecated spelling of "
            "`retire`. A change set refuses or accepts -- it has no warning channel to "
            "answer a deprecated word with -- so say which retirement this is.",
            repair_kind="replace_disposition",
            repair_description=(
                "Disposition this dependent `retire` with a claim_retirement_reason, or "
                "run the succession through the operator route "
                "`cruxible playbill claim-type migrate`, which still tolerates the "
                "deprecated word and warns."
            ),
            replacement={
                "identity": dependent.identity.qualified,
                "operator_route": "playbill claim-type migrate",
                "permitted_dispositions": ["re_author", "retire", "successor"],
            },
        )
    try:
        type_path, predecessor, successor = resolve_claim_type_succession(
            staged_tree,
            member.successor,
        )
    except ClaimTypeMigrationError as error:
        _refuse(
            "playbill.authoring.claim_type_succession_invalid",
            "successor",
            str(error),
            repair_kind="replace_successor",
            repair_description=(
                "Name the ClaimType this set succeeds and pin its exact current digest."
            ),
        )
    try:
        inventory = claim_type_migration_inventory(staged_tree, root=successor.identity)
    except ClaimTypeMigrationError as error:
        _refuse(
            "playbill.authoring.claim_type_succession_closure_unsupported",
            "dependents",
            str(error),
            repair_kind="split_change_set",
            repair_description=(
                "Settle the dependents this succession cannot reach through their own "
                "governed change first."
            ),
        )
    required = {item.identity.qualified: item for item in inventory}
    supplied = {item.identity.qualified: item for item in member.dependents}
    if set(required) != set(supplied):
        _refuse(
            "playbill.authoring.claim_type_succession_closure_incomplete",
            "dependents",
            "A ClaimType succession must disposition its exact reverse-pin closure.",
            repair_kind="replace_dependents",
            repair_description="Carry exactly the listed dependents at their exact digests.",
            replacement={
                "required_dependents": [item.model_dump(mode="json") for item in inventory],
                "supplied_dependents": sorted(supplied, key=lambda item: item.encode("utf-8")),
            },
        )
    if successor.object_kind != predecessor.object_kind:
        for position, dependent in enumerate(member.dependents):
            row = required[dependent.identity.qualified]
            live_claim = row.artifact_kind == "claim" and "retire" in row.permitted_dispositions
            if dependent.disposition == "successor" and live_claim:
                _refuse(
                    "playbill.authoring.claim_type_succession_object_kind_change",
                    f"dependents[{position}].disposition",
                    "A successor that changes object_kind cannot carry a live Claim "
                    "dependent unchanged: its object no longer says what the ClaimType "
                    "now means.",
                    repair_kind="replace_disposition",
                    repair_description=(
                        "Retire this dependent, or re-author it as a sibling Claim member "
                        "under the successor."
                    ),
                    replacement={
                        "identity": dependent.identity.qualified,
                        "permitted_dispositions": ["retire", "re_author"],
                        "predecessor_object_kind": predecessor.object_kind,
                        "successor_object_kind": successor.object_kind,
                    },
                )
    # Two trees, deliberately. A re-authoring sibling is lowered against the
    # SUCCESSOR vocabulary, or its own object-kind and schema laws would be
    # judged by the ClaimType this set is replacing. The builder, meanwhile,
    # takes the tree with the PREDECESSOR still installed: what it migrates
    # every dependent away from is exactly the digest it reads there.
    lowering_tree = dict(staged_tree)
    lowering_tree[type_path] = render_claim_type(successor)
    working = dict(staged_tree)
    authored: dict[str, bytes] = {}
    sibling_resolved: dict[int, dict[str, object]] = {}
    extra: set[str] = set()
    for position, dependent in enumerate(member.dependents):
        if dependent.disposition != "re_author":
            continue
        sibling_index = re_author_siblings[dependent.identity.qualified]
        sibling = members[sibling_index]
        assert isinstance(sibling, ClaimAuthoringPayloadV1)
        claim_id = claim_identities[authoring_member_identity(sibling)]
        try:
            lowered = _lower_claim(
                instance,
                intent=intent,
                actor_id=actor_id,
                base=base,
                base_tree=lowering_tree,
                payload=sibling,
                claim_identity=claim_id,
            )
        except AuthoringLoweringError as error:
            _refuse(
                "playbill.authoring.claim_type_succession_re_author_refused",
                f"dependents[{position}].successor_claim_id",
                error.message,
                repair_kind="edit_re_author_member",
                repair_description=(
                    "Repair the sibling Claim member this dependent is re-authored as."
                ),
                replacement={
                    "code": error.code,
                    "offending_element": (f"members[{sibling_index}].{error.offending_element}"),
                    "sibling_member": sibling_index,
                    "successor_claim_id": dependent.identity.name,
                },
            )
        sibling_path = claim_path(claim_id)
        authored[dependent.identity.qualified] = lowered.proposed_tree[sibling_path]
        for extra_path, content in lowered.changed_members:
            if extra_path == sibling_path:
                continue
            lowering_tree[extra_path] = content
            working[extra_path] = content
            extra.add(extra_path)
        member_resolved = dict(lowered.resolved_authoring)
        member_resolved["claim_id"] = claim_id
        sibling_resolved[sibling_index] = member_resolved
    dispositions = tuple(
        ClaimTypeDependentDispositionV3(
            identity=item.identity,
            disposition="successor" if item.disposition == "re_author" else item.disposition,
            claim_retirement_reason=item.claim_retirement_reason,
            claim_effective_until=item.claim_effective_until,
        )
        for item in member.dependents
    )
    try:
        candidate_tree, normalized, _warnings = build_claim_type_migration_candidate(
            tree=working,
            type_path=type_path,
            successor=successor,
            inventory=inventory,
            dispositions=dispositions,
            authored_successors=authored,
        )
    except ClaimTypeMigrationError as error:
        _refuse(
            "playbill.authoring.claim_type_succession_dependent_invalid",
            "dependents",
            str(error),
            repair_kind="replace_dependents",
            repair_description=(
                "Re-read the closure this succession owes and disposition it exactly."
            ),
        )
    for changed_path, content in candidate_tree.items():
        if changed_path != path and working.get(changed_path) != content:
            extra.add(changed_path)
    resolved: dict[str, object] = {
        "artifact_digest": claim_type_digest(successor).tagged,
        "predecessor_digest": claim_type_digest(predecessor).tagged,
        "dependents": [item.model_dump(mode="json") for item in normalized],
    }
    return candidate_tree, resolved, extra, sibling_resolved


def _stage_claim_retirement(
    instance: PlaybillInstance,
    *,
    member: ClaimRetirementMemberV1,
    base: AcceptedProjectionCoordinate,
    staged_tree: dict[str, bytes],
    path: str,
) -> tuple[dict[str, bytes], dict[str, object], set[str]]:
    """Retire one Claim and its live closure inside the same change set."""

    content = staged_tree.get(path)
    if content is None:
        _refuse(
            "playbill.authoring.claim_predecessor_not_found",
            "claim_ref",
            "The Claim named for retirement does not exist in this change set's tree.",
            repair_kind="replace_claim_ref",
            repair_description="Retire a Claim accepted at the intent base.",
        )
    claim = parse_claim(content, path=path)
    if claim.lifecycle.state != "live":
        _refuse(
            "playbill.authoring.claim_terminal",
            "claim_ref",
            "A retired Claim cannot be retired again.",
            repair_kind="drop_member",
            repair_description="Remove this retirement member; the Claim is already retired.",
        )
    root = ClaimRetireDependentV1(
        artifact_identity=claim.identity,
        predecessor_digest=claim_artifact_digest(claim).tagged,
        reason=member.reason,
        effective_until=member.effective_until,
    )
    try:
        inventory = claim_retirement_inventory(
            instance,
            tree=staged_tree,
            coordinate=AcceptedCoordinate.from_internal(base),
            claim=claim,
        )
    except ClaimRetireError as error:
        _refuse(
            "playbill.authoring.claim_retirement_closure_unsupported",
            "dependents",
            str(error),
            repair_kind="split_change_set",
            repair_description=(
                "Retire the non-Claim dependents through their own governed change first."
            ),
        )
    expected = {item.artifact_identity.qualified: item.predecessor_digest for item in inventory}
    supplied = {
        item.artifact_identity.qualified: item.predecessor_digest for item in member.dependents
    }
    if supplied != expected:
        _refuse(
            "playbill.authoring.claim_retirement_closure_incomplete",
            "dependents",
            "A retirement member must carry its exact live Claim closure.",
            repair_kind="replace_dependents",
            repair_description="Carry exactly the listed dependents at their exact digests.",
            replacement={
                "required_dependents": [item.model_dump(mode="json") for item in inventory],
                "supplied_dependents": sorted(
                    supplied,
                    key=lambda item: item.encode("utf-8"),
                ),
            },
        )
    try:
        candidate_tree, retirements = build_claim_retirement_candidate(
            staged_tree,
            root=root,
            dependents=member.dependents,
        )
    except ClaimRetireError as error:
        _refuse(
            "playbill.authoring.claim_retirement_stale",
            "dependents",
            str(error),
            repair_kind="replace_dependents",
            repair_description="Re-read the closure at the intent base and resubmit.",
        )
    extra = {
        claim_path(item.artifact_identity.name)
        for item in retirements
        if item.artifact_identity.name != claim.identity.name
    }
    successor = next(
        item.successor_digest
        for item in retirements
        if item.artifact_identity.qualified == claim.identity.qualified
    )
    return (
        dict(candidate_tree),
        {
            "artifact_digest": successor,
            "claim_id": claim.identity.name,
            "predecessor_digest": root.predecessor_digest,
            "retirements": [item.model_dump(mode="json") for item in retirements],
        },
        extra,
    )


def _encoded_members(members: tuple[tuple[str, bytes], ...]) -> list[dict[str, object]]:
    return [
        {
            "content_base64": base64.b64encode(content).decode("ascii"),
            "content_sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "path": path,
        }
        for path, content in members
    ]


def lower_authoring(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor_id: str,
) -> LoweredAuthoring:
    """Resolve one intent against its immutable base without submitting anything."""

    base = instance.resolve_accepted_coordinate(
        git_oid=intent.base_coordinate.git_oid,
        semantic_root=intent.base_coordinate.semantic_root,
        generation_root=intent.base_coordinate.generation_root,
    )
    base_tree = instance.tree_at(base.git_oid)
    if isinstance(intent.payload, ClaimAuthoringPayloadV1):
        return _lower_claim(
            instance,
            intent=intent,
            actor_id=actor_id,
            base=base,
            base_tree=base_tree,
        )
    if isinstance(intent.payload, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2):
        return _lower_procedure(intent=intent, base=base, base_tree=base_tree)
    if isinstance(intent.payload, ProcedureMandateAuthoringPayloadV1):
        path, content, digest = _render_procedure_mandate_member(
            intent.payload,
            tree=base_tree,
        )
        candidate_tree = dict(base_tree)
        candidate_tree[path] = content
        changed = () if base_tree.get(path) == content else ((path, content),)
        return LoweredAuthoring(
            proposed_tree=candidate_tree,
            resolved_authoring={
                "artifact_digest": digest,
                "changed_members": _encoded_members(changed),
                "identity": authoring_member_identity(intent.payload),
            },
            changed_members=changed,
        )
    if isinstance(intent.payload, ChangeSetAuthoringPayloadV1):
        return _lower_change_set(
            instance,
            intent=intent,
            actor_id=actor_id,
            base=base,
            base_tree=base_tree,
        )
    return _lower_non_procedure(payload=intent.payload, base_tree=base_tree)


__all__ = [
    "AuthoringLoweringError",
    "LoweredAuthoring",
    "lower_authoring",
]
