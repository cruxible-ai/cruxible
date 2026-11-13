"""The production bridge from a `claims` terminal to a durable proposal.

A terminal item is a typed Claim or Subject proposal, never a candidate tree (a
Subject item names its kind and key; core assigns the id). This module
turns the items one terminal reached into one change set, hands that set to the
SAME lowering every authoring surface uses, evaluates the exact live
ProcedureMandate against the paths lowering actually changed, and calls the
sole proposal door exactly once per admitted operation.

Three properties are load-bearing:

* **Evidence is the run's own.** Each item cites the one root Capture of its
  own dependency closure -- every Capture it produced or was admitted (a
  Line's trigger input), together -- as `ExistingCaptureCitationSource`.
  Nothing an
  author writes into a template can name, borrow, or omit evidence, and the
  Claim's grade is whatever its ClaimType's evidence admission policy says
  about that Capture.
* **Lowering is a pure function of the admitted operation.** Claim IDs are
  minted from the admission binding and the item key, the intent is built in
  memory at the admitted base, and the changed member bytes are digested. A
  retry reproduces the same tree; recovery re-derives it from the journal.
* **One operation, one proposal.** The retained admission binds the operation
  key to the exact authored payload. Recovery returns the same receipt or
  refuses a changed payload even after the candidate Git objects are collected.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar, cast

from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.authoring.models import (
    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
    AuthoringChangeSetMember,
    AuthoringClaimStatement,
    AuthoringExistingClaimDisposition,
    AuthoringIntent,
    CandidateStatus,
    ChangeSetAuthoringPayload,
    ChangeSetClaimIdentity,
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    ClaimRetirementMember,
    ExistingCaptureCitationSource,
    SelfSourceBody,
    SubjectCreationAuthoringPayload,
    authoring_change_set_membership,
    authoring_create_fingerprint,
    authoring_member_identity,
    authoring_payload_digest,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import (
    CaptureEnvelopeAny,
    classify_capture_reuse,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import (
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.content_memo import seal
from cruxible_client.contracts.mandate_scope import SettleScopeGap
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandate,
    ProcedureMandateAny,
    ProcedureMandateInvocation,
    evaluate_procedure_mandate,
    mandate_rung,
)
from cruxible_client.contracts.procedures.node_summary import ReobservationRefusalReason
from cruxible_client.contracts.proposal_models import (
    SETTLE_OUTSIDE_MANDATE_SCOPE,
    ProposalItemReason,
    ProposalResult,
    ProposalSettleSubmission,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import (
    SubjectKeyPart,
    SubjectKeyReference,
    key_reference_mapping,
    live_subject_kind,
)
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.authoring import lowering as authoring_lowering
from cruxible_core.authoring.assignment_records import (
    PROCEDURE_OPERATION_ID_DOMAIN,
    procedure_operation_id,
)
from cruxible_core.authoring.lowering import (
    AuthoringLoweringError,
    LoweredAuthoring,
    canonical_authored_claims,
    claim_aliases_resolved,
    members_identity_overlay,
    subject_creation_paths,
)
from cruxible_core.evidence.claim_reobservations import (
    ClaimReobservationRefused,
    ClaimReobservationStore,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.indexes.subject_identity import (
    IdentityView,
    KeyTuple,
    RecordedCreation,
    SubjectKeyInvalid,
    canonical_key,
    key_ambiguous_message,
    key_encoding,
    tree_key_reader,
)
from cruxible_core.procedures.claims_changes import (
    CoreClaimChange,
    CoreClaimsChange,
    CoreCreatedSubject,
    CoreReobservationChange,
    CoreRetirementChange,
    CoreSubjectChange,
)
from cruxible_core.procedures.egress import (
    LINE_CHANGE_SIGNATURE_DOMAIN,
    LineProposalPreparationV1,
    PreparedTerminalEgressV1,
    TerminalEgressChildReceiptV1,
    TerminalEgressChildReceiptV3,
    TerminalEgressChildReceiptV4,
    TerminalEgressReceiptV1,
    TerminalEgressReceiptV3,
    TerminalEgressRequestV1,
    TerminalEgressRequestV2,
    TerminalEgressStillCurrentReceiptV1,
    TerminalEgressUnchangedReceiptV1,
    UnchangedChildReceiptAny,
    settle_authority_capped_reason,
)
from cruxible_core.procedures.nested import ProcedureDelegation, authority_line
from cruxible_core.procedures.terminal_dependencies import (
    CaptureRootCache,
    TerminalItemDependencyManifestV1,
    capture_root,
    citable_captures,
    witnessed_list_capture,
)
from cruxible_core.procedures.terminal_services import (
    ProposalDeliveryRefused,
    ProposalTerminalAdapter,
    proposal_terminal_payload_digest,
    proposal_terminal_ref,
    settle_terminal_receipt,
)
from cruxible_core.proposals.delegated_authority import (
    DelegatedAuthorityVerdict,
    delegated_authority_issues,
    settle_scope_gaps,
)
from cruxible_core.proposals.prepared_evaluation import (
    FactsObservation,
    SettlementHandoff,
    observed_facts_build,
)
from cruxible_core.proposals.proposals import ProposalService
from cruxible_core.runtime.phase_timing import timed_operation

if TYPE_CHECKING:
    from cruxible_core.derived.derived_state import ClaimPredicateKey, SnapshotTree
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.procedures.execution import ProcedureRunAdmission
    from cruxible_core.query.backends import ClaimQueryFactsV1
    from cruxible_core.runtime.instance import PlaybillInstance

PROPOSAL_CLAIM_ID_DOMAIN = "playbill-procedure-proposal-claim-id-v1"
PROPOSAL_INTENT_ID_DOMAIN = PROCEDURE_OPERATION_ID_DOMAIN
PROPOSAL_LOWERING_DOMAIN = "playbill-procedure-proposal-lowering-v1"


def _validate_absence_bindings(
    instance: PlaybillInstance,
    request: TerminalEgressRequestV1,
    admission: ProcedureRunAdmission,
    evidence: Mapping[str, str],
) -> None:
    """Check only core request metadata against its admitted run and pinned extract."""
    selected = [
        (item, change, request.claims_evidence.get(item.item_key))
        for item, change in zip(request.items, proposal_items(request), strict=True)
        if isinstance(change, CoreRetirementChange)
        or (
            request.claims_evidence.get(item.item_key) is not None
            and request.claims_evidence[item.item_key].absence is not None
        )
    ]
    if not selected:
        return
    from cruxible_client.contracts.captures import capture_contract_path, parse_capture_contract
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_client.contracts.procedures.extract import ExtractNode
    from cruxible_client.contracts.procedures.models import ClaimsNode
    from cruxible_core.procedures.claims_terminal import _receipt_extract_digest
    from cruxible_core.procedures.extract import extract_specs

    content = instance.blob_at(admission.bound_coordinate.git_oid, admission.procedure_path)
    procedure = None if content is None else parse_procedure(content, path=admission.procedure_path)
    nodes = () if procedure is None else procedure.definition.nodes
    node = next(
        (n for n in nodes if isinstance(n, ClaimsNode) and n.node_id == request.node_id), None
    )

    def refuse() -> NoReturn:
        raise ProposalDeliveryRefused(
            "claims_absence_binding_invalid",
            "The absence witness does not bind this run, item and declared extract; "
            "run the Line again",
        )

    if node is None:
        refuse()
    from cruxible_core.procedures.claims_typing import ClaimsTypingRefused, absence_enumeration

    def kind_of(reference: Any) -> Any:
        # The kind as accepted at the run's bound coordinate: one path read.
        path = capture_contract_path(reference.target.name)
        found = instance.blob_at(admission.bound_coordinate.git_oid, path)
        return None if found is None else parse_capture_contract(found, path=path)

    extracting: ExtractNode | None
    try:
        extracting = extract_specs(nodes, kind_of)(
            absence_enumeration(node, {getattr(n, "as_", None): n for n in nodes})
        )
    except (ClaimsTypingRefused, ValueError):
        extracting = None
    for item, change, receipt in selected:
        witness = (
            change.witness
            if isinstance(change, CoreRetirementChange)
            else None
            if receipt is None
            else receipt.absence
        )
        if (
            receipt is None
            or witness is None
            or receipt.absence != witness
            or witness.run_id != request.run_id
            or witness.run_id != admission.run_id
            or witness.procedure_digest != admission.procedure_artifact_digest
            or witness.procedure_digest != request.procedure_artifact_digest
            or receipt.procedure_digest != witness.procedure_digest
            or witness.node_id != request.node_id
            or receipt.node_id != request.node_id
            or witness.capture != evidence.get(item.item_key)
            or witness.expect_coordinate != admission.accepted_coordinate
            or receipt.template >= len(node.claims)
        ):
            refuse()
        if (
            extracting is None
            or witness.extract_spec != _receipt_extract_digest(extracting)
            or (witness.complete_when is None) != (extracting.complete_when is None)
            or (
                witness.complete_when is not None
                and extracting.complete_when is not None
                and witness.complete_when.count_at != extracting.complete_when.count_at
            )
        ):
            refuse()
        mapping = node.claims[receipt.template]
        if mapping.when_absent not in {"mirror", "inverse"} or (
            # mirror only retires; only inverse writes or re-observes an inverse.
            mapping.when_absent == "mirror" and not isinstance(change, CoreRetirementChange)
        ):
            refuse()
        value = item.value
        if isinstance(change, CoreRetirementChange):
            if (
                not isinstance(value, dict)
                or value.get("retire") != change.claim_id
                or value.get("template") != receipt.template
                or change.claim_type != mapping.claim_type.target.name
            ):
                refuse()
        elif isinstance(change, CoreClaimChange):
            unit_wire = change.statement.object.model_dump(mode="json")
            if unit_wire.get("of") is None:
                unit_wire.pop("of", None)
            if (
                change.statement.object.kind != "none"
                or change.statement.predicate != mapping.claim_type.target.name
                or not isinstance(value, dict)
                or value.get("template") != receipt.template
                or value.get("unit") != unit_wire
            ):
                refuse()


def proposal_claim_id(*, admission_binding_digest: str, node_id: str, item_key: str) -> str:
    """Mint the Claim ID one terminal item lowers into, from the admitted operation alone."""

    digest = typed_digest(
        Sha256Value,
        PROPOSAL_CLAIM_ID_DOMAIN,
        {
            "admission_binding_digest": admission_binding_digest,
            "item_key": item_key,
            "node_id": node_id,
        },
    ).tagged.removeprefix("sha256:")
    return f"CLM-{digest[:32]}"


def proposal_lowering_digest(changed_members: tuple[tuple[str, bytes], ...]) -> str:
    """Commit to exactly the member bytes one lowering changed."""

    return typed_digest(
        Sha256Value,
        PROPOSAL_LOWERING_DOMAIN,
        {
            "changed_members": [
                {"path": path, "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
                for path, content in changed_members
            ]
        },
    ).tagged


def line_change_signature(instance: PlaybillInstance, lowered: LoweredAuthoring) -> str:
    """Commit to what a Line run's change says, not to the ids it minted.

    Two runs of one Line carry the same change when they make the same set of
    statements (subject, predicate, qualifier, object, and role) on the same
    evidence content (``claim_refusal_match_key``), retire the same Claims, and
    create the same Subjects. New-Claim ids, Capture digests and timestamps are
    run-minted and left out. Any other changed member counts by its bytes.
    Reads only this change's members (and the Captures its Claims cite).
    """

    from cruxible_core.proposals.refusal_index import claim_refusal_key

    created = {item.path for item in lowered.subject_creations if item.record.outcome == "created"}
    statements: set[tuple[str, str]] = set()
    retired: set[str] = set()
    other: set[tuple[str, str]] = set()
    for path, content in lowered.changed_members:
        if path in created:
            continue
        if path.startswith("claims/"):
            claim = parse_claim(content, path=path)
            if claim.lifecycle.state == "live":
                statements.add((claim_refusal_key(instance, claim), claim.statement.role))
            else:
                retired.add(path)
            continue
        other.add((path, "sha256:" + hashlib.sha256(content).hexdigest()))

    def ordered(values: set[str]) -> list[str]:
        return sorted(values, key=lambda item: item.encode("utf-8"))

    return typed_digest(
        Sha256Value,
        LINE_CHANGE_SIGNATURE_DOMAIN,
        {
            "statements": [{"match_key": key, "role": role} for key, role in sorted(statements)],
            "retired_claims": ordered(retired),
            "created_subjects": ordered(created),
            "other_members": [
                {"path": path, "digest": digest}
                for path, digest in sorted(other, key=lambda item: item[0].encode("utf-8"))
            ],
        },
    ).tagged


@dataclass(frozen=True)
class PreparedProposal:
    """Everything delivery needs, resolved once from the admitted operation."""

    prepared: PreparedTerminalEgressV1
    #: The lowered tree itself: a sealed fork of the accepted base, so receive
    #: validation and evaluation advance the base over the changed paths only.
    candidate_tree: Mapping[str, bytes]
    changed_members: tuple[tuple[str, bytes], ...]
    item_paths: dict[str, str]
    rationale: str
    #: Why the one applicable settle grant's scope leaves this change out: any
    #: gap proposes the whole change under that grant instead (all or nothing).
    outside_scope: tuple[SettleScopeGap, ...] = ()
    #: The Subject items' actual assignments as lowered: recorded as this
    #: operation's core creation record before it is submitted (review F-001).
    creations: tuple[RecordedCreation, ...] = ()
    #: Each changed item's own reason and the member it explains, recorded
    #: with the proposal before it is submitted (per-Claim rationale).
    item_reasons: tuple[ProposalItemReason, ...] = ()


@dataclass(frozen=True)
class SettleMandateSelection:
    """The settle grant a settle terminal binds, and why its scope leaves the change out.

    Empty ``outside_scope`` means the grant's scope covers every change and the
    terminal settles under it; otherwise the terminal proposes under it.
    """

    digest: str
    outside_scope: tuple[SettleScopeGap, ...] = ()


def proposal_items(request: TerminalEgressRequestV1) -> tuple[CoreClaimsChange, ...]:
    """Consume only executor-owned changes, never parse a terminal value as a command."""
    try:
        return tuple(request.claims_changes[item.item_key] for item in request.items)
    except KeyError as exc:
        raise ProposalDeliveryRefused(
            "proposal_item_invalid", "Claim commands cannot come from data; use a claims node."
        ) from exc


def evidence_by_item(
    request: TerminalEgressRequestV1,
    manifests: Mapping[str, TerminalItemDependencyManifestV1],
    *,
    derivations: Mapping[str, Collection[str]] | None = None,
    cache: CaptureRootCache | None = None,
) -> dict[str, str]:
    """Pick the one Capture each item's own closure proves as its root (D-R37 d1).

    The closure is the UNION of produced and admitted Captures
    (``citable_captures``). A single one is its own root. Several must share one
    recorded derivation root (``capture_root``, over ``derivations``): a
    receipt names which Capture the claims terminal cited, never that its
    recorded inputs cover the rest of the closure, so the cited Capture is
    checked against the closure's actual root, not merely its membership.
    """

    known_derivations = derivations or {}
    evidence: dict[str, str] = {}
    for item in request.items:
        manifest = manifests.get(item.item_key)
        produced = citable_captures(manifest)
        if len(produced) == 0:
            raise ProposalDeliveryRefused(
                "proposal_item_evidence_missing",
                "The item's dependency closure reached no produced or admitted Capture to cite.",
                details={"item_key": item.item_key, "child_index": item.child_index},
            )
        if len(produced) == 1:
            evidence[item.item_key] = produced[0]
            continue
        # A derived root and the sources it records: the claims terminal
        # chose the one root (``capture_root``) and its receipt cites it. A
        # receipt alone never proves a root; delivery proves one independently
        # over the recorded derivation graph and checks the receipt against it.
        root = capture_root(produced, known_derivations, cache=cache)
        receipt = request.claims_evidence.get(item.item_key)
        cited = getattr(getattr(receipt, "value", None), "capture_digest", None)
        if root is not None and isinstance(cited, str) and cited == root:
            evidence[item.item_key] = root
            continue
        if root is not None and receipt is None and _creates_subject(request, item.item_key):
            # A Subject item carries no receipt to check: its key Claims cite the
            # root delivery proves over the recorded derivation graph, the root
            # its sibling Claims' receipts cite (a Subject made from a derive).
            evidence[item.item_key] = root
            continue
        # Absence over a Capture list: its witness cites exactly the closure (D-R37 a6).
        listed = witnessed_list_capture(produced, receipt, cache=cache)
        if listed is not None:
            evidence[item.item_key] = listed
            continue
        raise ProposalDeliveryRefused(
            "proposal_item_evidence_ambiguous",
            "The item's dependency closure reached more than one Capture it could cite.",
            details={
                "item_key": item.item_key,
                "child_index": item.child_index,
                "capture_digests": list(produced),
            },
        )
    return evidence


def _creates_subject(request: TerminalEgressRequestV1, item_key: str) -> bool:
    changes = getattr(request, "claims_changes", None) or {}
    return isinstance(changes.get(item_key), CoreSubjectChange)


def _keyless_statement(item: CoreClaimChange) -> AuthoringClaimStatement:
    return AuthoringClaimStatement.model_validate(item.statement.model_dump(mode="python"))


def _claim_members(
    request: TerminalEgressRequestV1,
    *,
    items: tuple[CoreClaimsChange, ...],
    evidence: Mapping[str, str],
    read: Callable[[str], bytes | None] = lambda _path: None,
    resolve: Callable[[ClaimAuthoringPayload], ClaimAuthoringPayload] = lambda member: member,
    statement_of: Callable[[CoreClaimChange], AuthoringClaimStatement] = _keyless_statement,
    claim_bound_captures: frozenset[str] = frozenset(),
) -> tuple[tuple[ClaimAuthoringPayload, ...], dict[str, str], dict[str, str]]:
    """Build one Claim member per Claim item; return members, item->claim id, identity->item.

    Subject items are not Claims (``_subject_members``); ``statement_of`` gives
    each Claim item's statement with any Subject it names by key resolved.

    Canonicalization and alias resolution precede identity checks. Only the
    claims eligibility law chooses a predecessor; delivery makes no second
    restatement decision and cannot infer a refresh from item data.
    """

    members: list[ClaimAuthoringPayload] = []
    dependency_drafts = ClaimDependencyDrafts()
    seal(dependency_drafts)
    claim_ids: dict[str, str] = {}
    identity_to_item: dict[str, str] = {}
    # A Capture is immutable and shared by the rows extracted from it. Build
    # its citation source once per distinct Capture in this terminal.
    sources = {
        digest: ExistingCaptureCitationSource(capture_digest=digest)
        for digest in set(evidence.values())
    }
    for egress_item, item in zip(request.items, items, strict=True):
        if not isinstance(item, CoreClaimChange):
            continue
        claim_id = item.predecessor or proposal_claim_id(
            admission_binding_digest=request.admission_binding_digest,
            node_id=request.node_id,
            item_key=egress_item.item_key,
        )
        member = ClaimAuthoringPayload(
            procedure_evidence=request.claims_evidence.get(egress_item.item_key),
            statement=statement_of(item),
            rationale=item.rationale,
            source=SelfSourceBody(
                content_base64=base64.b64encode(item.rationale.encode("utf-8")).decode("ascii")
            )
            if evidence[egress_item.item_key] in claim_bound_captures
            else sources[evidence[egress_item.item_key]],
            citation_role=None
            if evidence[egress_item.item_key] in claim_bound_captures
            else "evidence",
            derivation=item.derivation,
            revises=item.predecessor,
            dependency_drafts=dependency_drafts,
        )
        member = resolve(canonical_authored_claims(member, read))
        # The member is final here: seal it so its identity and its statement's
        # digests are taken once however many laws ask (copies are never sealed).
        seal(member)
        identity = authoring_member_identity(member)
        if identity in identity_to_item:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "Two terminal items author the same Claim statement.",
                details={
                    "item_key": egress_item.item_key,
                    "duplicate_of": identity_to_item[identity],
                },
            )
        identity_to_item[identity] = egress_item.item_key
        claim_ids[egress_item.item_key] = claim_id
        members.append(member)
    return tuple(members), claim_ids, identity_to_item


def _created_key(subject_kind: str, key: object) -> bytes:
    """The one spelling of a terminal Subject item's kind and key, to find it by."""

    return canonical_bytes({"subject_kind": subject_kind, "key": key})


