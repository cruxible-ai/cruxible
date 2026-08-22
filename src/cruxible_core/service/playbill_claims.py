"""Canonical service contract for first-class Playbill Claims."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_core.playbill.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    DirectByteSpanSelectionV1,
    DirectCaptureBuildResult,
    DirectClaimSelectionV1,
    DirectForeignSourceSelectionV1,
    build_direct_claim_capture,
    build_direct_claim_selection_capture,
    build_foreign_source_capture,
    capture_contract_path,
    parse_capture_envelope,
    render_capture_contract,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_attestations import VerifiedClaimAttestationV1
from cruxible_core.playbill.claim_type_structure import claim_type_structural_signature
from cruxible_core.playbill.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_core.playbill.claim_verdicts import ClaimVerdictResultV1
from cruxible_core.playbill.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimBacking,
    ClaimLawEvidenceV1,
    ClaimReferentContext,
    ClaimStatement,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_referent_context_digest,
    claim_statement_address,
    claim_statement_digest,
    new_claim_id,
    parse_claim,
    render_claim,
)
from cruxible_core.playbill.dereference import (
    ExternalSelectionReaderProtocol,
    dereference_source_handle,
)
from cruxible_core.playbill.diagnostics import GovernedOperationReference
from cruxible_core.playbill.discovery import ContextCapsuleV1, ExpandRequestV1
from cruxible_core.playbill.errors import (
    ClaimNotFoundError,
    ProposalIntegrityError,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.policies import (
    ClaimVerdict,
    ResolutionContenderV1,
    resolve_claim_contenders,
)
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_claims import ClaimProjectionView
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.query.cards import (
    ClaimTypeUsageRowV1,
    SemanticRelationV1,
    build_claim_type_card,
    build_subject_profile,
)
from cruxible_core.playbill.query.definitions import (
    parse_query_definition,
    query_definition_digest,
)
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.query.semantic_discovery import DiscoveryEntryV1
from cruxible_core.playbill.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)
from cruxible_core.playbill.settlement import ChangeSetRecord
from cruxible_core.playbill.source_references import (
    CoverageDescriptorV1,
    ExternalSourceReferenceV1,
    OpenSourceRequestV1,
    SourceDereferenceResultV1,
    SourceHandleV1,
    source_handle_digest,
)
from cruxible_core.playbill.subjects import (
    SubjectShell,
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
    subject_reuse_signature,
)


class _StrictClaimServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ExistingStatementDisposition = Literal["not_tested", "support", "contradict", "unsure"]


class ExistingClaimStatementHandleV1(_StrictClaimServiceModel):
    tag: Literal["playbill-existing-claim-statement-v1"] = "playbill-existing-claim-statement-v1"
    claim_identity: str
    claim_path: str
    statement_address: SemanticAddress
    statement_digest: str
    artifact_digest: str

    @field_validator("statement_digest", "artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ExistingStatementHandoffV1(_StrictClaimServiceModel):
    statement_digest: str
    disposition: ExistingStatementDisposition

    @field_validator("statement_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class DirectClaimAuthoringV1(_StrictClaimServiceModel):
    """Caller input deliberately has no observed_at or evidence-validity field."""

    tag: Literal["playbill-direct-claim-authoring-v1"] = "playbill-direct-claim-authoring-v1"
    statement: ClaimStatement
    rationale: str
    claim_id: str | None = None
    predecessor_artifact_digest: str | None = None
    retire: bool = False
    materialize_source: bool = True
    source_selection: DirectClaimSelectionV1 | None = None
    subject_shell: SubjectShell | None = None
    claim_type_artifact: ClaimType | None = None
    dependency_subject_shells: tuple[SubjectShell, ...] = ()
    dependency_claim_types: tuple[ClaimType, ...] = ()
    existing_statement_handoffs: tuple[ExistingStatementHandoffV1, ...] = ()

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("direct Claim rationale must not be empty")
        return value

    @field_validator("claim_id")
    @classmethod
    def _claim_id(cls, value: str | None) -> str | None:
        if value is not None:
            claim_path(value)
        return value

    @field_validator("predecessor_artifact_digest")
    @classmethod
    def _predecessor_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("existing_statement_handoffs")
    @classmethod
    def _handoffs(
        cls, value: tuple[ExistingStatementHandoffV1, ...]
    ) -> tuple[ExistingStatementHandoffV1, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.statement_digest.encode("ascii")))
        if value != ordered or len({item.statement_digest for item in value}) != len(value):
            raise ValueError("existing statement handoffs must be sorted and unique")
        return value

    @field_validator("dependency_subject_shells")
    @classmethod
    def _dependency_subjects(cls, value: tuple[SubjectShell, ...]) -> tuple[SubjectShell, ...]:
        keys = tuple((item.subject_kind, item.subject_id) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("dependency Subject shells must be sorted and unique")
        return value

    @field_validator("dependency_claim_types")
    @classmethod
    def _dependency_types(cls, value: tuple[ClaimType, ...]) -> tuple[ClaimType, ...]:
        keys = tuple(item.predicate for item in value)
        if keys != tuple(sorted(set(keys), key=lambda item: item.encode("utf-8"))):
            raise ValueError("dependency ClaimTypes must be sorted and unique")
        return value


class AuthoredClaimV1(_StrictClaimServiceModel):
    """One authored Claim's result, independent of how many shared its proposal."""

    tag: Literal["playbill-authored-claim-v1"] = "playbill-authored-claim-v1"
    claim_identity: str
    claim_path: str
    statement_digest: str
    artifact_digest: str
    capture_digest: str
    capture_digests: tuple[str, ...]
    observed_at: datetime
    existing_statements: tuple[ExistingClaimStatementHandleV1, ...]
    handoffs: tuple[ExistingStatementHandoffV1, ...]


class DirectClaimProposalV1(_StrictClaimServiceModel):
    tag: Literal["playbill-direct-claim-proposal-v1"] = "playbill-direct-claim-proposal-v1"
    proposal: PlaybillProposalInspection
    claim_identity: str
    claim_path: str
    statement_digest: str
    artifact_digest: str
    capture_digest: str
    capture_digests: tuple[str, ...]
    observed_at: datetime
    existing_statements: tuple[ExistingClaimStatementHandleV1, ...]
    handoffs: tuple[ExistingStatementHandoffV1, ...]


class DirectClaimBatchProposalV1(_StrictClaimServiceModel):
    """One proposal carrying every authored Claim as ordinary change-set members."""

    tag: Literal["playbill-direct-claim-batch-proposal-v1"] = (
        "playbill-direct-claim-batch-proposal-v1"
    )
    proposal: PlaybillProposalInspection
    claims: tuple[AuthoredClaimV1, ...]


