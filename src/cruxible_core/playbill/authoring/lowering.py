"""Lower ergonomic authoring payloads onto ordinary governed artifact wires."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from pydantic import ValidationError

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
    AuthoringExactContentObjectV1,
    AuthoringIntentV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimAuthoringPayloadV3,
    ExistingCaptureCitationSourceV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
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
from cruxible_core.playbill.compiler import (
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
from cruxible_core.playbill.instance import PlaybillInstance
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


def _provider_digests_for_capture(
    tree: Mapping[str, bytes],
    *,
    producer: ArtifactIdentity,
    executable: ArtifactIdentity,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for identity in {producer, executable}:
        if identity.kind != "Provider":
            continue
        path = provider_path(identity.name)
        content = tree.get(path)
        if content is None:
            continue
        provider = parse_provider(content, path=path)
        result[identity.qualified] = provider_digest(provider).tagged
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
        if (
            subject_draft.lifecycle.state != "live"
            or subject_draft.lifecycle.predecessor_digest is not None
        ):
            _refuse(
                "playbill.authoring.dependency_not_one_claim",
                "dependency_drafts.subject.lifecycle",
                "A one-Claim dependency closure cannot carry a Subject succession.",
                repair_kind="remove_dependency_successor",
                repair_description="Submit the Subject successor as a separate governed change.",
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
        if (
            claim_type_draft.lifecycle.state != "live"
            or claim_type_draft.lifecycle.predecessor_digest is not None
        ):
            _refuse(
                "playbill.authoring.dependency_not_one_claim",
                "dependency_drafts.claim_type.lifecycle",
                "A one-Claim dependency closure cannot carry a ClaimType succession.",
                repair_kind="remove_dependency_successor",
                repair_description="Submit the ClaimType successor as a separate governed change.",
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
) -> LoweredAuthoring:
    payload = intent.payload
    assert isinstance(payload, ClaimAuthoringPayloadV1)
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
    qualifier = payload.statement.qualifier
    object_referent: tuple[ArtifactIdentity, str] | None = None
    if isinstance(statement_object, SubjectClaimObject):
        object_referent = _referent(
            candidate_base_tree, statement_object.address, descriptor=descriptor
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

    claim_id = intent.semantic_identity
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
            envelope = verify_capture(
                payload.source.capture_digest,
                store=store,
                contract=accepted_contract.contract,
                ledger_resolver=_TreeLedgerResolver(
                    candidate_base_tree,
                    AcceptedCoordinate.from_internal(base),
                ),
                producer_artifact_digests=_provider_digests_for_capture(
                    candidate_base_tree,
                    producer=envelope.producer,
                    executable=envelope.run_coordinate.executable_identity,
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
    | ApprovalPolicyAuthoringPayloadV1
    | ProcedureRuntimePolicyAuthoringPayloadV1,
) -> tuple[str, bytes, str]:
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


def _lower_change_set(
    *,
    intent: AuthoringIntentV1,
    base: AcceptedProjectionCoordinate,
    base_tree: dict[str, bytes],
) -> LoweredAuthoring:
    payload = intent.payload
    assert isinstance(payload, ChangeSetAuthoringPayloadV1)
    if any(isinstance(member, ApprovalPolicyAuthoringPayloadV1) for member in payload.members):
        _refuse(
            "playbill.authoring.approval_policy_singleton_required",
            "members",
            "ApprovalPolicy authoring must be submitted as a singleton payload.",
            repair_kind="split_change_set",
            repair_description=(
                "Author and accept the ApprovalPolicy separately from the change set."
            ),
        )
    if any(
        isinstance(member, ProcedureRuntimePolicyAuthoringPayloadV1) for member in payload.members
    ):
        _refuse(
            "playbill.authoring.procedure_runtime_policy_singleton_required",
            "members",
            "ProcedureRuntimePolicy authoring must be submitted as a singleton payload.",
            repair_kind="split_change_set",
            repair_description=(
                "Author and accept the ProcedureRuntimePolicy separately from the change set."
            ),
        )
    staged_tree = dict(base_tree)
    member_paths: set[str] = set()
    resolved: list[dict[str, object]] = []
    candidate_identities = frozenset(
        authoring_member_identity(member)
        for member in payload.members
        if not isinstance(member, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2)
    )
    for member in payload.members:
        if isinstance(member, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2):
            continue
        path, content, digest = _render_non_procedure_member(member)
        member_paths.add(path)
        staged_tree[path] = content
        resolved.append(
            {
                "artifact_digest": digest,
                "identity": authoring_member_identity(member),
                "path": path,
            }
        )
    for member in payload.members:
        if not isinstance(member, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2):
            continue
        path = procedure_path(str(member.definition["name"]))
        member_paths.add(path)
        lowered = _lower_procedure(
            intent=intent.model_copy(update={"payload": member}),
            base=base,
            base_tree=staged_tree,
            accepted_reference_tree=base_tree,
            candidate_identities=candidate_identities,
        )
        staged_tree = lowered.proposed_tree
        member_resolved = dict(lowered.resolved_authoring)
        member_resolved["identity"] = authoring_member_identity(member)
        member_resolved["path"] = path
        resolved.append(member_resolved)
    changed = tuple(
        (path, staged_tree[path])
        for path in sorted(member_paths, key=lambda item: item.encode("utf-8"))
        if base_tree.get(path) != staged_tree[path]
    )
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
    if isinstance(intent.payload, ChangeSetAuthoringPayloadV1):
        return _lower_change_set(intent=intent, base=base, base_tree=base_tree)
    return _lower_non_procedure(payload=intent.payload, base_tree=base_tree)


__all__ = [
    "AuthoringLoweringError",
    "LoweredAuthoring",
    "lower_authoring",
]