def _created_address(
    reference: CoreCreatedSubject, created: Mapping[bytes, str]
) -> SemanticAddress:
    """The Subject a created-Subject reference names: its Subject item's creation member.

    A pair this run creates (or an end of one) is named by the exact key of the
    terminal's Subject item creating it; shared lowering lands that member on
    the path it assigns or reuses (``subject-creation-v1``).
    """

    wire = reference.model_dump(mode="json")
    member = created.get(_created_key(wire["subject_kind"], wire["key"]))
    if member is None:
        raise ProposalDeliveryRefused(
            "proposal_item_invalid",
            "An item names a Subject the terminal creates, but no earlier Subject item "
            "of the terminal carries that kind and key.",
            details={"subject_kind": reference.subject_kind},
        )
    return SemanticAddress.subject_creation(reference.subject_kind, member.rpartition("@")[2])


def _subject_creation(
    item: CoreSubjectChange,
    *,
    capture: str | None,
    created: Mapping[bytes, str] | None = None,
) -> SubjectCreationAuthoringPayload:
    """The creation member one Subject item lowers into: its kind and key, never an id.

    Key parts are named by predicate (a short name is read under the kind) and
    sorted, so one key is one member identity however the run spelled it. A
    pair's end is the existing end's address, or the creation member of the
    earlier Subject item creating it (``created``: ends before their pairs).
    The key Claims cite the one verified Capture in their record's closure.
    """

    kind = item.subject_kind

    def part_value(value: object) -> object:
        if isinstance(value, CoreCreatedSubject):
            return SubjectClaimObject(address=_created_address(value, created or {}))
        return value

    parts = sorted(
        (
            SubjectKeyPart(
                predicate=name if "." in name else f"{kind}.{name}",
                value=part_value(value),  # type: ignore[arg-type]
            )
            for name, value in item.key.items()
        ),
        key=lambda part: part.predicate.encode("utf-8"),
    )
    if capture is None:
        raise ProposalDeliveryRefused(
            "proposal_item_evidence_missing", "Subject key creation requires its verified Capture."
        )
    return SubjectCreationAuthoringPayload(
        subject_kind=kind,
        key=tuple(parts),
        rationale=item.rationale,
        source=ExistingCaptureCitationSource(capture_digest=capture),
        citation_role="evidence",
    )


def _subject_members(
    request: TerminalEgressRequestV1,
    *,
    items: tuple[CoreClaimsChange, ...],
    evidence: Mapping[str, str],
    created: dict[bytes, str] | None = None,
) -> tuple[tuple[SubjectCreationAuthoringPayload, ...], dict[str, str]]:
    """One creation member per Subject item; return the members and identity->item.

    ``created`` collects each Subject item's kind and key -> its member
    identity, in item order, so a later pair item (or a Claim item) naming it
    as a created Subject finds its creation member.
    """

    created = {} if created is None else created

    members: list[SubjectCreationAuthoringPayload] = []
    identity_to_item: dict[str, str] = {}
    for egress_item, item in zip(request.items, items, strict=True):
        if not isinstance(item, CoreSubjectChange):
            continue
        try:
            member = _subject_creation(
                item, capture=evidence.get(egress_item.item_key), created=created
            )
        except ValidationError as exc:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "A Subject item does not name a kind and its key.",
                details={"item_key": egress_item.item_key, "errors": str(exc)},
            ) from exc
        identity = member.member_identity
        if identity in identity_to_item:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "Two terminal items create the same Subject.",
                details={
                    "item_key": egress_item.item_key,
                    "duplicate_of": identity_to_item[identity],
                },
            )
        identity_to_item[identity] = egress_item.item_key
        created.setdefault(
            _created_key(item.subject_kind, item.model_dump(mode="json")["key"]), identity
        )
        members.append(member)
    return tuple(members), identity_to_item