class PlaybillClaimView(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-read-v1"] = "playbill-claim-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, object]
    facts: tuple[dict[str, object], ...]


class PlaybillClaimList(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-list-v1"] = "playbill-claim-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    claims: tuple[PlaybillClaimView, ...]


class PlaybillClaimQueryResult(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-query-v1"] = "playbill-claim-query-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    subject: SemanticAddress
    predicate: str
    cardinality: Literal["one", "many"]
    status: Literal["resolved", "unresolved", "refused"]
    selected_claim_identities: tuple[str, ...]
    contender_claim_identities: tuple[str, ...]
    claims: tuple[PlaybillClaimView, ...]
    verdicts: tuple[ClaimVerdictResultV1, ...]


class PlaybillClaimHistoryEntry(_StrictClaimServiceModel):
    sequence: int
    coordinate: PlaybillAcceptedCoordinate
    statement_digest: str
    artifact_digest: str
    predecessor_digest: str | None
    lifecycle_state: Literal["live", "retired"]
    change_set_path: str
    changeset_digest: str
    candidate_digest: str


class PlaybillClaimHistory(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-history-v1"] = "playbill-claim-history-v1"
    identity: str
    entries: tuple[PlaybillClaimHistoryEntry, ...]


class PlaybillClaimExplanationV1(_StrictClaimServiceModel):
    tag: Literal["playbill-claim-explanation-v1"] = "playbill-claim-explanation-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    claim: PlaybillClaimView
    law_evidence: ClaimLawEvidenceV1
    verdict: ClaimVerdictResultV1
    exact_attestations: tuple[VerifiedClaimAttestationV1, ...]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: tuple[SourceHandleV1, ...]
    coverage: CoverageDescriptorV1


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _public_claim(view: ClaimProjectionView) -> PlaybillClaimView:
    if view.coordinate_kind != "canonical" or not isinstance(
        view.coordinate, AcceptedProjectionCoordinate
    ):
        raise ProposalIntegrityError("canonical Claim service received a provisional view")
    return PlaybillClaimView(
        coordinate=PlaybillAcceptedCoordinate.from_internal(view.coordinate),
        envelope=view.envelope.model_dump(mode="json"),
        facts=tuple(fact.model_dump(mode="json") for fact in view.facts),
    )


def _claim_from_view(view: PlaybillClaimView) -> ClaimArtifactAny:
    path = view.envelope.get("path")
    if not isinstance(path, str):
        raise ProposalIntegrityError("Claim projection envelope has no path")
    statement = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.statement"
        ),
        None,
    )
    backing = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.backing"
        ),
        None,
    )
    lifecycle = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.lifecycle"
        ),
        None,
    )
    identity = view.envelope.get("identity")
    if not (
        isinstance(identity, str)
        and isinstance(statement, dict)
        and isinstance(backing, dict)
        and isinstance(lifecycle, dict)
    ):
        raise ProposalIntegrityError("Claim projection lacks its complete canonical artifact")
    artifact_format = view.envelope.get("format_tag")
    model = ClaimArtifactV2 if artifact_format == "playbill-claim-v2" else ClaimArtifact
    return model.model_validate(
        {
            "artifact_format": artifact_format,
            "identity": {
                "kind": "Claim",
                "name": identity.removeprefix("Claim:"),
            },
            "statement": statement,
            "backing": backing,
            "authority": lifecycle.get("authority"),
            "pins": lifecycle.get("pins"),
            "lifecycle": lifecycle.get("lifecycle"),
        }
    )


def _existing_statements(
    tree: dict[str, bytes], statement: ClaimStatement
) -> tuple[ExistingClaimStatementHandleV1, ...]:
    handles: list[ExistingClaimStatementHandleV1] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if (
            claim.lifecycle.state == "live"
            and claim.statement.subject == statement.subject
            and claim.statement.predicate == statement.predicate
        ):
            handles.append(
                ExistingClaimStatementHandleV1(
                    claim_identity=claim.identity.qualified,
                    claim_path=path,
                    statement_address=claim_statement_address(path),
                    statement_digest=claim_statement_digest(claim.statement).tagged,
                    artifact_digest=claim_artifact_digest(claim).tagged,
                )
            )
    return tuple(handles)


def _observed_at(timestamp: str) -> datetime:
    raw = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProposalIntegrityError("authenticated request timestamp is malformed") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProposalIntegrityError("authenticated request timestamp must be timezone-aware")
    return value


def _direct_referent(
    tree: dict[str, bytes],
    address: SemanticAddress,
    *,
    descriptor: bool,
) -> tuple[ArtifactIdentity, str]:
    if address.selector.scheme != "artifact-v1":
        raise ProposalIntegrityError("direct Claim referent must use whole-artifact identity")
    content = tree.get(address.artifact_path)
    if content is None:
        raise ProposalIntegrityError("direct Claim referent does not resolve in its candidate")
    if address.artifact_path.startswith("subjects/"):
        shell = parse_subject(content, path=address.artifact_path)
        return shell.identity, subject_digest(shell).tagged
    if descriptor and address.artifact_path.startswith("claim-types/"):
        referent_type = parse_claim_type(content, path=address.artifact_path)
        return referent_type.identity, claim_type_digest(referent_type).tagged
    raise ProposalIntegrityError("direct Claim referent kind is not admitted by this ClaimType")


def _write_shared_member(
    candidate_tree: dict[str, bytes],
    authored: dict[str, bytes],
    *,
    path: str,
    content: bytes,
    conflict: str,
) -> None:
    """Write one candidate path, refusing only a conflict with a sibling authoring.

    An authoring may always restate an artifact the accepted base already holds:
    the acceptance laws adjudicate that succession. What it may never do is
    contradict another authoring inside the same change set, because a change
    set settles as one indivisible generation and there is no later moment at
    which the two byte strings could be reconciled.
    """

    if authored.get(path, content) != content:
        raise ProposalIntegrityError(conflict)
    candidate_tree[path] = content
    authored[path] = content


def _write_dependency_member(
    candidate_tree: dict[str, bytes],
    authored: dict[str, bytes],
    *,
    path: str,
    content: bytes,
    conflict: str,
) -> None:
    """Write one declared dependency artifact, which may never contradict the candidate.

    A dependency is stated so the change set closes over it, not to change it,
    so identical bytes deduplicate silently against both the accepted base and
    every sibling authoring while differing bytes are a typed refusal.
    """

    if candidate_tree.get(path, content) != content:
        raise ProposalIntegrityError(conflict)
    candidate_tree[path] = content
    authored[path] = content


