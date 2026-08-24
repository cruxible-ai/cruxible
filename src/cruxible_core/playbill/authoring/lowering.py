"""Lower ergonomic authoring payloads onto ordinary governed artifact wires."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    AuthoringArtifactReferenceV1,
    AuthoringExactContentObjectV1,
    AuthoringIntentV1,
    ClaimAuthoringPayloadV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
    RepairAlternativeV1,
    SelfSourceBodyV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    CaptureBuildResult,
    DirectCaptureBuildResult,
    build_coordinator_self_source_capture,
    build_working_selection_capture,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
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
    merge_claim_citations,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.knowledge_briefs import (
    KNOWLEDGE_BRIEF_CLAIM_TYPE,
    KNOWLEDGE_BRIEF_PREDICATE,
    knowledge_brief_purpose_digest,
    parse_knowledge_brief_value,
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
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV3, iter_pin_bindings
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_core.playbill.compiler import projection_registry_for_compiler
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_artifacts import parse_projection_tree


@dataclass(frozen=True)
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


def _same_statement_claims(
    tree: dict[str, bytes], statement: ClaimStatement
) -> tuple[ClaimArtifactAny, ...]:
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
    type_content = base_tree.get(type_path)
    candidate_base_tree = dict(base_tree)
    installs_brief_type = (
        type_content is None and payload.statement.predicate == KNOWLEDGE_BRIEF_PREDICATE
    )
    if installs_brief_type:
        type_content = render_claim_type(KNOWLEDGE_BRIEF_CLAIM_TYPE)
        candidate_base_tree[type_path] = type_content
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
        base_tree, payload.statement.subject, descriptor=descriptor
    )
    statement_object = _exact_object(instance, payload.statement.object)
    qualifier = payload.statement.qualifier
    if payload.statement.predicate == KNOWLEDGE_BRIEF_PREDICATE:
        if not isinstance(statement_object, LiteralClaimObject):
            _refuse(
                "playbill.authoring.knowledge_brief_object_invalid",
                "statement.object",
                "knowledge.brief requires its exact versioned literal value.",
                repair_kind="replace_object",
                repair_description="Supply a playbill-knowledge-brief-value-v1 literal.",
            )
        try:
            brief = parse_knowledge_brief_value(statement_object.value)
        except ValueError:
            _refuse(
                "playbill.authoring.knowledge_brief_value_invalid",
                "statement.object.value",
                "The Brief literal fails its frozen profile.",
                repair_kind="replace_object",
                repair_description="Supply a canonical playbill-knowledge-brief-value-v1.",
            )
        expected_qualifier = knowledge_brief_purpose_digest(brief.purpose)
        if qualifier is not None and qualifier != expected_qualifier:
            _refuse(
                "playbill.authoring.knowledge_brief_qualifier_mismatch",
                "statement.qualifier",
                "The Brief qualifier differs from its purpose slot digest.",
                repair_kind="omit_qualifier",
                repair_description="Omit the qualifier so the coordinator derives it.",
                replacement=None,
            )
        qualifier = expected_qualifier
        statement_object = LiteralClaimObject(value=brief.model_dump(mode="json"))
    object_referent: tuple[ArtifactIdentity, str] | None = None
    if isinstance(statement_object, SubjectClaimObject):
        object_referent = _referent(base_tree, statement_object.address, descriptor=descriptor)
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
    existing = _same_statement_claims(candidate_base_tree, statement)
    if claim_type.artifact_format == "playbill-claim-type-v2":
        existing = tuple(item for item in existing if item.statement.qualifier == qualifier)
    expected = {item.identity.name for item in existing}
    supplied = {item.claim_id for item in payload.existing_claim_dispositions}
    if expected != supplied:
        _refuse(
            "playbill.authoring.existing_claim_dispositions_incomplete",
            "existing_claim_dispositions",
            "Every live same-subject/predicate Claim must receive an explicit disposition.",
            repair_kind="replace_dispositions",
            repair_description="Disposition exactly the listed existing Claim IDs.",
            replacement=sorted(expected, key=lambda item: item.encode("ascii")),
        )

    claim_id = intent.semantic_identity
    path = claim_path(claim_id)
    predecessor: ClaimArtifactAny | None = None
    if path in candidate_base_tree:
        predecessor = parse_claim(candidate_base_tree[path], path=path)
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
    built_capture: CaptureBuildResult | DirectCaptureBuildResult
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

    identity = ArtifactIdentity(kind="Claim", name=claim_id)
    citation = build_claim_citation(
        identity,
        capture_digest=built_capture.capture_digest,
        role=citation_role,
        origin=citation_origin,
    )
    predecessor_citations = (
        predecessor.backing.citations if isinstance(predecessor, ClaimArtifactV2) else ()
    )
    capture_digests = tuple(
        sorted(
            {
                *(() if predecessor is None else predecessor.backing.capture_digests),
                built_capture.capture_digest,
            },
            key=lambda item: item.encode("ascii"),
        )
    )
    source_mapping = SourceMapping(
        subject=claim_statement_address(path),
        spans=(
            ContentSpan(
                content_digest=built_capture.envelope.commitment.digest,
                start_byte=0,
                end_byte=built_capture.envelope.commitment.byte_length or 0,
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
        authority=claim_type.authority,
        pins=_merge_pins(() if predecessor is None else predecessor.pins, tuple(pins)),
        lifecycle=ArtifactLifecycle(
            predecessor_digest=(
                None if predecessor is None else claim_artifact_digest(predecessor).tagged
            )
        ),
    )
    candidate_tree = dict(candidate_base_tree)
    contract_member_path = capture_contract_path(contract.identity.name)
    contract_bytes = render_capture_contract(contract)
    claim_bytes = render_claim(claim)
    candidate_tree[contract_member_path] = contract_bytes
    candidate_tree[path] = claim_bytes
    member_paths = {contract_member_path, path}
    if installs_brief_type:
        member_paths.add(type_path)
    changed = tuple(
        (member_path, candidate_tree[member_path])
        for member_path in sorted(
            member_paths,
            key=lambda item: item.encode("utf-8"),
        )
        if base_tree.get(member_path) != candidate_tree[member_path]
    )
    return LoweredAuthoring(
        proposed_tree=candidate_tree,
        resolved_authoring={
            "artifact_digest": claim_artifact_digest(claim).tagged,
            "capture_digest": built_capture.capture_digest,
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
    )


def _resolve_authoring_references(
    value: object,
    *,
    accepted: dict[str, tuple[str, str]],
    owned_contracts: dict[str, ProcedureOwnedContractV1] | None = None,
    location: str = "definition",
) -> object:
    if isinstance(value, dict):
        if value.get("tag") == "playbill-authoring-artifact-reference-v1":
            try:
                reference = AuthoringArtifactReferenceV1.model_validate(value)
            except ValueError as exc:
                _refuse(
                    "playbill.authoring.artifact_reference_invalid",
                    location,
                    f"The procedure authoring reference is invalid: {exc}",
                    repair_kind="replace_reference",
                    repair_description="Use a valid accepted-at-intent-base artifact reference.",
                )
            resolved = accepted.get(reference.target.qualified)
            if resolved is None:
                _refuse(
                    "playbill.authoring.artifact_reference_unresolved",
                    location,
                    "The referenced artifact is not uniquely accepted at the intent base.",
                    repair_kind="replace_with_pin_slot",
                    repair_description="Use an explicit Procedure pin-slot reference.",
                )
            _path, digest = resolved
            return ArtifactPin(
                role=reference.role,
                target=reference.target,
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
) -> LoweredAuthoring:
    payload = intent.payload
    assert isinstance(payload, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2)
    parsed = parse_projection_tree(
        base_tree,
        registry=projection_registry_for_compiler(base.compiler),
    )
    accepted: dict[str, tuple[str, str]] = {}
    duplicates: set[str] = set()
    for envelope in parsed.envelopes:
        if envelope.identity in accepted:
            duplicates.add(envelope.identity)
        accepted[envelope.identity] = (envelope.path, envelope.artifact_digest)
    for duplicate_identity in duplicates:
        accepted.pop(duplicate_identity, None)
    owned_contracts = (
        {contract.identity.name: contract for contract in payload.owned_contracts}
        if isinstance(payload, ProcedureAuthoringPayloadV2)
        else None
    )
    resolved_definition = _resolve_authoring_references(
        payload.definition,
        accepted=accepted,
        owned_contracts=owned_contracts,
    )
    try:
        definition = ProcedureDefinitionV3.model_validate(resolved_definition)
    except ValueError as exc:
        _refuse(
            "playbill.authoring.procedure_definition_invalid",
            "definition",
            f"The lowered graph-v3 Procedure definition is invalid: {exc}",
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
    lifecycle = ArtifactLifecycle(
        state="retired" if payload.retire else "live",
        predecessor_digest=(
            None if predecessor is None else procedure_artifact_digest(predecessor).tagged
        ),
    )
    if isinstance(payload, ProcedureAuthoringPayloadV2):
        procedure: ProcedureArtifactAny = ProcedureArtifactV2(
            identity=identity,
            definition=definition,
            definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
            authority=payload.authority,
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
            authority=payload.authority,
            pins=pins,
            activation_policy=payload.activation_policy,
            lifecycle=lifecycle,
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
    return _lower_procedure(intent=intent, base=base, base_tree=base_tree)


__all__ = [
    "AuthoringLoweringError",
    "LoweredAuthoring",
    "lower_authoring",
]