def _key_refusal(message: str, *, code: str, **details: object) -> ProposalDeliveryRefused:
    return ProposalDeliveryRefused(
        "proposal_lowering_refused",
        f"Shared lowering refused the proposed Claims: {message}",
        details={"code": code, **details},
    )


IDENTITY_REFERENCE_UNSTABLE = "cruxible.procedure.identity_reference_unstable"


@dataclass
class _KeyedReference:
    """One Claim item's Subject named by key, and what it was frozen to."""

    kind: str
    key: KeyTuple
    spelled: Mapping[str, object]
    single_text: str | None
    #: The terminal Subject item it names (a creation member), else None.
    creation: str | None = None
    #: The existing Subject it resolved to at the admitted base, else None.
    path: str | None = None
    #: It resolved to no Subject (``key_missing``) at the admitted base.
    missing: bool = False
    #: The terminal Subject item carrying this key when the reference was frozen
    #: to the live Subject holding it instead: that item must land there too.
    sibling: str | None = None

    def described(self) -> dict[str, object]:
        return {"subject_kind": self.kind, "key": dict(self.spelled)}


class _SubjectKeys:
    """Resolve the Subjects a terminal's Claim items name by key (v3 items).

    A reference is frozen once, against the admitted base and the terminal's
    own Subject items: a key one of those items carries names the one live
    Subject of the base holding it, when there is exactly one (the item reuses
    it), else that item (a creation-member reference, ``subject-creation-v1``,
    which shared lowering resolves to the path it finally assigns or reuses);
    any other key resolves
    through the one resolver (``IdentityView.resolve_key_reference``: exact
    live id, alias, key, exact retired id) over the base. None refuses
    (``key_missing``: create it with a Subject item), several refuse as
    ambiguous, as every create-by-key route does.

    Nothing is resolved to a fixed point. After final lowering every reference
    is resolved again by the same resolver over the final candidate state
    (``unstable``): a different result -- or a ``key_missing`` the terminal's
    own changes would now resolve -- refuses the terminal
    (``cruxible.procedure.identity_reference_unstable``, review F-002).
    """

    _SEVERAL = ""

    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        base: AcceptedProjectionCoordinate,
        base_tree: SnapshotTree,
    ) -> None:
        self.instance = instance
        self.base = base
        self.base_tree = base_tree
        self.created: dict[tuple[str, str], str] = {}
        #: Subject items asking for a further holder of their key (``create_new``).
        self.creating_new: set[str] = set()
        self.view: IdentityView | None = None
        self.references: list[_KeyedReference] = []
        #: Each Subject item's kind and key -> its creation member (``_subject_members``).
        self.created_members: dict[bytes, str] = {}

    def add_created(self, members: tuple[SubjectCreationAuthoringPayload, ...]) -> None:
        """Index each Subject item of the terminal by its kind and canonical key encoding."""

        for member in members:
            try:
                key = self.key(
                    member.subject_kind, {part.predicate: part.value for part in member.key}
                )
            except SubjectKeyInvalid:
                continue  # lowering refuses the item itself
            if key is None:
                continue
            slot = (member.subject_kind, key_encoding(key))
            held = self.created.get(slot)
            identity = member.member_identity
            self.created[slot] = identity if held in {None, identity} else self._SEVERAL
            if member.create_new:
                self.creating_new.add(identity)

    def literal_key(self, member: SubjectCreationAuthoringPayload) -> KeyTuple:
        """A creation member's canonical key when it names it by literals only, else ()."""

        try:
            return (
                self.key(member.subject_kind, {part.predicate: part.value for part in member.key})
                or ()
            )
        except SubjectKeyInvalid:
            return ()  # a pair's ends land in lowering; lowering refuses a bad key

    def bind(self, view: IdentityView) -> None:
        """Freeze references against ``view`` (the admitted base)."""

        self.view = view

    def claim_type(self, predicate: str) -> ClaimType | None:
        path = claim_type_path(predicate)
        content = self.base_tree.get(path)
        return None if content is None else parse_claim_type(content, path=path)

    def key(self, kind: str, given: Mapping[str, object]) -> KeyTuple | None:
        """The canonical key ``given`` names under ``kind``, or None for an unkeyed kind."""

        declared = live_subject_kind(self.base_tree, kind)
        if declared is None or declared.minted:
            return None
        return canonical_key(declared, given, claim_type=self.claim_type)

    def address(self, reference: SubjectKeyReference) -> SemanticAddress:
        kind = reference.subject_kind
        declared = live_subject_kind(self.base_tree, kind)
        try:
            given = key_reference_mapping(declared, reference.key) if declared is not None else {}
            key = self.key(kind, given)
        except (SubjectKeyInvalid, ValueError) as exc:
            raise _key_refusal(f"{kind}: {exc}", code="cruxible.subject.key_invalid") from exc
        if key is None:
            raise _key_refusal(
                f"{kind} has no declared keys, so no Subject of it is named by key.",
                code="cruxible.subject.key_invalid",
            )
        values = tuple(given.values())
        single = values[0] if len(key) == 1 and len(values) == 1 else None
        frozen = _KeyedReference(
            kind=kind,
            key=key,
            spelled=given,
            single_text=single if isinstance(single, str) else None,
        )
        self.references.append(frozen)
        encoded = key_encoding(key)
        member = self.created.get((kind, encoded))
        if member == self._SEVERAL:
            raise _key_refusal(
                f"Several Subject items of this terminal hold this {kind} key; name one by id.",
                code="cruxible.subject.key_ambiguous",
            )
        assert self.view is not None, "references are frozen against the bound base"
        if member is not None:
            if member not in self.creating_new:
                # The terminal's Subject item restates a Subject the base already
                # holds (it lowers to that Subject, creating nothing): name the live
                # Subject, so an item restating one of its live Claims targets that
                # Claim's lineage and revises it or changes nothing.
                held = self.view.holders(kind, key)
                if len(held) == 1:
                    frozen.path = held[0]
                    frozen.sibling = member
                    return SemanticAddress.whole_artifact(held[0])
            frozen.creation = member
            return SemanticAddress.subject_creation(kind, member.rpartition("@")[2])
        resolution = self.view.resolve_key_reference(kind, key, single_text=frozen.single_text)
        if resolution.ambiguous:
            holders = [f"subjects/{item}.json" for item in resolution.candidates]
            raise _key_refusal(
                key_ambiguous_message(kind, holders)
                if resolution.via == "key"
                else f"An alias of {kind} names {len(holders)} Subjects: "
                + ", ".join(resolution.candidates)
                + "; name one by id.",
                code="cruxible.subject.key_ambiguous"
                if resolution.via == "key"
                else "cruxible.authoring.alias_ambiguous",
            )
        if resolution.path is None:
            # Refused once every member is built, unless the terminal's own changes
            # would resolve it (then it is unstable, not missing).
            frozen.missing = True
            placeholder = hashlib.sha256(f"{kind}\0{encoded}".encode()).hexdigest()[:32]
            return SemanticAddress.subject_creation(kind, placeholder)
        frozen.path = resolution.path
        return SemanticAddress.whole_artifact(resolution.path)

    def unstable(
        self,
        connection: sqlite3.Connection,
        *,
        members: tuple[ClaimAuthoringPayload, ...],
        subject_members: tuple[SubjectCreationAuthoringPayload, ...],
        creations: Mapping[str, tuple[str, KeyTuple, bool]],
    ) -> None:
        """Refuse when a reference resolves otherwise over the final candidate state.

        ``creations`` is where each Subject item landed: member identity ->
        (path, key, whether it created that Subject). The final state is the
        admitted base overlaid with the terminal's members, their Subject
        references resolved to those paths, and its created Subjects.
        """

        # A creation reference's selector value is its member identity's digest:
        # one lookup per reference, never a scan of the creations.
        by_digest = {
            identity.rpartition("@")[2]: path for identity, (path, _key, _made) in creations.items()
        }

        def resolved(address: SemanticAddress) -> SemanticAddress:
            if address.selector.scheme != "subject-creation-v1":
                return address
            path = by_digest.get(address.selector.value)
            return address if path is None else SemanticAddress.whole_artifact(path)

        final_members: list[AuthoringChangeSetMember] = list(subject_members)
        for member in members:
            statement = member.statement
            value = statement.object
            if isinstance(value, SubjectClaimObject):
                value = SubjectClaimObject(address=resolved(value.address))
            final_members.append(
                member.model_copy(
                    update={
                        "statement": statement.model_copy(
                            update={"subject": resolved(statement.subject), "object": value}
                        )
                    }
                )
            )
        view = IdentityView(
            connection,
            members_identity_overlay(
                connection, members=tuple(final_members), base_tree=self.base_tree
            ),
            key_reader=tree_key_reader(self.base_tree),
        )
        for path, key, created in creations.values():
            if created:
                view.add_creation(path, key)
        changed: list[dict[str, object]] = []
        for reference in self.references:
            frozen = (
                creations[reference.creation][0]
                if reference.creation is not None and reference.creation in creations
                else reference.path
            )
            final = view.resolve_key_reference(
                reference.kind, reference.key, single_text=reference.single_text
            )
            now: object = final.path if not final.ambiguous else list(final.candidates)
            if now == frozen and reference.sibling is not None and reference.sibling in creations:
                # Frozen to the live holder its Subject item restates: the item
                # must reuse that Subject, not create another holder of the key
                # (the terminal moved the live Subject's key away).
                now = creations[reference.sibling][0]
            if now != frozen:
                changed.append(
                    {
                        "reference": reference.described(),
                        "frozen": "key_missing" if reference.missing else frozen,
                        "final": now,
                    }
                )
        if changed:
            first = changed[0]
            raise _key_refusal(
                f"The {first['reference']} reference names {first['frozen']} as this terminal "
                f"was prepared, and {first['final']} once its own identity changes (alias or "
                "key Claims) apply: put the identity change and the references that depend "
                "on it in separate runs or terminals.",
                code=IDENTITY_REFERENCE_UNSTABLE,
                references=changed,
                repair=(
                    "Put the identity change (an alias or key Claim) and the references that "
                    "depend on it in separate runs or terminals."
                ),
            )
        missing = next((item for item in self.references if item.missing), None)
        if missing is not None:
            raise _key_refusal(
                f"No live {missing.kind} Subject holds this key; add a Subject item creating "
                "it to the same terminal.",
                code="cruxible.subject.key_missing",
                reference=missing.described(),
            )

    def statement(self, item: CoreClaimChange) -> AuthoringClaimStatement:
        statement = item.statement
        value = statement.object
        if isinstance(statement.subject, SemanticAddress) and not isinstance(
            value, CoreCreatedSubject
        ):
            return statement
        subject = (
            statement.subject
            if isinstance(statement.subject, SemanticAddress)
            else _created_address(statement.subject, self.created_members)
            if isinstance(statement.subject, CoreCreatedSubject)
            else self.address(statement.subject)
        )
        if isinstance(value, CoreCreatedSubject):
            # A pair the run creates (V1), named by its Subject item's creation member.
            value = SubjectClaimObject(address=_created_address(value, self.created_members))
        return AuthoringClaimStatement(
            subject=subject,
            predicate=statement.predicate,
            qualifier=statement.qualifier,
            object=value,
            role=statement.role,
            effective_from=statement.effective_from,
            effective_until=statement.effective_until,
        )


def _lowered_subject_paths(
    lowered: LoweredAuthoring, subject_items: Mapping[str, str]
) -> dict[str, tuple[str, bool]]:
    """Where final shared lowering put each Subject item: (path, whether it reused one)."""

    found: dict[str, tuple[str, bool]] = {}
    members = lowered.resolved_authoring.get("members")
    for entry in members if isinstance(members, list) else ():
        identity = str(entry.get("identity"))
        if identity in subject_items:
            found[identity] = (str(entry.get("path")), entry.get("resolved") is True)
    return found