def _author_direct_claim(
    instance: PlaybillInstance,
    *,
    authoring: DirectClaimAuthoringV1,
    actor_id: str,
    timestamp: str,
    proposed_base: AcceptedProjectionCoordinate,
    base_tree: dict[str, bytes],
    candidate_tree: dict[str, bytes],
    authored: dict[str, bytes],
) -> AuthoredClaimV1:
    """Write one Capture and one dependency-closed Claim into a shared candidate."""

    if authoring.subject_shell is not None:
        shell_path = subject_path(
            authoring.subject_shell.subject_kind,
            authoring.subject_shell.subject_id,
        )
        _write_shared_member(
            candidate_tree,
            authored,
            path=shell_path,
            content=render_subject(authoring.subject_shell),
            conflict="Claim authorings in one proposal disagree on their Subject bytes",
        )
    for dependency_subject in authoring.dependency_subject_shells:
        dependency_path = subject_path(
            dependency_subject.subject_kind,
            dependency_subject.subject_id,
        )
        _write_dependency_member(
            candidate_tree,
            authored,
            path=dependency_path,
            content=render_subject(dependency_subject),
            conflict="dependency Subject bytes conflict with the candidate",
        )
    if authoring.claim_type_artifact is not None:
        type_path = claim_type_path(authoring.claim_type_artifact.predicate)
        _write_shared_member(
            candidate_tree,
            authored,
            path=type_path,
            content=render_claim_type(authoring.claim_type_artifact),
            conflict="Claim authorings in one proposal disagree on their ClaimType bytes",
        )
    for dependency_type in authoring.dependency_claim_types:
        dependency_path = claim_type_path(dependency_type.predicate)
        _write_dependency_member(
            candidate_tree,
            authored,
            path=dependency_path,
            content=render_claim_type(dependency_type),
            conflict="dependency ClaimType bytes conflict with the candidate",
        )

    statement = authoring.statement
    type_path = claim_type_path(statement.predicate)
    type_content = candidate_tree.get(type_path)
    if type_content is None:
        raise ProposalIntegrityError("direct ClaimType does not resolve in its candidate")
    claim_type = parse_claim_type(type_content, path=type_path)
    if (
        statement.claim_type != claim_type.identity
        or statement.claim_type_digest != claim_type_digest(claim_type).tagged
    ):
        raise ProposalIntegrityError("direct Claim statement does not pin its exact ClaimType")

    descriptor = statement.predicate in {
        "semantic.alias",
        "semantic.distinct_from",
        "semantic.related_to",
        "semantic.tag",
    }
    subject_identity, subject_artifact_digest = _direct_referent(
        candidate_tree,
        statement.subject,
        descriptor=descriptor,
    )

    object_referent: tuple[ArtifactIdentity, str] | None = None
    if isinstance(statement.object, SubjectClaimObject):
        object_referent = _direct_referent(
            candidate_tree,
            statement.object.address,
            descriptor=descriptor,
        )

    observed_at = _observed_at(timestamp)
    context = ClaimReferentContext(
        subject_content_digest=subject_artifact_digest,
        object_content_digest=(None if object_referent is None else object_referent[1]),
        observed_at=observed_at,
    )
    if claim_type.referent_sensitivity == "shell" and statement.shell_context_digest is None:
        statement = statement.model_copy(
            update={"shell_context_digest": claim_referent_context_digest(context).tagged}
        )

    existing = _existing_statements(base_tree, statement)
    expected_handoffs = {item.statement_digest for item in existing}
    supplied_handoffs = {item.statement_digest for item in authoring.existing_statement_handoffs}
    if expected_handoffs != supplied_handoffs:
        raise ProposalIntegrityError(
            "authoring must explicitly disposition every existing same-subject/predicate "
            "statement before proposing an adjacent Claim"
        )

    claim_id = authoring.claim_id or new_claim_id()
    path = claim_path(claim_id)
    predecessor: ClaimArtifactAny | None = None
    predecessor_content = base_tree.get(path)
    if predecessor_content is not None:
        predecessor = parse_claim(predecessor_content, path=path)
        actual_predecessor_digest = claim_artifact_digest(predecessor).tagged
        if authoring.predecessor_artifact_digest != actual_predecessor_digest:
            raise ProposalIntegrityError(
                "Claim succession requires the exact accepted predecessor artifact digest"
            )
    elif authoring.predecessor_artifact_digest is not None:
        raise ProposalIntegrityError("new Claim cannot name a predecessor artifact digest")
    if authoring.retire and predecessor is None:
        raise ProposalIntegrityError("only an accepted Claim lineage can be retired")
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id=actor_id,
        claim_id=claim_id,
        value=statement.object.model_dump(mode="json"),
        rationale=authoring.rationale,
        observed_at=observed_at,
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(proposed_base),
        materialize_source=authoring.materialize_source,
    )
    # A foreign-source selection is the one authoring input that binds a Capture
    # to a *logical* source rather than to content, so it is built under its own
    # per-source contract instead of the shared direct one.
    selection = authoring.source_selection
    foreign_capture: DirectCaptureBuildResult | None = None
    selection_capture: DirectCaptureBuildResult | None = None
    if isinstance(selection, DirectForeignSourceSelectionV1):
        foreign_capture = build_foreign_source_capture(
            store=instance.body_store(),
            actor_id=actor_id,
            claim_id=claim_id,
            rationale=authoring.rationale,
            observed_at=observed_at,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(proposed_base),
            selection=selection,
        )
    elif selection is not None:
        selection_capture = build_direct_claim_selection_capture(
            store=instance.body_store(),
            actor_id=actor_id,
            claim_id=claim_id,
            rationale=authoring.rationale,
            observed_at=observed_at,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(proposed_base),
            selection=selection,
        )
    contract_path = capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name)
    _write_dependency_member(
        candidate_tree,
        authored,
        path=contract_path,
        content=render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT),
        conflict="accepted direct CaptureContract seed bytes differ",
    )
    if foreign_capture is not None:
        _write_dependency_member(
            candidate_tree,
            authored,
            path=capture_contract_path(foreign_capture.contract.identity.name),
            content=render_capture_contract(foreign_capture.contract),
            conflict="accepted foreign-source CaptureContract seed bytes differ",
        )

    mapping_members: list[SourceMapping] = []
    if authoring.materialize_source:
        length = capture.envelope.commitment.byte_length
        if length is None:
            raise ProposalIntegrityError("materialized direct Capture has no byte length")
        mapping_members.append(
            SourceMapping(
                subject=claim_statement_address(path),
                spans=(
                    ContentSpan(
                        content_digest=capture.source_body_digest,
                        start_byte=0,
                        end_byte=length,
                    ),
                ),
            )
        )
    if isinstance(selection, DirectByteSpanSelectionV1):
        mapping_members.append(
            SourceMapping(
                subject=claim_statement_address(path),
                spans=(selection.span,),
            )
        )
    if foreign_capture is not None:
        # The mapping covers the committed *selection*, not the presented
        # snapshot: the selected bytes are what the Capture commits to and what
        # a working occurrence is later matched against.
        selected_length = foreign_capture.envelope.commitment.byte_length
        if selected_length is None:
            raise ProposalIntegrityError("foreign-source Capture has no committed byte length")
        mapping_members.append(
            SourceMapping(
                subject=claim_statement_address(path),
                spans=(
                    ContentSpan(
                        content_digest=foreign_capture.source_body_digest,
                        start_byte=0,
                        end_byte=selected_length,
                    ),
                ),
            )
        )
    mapping_by_wire = {
        canonical_bytes(item.model_dump(mode="json")): item
        for item in [
            *(() if predecessor is None else predecessor.backing.source_mappings),
            *mapping_members,
        ]
    }
    mappings = tuple(mapping_by_wire[key] for key in sorted(mapping_by_wire))
    pins = [
        ArtifactPin(
            role="capture-contract",
            target=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
            artifact_digest=capture.contract_digest,
        ),
        ArtifactPin(
            role="claim-type",
            target=claim_type.identity,
            artifact_digest=claim_type_digest(claim_type).tagged,
        ),
        ArtifactPin(
            role="subject",
            target=subject_identity,
            artifact_digest=subject_artifact_digest,
        ),
    ]
    if foreign_capture is not None:
        pins.append(
            ArtifactPin(
                role="capture-contract",
                target=foreign_capture.contract.identity,
                artifact_digest=foreign_capture.contract_digest,
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
    claim = ClaimArtifact(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=statement,
        backing=ClaimBacking(
            referent_context=context,
            capture_digests=tuple(
                sorted(
                    {
                        *(() if predecessor is None else predecessor.backing.capture_digests),
                        capture.capture_digest,
                        *(() if selection_capture is None else (selection_capture.capture_digest,)),
                        *(() if foreign_capture is None else (foreign_capture.capture_digest,)),
                    },
                    key=lambda item: item.encode("ascii"),
                )
            ),
            attestation_digests=(
                () if predecessor is None else predecessor.backing.attestation_digests
            ),
            input_claim_digests=(
                () if predecessor is None else predecessor.backing.input_claim_digests
            ),
            reducer_digest=(None if predecessor is None else predecessor.backing.reducer_digest),
            source_mappings=mappings,
        ),
        authority=claim_type.authority,
        pins=tuple(
            sorted(
                pins,
                key=lambda item: (
                    item.role.encode("utf-8"),
                    item.target.qualified.encode("utf-8"),
                ),
            )
        ),
        lifecycle=ArtifactLifecycle(
            state="retired" if authoring.retire else "live",
            predecessor_digest=(
                None if predecessor is None else claim_artifact_digest(predecessor).tagged
            ),
        ),
    )
    _write_shared_member(
        candidate_tree,
        authored,
        path=path,
        content=render_claim(claim),
        conflict="two Claim authorings in one proposal write the same Claim path",
    )
    return AuthoredClaimV1(
        claim_identity=claim.identity.qualified,
        claim_path=path,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
        capture_digest=capture.capture_digest,
        capture_digests=claim.backing.capture_digests,
        observed_at=observed_at,
        existing_statements=existing,
        handoffs=authoring.existing_statement_handoffs,
    )


def service_propose_playbill_claims(
    instance: PlaybillInstance,
    *,
    authorings: tuple[DirectClaimAuthoringV1, ...],
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
) -> DirectClaimBatchProposalV1:
    """Author every supplied Claim into one candidate tree and one proposal.

    The change set this produces is ordinary: nothing about settlement,
    evaluation, or the wire format distinguishes a proposal carrying five Claims
    from one carrying a single Claim. What the plural entrypoint adds is the
    atomicity guarantee callers need when a Claim is only meaningful beside its
    siblings -- the whole set is admitted as one generation or none of it is.
    """

    if not authorings:
        raise ProposalIntegrityError("a Claim proposal must author at least one Claim")

    proposed_base = _resolve_coordinate(instance, base)
    base_tree = instance.tree_at(proposed_base.git_oid)
    candidate_tree = instance.tree_at(proposed_base.git_oid)
    authored: dict[str, bytes] = {}
    claims = tuple(
        _author_direct_claim(
            instance,
            authoring=authoring,
            actor_id=actor_id,
            timestamp=timestamp,
            proposed_base=proposed_base,
            base_tree=base_tree,
            candidate_tree=candidate_tree,
            authored=authored,
        )
        for authoring in authorings
    )
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{proposal_name}",
            proposed_base_oid=proposed_base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return DirectClaimBatchProposalV1(
        proposal=PlaybillProposalInspection(
            proposal=result,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        ),
        claims=claims,
    )


def service_propose_playbill_claim(
    instance: PlaybillInstance,
    *,
    authoring: DirectClaimAuthoringV1,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
) -> DirectClaimProposalV1:
    """Create one inert Capture and dependency-closed Claim proposal in one operation."""

    batch = service_propose_playbill_claims(
        instance,
        authorings=(authoring,),
        actor_id=actor_id,
        proposal_name=proposal_name,
        timestamp=timestamp,
        base=base,
    )
    authored = batch.claims[0]
    return DirectClaimProposalV1(
        proposal=batch.proposal,
        claim_identity=authored.claim_identity,
        claim_path=authored.claim_path,
        statement_digest=authored.statement_digest,
        artifact_digest=authored.artifact_digest,
        capture_digest=authored.capture_digest,
        capture_digests=authored.capture_digests,
        observed_at=authored.observed_at,
        existing_statements=authored.existing_statements,
        handoffs=authored.handoffs,
    )


def service_get_playbill_claim(
    instance: PlaybillInstance,
    *,
    identity: str,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillClaimView:
    expected = "Claim:CLM-<32 lowercase hex> or CLM-<32 lowercase hex>"
    bare = identity.removeprefix("Claim:")
    try:
        path = claim_path(bare)
    except ValueError as exc:
        raise ClaimNotFoundError(
            f"Claim not found; expected {expected}; received {identity!r}"
        ) from exc
    qualified = f"Claim:{bare}"
    coordinate = _resolve_coordinate(instance, at)
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        raise ClaimNotFoundError(f"Claim not found; expected {expected}; received {identity!r}")
    with instance.bind_accepted_projection(coordinate) as projection:
        claim = projection.claim(qualified)
    if claim is None:
        raise ClaimNotFoundError(f"Claim not found; expected {expected}; received {identity!r}")
    public = _public_claim(claim)
    if public.envelope.get("path") != path:
        raise ProposalIntegrityError("Claim projection path differs from normalized identity")
    return public


def service_list_playbill_claims(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
    subject: SemanticAddress | None = None,
    predicate: str | None = None,
    include_retired: bool = False,
) -> PlaybillClaimList:
    coordinate = _resolve_coordinate(instance, at)
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        claims: tuple[PlaybillClaimView, ...] = ()
    else:
        with instance.bind_accepted_projection(coordinate) as projection:
            projected = tuple(_public_claim(item) for item in projection.list_claims())
        claims = tuple(
            item
            for item in projected
            if (
                (include_retired or _claim_from_view(item).lifecycle.state == "live")
                and (subject is None or _claim_from_view(item).statement.subject == subject)
                and (predicate is None or _claim_from_view(item).statement.predicate == predicate)
            )
        )
    return PlaybillClaimList(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        claims=claims,
    )


def _claim_law_evidence(
    instance: PlaybillInstance,
    *,
    path: str,
    at: AcceptedProjectionCoordinate,
) -> ClaimLawEvidenceV1:
    found: ClaimLawEvidenceV1 | None = None
    target_sequence = next(
        item.sequence for item in instance.accepted_history() if item.oid == at.git_oid
    )
    for generation in instance.accepted_history()[1:]:
        if generation.sequence > target_sequence:
            break
        record = generation.record
        # The v1 receipt predates structured member law evidence; every later
        # receipt version carries it in the same shape.
        if record is None or isinstance(record, ChangeSetRecord):
            continue
        for evidence in record.law_evidence:
            if evidence.path != path:
                continue
            raw = evidence.result.get("claim_evidence")
            if raw is not None:
                found = ClaimLawEvidenceV1.model_validate(raw)
    if found is None:
        raise ProposalIntegrityError("accepted Claim has no reproducible Claim law evidence")
    return found


def service_query_playbill_claims(
    instance: PlaybillInstance,
    *,
    subject: SemanticAddress,
    predicate: str,
    at: PlaybillAcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
) -> PlaybillClaimQueryResult:
    from cruxible_core.service.playbill_evidence import (
        service_evaluate_playbill_claim_verdict,
    )

    coordinate = _resolve_coordinate(instance, at)
    evaluated_at = evaluation_time or datetime.now(UTC)
    listed = service_list_playbill_claims(
        instance,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        subject=subject,
        predicate=predicate,
    )
    tree = instance.tree_at(coordinate.git_oid)
    type_path = claim_type_path(predicate)
    content = tree.get(type_path)
    if content is None:
        raise ClaimNotFoundError(f"ClaimType:{predicate}")
    claim_type = parse_claim_type(content, path=type_path)
    contenders: list[ResolutionContenderV1] = []
    verdicts: list[ClaimVerdictResultV1] = []
    for view in listed.claims:
        claim = _claim_from_view(view)
        evaluated = service_evaluate_playbill_claim_verdict(
            instance,
            claim_identity=claim.identity.qualified,
            evaluation_time=evaluated_at,
            at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        )
        verdicts.append(evaluated.verdict)
        value: object
        if claim.statement.object.kind == "literal":
            value = claim.statement.object.value
        else:
            value = claim.statement.object.model_dump(mode="json")
        contender_verdict: ClaimVerdict = evaluated.verdict.verdict
        contenders.append(
            ResolutionContenderV1(
                claim_identity=claim.identity.name,
                object_value=value,
                verdict=contender_verdict,
                basis_kinds=evaluated.verdict.basis_kinds,
            )
        )
    resolution = resolve_claim_contenders(
        claim_type.resolution_policy,
        tuple(contenders),
    )
    selected = tuple(f"Claim:{item}" for item in resolution.selected_claim_identities)
    all_contenders = tuple(f"Claim:{item}" for item in resolution.contender_claim_identities)
    return PlaybillClaimQueryResult(
        coordinate=listed.coordinate,
        evaluation_time=evaluated_at,
        subject=subject,
        predicate=predicate,
        cardinality=claim_type.cardinality,
        status=resolution.status,
        selected_claim_identities=selected,
        contender_claim_identities=all_contenders,
        claims=listed.claims,
        verdicts=tuple(verdicts),
    )


def service_playbill_claim_history(
    instance: PlaybillInstance,
    *,
    identity: str,
) -> PlaybillClaimHistory:
    parsed_identity = ArtifactIdentity(
        kind="Claim",
        name=identity.removeprefix("Claim:"),
    )
    path = claim_path(parsed_identity.name)
    entries: list[PlaybillClaimHistoryEntry] = []
    for generation in instance.accepted_history()[1:]:
        record = generation.record
        if record is None or not any(member.path == path for member in record.members):
            continue
        content = instance.tree_at(generation.oid).get(path)
        if content is None:
            continue
        claim = parse_claim(content, path=path)
        entries.append(
            PlaybillClaimHistoryEntry(
                sequence=generation.sequence,
                coordinate=PlaybillAcceptedCoordinate.from_internal(
                    instance.coordinate_for_oid(generation.oid)
                ),
                statement_digest=claim_statement_digest(claim.statement).tagged,
                artifact_digest=claim_artifact_digest(claim).tagged,
                predecessor_digest=claim.lifecycle.predecessor_digest,
                lifecycle_state=claim.lifecycle.state,
                change_set_path=f"changesets/cs-{record.sequence:020d}.json",
                changeset_digest=record.changeset_digest,
                candidate_digest=record.candidate_digest,
            )
        )
    if not entries:
        raise ClaimNotFoundError(identity)
    return PlaybillClaimHistory(identity=parsed_identity.qualified, entries=tuple(entries))


def service_explain_playbill_claim(
    instance: PlaybillInstance,
    *,
    identity: str,
    at: PlaybillAcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
) -> PlaybillClaimExplanationV1:
    from cruxible_core.service.playbill_evidence import (
        service_evaluate_playbill_claim_verdict,
    )

    coordinate = _resolve_coordinate(instance, at)
    evaluated_at = evaluation_time or datetime.now(UTC)
    view = service_get_playbill_claim(
        instance,
        identity=identity,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
    )
    claim = _claim_from_view(view)
    law = _claim_law_evidence(
        instance,
        path=claim_path(claim.identity.name),
        at=coordinate,
    )
    verdict = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=claim.identity.qualified,
        evaluation_time=evaluated_at,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
    )
    handles: list[SourceHandleV1] = []
    for digest in claim.backing.capture_digests:
        envelope = parse_capture_envelope(
            instance.body_store().read(
                digest,
                access=BodyAccessContext(principal_id="playbill-service", can_read_body=True),
            )
        )
        spans = tuple(
            span
            for mapping in claim.backing.source_mappings
            for span in mapping.spans
            if span.content_digest == envelope.commitment.digest
        )
        handles.append(
            SourceHandleV1(
                subject=claim_statement_address(claim_path(claim.identity.name)),
                at=PlaybillAcceptedCoordinate.from_internal(coordinate),
                source=envelope.source,
                commitment=envelope.commitment,
                media_type=(
                    "application/json" if envelope.commitment.digest_kind != "exact_bytes" else None
                ),
                exact_spans=spans,
                access_class="instance",
            )
        )
    return PlaybillClaimExplanationV1(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluated_at,
        claim=view,
        law_evidence=law,
        verdict=verdict.verdict,
        exact_attestations=law.verified_attestations,
        source_handles=tuple(handles),
        coverage=CoverageDescriptorV1(
            requested_facets=("governance", "provenance", "sources"),
            available_facets=("governance", "provenance", "sources"),
        ),
    )


_EXPAND_FACETS = frozenset(
    {
        "attestation_coverage",
        "claim_context",
        "claim_type_card",
        "governance",
        "profile",
        "provenance",
        "relations",
        "sources",
        "summary",
    }
)


def _expand_subject_relations(
    tree: dict[str, bytes],
    address: SemanticAddress,
) -> tuple[dict[str, object], ...]:
    relations: list[dict[str, object]] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.lifecycle.state != "live" or claim.statement.predicate not in {
            "semantic.alias",
            "semantic.distinct_from",
            "semantic.related_to",
            "semantic.tag",
        }:
            continue
        object_address = (
            claim.statement.object.address
            if isinstance(claim.statement.object, SubjectClaimObject)
            else None
        )
        if claim.statement.subject != address and object_address != address:
            continue
        relations.append(
            {
                "claim": claim_statement_address(path).model_dump(mode="json"),
                "predicate": claim.statement.predicate,
                "subject": claim.statement.subject.model_dump(mode="json"),
                "object": claim.statement.object.model_dump(mode="json"),
                "statement_digest": claim_statement_digest(claim.statement).tagged,
            }
        )
    return tuple(relations)


def _interface_vocabulary(
    relations: tuple[dict[str, object], ...],
    address: SemanticAddress,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[SemanticRelationV1, ...]]:
    """Split the accepted descriptor Claims of one address into card vocabulary.

    An alias or a tag belongs to the address the Claim states it about, while a
    typed relation is indexed from both ends and marked inbound on the end that
    did not author it.
    """

    owner = address.model_dump(mode="json")
    aliases: set[str] = set()
    tags: set[str] = set()
    edges: dict[bytes, SemanticRelationV1] = {}
    for row in relations:
        predicate = row["predicate"]
        subject_value = row["subject"]
        object_value = row["object"]
        if not isinstance(object_value, dict):
            continue
        if predicate in {"semantic.alias", "semantic.tag"} and subject_value == owner:
            value = object_value.get("value")
            if object_value.get("kind") == "literal" and isinstance(value, str):
                (aliases if predicate == "semantic.alias" else tags).add(value)
        elif predicate in {"semantic.distinct_from", "semantic.related_to"}:
            if object_value.get("kind") != "subject":
                continue
            inbound = subject_value != owner
            target = subject_value if inbound else object_value["address"]
            edge = SemanticRelationV1(
                predicate=predicate,  # type: ignore[arg-type]
                target=SemanticAddress.model_validate(target),
                inbound=inbound,
            )
            edges[canonical_bytes(edge.model_dump(mode="json"))] = edge
    return (
        byte_sorted(tuple(aliases)),
        byte_sorted(tuple(tags)),
        tuple(edges[key] for key in sorted(edges)),
    )


def _subject_identity(tree: dict[str, bytes], path: str) -> str | None:
    """Return one accepted Subject's identity, or None when it is absent.

    An accepted Claim pins the Subject it is about, so the absent case is
    defensive rather than reachable.
    """

    content = tree.get(path)
    return None if content is None else parse_subject(content, path=path).identity.qualified


def _claim_source_handles(
    instance: PlaybillInstance,
    *,
    claim: ClaimArtifactAny,
    coordinate: AcceptedProjectionCoordinate,
) -> tuple[SourceHandleV1, ...]:
    explanation = service_explain_playbill_claim(
        instance,
        identity=claim.identity.qualified,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
    )
    return explanation.source_handles


def service_expand_playbill_semantic(
    instance: PlaybillInstance,
    *,
    request: ExpandRequestV1,
) -> ContextCapsuleV1:
    """Return a bounded, coordinate-bound semantic context capsule without mutation."""

    if not isinstance(request.at, PlaybillAcceptedCoordinate):
        raise ProposalIntegrityError("PC-B expand accepts only verified accepted coordinates")
    coordinate = _resolve_coordinate(instance, request.at)
    try:
        _observed_at(request.evaluation_time)
    except ProposalIntegrityError as exc:
        raise ProposalIntegrityError("expand evaluation_time must be timezone-aware") from exc
    if request.facets != tuple(sorted(set(request.facets), key=lambda item: item.encode("utf-8"))):
        raise ProposalIntegrityError("expand facets must be sorted and unique")
    unknown = set(request.facets).difference(_EXPAND_FACETS)
    if unknown:
        raise ProposalIntegrityError(f"unknown expand facets: {sorted(unknown)!r}")

    tree = instance.tree_at(coordinate.git_oid)
    path = request.address.artifact_path
    content = tree.get(path)
    if content is None:
        raise ClaimNotFoundError(path)

    all_relations = _expand_subject_relations(tree, request.address)
    aliases, tags, relation_edges = _interface_vocabulary(all_relations, request.address)
    at = PlaybillAcceptedCoordinate.from_internal(coordinate)

    canonical_summary: object
    governance: object
    provenance: object
    claim_context: object | None = None
    claim_type_card: object | None = None
    subject_profile: object | None = None
    source_handles: tuple[SourceHandleV1, ...] = ()
    source_budget_truncated = False
    kind: str

    if path.startswith("claims/"):
        from cruxible_core.service.playbill_evidence import (
            service_evaluate_playbill_claim_verdict,
        )

        if request.address.selector.scheme not in {"artifact-v1", "claim-statement-v1"}:
            raise ProposalIntegrityError("Claim expansion requires artifact or statement identity")
        claim = parse_claim(content, path=path)
        law = _claim_law_evidence(instance, path=path, at=coordinate)
        kind = "claim"
        canonical_summary = {
            "artifact_digest": claim_artifact_digest(claim).tagged,
            "identity": claim.identity.qualified,
            "statement": claim.statement.model_dump(mode="json"),
            "statement_digest": claim_statement_digest(claim.statement).tagged,
        }
        governance = {
            "approval_coverage": "containing_change_set",
            "authority": claim.authority.model_dump(mode="json"),
            "lifecycle": claim.lifecycle.model_dump(mode="json"),
        }
        provenance = {
            "backing": claim.backing.model_dump(mode="json"),
            "pins": [item.model_dump(mode="json") for item in claim.pins],
        }
        all_source_handles = _claim_source_handles(
            instance,
            claim=claim,
            coordinate=coordinate,
        )
        source_handles = all_source_handles[: request.budget.max_source_handles]
        source_budget_truncated = len(all_source_handles) > len(source_handles)
        competitors = service_list_playbill_claims(
            instance,
            at=PlaybillAcceptedCoordinate.from_internal(coordinate),
            subject=claim.statement.subject,
            predicate=claim.statement.predicate,
        ).claims
        claim_context = {
            "competing_claim_identities": [
                item.envelope["identity"]
                for item in competitors
                if item.envelope["identity"] != claim.identity.qualified
            ],
            "law_evidence": law.model_dump(mode="json"),
            "verdict": service_evaluate_playbill_claim_verdict(
                instance,
                claim_identity=claim.identity.qualified,
                evaluation_time=_observed_at(request.evaluation_time),
                at=PlaybillAcceptedCoordinate.from_internal(coordinate),
            ).verdict.model_dump(mode="json"),
            "source_handles": [item.model_dump(mode="json") for item in source_handles],
        }
    elif path.startswith("subjects/"):
        if request.address.selector.scheme != "artifact-v1":
            raise ProposalIntegrityError("Subject expansion requires whole-artifact identity")
        subject = parse_subject(content, path=path)
        kind = "subject"
        canonical_summary = {
            "artifact_digest": subject_digest(subject).tagged,
            "identity": subject.identity.qualified,
            "subject_id": subject.subject_id,
            "subject_kind": subject.subject_kind,
        }
        governance = {
            "authority": subject.authority.model_dump(mode="json"),
            "lifecycle": subject.lifecycle.model_dump(mode="json"),
        }
        provenance = {"pins": [item.model_dump(mode="json") for item in subject.pins]}
        claims = service_list_playbill_claims(
            instance,
            at=at,
            subject=request.address,
        ).claims
        artifacts = tuple(_claim_from_view(view) for view in claims)
        cardinalities: dict[str, str] = {}
        for artifact in artifacts:
            contract_path = claim_type_path(artifact.statement.predicate)
            contract_content = tree.get(contract_path)
            if contract_content is not None:
                cardinalities[artifact.statement.predicate] = parse_claim_type(
                    contract_content,
                    path=contract_path,
                ).cardinality
        # The profile is taken without an evaluation time: it is coordinate-pure
        # accepted structure, and the verdict-bearing read stays claim_context.
        subject_profile = build_subject_profile(
            at=at,
            entry=DiscoveryEntryV1(
                kind="Subject",
                address=request.address,
                identity=subject.identity.qualified,
                label=subject.identity.qualified,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted((subject.subject_id, subject.subject_kind)),
                structural_signature_digest=subject_reuse_signature(subject.identity),
            ),
            subject_kind=subject.subject_kind,
            subject_id=subject.subject_id,
            artifact_digest=subject_digest(subject).tagged,
            claims=artifacts,
            cardinalities=cardinalities,
            relations=relation_edges,
        ).model_dump(mode="json")
    elif path.startswith("claim-types/"):
        if request.address.selector.scheme != "artifact-v1":
            raise ProposalIntegrityError("ClaimType expansion requires whole-artifact identity")
        claim_type = parse_claim_type(content, path=path)
        kind = "claim_type"
        canonical_summary = {
            "artifact_digest": claim_type_digest(claim_type).tagged,
            "identity": claim_type.identity.qualified,
            "predicate": claim_type.predicate,
            "structure": claim_type.structure.model_dump(mode="json"),
        }
        governance = {
            "authority": claim_type.authority.model_dump(mode="json"),
            "lifecycle": claim_type.lifecycle.model_dump(mode="json"),
        }
        provenance = {"pins": [item.model_dump(mode="json") for item in claim_type.pins]}
        usage_rows: list[ClaimTypeUsageRowV1] = []
        for view in service_list_playbill_claims(
            instance,
            at=at,
            predicate=claim_type.predicate,
        ).claims:
            statement_subject = _claim_from_view(view).statement.subject.artifact_path
            subject_identity = _subject_identity(tree, statement_subject)
            if subject_identity is None:
                continue
            usage_rows.append(
                ClaimTypeUsageRowV1(
                    subject_path=statement_subject,
                    subject_identity=subject_identity,
                )
            )
        claim_type_card = build_claim_type_card(
            claim_type,
            at=at,
            entry=DiscoveryEntryV1(
                kind="ClaimType",
                address=request.address,
                identity=claim_type.identity.qualified,
                label=claim_type.predicate,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted(
                    (
                        claim_type.predicate,
                        claim_type.predicate.rpartition(".")[2],
                        *claim_type.allowed_subject_kinds,
                    )
                ),
                structural_signature_digest=claim_type_structural_signature(claim_type.structure),
            ),
            usage_rows=tuple(usage_rows),
            relations=relation_edges,
        ).model_dump(mode="json")
    elif path.startswith("query-definitions/"):
        # A named entrypoint advertises its contract before any row is read: the
        # compact interface is the canonical summary, not a separate capsule slot.
        if request.address.selector.scheme != "artifact-v1":
            raise ProposalIntegrityError(
                "QueryDefinition expansion requires whole-artifact identity"
            )
        definition = parse_query_definition(content, path=path)
        kind = "query_definition"
        canonical_summary = {
            "artifact_digest": query_definition_digest(definition).tagged,
            "default_budgets": definition.default_budgets.model_dump(mode="json"),
            "description": definition.description,
            "entrypoint_name": definition.identity.name,
            "evaluation_policy": definition.evaluation_policy.model_dump(mode="json"),
            "identity": definition.identity.qualified,
            "parameters": [item.model_dump(mode="json") for item in definition.parameters],
            "referenced_predicates": list(definition.referenced_predicates),
            "result_cardinality": definition.result_cardinality,
            "result_shape": definition.result_shape,
            "subject_kinds": list(definition.subject_kinds),
        }
        governance = {
            "authority": definition.authority.model_dump(mode="json"),
            "lifecycle": definition.lifecycle.model_dump(mode="json"),
        }
        provenance = {"pins": [item.model_dump(mode="json") for item in definition.pins]}
    else:
        # Procedure and LineSpec expansion needs their full pin closure rendered;
        # discovery already returns their handles, and PC-G lands the expansion.
        raise ProposalIntegrityError(
            "expand supports Claim, Subject, ClaimType, and QueryDefinition"
        )

    relations = all_relations[: request.budget.max_relations]
    relation_budget_truncated = len(all_relations) > len(relations)
    requested = set(request.facets)
    available = {
        "attestation_coverage",
        "governance",
        "provenance",
        "summary",
    }
    if claim_context is not None:
        available.add("claim_context")
    if claim_type_card is not None:
        available.add("claim_type_card")
    if subject_profile is not None:
        available.add("profile")
    if relations:
        available.add("relations")
    if source_handles:
        available.add("sources")

    # Facets are additive presentation. Keep the required summary/governance/provenance
    # slots structurally stable while replacing unrequested content with a typed omission.
    if request.facets:
        canonical_summary = canonical_summary if "summary" in requested else None
        governance = governance if "governance" in requested else None
        provenance = provenance if "provenance" in requested else None
        claim_context = claim_context if "claim_context" in requested else None
        claim_type_card = claim_type_card if "claim_type_card" in requested else None
        subject_profile = subject_profile if "profile" in requested else None
        relations = relations if "relations" in requested else ()

    truncated: set[str] = set()
    if source_budget_truncated and (not request.facets or "sources" in requested):
        truncated.add("sources")
    if relation_budget_truncated and (not request.facets or "relations" in requested):
        truncated.add("relations")

    def material_size() -> int:
        return len(
            canonical_bytes(
                {
                    "canonical_summary": canonical_summary,
                    "claim_context": claim_context,
                    "claim_type_card": claim_type_card,
                    "governance": governance,
                    "provenance": provenance,
                    "relations": list(relations),
                    "subject_profile": subject_profile,
                }
            )
        )

    if material_size() > request.budget.max_bytes and relations:
        relations = ()
        truncated.add("relations")
    if material_size() > request.budget.max_bytes and isinstance(claim_context, dict):
        claim_context = {
            "law_evidence": claim_context["law_evidence"],
            "source_handle_digests": [source_handle_digest(item) for item in source_handles],
        }
        truncated.add("sources")
    if material_size() > request.budget.max_bytes:
        claim_context = None
        claim_type_card = None
        subject_profile = None
        truncated.update({"claim_context", "claim_type_card", "profile"}.intersection(requested))
    if material_size() > request.budget.max_bytes:
        provenance = None
        governance = None
        truncated.update({"governance", "provenance"}.intersection(requested))
    if material_size() > request.budget.max_bytes:
        canonical_summary = None
        truncated.add("summary")
    if material_size() > request.budget.max_bytes:
        raise ProposalIntegrityError("expand byte budget is smaller than the minimum capsule")

    available_facets = tuple(
        sorted((available.intersection(requested or available)).difference(truncated))
    )
    reason_codes: tuple[str, ...] = (
        ("open_source_required",) if "sources" in requested and source_handles else ()
    )
    coverage = CoverageDescriptorV1(
        requested_facets=request.facets,
        available_facets=available_facets,
        truncated_facets=tuple(sorted(truncated, key=lambda item: item.encode("utf-8"))),
        reason_codes=reason_codes,
    )
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-context-capsule-receipt-v1",
        {
            "address": request.address.model_dump(mode="json"),
            "at": request.at.model_dump(mode="json"),
            "budget": request.budget.model_dump(mode="json"),
            "coverage": coverage.model_dump(mode="json"),
            "evaluation_time": request.evaluation_time,
            "kind": kind,
        },
    ).tagged
    next_reads = (
        (GovernedOperationReference(operation="open_source", subject=request.address),)
        if source_handles
        else ()
    )
    return ContextCapsuleV1(
        address=request.address,
        at=request.at,
        evaluation_time=request.evaluation_time,
        canonical_summary=canonical_summary,
        governance=governance,
        provenance=provenance,
        attestation_coverage="containing_change_set",
        claim_context=claim_context,
        claim_type_card=claim_type_card,
        subject_profile=subject_profile,
        relations=relations,
        next_reads=next_reads,
        coverage=coverage,
        receipt_digest=receipt_digest,
    )


class _InstanceSourceMaterialResolver:
    """Bind the shared dereference engine to one accepted coordinate's retained bytes."""

    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        coordinate: AcceptedProjectionCoordinate,
        external_reader: ExternalSelectionReaderProtocol | None,
    ) -> None:
        self._instance = instance
        self._coordinate = coordinate
        self._external_reader = external_reader

    def read_ledger(self, artifact_path: str) -> bytes | None:
        return self._instance.tree_at(self._coordinate.git_oid).get(artifact_path)

    def read_cas(self, content_digest: str, *, access: BodyAccessContext) -> bytes | None:
        if not self._instance.body_store().verify(content_digest):
            return None
        return self._instance.body_store().read(content_digest, access=access)

    def read_external(self, source: ExternalSourceReferenceV1) -> object | None:
        if self._external_reader is None:
            return None
        return self._external_reader.read_external_selection(source)