_UNCHANGED_OUTCOMES = frozenset(
    {
        "cruxible.authoring.content_already_cited",
        "cruxible.authoring.existing_capture_already_associated",
    }
)


def _unchanged_children(
    instance: PlaybillInstance,
    request: TerminalEgressRequestV1,
    *,
    lowered: LoweredAuthoring,
    identity_to_item: Mapping[str, str],
    item_paths: Mapping[str, str],
    base_tree: SnapshotTree,
) -> dict[str, TerminalEgressChildReceiptV3]:
    """The items shared lowering left unchanged, each as its receipt child.

    Only an item whose member lowering resolved to an unchanged outcome counts;
    any other item that changed no member is not accounted for and refuses.
    """

    resolved = lowered.resolved_authoring.get("members")
    by_identity = {
        str(item.get("identity")): item
        for item in (resolved if isinstance(resolved, list) else ())
        if isinstance(item, dict)
    }
    access = BodyAccessContext(principal_id="procedure-proposal", can_read_body=True)
    children: dict[str, TerminalEgressChildReceiptV3] = {}
    by_key = {item.item_key: item for item in request.items}
    for identity, item_key in identity_to_item.items():
        lowered_member = by_identity.get(identity, {})
        capture_digest = lowered_member.get("capture_digest")
        path = item_paths[item_key]
        content = base_tree.get(path)
        if (
            lowered_member.get("outcome") not in _UNCHANGED_OUTCOMES
            or not isinstance(capture_digest, str)
            or content is None
        ):
            continue
        envelope = parse_capture_envelope(instance.body_store().read(capture_digest, access=access))
        children[item_key] = TerminalEgressChildReceiptV3(
            child_index=by_key[item_key].child_index,
            item_key=item_key,
            egress_digest=claim_artifact_digest(parse_claim(content, path=path)).tagged,
            path=path,
            reobserving_capture_digest=capture_digest,
            reobserved_at=envelope.observed_at,
        )
    return children


def proposal_rationale(request: TerminalEgressRequestV1, *, item_count: int) -> str:
    """The change set's prose: which run proposed it, and how many items it carries.

    Each item's own reason is recorded with the member it explains
    (``ProposalItemReason``), read back per Claim or Subject by `proposal
    review` and `get --detail why`; the change set no longer joins them into
    one bounded text.
    """

    noun = "item" if item_count == 1 else "items"
    return (
        f"Proposed by Procedure {request.procedure_identity.name} "
        f"run {request.run_id} terminal {request.node_id}\n\n"
        f"{item_count} {noun}; each item's reason is recorded with the Claim or Subject "
        "it explains."
    )


def proposal_item_reasons(
    request: TerminalEgressRequestV1,
    *,
    items: tuple[CoreClaimsChange, ...],
    item_paths: Mapping[str, str],
    unchanged: Mapping[str, object],
) -> tuple[ProposalItemReason, ...]:
    """Each item that changed a member: its reason record, built while preparing.

    Item validation already refused a rationale that normalizes to nothing; any
    record that still does not validate refuses the terminal typed to its item,
    before the preparation is journaled, never as an internal failure.
    """

    operation = _intent_id(request)
    records: list[ProposalItemReason] = []
    for egress_item, item in zip(request.items, items, strict=True):
        if egress_item.item_key in unchanged or egress_item.item_key not in item_paths:
            continue
        try:
            records.append(
                ProposalItemReason.bounded(
                    item.rationale,
                    operation=operation,
                    path=item_paths[egress_item.item_key],
                    item_key=egress_item.item_key,
                    child_index=egress_item.child_index,
                    procedure=request.procedure_identity.qualified,
                    run_id=request.run_id,
                    terminal=request.node_id,
                )
            )
        except ValidationError as exc:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "A terminal item's rationale cannot be recorded as its reason.",
                details={
                    "item_key": egress_item.item_key,
                    "child_index": egress_item.child_index,
                    "errors": [
                        {"location": list(map(str, error["loc"])), "message": error["msg"]}
                        for error in exc.errors()
                    ],
                    "repair": "Give the item a rationale with printable text.",
                },
            ) from exc
    return tuple(records)


_SlotKey = tuple[str, str, str, str, str | None]


def _slot_key(statement: ClaimPredicateKey, qualifier: str | None) -> _SlotKey:
    subject = statement.subject
    return (
        subject.artifact_path,
        subject.selector.scheme,
        subject.selector.value,
        statement.predicate,
        qualifier,
    )


def _with_many_valued_dispositions(
    members: tuple[ClaimAuthoringPayload, ...],
    *,
    claim_ids: Mapping[str, str],
    base_tree: SnapshotTree,
) -> tuple[ClaimAuthoringPayload, ...]:
    """Let an item add a value to a many-valued slot, as the ``add`` write verb does.

    The slot law demands a disposition of every live Claim in a new Claim's
    (subject, predicate, qualifier) slot as lowering stages it. On a ClaimType
    of cardinality ``many`` those values do not contend: they stay, and each is
    dispositioned ``not_tested`` unless the item dispositioned it itself. A
    one-valued slot is left to the law: an item there names the Claim it
    ``revises``.

    ``members`` is in staging order (member identity), and the slot each Claim
    sits in is followed through that order: it starts where the base holds it,
    and every earlier member moves its own Claim -- a new one, or the lineage it
    revises, out of whatever slot that lineage held -- into its own slot. So a
    member is dispositioned against exactly the slot lowering will stage it into.
    A sibling the same terminal retires is still live when the member stages
    (retirements stage last), so it is dispositioned ``not_tested`` too.
    """

    cardinality: dict[str, str | None] = {}
    # Claim ID -> the slot it sits in as staging reaches the current member.
    location: dict[str, _SlotKey] = {}
    loaded: set[tuple[str, str, str, str]] = set()

    def load(statement: ClaimPredicateKey) -> None:
        key = _slot_key(statement, None)[:4]
        if key in loaded:
            return
        loaded.add(key)
        for claim in base_tree.claims_for(statement):
            location.setdefault(
                claim.identity.name, _slot_key(claim.statement, claim.statement.qualifier)
            )

    filled: list[ClaimAuthoringPayload] = []
    for member in members:
        statement = member.statement
        predicate = statement.predicate
        slot = _slot_key(statement, statement.qualifier)
        own = member.revises or claim_ids[authoring_member_identity(member)]
        if predicate not in cardinality:
            path = claim_type_path(predicate)
            content = base_tree.get(path)
            cardinality[predicate] = (
                None if content is None else parse_claim_type(content, path=path).cardinality
            )
        load(statement)
        if member.revises is not None and member.revises not in location:
            # The revised lineage sits under another subject or predicate.
            revised_path = claim_path(member.revises)
            revised = base_tree.get(revised_path)
            if revised is not None:
                load(parse_claim(revised, path=revised_path).statement)
        if cardinality[predicate] != "many":
            filled.append(member)
            location[own] = slot
            continue
        explicit = {item.claim_id for item in member.existing_claim_dispositions}
        added = sorted(
            {claim_id for claim_id, held in location.items() if held == slot} - explicit - {own},
            key=lambda item: item.encode("ascii"),
        )
        location[own] = slot
        if not added:
            filled.append(member)
            continue
        dispositions = (
            *member.existing_claim_dispositions,
            *(
                AuthoringExistingClaimDisposition(claim_id=claim_id, disposition="not_tested")
                for claim_id in added
            ),
        )
        filled.append(
            member.model_copy(
                update={
                    "existing_claim_dispositions": tuple(
                        sorted(dispositions, key=lambda item: item.claim_id.encode("ascii"))
                    )
                }
            )
        )
    return tuple(filled)


def _intent_id(request: TerminalEgressRequestV1) -> str:
    """The intent one terminal operation lowers under, from the admitted operation alone."""

    return procedure_operation_id(
        admission_binding_digest=request.admission_binding_digest, node_id=request.node_id
    )


def _intent(
    request: TerminalEgressRequestV1,
    *,
    instance_id: str,
    payload: ChangeSetAuthoringPayload,
    claim_identities: tuple[ChangeSetClaimIdentity, ...],
) -> AuthoringIntent:
    membership_digest = typed_digest(
        Sha256Value,
        AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
        {
            "members": [
                {"kind": kind, "identity": identity}
                for kind, identity in authoring_change_set_membership(payload.members)
            ]
        },
    ).tagged.removeprefix("sha256:")
    evaluation_time = (
        request.evaluation_time
        if isinstance(request, TerminalEgressRequestV2)
        else request.prepared_at
    )
    return AuthoringIntent(
        intent_id=_intent_id(request),
        instance_id=instance_id,
        actor_id=request.actor_context.actor_id,
        canonical_timestamp=canonical_candidate_timestamp(evaluation_time),
        base_coordinate=request.accepted_coordinate,
        semantic_identity=f"ChangeSet:{membership_digest}",
        payload=payload,
        payload_digest=authoring_payload_digest(payload),
        create_fingerprint=authoring_create_fingerprint(
            instance_id=instance_id,
            actor_id=request.actor_context.actor_id,
            payload=payload,
        ),
        candidate_status=CandidateStatus(
            state="draft",
            current_accepted_coordinate=request.accepted_coordinate,
        ),
        change_set_claim_identities=claim_identities,
    )


def select_procedure_mandate(
    request: TerminalEgressRequestV1,
    *,
    admission: ProcedureRunAdmission,
    accepted_mandates: Mapping[str, ProcedureMandateAny],
    target_paths: tuple[str, ...],
    delegation: ProcedureDelegation | None = None,
) -> str | None:
    """Bind the one live mandate that covers this request, else the closest.

    Every accepted mandate pinned to this Procedure artifact is evaluated with
    the same law `require_procedure_mandate` applies later. A permitted one is
    bound: one granting exactly the requested rung before a wider grant (a
    proposal never leans on a settle grant while a propose grant covers it, so
    retiring the settle grant cannot strand it), then the lowest digest, so the
    choice is stable. If none permits, the one with the fewest refusals is
    bound instead, so the refusal the door raises names the real limiting term
    rather than "no mandate".
    """

    evaluation_time = (
        request.evaluation_time
        if isinstance(request, TerminalEgressRequestV2)
        else request.prepared_at
    )
    line_identity, line_digest = authority_line(admission, delegation)
    if line_identity is None or line_digest is None:
        return None
    ranked: list[tuple[int, bool, str]] = []
    for digest, mandate in sorted(accepted_mandates.items(), key=lambda item: item[0]):
        evaluation = evaluate_procedure_mandate(
            mandate,
            ProcedureMandateInvocation(
                line_identity=line_identity,
                line_artifact_digest=line_digest,
                requested_rung=request.required_rung,  # type: ignore[arg-type]
                target_paths=target_paths,
                evaluation_time=evaluation_time,
                accepted_mandate_digest=digest,
            ),
        )
        ranked.append(
            (
                len(evaluation.refusal_codes),
                mandate_rung(mandate) != request.required_rung,
                digest,
            )
        )
    if not ranked:
        return None
    return min(ranked)[2]


def select_settle_mandate(
    request: TerminalEgressRequestV1,
    *,
    admission: ProcedureRunAdmission,
    accepted_mandates: Mapping[str, ProcedureMandateAny],
    target_paths: tuple[str, ...],
    base_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    delegation: ProcedureDelegation | None = None,
) -> SettleMandateSelection:
    """Bind the one settle grant that covers every change; several refuse.

    Coverage is the mandate's own law (Line, window, namespace) and its scope:
    per ClaimType and change kind, the evidence bases (and enum/bool values)
    it settles on. Its condition is decided at delivery.

    All or nothing (§8.4): when no grant covers the change but a live grant for
    this Line covers every changed member as a target, the terminal binds that
    grant and proposes the whole change under it, recording each scope gap: the
    grant with the fewest gaps, then the lowest digest. A change no grant
    covers in any other way refuses ``settle_mandate_missing``.
    """

    line_identity, line_digest = authority_line(admission, delegation)
    covering: list[str] = []
    partial: list[tuple[int, str, tuple[SettleScopeGap, ...]]] = []
    for digest, mandate in sorted(accepted_mandates.items(), key=lambda item: item[0]):
        if not isinstance(mandate, ProcedureMandate) or mandate.grants != "settle":
            continue
        if line_identity is None or line_digest is None:
            continue  # a direct run has no Line, so no grant covers it
        evaluation = evaluate_procedure_mandate(
            mandate,
            ProcedureMandateInvocation(
                line_identity=line_identity,
                line_artifact_digest=line_digest,
                requested_rung=3,
                target_paths=target_paths,
                evaluation_time=request.prepared_at,
                accepted_mandate_digest=digest,
            ),
        )
        if evaluation.verdict != "permitted":
            continue
        gaps = settle_scope_gaps(
            mandate,
            scope=target_paths,
            current_tree=base_tree,
            candidate_tree=candidate_tree,
            evaluated_at=request.prepared_at,
        )
        if gaps == ():
            covering.append(digest)
        elif gaps is not None:
            partial.append((len(gaps), digest, gaps))
    if len(covering) > 1:
        raise ProposalDeliveryRefused(
            "settle_mandate_ambiguous",
            "More than one settle ProcedureMandate covers this change; exactly one must.",
            details={"mandates": covering},
        )
    if covering:
        return SettleMandateSelection(digest=covering[0])
    if partial:
        _count, digest, gaps = min(partial, key=lambda item: (item[0], item[1]))
        return SettleMandateSelection(digest=digest, outside_scope=gaps)
    raise ProposalDeliveryRefused(
        "settle_mandate_missing",
        "No live settle ProcedureMandate for this Procedure covers every changed Claim.",
        details={"target_paths": list(target_paths)},
    )


_Child = TypeVar("_Child", bound=BaseModel)


def _with_refused_reobservations(
    children: tuple[_Child, ...], refused: Mapping[str, ReobservationRefusalReason]
) -> tuple[_Child, ...]:
    """Each unchanged Claim child whose re-observation was refused, marked with why."""

    if not refused:
        return children
    return tuple(
        child.model_copy(update={"reobservation_refused": refused[child.item_key]})
        if isinstance(child, TerminalEgressChildReceiptV3) and child.item_key in refused
        else child
        for child in children
    )