def service_open_playbill_source(
    instance: PlaybillInstance,
    *,
    request: OpenSourceRequestV1,
    access: BodyAccessContext,
    at: PlaybillAcceptedCoordinate | None = None,
    external_reader: ExternalSelectionReaderProtocol | None = None,
) -> SourceDereferenceResultV1:
    """Dereference only the coordinate-bound handle; never mutate or refresh a source.

    Ledger, CAS, and external selections all resolve through one engine, so the
    coverage, budget, and access laws cannot drift apart by source kind.
    """

    coordinate = _resolve_coordinate(instance, at)
    return dereference_source_handle(
        request,
        access=access,
        resolver=_InstanceSourceMaterialResolver(
            instance,
            coordinate=coordinate,
            external_reader=external_reader,
        ),
    )


__all__ = [
    "AuthoredClaimV1",
    "DirectClaimAuthoringV1",
    "DirectClaimBatchProposalV1",
    "DirectClaimProposalV1",
    "ExistingClaimStatementHandleV1",
    "ExistingStatementHandoffV1",
    "PlaybillClaimExplanationV1",
    "PlaybillClaimHistory",
    "PlaybillClaimHistoryEntry",
    "PlaybillClaimList",
    "PlaybillClaimQueryResult",
    "PlaybillClaimView",
    "service_expand_playbill_semantic",
    "service_explain_playbill_claim",
    "service_get_playbill_claim",
    "service_list_playbill_claims",
    "service_open_playbill_source",
    "service_playbill_claim_history",
    "service_propose_playbill_claim",
    "service_propose_playbill_claims",
    "service_query_playbill_claims",
]