class ProposalTerminalEgressSink:
    """The production `claims` sink: prepare once, deliver exactly once."""

    def __init__(
        self,
        *,
        instance: PlaybillInstance,
        accepted_mandates: Mapping[str, ProcedureMandateAny],
        proposal_service: Callable[[], ProposalService] | None = None,
        delegation: ProcedureDelegation | None = None,
    ) -> None:
        self.instance = instance
        self.delegation = delegation
        self.accepted_mandates = dict(accepted_mandates)
        self._proposal_service = proposal_service or instance.proposal_service
        self._absence_evidence: dict[tuple[str, str], dict[str, str]] = {}
        self._prepared: dict[tuple[str, str], PreparedProposal] = {}

    def grants_settle(self) -> bool:
        """Whether any live mandate this sink holds grants settle."""

        return any(
            isinstance(mandate, ProcedureMandate) and mandate.grants == "settle"
            for mandate in self.accepted_mandates.values()
        )

    # -- preparation --------------------------------------------------------

    def prepare_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
        admission: ProcedureRunAdmission,
        manifests: Mapping[str, TerminalItemDependencyManifestV1] | None = None,
        evidence: Mapping[str, str] | None = None,
        journaled: PreparedTerminalEgressV1 | None = None,
        recompiles: str | None = None,
        derivations: Mapping[str, Collection[str]] | None = None,
        capture_root_cache: CaptureRootCache | None = None,
    ) -> PreparedTerminalEgressV1:
        # One preparation owns all sibling Claims' Capture proofs. Recorded
        # landing/erase/damage events invalidate them through the request epoch.
        with self.instance.capture_verification_request():
            return self._prepare_terminal_egress(
                request=request,
                admission=admission,
                manifests=manifests,
                evidence=evidence,
                journaled=journaled,
                recompiles=recompiles,
                derivations=derivations,
                capture_root_cache=capture_root_cache,
            )

    def _prepare_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
        admission: ProcedureRunAdmission,
        manifests: Mapping[str, TerminalItemDependencyManifestV1] | None = None,
        evidence: Mapping[str, str] | None = None,
        journaled: PreparedTerminalEgressV1 | None = None,
        derivations: Mapping[str, Collection[str]] | None = None,
        capture_root_cache: CaptureRootCache | None = None,
        recompiles: str | None = None,
    ) -> PreparedTerminalEgressV1:
        """Lower the terminal's items once; report what delivery will change.

        ``journaled`` is the preparation recovery read back from the journal: a
        Line run's decision about its open proposal is reproduced from it, never
        decided again against what changed since. ``recompiles`` names the stale
        proposal an explicit recompilation re-lowers at the head: the Line's
        decision is then made again at the head instead (``_line_proposal``).
        """

        if request.kind != "claims":
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "The proposal sink prepares claims terminals only.",
                details={"kind": request.kind},
            )
        if evidence is None:
            evidence = evidence_by_item(
                request,
                manifests or {},
                derivations=derivations or {},
                cache=capture_root_cache,
            )
        else:
            missing = [item.item_key for item in request.items if item.item_key not in evidence]
            if missing:
                raise ProposalDeliveryRefused(
                    "proposal_item_evidence_missing",
                    "The journaled preparation names no Capture for a terminal item.",
                    details={"item_keys": missing},
                )
        items = proposal_items(request)
        _validate_absence_bindings(self.instance, request, admission, evidence)
        if any(receipt.absence is not None for receipt in request.claims_evidence.values()):
            self._absence_evidence[(request.admission_binding_digest, request.node_id)] = dict(
                evidence
            )
        reobserved = {}
        observed = {}
        access = BodyAccessContext(principal_id=request.actor_context.actor_id, can_read_body=True)
        for child, change in zip(request.items, items, strict=True):
            if not isinstance(change, CoreReobservationChange):
                continue
            receipt = request.claims_evidence[child.item_key]
            if receipt.value.basis != "extracted" and receipt.absence is None:
                raise ProposalDeliveryRefused(
                    "proposal_item_invalid", "Re-observation requires an extraction receipt"
                )
            assert receipt.value.basis == "extracted" or receipt.value.basis == "capture"
            capture = receipt.value.capture_digest
            if evidence[child.item_key] != capture:
                raise ProposalDeliveryRefused(
                    "proposal_item_evidence_mismatch",
                    "The re-observation receipt must cite the item's own Capture closure.",
                )
            if capture not in observed:
                observed[capture] = parse_capture_envelope(
                    self.instance.body_store().read(capture, access=access)
                ).observed_at
            reobserved[child.item_key] = TerminalEgressChildReceiptV3(
                child_index=child.child_index,
                item_key=child.item_key,
                path=claim_path(change.claim_id),
                egress_digest=change.artifact_digest,
                reobserving_capture_digest=capture,
                reobserved_at=observed[capture],
            )
        if reobserved and len(reobserved) == len(items):
            prepared = PreparedTerminalEgressV1(
                target_paths=(),
                lowering_digest=proposal_lowering_digest(()),
                item_paths=tuple(sorted(((key, child.path) for key, child in reobserved.items()))),
                unchanged=tuple(reobserved.values()),
            )
            self._prepared[(request.admission_binding_digest, request.node_id)] = PreparedProposal(
                prepared=prepared,
                candidate_tree={},
                changed_members=(),
                item_paths={key: child.path for key, child in reobserved.items()},
                rationale="",
            )
            return prepared

        base_tree = self.instance.immutable_tree_at(request.accepted_coordinate.git_oid)
        base = self.instance.resolve_accepted_coordinate(
            git_oid=request.accepted_coordinate.git_oid,
            semantic_root=request.accepted_coordinate.semantic_root,
            generation_root=request.accepted_coordinate.generation_root,
            compiler_digest=request.accepted_coordinate.compiler_digest,
        )

        def resolve(member: ClaimAuthoringPayload) -> ClaimAuthoringPayload:
            try:
                return claim_aliases_resolved(self.instance, base, base_tree, member)
            except AuthoringLoweringError as exc:
                raise ProposalDeliveryRefused(
                    "proposal_lowering_refused",
                    f"Shared lowering refused the proposed Claims: {exc.message}",
                    details={
                        "code": exc.code,
                        "offending_element": exc.offending_element,
                        "repairs": [item.model_dump(mode="json") for item in exc.repairs],
                    },
                ) from exc

        keys = _SubjectKeys(self.instance, base=base, base_tree=base_tree)
        subject_members, subject_items = _subject_members(
            request, items=items, evidence=evidence, created=keys.created_members
        )
        keys.add_created(subject_members)

        # Accepted input Claims may cite Claim-bound self sources. Those remain
        # input evidence; a derived Claim needs its own origin instead of borrowing
        # another Claim's origin. Classify each distinct source once per preparation.
        bodies = self.instance.body_store()
        claim_bound_captures: set[str] = set()
        for digest in {
            evidence[egress.item_key]
            for egress, change in zip(request.items, items, strict=True)
            if isinstance(change, CoreClaimChange)
            and change.derivation is not None
            and change.derivation.inputs
        }:
            envelope = parse_capture_envelope(
                bodies.read(
                    digest,
                    access=BodyAccessContext(principal_id="claims-delivery", can_read_body=True),
                )
            )
            accepted_contract = self.instance.accepted_capture_contract_version(
                AcceptedCoordinate.from_internal(base), envelope.capture_contract_digest
            )
            if accepted_contract is not None and classify_capture_reuse(
                envelope, contract=accepted_contract.contract, store=bodies, claim_id=""
            ) in {"claim_bound", "claim_bound_mismatch"}:
                claim_bound_captures.add(digest)

        def claim_members() -> tuple[
            tuple[ClaimAuthoringPayload, ...], dict[str, str], dict[str, str]
        ]:
            return _claim_members(
                request,
                items=items,
                evidence=evidence,
                read=base_tree.get,
                resolve=resolve,
                statement_of=keys.statement,
                claim_bound_captures=frozenset(claim_bound_captures),
            )

        # Every keyed reference is frozen against the admitted base and the
        # terminal's own Subject items (review F-002), then checked against the
        # final candidate state below.
        with self.instance.bind_accepted_projection(base) as projection:
            keys.bind(
                IdentityView(projection.typed.connection, key_reader=tree_key_reader(base_tree))
            )
            members, claim_ids, identity_to_item = claim_members()
            if any(reference.missing for reference in keys.references):
                # Nothing to lower with a missing Subject: refuse it as missing, or as
                # unstable when the terminal's own changes would supply it.
                assigned = self._subject_paths(
                    request,
                    base=base,
                    base_tree=base_tree,
                    members=(*members, *subject_members),
                )
                keys.unstable(
                    projection.typed.connection,
                    members=members,
                    subject_members=subject_members,
                    creations={
                        member.member_identity: (
                            assigned[member.member_identity][0],
                            keys.literal_key(member),
                            not assigned[member.member_identity][1],
                        )
                        for member in subject_members
                        if member.member_identity in assigned
                    },
                )
        ordered = _with_many_valued_dispositions(
            tuple(
                sorted(
                    members, key=lambda member: authoring_member_identity(member).encode("utf-8")
                )
            ),
            claim_ids={
                identity: claim_ids[item_key] for identity, item_key in identity_to_item.items()
            },
            base_tree=self.instance.immutable_tree_at(request.accepted_coordinate.git_oid),
        )
        retirements = {
            item.item_key: change
            for item, change in zip(request.items, items, strict=True)
            if isinstance(change, CoreRetirementChange)
        }
        retirement_members = tuple(
            ClaimRetirementMember(
                retires=change.claim_id, reason="superseded", absence=change.witness
            )
            for change in retirements.values()
        )
        all_members: tuple[AuthoringChangeSetMember, ...] = tuple(
            sorted(
                (*ordered, *subject_members, *retirement_members),
                key=lambda member: authoring_member_identity(member).encode("utf-8"),
            )
        )
        # Each Subject item's path is the one shared lowering assigns it over the
        # whole terminal (every member, so every key change counts); the receipt
        # and the item's recorded reason read the same assignment below.
        subject_paths = self._subject_paths(
            request, base=base, base_tree=base_tree, members=all_members
        )
        rationale = proposal_rationale(request, item_count=len(request.items))
        payload = ChangeSetAuthoringPayload(members=all_members, rationale=rationale)
        claim_identities = tuple(
            sorted(
                (
                    ChangeSetClaimIdentity(
                        member_identity=identity,
                        claim_id=claim_ids[item_key],
                    )
                    for identity, item_key in identity_to_item.items()
                ),
                key=lambda item: item.member_identity.encode("utf-8"),
            )
        )
        intent = _intent(
            request,
            instance_id=self.instance.descriptor.instance_id,
            payload=payload,
            claim_identities=claim_identities,
        )
        try:
            lowered: LoweredAuthoring = authoring_lowering.lower_authoring(
                self.instance,
                intent=intent,
                actor_id=request.actor_context.actor_id,
                derivation_procedure=ArtifactPin(
                    role="reducer",
                    target=request.procedure_identity,
                    artifact_digest=request.procedure_artifact_digest,
                ),
            )
        except AuthoringLoweringError as exc:
            raise ProposalDeliveryRefused(
                "proposal_lowering_refused",
                f"Shared lowering refused the proposed Claims: {exc.message}",
                details={
                    "code": exc.code,
                    "offending_element": exc.offending_element,
                    "repairs": [item.model_dump(mode="json") for item in exc.repairs],
                },
            ) from exc
        target_paths = tuple(path for path, _content in lowered.changed_members)
        item_paths = {item_key: claim_path(claim_id) for item_key, claim_id in claim_ids.items()}
        item_paths.update({key: claim_path(change.claim_id) for key, change in retirements.items()})
        unchanged: dict[str, UnchangedChildReceiptAny] = dict(
            _unchanged_children(
                self.instance,
                request,
                lowered=lowered,
                identity_to_item=identity_to_item,
                item_paths=item_paths,
                base_tree=base_tree,
            )
        )
        unchanged.update(reobserved)
        item_paths.update({key: child.path for key, child in reobserved.items()})
        if keys.references:
            # Each frozen reference must name the same Subject over the final
            # candidate state, by the same resolver (review F-002).
            with self.instance.bind_accepted_projection(base) as projection:
                keys.unstable(
                    projection.typed.connection,
                    members=members,
                    subject_members=subject_members,
                    creations={
                        item.record.member: (
                            item.path,
                            item.key,
                            item.record.outcome == "created",
                        )
                        for item in lowered.subject_creations
                    },
                )
        child_index = {item.item_key: item.child_index for item in request.items}
        lowered_subjects = _lowered_subject_paths(lowered, subject_items)
        if lowered_subjects != {identity: subject_paths[identity] for identity in subject_items}:
            raise ProposalDeliveryRefused(
                "proposal_lowering_refused",
                "Shared lowering assigned the terminal's Subject items differently than "
                "the assignment it prepared.",
                details={"lowered": {key: list(value) for key, value in lowered_subjects.items()}},
            )
        for identity, item_key in subject_items.items():
            path, reused = lowered_subjects[identity]
            item_paths[item_key] = path
            if reused:
                unchanged[item_key] = TerminalEgressChildReceiptV4(
                    child_index=child_index[item_key],
                    item_key=item_key,
                    egress_digest=subject_digest(parse_subject(base_tree[path], path=path)).tagged,
                    path=path,
                )
        for item_key, path in item_paths.items():
            if path not in target_paths and item_key not in unchanged:
                raise ProposalDeliveryRefused(
                    "proposal_receipt_incomplete",
                    "A terminal item lowered into no changed member.",
                    details={"item_key": item_key, "path": path},
                )
        ordered_unchanged = tuple(sorted(unchanged.values(), key=lambda child: child.child_index))
        line_proposal = (
            self._line_proposal(
                request,
                admission=admission,
                lowered=lowered,
                journaled=journaled,
                recompiles=recompiles,
            )
            if target_paths and request.capped_by is not None
            else None
        )
        if line_proposal is not None and line_proposal.decision == "still_current":
            # The change is the Line's open proposal's: it is kept untouched, and
            # nothing is proposed or authorized. Unchanged items still re-observe.
            prepared = PreparedTerminalEgressV1(
                target_paths=target_paths,
                lowering_digest=proposal_lowering_digest(lowered.changed_members),
                item_paths=tuple(
                    sorted(item_paths.items(), key=lambda item: item[0].encode("utf-8"))
                ),
                unchanged=ordered_unchanged,
                line_proposal=line_proposal,
            )
            self._prepared[(request.admission_binding_digest, request.node_id)] = PreparedProposal(
                prepared=prepared,
                candidate_tree={},
                changed_members=(),
                item_paths=item_paths,
                rationale=rationale,
            )
            return prepared
        if not target_paths:
            # Every item restated what is already live: nothing to propose, no
            # mandate to bind. The terminal completes, every child unchanged.
            prepared = PreparedTerminalEgressV1(
                target_paths=(),
                lowering_digest=proposal_lowering_digest(()),
                item_paths=tuple(
                    sorted(item_paths.items(), key=lambda item: item[0].encode("utf-8"))
                ),
                unchanged=ordered_unchanged,
            )
            self._prepared[(request.admission_binding_digest, request.node_id)] = PreparedProposal(
                prepared=prepared,
                candidate_tree={},
                changed_members=(),
                item_paths=item_paths,
                rationale=rationale,
            )
            return prepared
        # A settle capped at propose binds the mandate a proposal would (a
        # propose grant first): it never uses a settle grant's settle authority.
        outside_scope: tuple[SettleScopeGap, ...] = ()
        if request.capped_by is None:
            selection = select_settle_mandate(
                request,
                admission=admission,
                accepted_mandates=self.accepted_mandates,
                target_paths=target_paths,
                base_tree=self.instance.immutable_tree_at(request.accepted_coordinate.git_oid),
                candidate_tree=lowered.proposed_tree,
                delegation=self.delegation,
            )
            mandate_digest: str | None = selection.digest
            outside_scope = selection.outside_scope
        else:
            mandate_digest = select_procedure_mandate(
                request,
                admission=admission,
                accepted_mandates=self.accepted_mandates,
                target_paths=target_paths,
                delegation=self.delegation,
            )
        prepared = PreparedTerminalEgressV1(
            target_paths=target_paths,
            procedure_mandate_digest=mandate_digest,
            lowering_digest=proposal_lowering_digest(lowered.changed_members),
            item_paths=tuple(sorted(item_paths.items(), key=lambda item: item[0].encode("utf-8"))),
            unchanged=ordered_unchanged,
            line_proposal=line_proposal,
        )
        # Lowering already read the base tree and reported exactly which member
        # paths moved; the door is handed those paths rather than a second
        # full-tree read whose only purpose would be to diff them out again.
        self._prepared[(request.admission_binding_digest, request.node_id)] = PreparedProposal(
            prepared=prepared,
            candidate_tree=lowered.proposed_tree,
            changed_members=lowered.changed_members,
            item_paths=item_paths,
            rationale=rationale,
            outside_scope=outside_scope,
            creations=tuple(item.record for item in lowered.subject_creations),
            item_reasons=proposal_item_reasons(
                request, items=items, item_paths=item_paths, unchanged=unchanged
            ),
        )
        return prepared

    def prepare_recompilation(
        self,
        *,
        request: TerminalEgressRequestV2,
        admission: ProcedureRunAdmission,
        evidence: Mapping[str, str],
        source_proposal_id: str,
    ) -> PreparedProposal:
        """Lower a stale proposal's retained terminal at a new coordinate, without delivery.

        The lowering is recovery's normal one, but nothing decided at the old
        head is replayed: a Line's open-proposal question is decided at the head
        (the source is the open proposal its successor replaces), so a change
        that is partly live by now lowers to what is left of it.
        """
        self.prepare_terminal_egress(
            request=request,
            admission=admission,
            evidence=evidence,
            recompiles=source_proposal_id,
        )
        return self._prepared[(request.admission_binding_digest, request.node_id)]

    def after_recompiled(
        self,
        request: TerminalEgressRequestV2,
        prepared: PreparedProposal,
        *,
        successor_id: str | None,
    ) -> dict[str, ReobservationRefusalReason]:
        """Once a recompilation committed: what delivery would have recorded at the head.

        Each unchanged item's re-observation is recorded, as delivery does; the
        ones the record refused are returned by item key, for the
        recompilation's result to report. A
        Line's open proposal moves to the successor, so the Line's next run
        finds its change still current rather than opening a duplicate; a source
        closed as already live leaves the record naming a closed proposal, which
        the next run reads as no open proposal.
        """

        from cruxible_core.exhaust.line_dispatch import LineDispatchStore, LineOpenProposal

        refused = self._record_reobservations(request, prepared.prepared.unchanged)
        decision = prepared.prepared.line_proposal
        if successor_id is None or decision is None or decision.decision != "supersede":
            return refused
        LineDispatchStore(self.instance).record_open_proposal(
            LineOpenProposal(
                line_id=decision.line_id,
                node_id=request.node_id,
                proposal_id=successor_id,
                signature=decision.signature,
                superseded=decision.open_proposal_id,
            ),
            actor=request.actor_context,
            now=datetime.now(timezone.utc),
        )
        return refused

    def _line_proposal(
        self,
        request: TerminalEgressRequestV1,
        *,
        admission: ProcedureRunAdmission,
        lowered: LoweredAuthoring,
        journaled: PreparedTerminalEgressV1 | None,
        recompiles: str | None = None,
    ) -> LineProposalPreparationV1 | None:
        """A Line run's decision about its Line's open proposal; ``None`` off a Line.

        One keyed read of the Line's open-proposal record and one of that
        proposal's status. Recovery reproduces the journaled decision, after
        checking this lowering still carries the change it was made about.
        Recompiling the stale proposal ``recompiles`` decides again at the head
        and never keeps the source: when the Line's record names it, the
        successor supersedes it (``after_recompiled`` moves the record); else
        the successor is proposed without becoming the Line's open proposal.
        """

        from cruxible_client.contracts.procedures.line_specs import line_identity_digest
        from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
        from cruxible_core.service.proposals.proposals import proposal_still_open

        identity = admission.line_identity
        if admission.invocation_origin != "line" or identity is None:
            return None
        line_id = line_identity_digest(identity)
        signature = line_change_signature(self.instance, lowered)
        if recompiles is not None:
            record = (
                LineDispatchStore(self.instance).open_proposal(line_id, request.node_id)
                if dispatch_root(self.instance).is_dir()
                else None
            )
            if record is not None and record.proposal_id == recompiles:
                return LineProposalPreparationV1(
                    line_id=line_id,
                    signature=signature,
                    decision="supersede",
                    open_proposal_id=recompiles,
                )
            return LineProposalPreparationV1(
                line_id=line_id, signature=signature, decision="propose"
            )
        if journaled is not None:
            decided = journaled.line_proposal
            if decided is not None and (
                decided.line_id != line_id or decided.signature != signature
            ):
                raise ProposalDeliveryRefused(
                    "effectful_operation_payload_mismatch",
                    "Recovery re-derived another change than the one its journaled "
                    "open-proposal decision was made about.",
                    details={"journaled_signature": decided.signature, "signature": signature},
                )
            return decided
        record = (
            LineDispatchStore(self.instance).open_proposal(line_id, request.node_id)
            if dispatch_root(self.instance).is_dir()
            else None
        )
        if record is None or not proposal_still_open(self.instance, record.proposal_id):
            return LineProposalPreparationV1(
                line_id=line_id, signature=signature, decision="propose"
            )
        return LineProposalPreparationV1(
            line_id=line_id,
            signature=signature,
            decision="still_current" if record.signature == signature else "supersede",
            open_proposal_id=record.proposal_id,
        )

    def _supersede_open_proposal(
        self,
        request: TerminalEgressRequestV2,
        decision: LineProposalPreparationV1,
        receipt: TerminalEgressReceiptV3,
    ) -> TerminalEgressReceiptV3:
        """After a Line's proposal lands: supersede the open one, then record this one.

        The open proposal is withdrawn as superseded by this one, under the
        run's own identity, unless it settled first (then nothing is
        withdrawn). Both steps are idempotent, so a redelivery repeats them.
        """

        from cruxible_core.exhaust.line_dispatch import LineDispatchStore, LineOpenProposal
        from cruxible_core.service.proposals.proposals import service_supersede_line_proposal

        now = datetime.now(timezone.utc)
        superseded: str | None = None
        if decision.decision == "supersede":
            assert decision.open_proposal_id is not None  # the decision's shape
            if decision.open_proposal_id != receipt.proposal_id and (
                service_supersede_line_proposal(
                    self.instance,
                    proposal_id=decision.open_proposal_id,
                    successor_id=receipt.proposal_id,
                    actor_id=request.actor_context.actor_id,
                    withdrawn_at=str(format_datetime(now)),
                )
            ):
                superseded = decision.open_proposal_id
        LineDispatchStore(self.instance).record_open_proposal(
            LineOpenProposal(
                line_id=decision.line_id,
                node_id=request.node_id,
                proposal_id=receipt.proposal_id,
                signature=decision.signature,
                superseded=superseded,
            ),
            actor=request.actor_context,
            now=now,
        )
        if superseded is None:
            return receipt
        return receipt.model_copy(update={"superseded_proposal_id": superseded})

    def _subject_paths(
        self,
        request: TerminalEgressRequestV1,
        *,
        base: AcceptedProjectionCoordinate,
        base_tree: SnapshotTree,
        members: tuple[AuthoringChangeSetMember, ...],
    ) -> dict[str, tuple[str, bool]]:
        """Where each Subject item lands, as shared lowering names it over all ``members``."""

        if not any(isinstance(member, SubjectCreationAuthoringPayload) for member in members):
            return {}
        try:
            return subject_creation_paths(
                self.instance,
                operation=_intent_id(request),
                base=base,
                base_tree=base_tree,
                members=members,
            )
        except AuthoringLoweringError as exc:
            raise ProposalDeliveryRefused(
                "proposal_lowering_refused",
                f"Shared lowering refused the proposed Subjects: {exc.message}",
                details={
                    "code": exc.code,
                    "offending_element": exc.offending_element,
                    "repairs": [item.model_dump(mode="json") for item in exc.repairs],
                },
            ) from exc

    # -- delivery -----------------------------------------------------------

    def deliver_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
        admission: ProcedureRunAdmission | None = None,
    ) -> TerminalEgressReceiptV1:
        # Submission and settlement share proofs for the duration of delivery,
        # including inherited Captures from runs other than the current run.
        with self.instance.capture_verification_request():
            return self._deliver_terminal_egress(request=request, admission=admission)

    def _deliver_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
        admission: ProcedureRunAdmission | None = None,
    ) -> TerminalEgressReceiptV1:
        prepared_nothing = self._prepared.get((request.admission_binding_digest, request.node_id))
        if (
            not isinstance(request, TerminalEgressRequestV2)
            and request.kind == "claims"
            and prepared_nothing is not None
            and prepared_nothing.prepared.changes_nothing
        ):
            # Every item restated what is already live: no proposal, no
            # authority exercised. The re-observations are recorded, and every
            # child is reported unchanged.
            refused = self._record_reobservations(request, prepared_nothing.prepared.unchanged)
            return TerminalEgressUnchangedReceiptV1(
                kind=request.kind,
                run_id=request.run_id,
                node_id=request.node_id,
                children=_with_refused_reobservations(prepared_nothing.prepared.unchanged, refused),
            )
        if (
            not isinstance(request, TerminalEgressRequestV2)
            and request.capped_by is not None
            and prepared_nothing is not None
            and prepared_nothing.prepared.keeps_open_proposal
        ):
            # The change is the Line's open proposal's: no change, the open
            # proposal is still current. Unchanged items still re-observe.
            kept = prepared_nothing.prepared
            assert kept.line_proposal is not None and kept.line_proposal.open_proposal_id
            refused = self._record_reobservations(request, kept.unchanged)
            unchanged_children = {
                child.item_key: child
                for child in _with_refused_reobservations(kept.unchanged, refused)
            }
            return TerminalEgressStillCurrentReceiptV1(
                kind=request.kind,
                run_id=request.run_id,
                node_id=request.node_id,
                open_proposal_id=kept.line_proposal.open_proposal_id,
                children=tuple(
                    unchanged_children.get(item.item_key)
                    or TerminalEgressChildReceiptV1(
                        child_index=item.child_index,
                        item_key=item.item_key,
                        egress_digest=item.manifest_digest,
                    )
                    for item in request.items
                ),
            )
        if not isinstance(request, TerminalEgressRequestV2) or request.kind != "claims":
            raise ProposalDeliveryRefused(
                "proposal_item_invalid",
                "The proposal sink delivers prepared v2 proposal and settle egress only.",
                details={"kind": request.kind},
            )
        key = (request.admission_binding_digest, request.node_id)
        prepared = self._prepared.get(key)
        if prepared is None:
            raise ProposalDeliveryRefused(
                "proposal_receipt_incomplete",
                "The proposal sink was asked to deliver an egress it never prepared.",
                details={"node_id": request.node_id},
            )
        if prepared.prepared.target_paths != request.target_paths:
            raise ProposalDeliveryRefused(
                "proposal_target_paths_mismatch",
                "The egress request names other targets than its own preparation.",
                details={
                    "prepared_target_paths": list(prepared.prepared.target_paths),
                    "request_target_paths": list(request.target_paths),
                },
            )
        if admission is None:
            raise ProposalDeliveryRefused(
                "procedure_authority_admission_invalid",
                "Proposal delivery requires the exact admitted run.",
            )
        proof_evidence = self._absence_evidence.get(key)
        if proof_evidence is not None:
            _validate_absence_bindings(self.instance, request, admission, proof_evidence)
        elif any(receipt.absence is not None for receipt in request.claims_evidence.values()):
            raise ProposalDeliveryRefused(
                "claims_absence_binding_invalid",
                "No prepared run binding exists for this absence witness",
            )
        refused = self._record_reobservations(request, prepared.prepared.unchanged)
        if prepared.creations:
            # The operation's own core record, which the candidate boundary reads
            # back to verify each Subject's recorded assignment (review F-001).
            self.instance.subject_assignment_authority().record_creations(
                _intent_id(request), prepared.creations
            )
        service = self._proposal_service()
        assert request.operation_key is not None  # v2 shape
        existing = self.recover_existing(
            request,
            service=service,
            lowering_digest=prepared.prepared.lowering_digest,
        )
        # Before the proposal exists (or again, idempotently, on recovery): an
        # admitted proposal never lacks its items' reasons.
        self._record_item_reasons(request, prepared)
        adapter = ProposalTerminalAdapter(
            service=service, bind_projection=self.instance.bind_accepted_projection
        )
        receipt: TerminalEgressReceiptV1 = self._deliver_settle(
            request,
            admission=admission,
            prepared=prepared,
            adapter=adapter,
            existing=existing,
        )
        # The exact admission this delivery submitted carries this operation's
        # reasons: linked before the run records its receipt, so a crash here
        # is redelivered through recovery and linked then. An admission
        # recovery adopted by its ref that does not name this exact operation
        # (an independent equivalent submission) is delivered as before but
        # never linked: its reasons are reported unattributed on the receipt.
        line_proposal = prepared.prepared.line_proposal
        if (
            line_proposal is not None
            and isinstance(receipt, TerminalEgressReceiptV3)
            and request.capped_by is not None
        ):
            receipt = self._supersede_open_proposal(request, line_proposal, receipt)
        proposal_id = getattr(receipt, "proposal_id", None)
        if prepared.item_reasons and isinstance(proposal_id, str):
            evidence = self.instance.proposal_evidence()
            operation = _intent_id(request)
            if evidence.read_admission(proposal_id).procedure_operation == operation:
                evidence.write_item_reason_link(proposal_id, operation)
            elif isinstance(receipt, TerminalEgressReceiptV3):
                receipt = receipt.model_copy(update={"item_reasons_unattributed": True})
        if refused and isinstance(receipt, TerminalEgressReceiptV3):
            receipt = receipt.model_copy(
                update={"children": _with_refused_reobservations(receipt.children, refused)}
            )
        return receipt

    def _record_item_reasons(
        self, request: TerminalEgressRequestV2, prepared: PreparedProposal
    ) -> None:
        """Record each changed item's reason under the immutable Procedure operation."""

        if prepared.item_reasons:
            self.instance.proposal_evidence().write_item_reasons(prepared.item_reasons)

    def _record_reobservations(
        self,
        request: TerminalEgressRequestV1,
        unchanged: tuple[UnchangedChildReceiptAny, ...],
    ) -> dict[str, ReobservationRefusalReason]:
        """Record each unchanged item's re-observation of content its Claim cites.

        The store admits each one at the current head under its append lock
        (``ClaimReobservationStore.record``). One it refuses there -- the Claim
        retired, or no longer citing that contract and content, since the run's
        base; or the very Capture the Claim already cites -- is not recorded.
        The terminal still completes, and its receipt still names the child as
        it was at the admitted base, with the reason its re-observation was
        refused (returned here by item key), so the run never counts it
        re-observed.
        """

        refused: dict[str, ReobservationRefusalReason] = {}
        if not unchanged:
            return refused
        store = ClaimReobservationStore(self.instance)
        cited_envelopes: dict[str, CaptureEnvelopeAny] = {}
        verified_witnesses: dict[object, None] = {}
        for child in unchanged:
            if not isinstance(child, TerminalEgressChildReceiptV3):
                continue  # a reused Subject observes nothing
            claim_id = PurePosixPath(child.path).stem
            try:
                store.record(
                    claim_id=claim_id,
                    capture_digest=child.reobserving_capture_digest,
                    actor_id=request.actor_context.actor_id,
                    extraction=request.claims_evidence.get(child.item_key),
                    cited_envelopes=cited_envelopes,
                    verified_witnesses=verified_witnesses,
                    extracted_value=(
                        change.value
                        if isinstance(
                            (change := request.claims_changes.get(child.item_key)),
                            CoreReobservationChange,
                        )
                        else None
                    ),
                )
            except ClaimReobservationRefused as exc:
                refused[child.item_key] = exc.reason
        return refused

    def recover_existing(
        self,
        request: TerminalEgressRequestV2,
        *,
        service: ProposalService,
        lowering_digest: str,
    ) -> ProposalResult | None:
        """Recover by immutable operation admission; refs only protect unfinished writes."""
        assert request.operation_key is not None
        actor_id = request.actor_context.actor_id
        ref = proposal_terminal_ref(actor_id, request.operation_key)
        prepared = self._prepared.get((request.admission_binding_digest, request.node_id))
        if prepared is None:  # pragma: no cover - deliver() prepares before recovering
            return None
        evidence = self.instance.proposal_evidence()
        assert evidence.index is not None
        rows = evidence.index.rows(evidence, "target_ref=? AND admission_path IS NOT NULL", (ref,))
        if len(rows) > 1:
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key names more than one durable proposal.",
            )
        if not rows:
            ref_oid = service.transport.read_proposal_ref(ref)
            if ref_oid is not None:
                self._require_same_member_bytes(
                    prepared,
                    lowering_digest=lowering_digest,
                    tree=service.transport.read_tree(ref_oid),
                    details={"target_ref": ref, "ref_oid": ref_oid, "publication": "interrupted"},
                )
            return None
        admission_record = evidence.read_admission(rows[0]["proposal_id"])
        evaluation = evidence.read_evaluation(admission_record.proposal_id)
        candidate = (
            None
            if evaluation.candidate_digest is None
            else evidence.read_candidate(evaluation.candidate_digest)
        )
        if (
            admission_record.proposed_base_oid != request.accepted_coordinate.git_oid
            or evaluation.evaluated_tree_oid is None
            or candidate is None
        ):
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key already names a proposal at another base or with no candidate.",
                details={
                    "proposal_id": admission_record.proposal_id,
                    "proposed_base_oid": admission_record.proposed_base_oid,
                    "admitted_base_oid": request.accepted_coordinate.git_oid,
                },
            )
        if (
            proposal_lowering_digest(prepared.changed_members) != lowering_digest
            or admission_record.source_compilation_digest
            != proposal_terminal_payload_digest(
                prepared.candidate_tree,
                prepared.prepared.target_paths,
                settle=admission_record.settle_submission,
            )
            or (admission_record.settle_submission is not None) != (request.kind == "claims")
            or (
                admission_record.settle_submission is not None
                and admission_record.settle_submission.mandate_digest
                != (request.procedure_mandate_digest or request.ephemeral_spec_digest)
            )
        ):
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key already names a proposal carrying another authored payload.",
                details={"proposal_id": admission_record.proposal_id},
            )
        return ProposalResult(
            admission=admission_record,
            evaluation=evaluation,
            candidate=candidate,
        )

    # -- settlement ---------------------------------------------------------

    @timed_operation("settle")
    def _deliver_settle(
        self,
        request: TerminalEgressRequestV2,
        *,
        admission: ProcedureRunAdmission,
        prepared: PreparedProposal,
        adapter: ProposalTerminalAdapter,
        existing: ProposalResult | None,
    ) -> TerminalEgressReceiptV1:
        """Settle under the one covering mandate, or fall back exactly as it declares.

        The condition is checked before anything is written, so a failing
        condition never leaves a refused proposal under the operation key. The
        submission mode is retained on the proposal's admission, so a repeated
        or recovered delivery reports what was actually submitted -- a fallback
        stays a fallback even after it is accepted through ordinary review.

        A settle the run's authority caps at propose takes the same fallback and
        never uses a settle grant's settle authority: it proposes under the
        mandate the proposal bound, for `cruxible.settle.authority_capped_by_<term>`.
        So does a change carrying ClaimTypes the bound grant's scope leaves out:
        it proposes under that grant, for `cruxible.settle.outside_mandate_scope`,
        naming them, without consulting the grant's condition.
        """

        digest = request.procedure_mandate_digest
        ephemeral_digest = request.ephemeral_spec_digest
        # Ruling C4: a direct run's settle terminal always arrives here capped
        # at propose (below), so it never needs a real accepted mandate --
        # its ephemeral spec digest stands in as the submission's authority
        # basis instead.
        authority_digest = digest if digest is not None else ephemeral_digest
        assert authority_digest is not None  # an effectful request binds one or the other
        mandate: ProcedureMandate | None = None
        if request.capped_by is None:
            assert digest is not None  # an uncapped settle always binds an accepted mandate
            found = self.accepted_mandates.get(digest)
            if not isinstance(found, ProcedureMandate) or found.grants != "settle":
                raise ProposalDeliveryRefused(
                    "settle_mandate_missing",
                    "The settle terminal's bound mandate is no longer an accepted settle grant.",
                )
            mandate = found
        result = existing
        # Only a submission this request makes itself hands its records to the
        # activation below; a recovered one is activated from what is stored.
        # This run's own journal receipts are stable within the request (maintainer
        # ruling settle-reuses-first-evaluation-1015).
        handoff = (
            SettlementHandoff(self.instance.prepared_evaluations, stable_run=request.run_id)
            if existing is None
            else None
        )
        if result is None:
            if request.capped_by is not None:
                submission = ProposalSettleSubmission(
                    mode="fallback",
                    mandate_digest=authority_digest,
                    fallback_reason=settle_authority_capped_reason(request.capped_by),
                )
            elif prepared.outside_scope:
                assert digest is not None  # reached only under a real accepted mandate
                submission = ProposalSettleSubmission(
                    mode="fallback",
                    mandate_digest=digest,
                    fallback_reason=SETTLE_OUTSIDE_MANDATE_SCOPE,
                    outside_scope=prepared.outside_scope,
                )
            else:
                assert mandate is not None  # checked above
                assert digest is not None  # reached only under a real accepted mandate
                head = self.instance.accepted_coordinate()
                timestamp = canonical_candidate_timestamp(request.evaluation_time)
                # Each facts build the condition consumes, bound to the mutable
                # inputs (bodies, re-observations) it was built on; None for one
                # no binding names.
                consumed: list[FactsObservation | None] = []

                def condition_facts(
                    predicates: tuple[str, ...], subject_kinds: tuple[str, ...]
                ) -> ClaimQueryFactsV1:
                    value, observation = observed_facts_build(
                        partial(self.instance._accepted_query_facts, self.instance),
                        head,
                        predicates=predicates,
                        subject_kinds=subject_kinds,
                        inputs=self.instance.query_fact_inputs,
                    )
                    consumed.append(observation)
                    return cast("ClaimQueryFactsV1", value)

                issues = delegated_authority_issues(
                    mandate_digest=digest,
                    scope=request.target_paths,
                    current_tree=self.instance.immutable_tree_at(head.git_oid),
                    candidate_tree=prepared.candidate_tree,
                    current=head,
                    timestamp=timestamp,
                    facts=condition_facts,
                    mandate_name=mandate.identity.name,
                )
                bound = tuple(item for item in consumed if item is not None)
                if handoff is not None and len(bound) == len(consumed):
                    # Admission evaluates this condition again at the same head;
                    # on exactly these inputs, while these fact builds stand, it
                    # takes this answer.
                    handoff.settle_condition = DelegatedAuthorityVerdict.of(
                        mandate_digest=digest,
                        current=head,
                        timestamp=timestamp,
                        scope=request.target_paths,
                        candidate_tree=prepared.candidate_tree,
                        facts=bound,
                        fact_inputs=self.instance.query_fact_inputs,
                        issues=issues,
                    )
                codes = sorted({code for code, _message in issues})
                # A grant without a condition has no refuse fallback: its scope gaps
                # were decided above, so only a moved head can still fail it.
                fallback = "propose" if mandate.condition is None else mandate.condition.fallback
                if issues and fallback == "refuse":
                    raise ProposalDeliveryRefused(
                        "settle_condition_refused",
                        "The settle mandate does not authorize this change and declares no "
                        "proposal fallback.",
                        details={"codes": codes, "messages": [m for _code, m in issues]},
                    )
                submission = (
                    ProposalSettleSubmission(
                        mode="fallback", mandate_digest=digest, fallback_reason=", ".join(codes)
                    )
                    if issues
                    else ProposalSettleSubmission(mode="delegated", mandate_digest=digest)
                )
            result = adapter.submit(
                request=request,
                admission=admission,
                candidate_tree=prepared.candidate_tree,
                accepted_mandates=self.accepted_mandates,
                rationale=prepared.rationale,
                changed_paths=prepared.prepared.target_paths,
                delegation=self.delegation,
                settle_submission=submission,
                handoff=handoff,
            )
        from cruxible_core.service.proposals.recompile import recompiled_successor

        result = recompiled_successor(self.instance, result)
        settle = result.admission.settle_submission
        assert settle is not None  # submitted above, or verified by recover_existing
        if settle.mode == "fallback":
            return settle_terminal_receipt(
                request,
                result=result,
                item_paths=prepared.item_paths,
                accepted_git_oid=None,
                fallback_reason=settle.fallback_reason,
                outside_scope=settle.outside_scope,
                unchanged={child.item_key: child for child in prepared.prepared.unchanged},
            )
        if mandate is None:
            # A capped settle only ever submits its fallback; a delegated
            # proposal under its operation key is another operation's.
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key names a delegated settlement, not this capped proposal.",
                details={"proposal_id": result.admission.proposal_id},
            )
        assert digest is not None  # a delegated settlement always binds a real mandate
        if result.candidate is None:
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "The delegated candidate was refused at submission.",
                details={
                    "proposal_id": result.admission.proposal_id,
                    "codes": [item.code for item in result.evaluation.diagnostics],
                },
            )
        accepted_oid = self._accepted_oid(result.candidate.candidate_digest, digest=digest)
        if accepted_oid is None:
            accepted_oid, result = self._activate(
                result, request=request, mandate=mandate, digest=digest, handoff=handoff
            )
        return settle_terminal_receipt(
            request,
            result=result,
            item_paths=prepared.item_paths,
            accepted_git_oid=accepted_oid,
            unchanged={child.item_key: child for child in prepared.prepared.unchanged},
        )

    def _accepted_oid(self, candidate_digest: str, *, digest: str) -> str | None:
        """The accepted generation that settled this exact candidate under this mandate.

        Found through the history index, never a history walk; its retained
        record must name the same mandate, or it is not this settlement.
        """

        with self.instance.accepted_history_reader() as history:
            location = history.generation_for_candidate(candidate_digest)
            if location is None:
                return None
            facts = history.read_generation_facts(
                location.sequence, self.instance.retained_record_reader()
            )
        if facts.mandate_digest != digest:
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The delegated candidate was accepted, but not under this settle mandate.",
                details={"accepted_git_oid": location.git_oid},
            )
        return location.git_oid

    def _activate(
        self,
        result: ProposalResult,
        *,
        request: TerminalEgressRequestV2,
        mandate: ProcedureMandate,
        digest: str,
        handoff: SettlementHandoff | None = None,
    ) -> tuple[str, ProposalResult]:
        from cruxible_client.contracts.errors import ProposalStale, SettlementIntegrityError
        from cruxible_client.contracts.recompilation import ProposalRecompiled
        from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

        # No late acceptance: the mandate must still stand when the change lands.
        now = datetime.now(timezone.utc)
        if mandate.suspended or not (mandate.valid_from <= now < mandate.expires_at):
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "The settle mandate expired or was suspended before publication.",
            )
        try:
            receipt = service_activate_playbill_proposal(
                self.instance,
                proposal_id=result.admission.proposal_id,
                activated_by=request.actor_context.actor_id,
                mandate_digest=digest,
                handoff=handoff,
            )
        except ProposalStale:
            # Accepted state moved past the candidate before it published.
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "Accepted state moved before the settlement published.",
                details={"proposal_id": result.admission.proposal_id, "status": "stale"},
            ) from None
        except SettlementIntegrityError:
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "The delegated candidate no longer reproduces under its mandate at publication.",
                details={"proposal_id": result.admission.proposal_id},
            ) from None
        if (
            isinstance(receipt, ProposalRecompiled)
            or receipt.status != "accepted"
            or receipt.accepted_coordinate is None
        ):
            raise ProposalDeliveryRefused(
                "settle_publication_refused",
                "Accepted state moved before the settlement published.",
                details={"proposal_id": result.admission.proposal_id, "status": receipt.status},
            )
        if receipt.proposal_id != result.admission.proposal_id:
            from cruxible_core.service.proposals.proposals import _proposal_result

            result = _proposal_result(self.instance, receipt.proposal_id)
        return receipt.accepted_coordinate.git_oid, result

    @staticmethod
    def _require_same_member_bytes(
        prepared: PreparedProposal,
        *,
        lowering_digest: str,
        tree: Mapping[str, bytes],
        details: dict[str, object],
    ) -> None:
        """Refuse unless `tree` carries exactly the bytes THIS preparation lowered.

        The members themselves are compared, not the journaled digest alone: a
        journal can only say what an earlier attempt claimed to lower, while the
        tree says what the ref actually holds.
        """

        current_digest = proposal_lowering_digest(prepared.changed_members)
        existing_digest = proposal_lowering_digest(
            tuple((path, tree[path]) for path, _content in prepared.changed_members if path in tree)
        )
        if (
            existing_digest != current_digest
            or current_digest != lowering_digest
            or any(path not in tree for path, _content in prepared.changed_members)
        ):
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "The operation key already names a proposal carrying other member bytes.",
                details=details,
            )


__all__ = [
    "PROPOSAL_CLAIM_ID_DOMAIN",
    "PROPOSAL_LOWERING_DOMAIN",
    "PreparedProposal",
    "ProposalTerminalEgressSink",
    "SettleMandateSelection",
    "evidence_by_item",
    "proposal_claim_id",
    "proposal_items",
    "proposal_lowering_digest",
    "select_procedure_mandate",
    "select_settle_mandate",
]
