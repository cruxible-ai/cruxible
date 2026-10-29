"""Lower ergonomic authoring payloads onto ordinary governed artifact wires."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, NoReturn, TypeAlias, TypeVar, cast

from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.accepted_attestations import attestation_path
from cruxible_client.contracts.acquisition_policies import (
    ACQUISITION_POLICY_PIN_ROLE,
    acquisition_policy_digest,
    acquisition_policy_path,
    parse_acquisition_policy,
    render_acquisition_policy,
)
from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_IDENTITY,
    APPROVAL_POLICY_PATH,
    approval_policy_digest,
    render_approval_policy,
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
    ArtifactRef,
)
from cruxible_client.contracts.authoring.inputs import (
    AuthoringInputError,
    refuse_unserved_procedure_nodes,
)
from cruxible_client.contracts.authoring.models import (
    MAX_REPAIR_BYTES,
    ApprovalPolicyAuthoringPayload,
    AttestationAuthoringPayload,
    AuthoringArtifactReference,
    AuthoringCandidateReference,
    AuthoringChangeSetMember,
    AuthoringClaimStatement,
    AuthoringExactContentObject,
    AuthoringIntentV1,
    AuthoringNoneClaimObject,
    AuthoringTriggerSchedule,
    CaptureContractAuthoringPayload,
    CaptureLandingByNameSchedule,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    ClaimRetirementMember,
    ClaimTypeAuthoringPayload,
    ClaimTypeSuccessionDependent,
    ClaimTypeSuccessionMember,
    ContractAuthoringPayload,
    ExistingCaptureCitationSource,
    LineAuthoringPayload,
    ProcedureAuthoringPayload,
    ProcedureMandateAuthoringPayload,
    ProcedureRuntimePolicyAuthoringPayload,
    QueryDefinitionAuthoringPayload,
    RepairAlternative,
    ResolutionContractAuthoringPayload,
    SelfSourceBody,
    SourceAcquisitionPolicyAuthoringPayload,
    SubjectAuthoringPayload,
    SubjectCreationAuthoringPayload,
    SubjectKindAuthoringPayload,
    TriggerAuthoringPayload,
    WindowCloseAuthoringSchedule,
    WorkingSelectionObservation,
    authoring_member_identity,
)
from cruxible_client.contracts.builtin_claim_types import (
    DESCRIPTOR_PREDICATES,
    admits_object_subject,
)
from cruxible_client.contracts.canonical import (
    Sha256Value,
    artifact_path_for_codec,
    canonical_bytes,
    file_digest,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.canonical_forms import (
    CanonicalFormDidNotConverge,
    canonicalize_literal,
)
from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1,
    AcceptedCaptureContract,
    CaptureBuildResult,
    CaptureContractAny,
    CaptureEnvelopeAny,
    CaptureFormatError,
    CaptureObjectStoreProtocol,
    CaptureProductionUnproven,
    DirectCaptureBuildResult,
    ProducerReceiptResolverProtocol,
    VerifiedCaptureMemo,
    build_coordinator_self_source_capture,
    build_working_selection_capture,
    builtin_contract_for_write,
    capture_contract_digest,
    capture_contract_path,
    classify_capture_reuse,
    foreign_source_capture_contract,
    foreign_source_capture_contract_v1,
    observed_content,
    parse_capture_contract,
    parse_capture_envelope,
    render_capture_contract,
    verify_capture,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    effective_canonical_form,
    effective_revision_evidence,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    CitationOrigin,
    CitationRole,
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimBacking,
    ClaimCitation,
    ClaimFormatError,
    ClaimReferentContext,
    ClaimRetireDependent,
    ClaimStatement,
    ExactContentClaimObject,
    LiteralClaimObject,
    NoneClaimObject,
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
    projection_window_cited,
    render_claim,
    resolve_cited_source_window,
)
from cruxible_client.contracts.content_memo import request_derive, seal
from cruxible_client.contracts.declared_blocks import (
    projection_window_intersecting,
    stamped_projection_windows,
)
from cruxible_client.contracts.documents import document_path
from cruxible_client.contracts.errors import CruxibleError, FormatError
from cruxible_client.contracts.named_references import name_line, name_trigger
from cruxible_client.contracts.procedure_mandates import (
    MANDATE_CHANGE_KIND_ORDER,
    MandateClaimScope,
    MandateCondition,
    ProcedureMandate,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_IDENTITY,
    PROCEDURE_RUNTIME_POLICY_PATH,
    procedure_runtime_policy_digest,
    render_procedure_runtime_policy,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    parse_procedure,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.blueprints import (
    blueprint_path,
)
from cruxible_client.contracts.procedures.contracts import (
    OwnedProcedureContractValidator,
    ProcedureContractValidationError,
)
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest,
)
from cruxible_client.contracts.procedures.line_specs import (
    RUNG_AUTHORITY,
    LineSpec,
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    ExhaustTapNode,
    ProcedureDefinition,
    SourceNode,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.names import name_procedure
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelector,
    CaptureEventWindow,
    FixedWindow,
)
from cruxible_client.contracts.provider_interfaces import (
    parse_provider_interface,
    provider_interface_path,
)
from cruxible_client.contracts.providers import (
    ProviderV2,
    parse_provider,
    provider_digest,
    provider_path,
)
from cruxible_client.contracts.query.definitions import (
    CLAIM_TYPE_PIN_ROLE,
    QueryDefinition,
    parse_query_definition,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.resolution_contracts import resolution_contract_path
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.shapes import (
    ContractRef,
    contract_digest,
    contract_path,
    contract_path_for_identity,
    parse_contract,
    type_refs,
)
from cruxible_client.contracts.source_references import (
    ExternalSourceReference,
    LedgerSourceReference,
)
from cruxible_client.contracts.subject_kinds import (
    SubjectIdUnassignable,
    SubjectKeyValue,
    SubjectKind,
    SubjectKindKeys,
    SubjectKindMinted,
    distinct_holder_id,
    key_reference_mapping,
    key_value_problem,
    live_subject_kind,
    minted_subject_id,
    parse_subject_kind,
    render_subject_kind,
    subject_kind_digest,
    subject_kind_path,
)
from cruxible_client.contracts.subjects import (
    SubjectAssignment,
    SubjectShell,
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_client.contracts.triggers import (
    TRIGGER_LINE_REF_ROLE,
    ActionTarget,
    CaptureEventInput,
    CaptureLandingSchedule,
    LineTarget,
    Trigger,
    TriggerSchedule,
    TriggerTarget,
    WindowCloseSchedule,
    parse_trigger,
    render_trigger,
    schedule_capture_selector,
    schedule_satisfies_input,
    trigger_digest,
    trigger_path,
    trigger_schedule_pins,
)
from cruxible_core.authoring.registrations import source_projection_blocks
from cruxible_core.claims.claim_retirement import (
    ClaimRetireError,
    build_claim_retirement_candidate,
    claim_retirement_inventory,
)
from cruxible_core.claims.claim_type_inputs import identity_rules_supported
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    build_claim_type_migration_candidate,
    claim_type_migration_inventory,
    resolve_claim_type_succession,
)
from cruxible_core.compiler.compiler import (
    LIGHT_ARTIFACTS_COMPILER,
    artifact_codec_for_compiler,
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
from cruxible_core.compiler.projection_artifacts import (
    ParsedProjectionTree,
    parse_projection_tree,
)
from cruxible_core.derived.derived_state import CandidateTree, SnapshotTree, fork_tree
from cruxible_core.exhaust.promotions import exhaust_promotion_path
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.indexes.subject_aliases import (
    candidate_creation_collisions,
    resolve_reference_text,
    resolve_subject_ref,
    split_reference,
    subject_collision_message,
)
from cruxible_core.indexes.subject_identity import (
    SUBJECT_COMPILER_UPGRADE_REQUIRED,
    SUBJECT_KEY_MISSING,
    SUBJECT_KIND_UNDECLARED,
    IdentityOverlay,
    IdentityView,
    KeyTuple,
    RecordedCreation,
    SubjectAssignmentAuthority,
    SubjectKeyInvalid,
    admits_subject_kinds,
    assign_creation_id,
    boundary_identity_issues,
    canonical_key,
    compiler_upgrade_repair,
    key_ambiguous_message,
    key_encoding,
    key_order,
    key_values,
    live_key_claims,
    subject_creation_shape,
    subject_kind_of,
    tree_key_reader,
    undeclared_kind_repair,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.phase_timing import timed_phase
from cruxible_core.storage.capture_metadata import capture_envelope


@dataclass(eq=False)
class AuthoringLoweringError(ValueError):
    code: str
    offending_element: str
    message: str
    repairs: tuple[RepairAlternative, ...]

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AssignedCreation:
    """One creation member as lowering assigned it: its record, path and key tuple."""

    record: RecordedCreation
    path: str
    key: KeyTuple


@dataclass(frozen=True)
class LoweredAuthoring:
    proposed_tree: Mapping[str, bytes]
    resolved_authoring: dict[str, object]
    changed_members: tuple[tuple[str, bytes], ...]
    idempotent: bool = False
    member_by_path: Mapping[str, int] = field(default_factory=dict)
    subject_creations: tuple[AssignedCreation, ...] = ()
    """Every Subject creation member's actual assignment (empty unless a change
    set creates Subjects): what core records for the operation (review F-001)."""
    """Which change-set member authored each path this lowering wrote.

    A change set admits or refuses whole, typed to the offending member, and
    the compiler laws that run after lowering -- succession, permitted roles,
    cardinality, evidence admission -- address the artifact path they refused,
    not the member the author wrote. This is the map back. Empty for a singular
    intent, which owns every path it writes."""

    def __post_init__(self) -> None:
        # A lowered result may outlive the member's mutable candidate builder.
        # Seal only the tree; public explanatory containers retain their existing
        # ownership and are detached by prepared-result retention.
        if not isinstance(self.proposed_tree, SnapshotTree):
            object.__setattr__(self, "proposed_tree", fork_tree(self.proposed_tree).snapshot())


@dataclass(frozen=True)
class _TreeLedgerResolver:
    tree: Mapping[str, bytes]
    coordinate: AcceptedCoordinate

    def read_ledger_source(self, source: LedgerSourceReference) -> bytes:
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
        path = projection.citations.capture_contract_path(contract_digest)
    if path is None:
        # The Capture used a version that has since been succeeded. That version
        # is still its exact provenance, so reuse resolves it in accepted history.
        historical = instance.accepted_capture_contract_version(
            AcceptedCoordinate.from_internal(base), contract_digest
        )
        if historical is not None:
            return historical
        _refuse(
            "cruxible.authoring.capture_contract_unresolved",
            "source.capture_digest",
            "The existing Capture's exact CaptureContract is not accepted at this base.",
            repair_kind="replace_capture",
            repair_description=(
                "Choose a Capture whose exact contract is accepted at the intent base."
            ),
        )
    if not isinstance(path, str) or not isinstance(base_tree.get(path), bytes):
        _refuse(
            "cruxible.authoring.capture_contract_unresolved",
            "source.capture_digest",
            "The CaptureContract relation does not resolve an accepted artifact.",
            repair_kind="replace_capture",
            repair_description=(
                "Choose a Capture whose exact contract is accepted at the intent base."
            ),
        )
    contract = parse_capture_contract(base_tree[path], path=path)
    accepted = request_derive(
        ("core.accepted_capture_contract", path, base_tree[path]),
        lambda: AcceptedCaptureContract(
            path=path,
            contract=contract,
            artifact_digest=capture_contract_digest(contract).tagged,
        ),
    )
    if accepted.artifact_digest != contract_digest:
        _refuse(
            "cruxible.authoring.capture_contract_unresolved",
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


def _repair_bytes(kind: str, description: str, replacement: object | None) -> int:
    return len(
        canonical_bytes(
            {
                "kind": kind,
                "description": description,
                "replacement": None if replacement is None else normalize_canonical(replacement),
            }
        )
    )


def bounded_repair(kind: str, description: str, replacement: object | None) -> RepairAlternative:
    """A repair that fits the frozen repair-byte limit, however large its replacement.

    A replacement over the limit used to fail the repair's own validator, which
    turned a coded refusal into a generic `lowering_invalid` and dropped the
    very list the refusal existed to name. Instead the replacement's lists are
    cut, longest first, to the longest prefix that fits; each cut list keeps
    its full length as ``<name>_count`` and the replacement says ``truncated``.
    """

    if _repair_bytes(kind, description, replacement) <= MAX_REPAIR_BYTES:
        return RepairAlternative(kind=kind, description=description, replacement=replacement)
    if not isinstance(replacement, Mapping):
        return RepairAlternative(
            kind=kind,
            description=description,
            replacement={"truncated": True, "omitted": "the replacement exceeds the repair limit"},
        )
    full = dict(replacement)
    lists = {key: value for key, value in full.items() if isinstance(value, list | tuple)}
    trimmed: dict[str, object] = {
        **full,
        **{f"{key}_count": len(value) for key, value in lists.items()},
        "truncated": True,
    }
    for key in sorted(lists, key=lambda name: -len(canonical_bytes(list(lists[name])))):
        items = list(lists[key])
        low, high = 0, len(items)
        while low < high:
            middle = (low + high + 1) // 2
            if (
                _repair_bytes(kind, description, {**trimmed, key: items[:middle]})
                <= MAX_REPAIR_BYTES
            ):
                low = middle
            else:
                high = middle - 1
        trimmed[key] = items[:low]
        if _repair_bytes(kind, description, trimmed) <= MAX_REPAIR_BYTES:
            return RepairAlternative(kind=kind, description=description, replacement=trimmed)
    return RepairAlternative(
        kind=kind,
        description=description,
        replacement={"truncated": True, "omitted": "the replacement exceeds the repair limit"},
    )


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
        repairs=(bounded_repair(repair_kind, repair_description, replacement),),
    )


def _read_existing_capture(store: CaptureObjectStoreProtocol, digest: str) -> CaptureEnvelopeAny:
    """The cited Capture envelope, or the typed refusal for a missing or foreign body."""

    try:
        raw_envelope = store.read(
            digest,
            access=BodyAccessContext(principal_id="playbill-authoring", can_read_body=True),
        )
    except CruxibleError:
        _refuse(
            "cruxible.authoring.existing_capture_not_found",
            "source.capture_digest",
            "The existing Capture digest is not present in the daemon CAS.",
            repair_kind="replace_capture",
            repair_description="Use a Capture digest returned by a typed Claim/evidence read.",
        )
    try:
        return parse_capture_envelope(raw_envelope)
    except (CruxibleError, ValueError):
        _refuse(
            "cruxible.authoring.existing_capture_invalid",
            "source.capture_digest",
            "The referenced bytes are not a canonical Capture envelope.",
            repair_kind="replace_capture",
            repair_description="Use a Capture digest returned by a typed Claim/evidence read.",
        )


def _refuse_unproduced_capture(gap: CaptureProductionUnproven) -> NoReturn:
    """Refuse a cited Capture no daemon run is recorded as producing."""

    if gap.reason == "v1_envelope":
        _refuse(
            "cruxible.authoring.capture_v1_citation_refused",
            "source.capture_digest",
            "This is a version-1 Capture under a contract that is not self-asserted: it "
            "carries no producer receipt, so nothing proves a daemon run produced it, and "
            "it cannot be cited as new evidence. Claims that already cite it keep it.",
            repair_kind="replace_capture",
            repair_description=(
                "Re-run the Procedure or Line that produces this evidence and cite the "
                "Capture it records, or author the evidence as a working selection."
            ),
        )
    _refuse(
        "cruxible.authoring.capture_production_unproven",
        "source.capture_digest",
        "No daemon run is recorded producing this Capture: its digest is named by no "
        "produced_capture record or delivered emit_capture terminal. A Source read the "
        "acquisition rule did not select is never published, and bytes stored outside "
        "a run prove nothing.",
        repair_kind="replace_capture",
        repair_description=(
            "Cite a Capture a run produced (a run's produced Captures, or a Capture a "
            "typed evidence read returned), or author the evidence as a working selection."
        ),
    )


def _closure_difference(required: Iterable[str], supplied: Iterable[str]) -> dict[str, list[str]]:
    """What a closure still owes and what it names that it does not owe, byte-sorted."""

    required_set, supplied_set = set(required), set(supplied)
    return {
        "missing": sorted(required_set - supplied_set, key=lambda item: item.encode("utf-8")),
        "unexpected": sorted(supplied_set - required_set, key=lambda item: item.encode("utf-8")),
    }


def _observed_at(timestamp: str) -> datetime:
    raw = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    return datetime.fromisoformat(raw)


def _referent(
    tree: Mapping[str, bytes],
    address: SemanticAddress,
    *,
    descriptor: bool,
) -> tuple[ArtifactIdentity, str]:
    if address.selector.scheme != "artifact-v1":
        _refuse(
            "cruxible.authoring.referent_not_whole_artifact",
            "statement.subject",
            "Claim referents must name a whole accepted Subject artifact.",
            repair_kind="replace_subject",
            repair_description="Use the Subject's whole-artifact semantic address.",
        )
    content = tree.get(address.artifact_path)
    if content is None:
        _refuse(
            "cruxible.authoring.referent_not_found",
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
        "cruxible.authoring.referent_kind_not_admitted",
        "statement.subject",
        "The selected artifact kind is not admitted as this Claim's referent.",
        repair_kind="replace_subject",
        repair_description="Choose an accepted Subject of an admitted kind.",
    )


def _exact_object(
    instance: PlaybillInstance,
    value: object,
) -> LiteralClaimObject | SubjectClaimObject | ExactContentClaimObject | NoneClaimObject:
    if isinstance(value, AuthoringNoneClaimObject):
        return NoneClaimObject(of=value.of)
    if not isinstance(value, AuthoringExactContentObject):
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


_PayloadV1T = TypeVar("_PayloadV1T", bound=ClaimAuthoringPayloadV1)


def _live_subject_in(tree: Mapping[str, bytes], path: str) -> bool:
    """Whether ``tree`` holds a live Subject at ``path``: it stands as written.

    A retired Subject does not: a reference to it resolves through the aliases
    first, so after a merge it reaches the surviving Subject.
    """

    content = tree.get(path)
    if content is None:
        return False
    try:
        return parse_subject(content, path=path).lifecycle.state == "live"
    except (CruxibleError, ValueError):
        return True  # malformed bytes are the referent laws' to refuse


def _with_aliases_resolved(
    instance: PlaybillInstance,
    base: AcceptedProjectionCoordinate,
    tree: Mapping[str, bytes],
    payload: _PayloadV1T,
) -> _PayloadV1T:
    """Resolve a Subject the Claim names through its aliases (``semantic.alias``).

    A statement subject or Subject-valued object whose path is not in the tree
    being written resolves to the one live Subject of its kind an alias names
    (``indexes.subject_aliases``); several refuse naming each; none leaves the
    address as written, for the referent laws to refuse.
    """

    def resolved(address: SemanticAddress, element: str) -> SemanticAddress:
        path = address.artifact_path
        if (
            address.selector.scheme != "artifact-v1"
            or not path.startswith("subjects/")
            or _live_subject_in(tree, path)
        ):
            return address
        kind, _, rest = path.removeprefix("subjects/").partition("/")
        subject_id = rest.removesuffix(".json")
        if not kind or not subject_id:
            return address
        with instance.bind_accepted_projection(base) as projection:
            resolution = resolve_subject_ref(
                projection.typed.connection, kind, subject_id, key_reader=tree_key_reader(tree)
            )
        if resolution.ambiguous and resolution.via == "key":
            _refuse(
                "cruxible.subject.key_ambiguous",
                element,
                key_ambiguous_message(
                    kind, [f"subjects/{item}.json" for item in resolution.candidates]
                ),
                repair_kind="replace_subject",
                repair_description="Name one of the listed Subjects as kind/id.",
                replacement={"candidates": list(resolution.candidates)},
            )
        if resolution.via == "key" and resolution.path is not None and resolution.path in tree:
            # A single key names the one Subject that holds it (identity D4).
            return SemanticAddress.whole_artifact(resolution.path)
        if resolution.ambiguous:
            _refuse(
                "cruxible.authoring.alias_ambiguous",
                element,
                f"{kind}/{subject_id} names no Subject, and an alias of {kind} names "
                f"{len(resolution.candidates)}: {', '.join(resolution.candidates)}.",
                repair_kind="replace_subject",
                repair_description="Name one of the listed Subjects as kind/id.",
                replacement={"candidates": list(resolution.candidates)},
            )
        if resolution.via != "alias" or resolution.path is None or resolution.path not in tree:
            return address
        return SemanticAddress.whole_artifact(resolution.path)

    statement = payload.statement
    drafted = (
        isinstance(payload, ClaimAuthoringPayloadV2 | ClaimAuthoringPayload)
        and payload.dependency_drafts.subject is not None
    )
    # A Subject the Claim drafts in the same change is named exactly, never by alias.
    subject = statement.subject if drafted else resolved(statement.subject, "statement.subject")
    value = statement.object
    if isinstance(value, SubjectClaimObject):
        target = resolved(value.address, "statement.object.address")
        if target != value.address:
            value = SubjectClaimObject(address=target)
    elif isinstance(value, AuthoringNoneClaimObject) and isinstance(value.of, SubjectClaimObject):
        # A none of a Subject (an object-keyed inverse) names it as a value does.
        target = resolved(value.of.address, "statement.object.of.address")
        if target != value.of.address:
            value = AuthoringNoneClaimObject(of=SubjectClaimObject(address=target))
    if subject == statement.subject and value is statement.object:
        return payload
    return payload.model_copy(
        update={"statement": statement.model_copy(update={"subject": subject, "object": value})}
    )


def claim_aliases_resolved(
    instance: PlaybillInstance,
    base: AcceptedProjectionCoordinate,
    tree: Mapping[str, bytes],
    payload: _PayloadV1T,
) -> _PayloadV1T:
    """The Claim payload with the Subjects it names resolved through their aliases.

    The same resolution lowering applies, for a caller that must locate the
    statement before lowering (a Procedure item restating a live Claim).
    """

    return _with_aliases_resolved(instance, base, tree, payload)


def cited_observation(instance: PlaybillInstance, capture_digest: str) -> tuple[str, str]:
    """What one Capture observed: its exact contract digest and observed content.

    A Capture whose envelope cannot be read contributes its own digest for
    both, which no other Capture's observation equals.
    """

    access = BodyAccessContext(principal_id="playbill-authoring", can_read_body=True)
    try:
        envelope = capture_envelope(instance.body_store(), capture_digest, access=access)
    except (CruxibleError, OSError, ValueError):
        return capture_digest, capture_digest
    return envelope.capture_contract_digest, observed_content(envelope).digest


def _cited_associations(
    instance: PlaybillInstance, citations: Iterable[ClaimCitation]
) -> set[tuple[str, str, str, str]]:
    """Each citation a Claim holds, as (contract, observed content, role, origin)."""

    return {
        (*cited_observation(instance, item.capture_digest), item.role, item.origin)
        for item in citations
    }


def _canonical_object(
    value: LiteralClaimObject | SubjectClaimObject | ExactContentClaimObject | NoneClaimObject,
    claim_type: ClaimType,
) -> LiteralClaimObject | SubjectClaimObject | ExactContentClaimObject | NoneClaimObject:
    """Store a literal in the canonical form its ClaimType declares (v8).

    Every Claim lowering path -- a change-set member, a write verb, a Procedure
    terminal item -- comes through here, so the stored object is canonical and
    the raw spelling stays only in the evidence the Claim cites.
    """

    form = effective_canonical_form(claim_type)
    if not form or not isinstance(value, LiteralClaimObject):
        return value
    try:
        canonical = canonicalize_literal(value.value, form)
    except CanonicalFormDidNotConverge as exc:
        _refuse(
            "cruxible.claim.literal_not_canonical",
            "statement.object",
            str(exc),
            repair_kind="replace_object",
            repair_description="State a value the ClaimType's canonical form settles.",
        )
    return value if canonical == value.value else LiteralClaimObject(value=canonical)


_PayloadT = TypeVar("_PayloadT")


def canonical_authored_claims(payload: _PayloadT, read: Callable[[str], bytes | None]) -> _PayloadT:
    """Rewrite authored Claim literals into their ClaimType's canonical form (v8).

    Lowering stores the canonical value anyway; doing it before identities are
    minted makes two spellings equal under the form one authored statement, so
    member identity, duplicate detection and idempotent re-authoring all compare
    canonical statements. A ClaimType the same change set defines or succeeds is
    the one read; otherwise ``read`` returns the accepted bytes at ``path``. A
    value the form cannot settle is left as written: lowering refuses it.
    """

    forms: dict[str, tuple[str, ...]] = {}
    members = payload.members if isinstance(payload, ChangeSetAuthoringPayload) else (payload,)
    for member in members:
        if isinstance(member, ClaimTypeAuthoringPayload):
            forms[member.claim_type.predicate] = effective_canonical_form(member.claim_type)
        elif isinstance(member, ClaimTypeSuccessionMember):
            forms[member.successor.predicate] = effective_canonical_form(member.successor)

    def form_of(predicate: str) -> tuple[str, ...]:
        if predicate not in forms:
            path = claim_type_path(predicate)
            content = read(path)
            forms[predicate] = (
                ()
                if content is None
                else effective_canonical_form(parse_claim_type(content, path=path))
            )
        return forms[predicate]

    declared_kinds: dict[str, SubjectKind | None] = {}

    def mapped_reference(address: SemanticAddress) -> SemanticAddress:
        reference = address.subject_key_reference
        if reference is None or isinstance(reference[1], dict):
            return address
        kind, given = reference
        if kind not in declared_kinds:
            path = subject_kind_path(kind)
            content = read(path)
            declared_kinds[kind] = _declared_in_change(
                kind,
                members=cast(tuple[AuthoringChangeSetMember, ...], members),
                base_tree={} if content is None else {path: content},
            )
        declared = declared_kinds[kind]
        if declared is None or len(declared.keys) != 1:
            return address  # lowering supplies the typed refusal and mapping repair
        return SemanticAddress.subject_key(kind, key_reference_mapping(declared, given))

    def canonical(member: object) -> object:
        if not isinstance(member, ClaimAuthoringPayloadV1):
            return member
        member = cast(ClaimAuthoringPayloadV1, _with_landed_addresses(member, mapped_reference))
        value = member.statement.object
        if not isinstance(value, LiteralClaimObject):
            return member
        form = form_of(member.statement.predicate)
        if not form:
            return member
        try:
            settled = canonicalize_literal(value.value, form)
        except CanonicalFormDidNotConverge:
            return member
        if settled == value.value:
            return member
        statement = member.statement.model_copy(
            update={"object": LiteralClaimObject(value=settled)}
        )
        return member.model_copy(update={"statement": statement})

    if isinstance(payload, ChangeSetAuthoringPayload):
        rewritten = tuple(canonical(member) for member in payload.members)
        if all(new is old for new, old in zip(rewritten, payload.members, strict=True)):
            return payload
        by_identity: dict[str, int] = {}
        for index, written in enumerate(rewritten):
            identity = authoring_member_identity(cast(AuthoringChangeSetMember, written))
            if identity in by_identity:
                raise AuthoringDuplicateStatement(
                    f"{AuthoringDuplicateStatement.error_code}: members "
                    f"{by_identity[identity]} and {index} state one statement once their "
                    "values are in the canonical form the ClaimType declares; leave one out"
                )
            by_identity[identity] = index
        ordered = sorted(
            rewritten,
            key=lambda member: authoring_member_identity(
                cast(AuthoringChangeSetMember, member)
            ).encode("utf-8"),
        )
        return cast(
            _PayloadT,
            ChangeSetAuthoringPayload.model_validate(
                {**payload.model_dump(mode="python"), "members": ordered}
            ),
        )
    return cast(_PayloadT, canonical(payload))


class AuthoringDuplicateStatement(FormatError):
    """Two authored members state one statement once canonicalized (ClaimType v8)."""

    error_code = "cruxible.authoring.duplicate_statement"


def _same_predicate_claims(
    tree: Mapping[str, bytes], statement: ClaimStatement
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
    tree: Mapping[str, bytes], statement: ClaimStatement
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


class _ClaimPredicateIndex:
    """Compatibility cursor over live contenders in one exact staged tree.

    Immutable snapshots own shared memberships; ordinary Mapping callers retain
    the scan oracle. Both build at the first contender lookup, after ordinary
    referent checks. Every staged member advances to its new tree, including
    closure paths written by retirements and ClaimType successions.
    """

    def __init__(self, tree: Mapping[str, bytes]) -> None:
        self._tree = tree
        self._claims: dict[tuple[SemanticAddress, str], dict[str, ClaimArtifactAny]] | None = None
        self._keys: dict[str, tuple[SemanticAddress, str]] = {}
        # Models belong to this lowering only. Shared roots retain immutable
        # bytes; repeated siblings avoid parsing the same requested bytes again.
        self._parsed: dict[str, tuple[bytes, ClaimArtifactAny]] = {}

    def _replace(self, path: str) -> None:
        if not path.startswith("claims/"):
            return
        assert self._claims is not None
        previous = self._keys.pop(path, None)
        if previous is not None:
            group = self._claims[previous]
            del group[path]
            if not group:
                del self._claims[previous]
        content = self._tree.get(path)
        if content is None:
            return
        claim = parse_claim(content, path=path)
        if claim.lifecycle.state != "live":
            return
        key = (claim.statement.subject, claim.statement.predicate)
        self._keys[path] = key
        self._claims.setdefault(key, {})[path] = claim

    def advance(self, tree: Mapping[str, bytes], changed_paths: Iterable[str]) -> None:
        self._tree = tree
        if isinstance(tree, SnapshotTree | CandidateTree):
            # The structurally shared tree owns exactly its changed memberships.
            # Do not rebuild a parallel candidate-local index on every member.
            self._claims = None
            self._keys = {}
            for path in changed_paths:
                self._parsed.pop(path, None)
            return
        if self._claims is not None:
            for path in sorted(set(changed_paths), key=lambda item: item.encode("utf-8")):
                self._replace(path)

    def claims_for(self, statement: ClaimStatement) -> tuple[ClaimArtifactAny, ...]:
        if isinstance(self._tree, SnapshotTree | CandidateTree):
            requested: list[ClaimArtifactAny] = []
            for path, content in self._tree.claim_items(statement):
                cached = self._parsed.get(path)
                if cached is None or cached[0] != content:
                    claim = parse_claim(content, path=path)
                    self._parsed[path] = (content, claim)
                else:
                    claim = cached[1]
                requested.append(claim)
            return tuple(requested)
        if self._claims is None:
            self._claims = {}
            for path in sorted(self._tree, key=lambda item: item.encode("utf-8")):
                self._replace(path)
        claims = self._claims.get((statement.subject, statement.predicate), {})
        return tuple(claims[path] for path in sorted(claims, key=lambda item: item.encode("utf-8")))


def _merge_pins(*groups: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
    """Later groups replace a required pin; capture-contract provenance accumulates.

    A revision keeps its predecessor's backing Captures, so it keeps the exact
    contract version each of them used beside the version a new Capture used.
    """

    by_key: dict[tuple[str, str, str], ArtifactPin] = {}
    for pin in (item for group in groups for item in group):
        version = pin.artifact_digest if pin.role == "capture-contract" else ""
        by_key[(pin.role, pin.target.qualified, version)] = pin
    return tuple(
        sorted(
            by_key.values(),
            key=lambda pin: (
                pin.role.encode("utf-8"),
                pin.target.qualified.encode("utf-8"),
                pin.artifact_digest.encode("ascii"),
            ),
        )
    )


@dataclass(frozen=True)
class _Rebound:
    """What a re-bind keeps of its predecessor, and what leaves with the replaced citations."""

    kept: tuple[ClaimCitation, ...]
    #: Captures only the replaced citations named.
    captures: frozenset[str]
    #: Their commitments no kept Capture shares: the content mappings that leave.
    commitments: frozenset[str]
    #: Their contract versions no kept Capture used: the provenance pins that leave.
    contract_versions: frozenset[str]


def _rebound_backing(
    store: CaptureObjectStoreProtocol,
    *,
    payload: ClaimAuthoringPayloadV1,
    statement: ClaimStatement,
    predecessor: ClaimArtifactAny | None,
    citations: tuple[ClaimCitation, ...],
) -> _Rebound | None:
    """The backing a re-bind keeps, or None when the payload replaces nothing.

    Refuses a re-bind that changes the statement (``rebind_statement_changed``):
    re-binding moves a Claim onto the text it rests on now, never to a new
    value. Cost: this Claim's own citations and Captures.
    """

    replaced = frozenset(getattr(payload, "replaces_citations", ()))
    if not replaced:
        return None
    if predecessor is None or statement != predecessor.statement.model_copy(
        update={"claim_type_digest": statement.claim_type_digest}
    ):
        _refuse(
            "cruxible.authoring.rebind_statement_changed",
            "replaces_citations",
            "A re-bind replaces citations of the statement its Claim holds; this payload "
            "states something else.",
            repair_kind="restate_value",
            repair_description=(
                "Re-bind without restating the value, or set the new value as a revision."
            ),
        )
    held = {item.citation_id for item in citations}
    if not replaced <= held:
        _refuse(
            "cruxible.authoring.rebind_citation_not_held",
            "replaces_citations",
            "A re-bind names a citation its Claim does not hold.",
            repair_kind="read_claim",
            repair_description="Read the Claim's citations again and name one it holds.",
        )
    kept = tuple(item for item in citations if item.citation_id not in replaced)
    kept_captures = {item.capture_digest for item in kept} | (
        set(predecessor.backing.capture_digests) - {item.capture_digest for item in citations}
    )
    dropped = {
        item.capture_digest for item in citations if item.citation_id in replaced
    } - kept_captures
    kept_envelopes = [_read_existing_capture(store, digest) for digest in sorted(kept_captures)]
    dropped_envelopes = [_read_existing_capture(store, digest) for digest in sorted(dropped)]
    return _Rebound(
        kept=kept,
        captures=frozenset(dropped),
        commitments=frozenset(item.commitment.digest for item in dropped_envelopes)
        - {item.commitment.digest for item in kept_envelopes},
        contract_versions=frozenset(item.capture_contract_digest for item in dropped_envelopes)
        - {item.capture_contract_digest for item in kept_envelopes},
    )


def _merge_mappings(*groups: tuple[SourceMapping, ...]) -> tuple[SourceMapping, ...]:
    by_wire = {
        canonical_bytes(item.model_dump(mode="json")): item for group in groups for item in group
    }
    return tuple(by_wire[key] for key in sorted(by_wire))


def _refuse_unassigned_subject(kind: str, subject_id: str, *, element: str) -> NoReturn:
    """Refuse a Subject named by an author-chosen id: core assigns every new id (D1)."""

    _refuse(
        "cruxible.authoring.subject_id_not_assigned",
        element,
        f"{kind}/{subject_id} does not exist, and a new Subject's id is never chosen by its "
        "author: core assigns it from the kind's SubjectKind (the rendering of its key, or a "
        "minted id).",
        repair_kind="create_subject",
        repair_description=(
            "Create it with a subject member naming its kind and key (omit key for a minted "
            f"kind): {subject_creation_shape(kind, subject_id)}; the result reports its id."
        ),
        replacement={"subject_kind": kind},
    )


def _install_claim_dependencies(
    payload: ClaimAuthoringPayloadV1,
    *,
    base_tree: Mapping[str, bytes],
    light: bool = False,
) -> tuple[MutableMapping[str, bytes], set[str]]:
    """Install a Claim's Subject and ClaimType drafts against the tree it lowers on.

    `base_tree` is the STAGED tree, so a sibling member that defines the Subject
    or the ClaimType earlier in the same change set already satisfies the draft.
    The withdrawn `dependency_not_one_claim` refusal read a succession in a draft
    as proof that the author meant a second change; a change set IS one change,
    so the staged tree decides instead: a draft that differs from what is already
    staged or accepted at that identity still refuses, and one that agrees with
    it installs nothing.
    """

    candidate_tree = fork_tree(base_tree)
    changed_paths: set[str] = set()
    if not isinstance(payload, ClaimAuthoringPayloadV2 | ClaimAuthoringPayload):
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
                "cruxible.authoring.dependency_subject_mismatch",
                "dependency_drafts.subject",
                "The Subject draft does not equal this Claim's exact subject.",
                repair_kind="replace_dependency_subject",
                repair_description="Use the Subject named by statement.subject.",
            )
        rendered = render_subject(subject_draft)
        accepted = base_tree.get(draft_path)
        if accepted is None:
            _refuse_unassigned_subject(
                subject_draft.subject_kind,
                subject_draft.subject_id,
                element="dependency_drafts.subject",
            )
        if accepted is not None and accepted != rendered:
            _refuse(
                "cruxible.authoring.dependency_conflicts_with_accepted",
                "dependency_drafts.subject",
                "The Subject dependency conflicts with accepted bytes at this identity.",
                repair_kind="use_accepted_subject",
                repair_description="Refresh the Subject ref from the accepted coordinate.",
            )
        if accepted is None:
            candidate_tree[draft_path] = rendered
            changed_paths.add(draft_path)
    elif subject_artifact_path not in base_tree:
        missing_kind = subject_artifact_path.removeprefix("subjects/").partition("/")[0]
        _refuse(
            "cruxible.authoring.dependency_subject_required",
            "statement.subject",
            "This Claim's Subject is absent at the intent base and is not created in this change.",
            repair_kind="create_subject",
            repair_description=(
                "Name an accepted Subject, or create it in the same change set with a subject "
                f"member naming its kind and key: {subject_creation_shape(missing_kind)}; core "
                "assigns its id."
            ),
        )

    type_artifact_path = claim_type_path(payload.statement.predicate)
    claim_type_draft = drafts.claim_type
    if claim_type_draft is not None:
        draft_path = claim_type_path(claim_type_draft.predicate)
        if draft_path != type_artifact_path:
            _refuse(
                "cruxible.authoring.dependency_claim_type_mismatch",
                "dependency_drafts.claim_type",
                "The ClaimType draft does not equal this Claim's predicate.",
                repair_kind="replace_dependency_claim_type",
                repair_description="Use the ClaimType named by statement.predicate.",
            )
        rendered = render_claim_type(claim_type_draft)
        accepted = base_tree.get(draft_path)
        if accepted is not None and accepted != rendered:
            _refuse(
                "cruxible.authoring.dependency_conflicts_with_accepted",
                "dependency_drafts.claim_type",
                "The ClaimType dependency conflicts with accepted bytes at this identity.",
                repair_kind="use_accepted_claim_type",
                repair_description="Refresh the ClaimType ref from the accepted coordinate.",
            )
        if accepted is None:
            candidate_tree[draft_path] = rendered
            changed_paths.add(draft_path)
            if light:
                from cruxible_core.claims.claim_type_inputs import (
                    install_builtin_capture_dependencies,
                )

                changed_paths.update(
                    install_builtin_capture_dependencies(claim_type_draft, candidate_tree)
                )
    elif type_artifact_path not in base_tree:
        _refuse(
            "cruxible.authoring.dependency_claim_type_required",
            "dependency_drafts.claim_type",
            "This Claim's ClaimType is absent at the intent base.",
            repair_kind="provide_dependency_claim_type",
            repair_description="Supply the exact ClaimType draft for statement.predicate.",
        )

    return candidate_tree, changed_paths


def _refuse_citation_into_projection_window(
    instance: PlaybillInstance,
    *,
    payload: ClaimAuthoringPayloadV1,
    envelope: CaptureEnvelopeAny,
    memo: VerifiedCaptureMemo | None = None,
) -> None:
    """Refuse a citation whose span lies inside a stamped projection block.

    The daemon, not the client, says so: a citation of any role and any
    origin whose span intersects a projection block window in the cited
    source refuses here, before the wire ever mints a Claim. The cited
    capture's own bytes are the manifest of its windows -- and a working
    selection commits to its selected bytes alone, so a page that declares a
    block is handed over with the observation and kept in the store, where the
    citation gate reads it back at every later evaluation.

    The bytes are the caller's, so the caller does not get the last word on
    them. Whatever page the span was measured against -- presented with the
    observation or read back from the store -- has to carry every block the
    INSTANCE registers in that source, or it is not that source's page: a
    marker-stripped copy carrying the same block prose says nothing about
    where the windows are. When the page cannot be obtained at all, or is
    missing a registered window, or the registration fold cannot be read, the
    span cannot be proved outside the windows, and an unprovable citation into
    a governed page refuses rather than passes.
    """

    store = instance.body_store()
    source_id: str | None = None
    page: bytes | None = None
    if isinstance(payload.source, WorkingSelectionObservation):
        source_id = payload.source.source_id
        page = payload.source.source_content
        if page is not None:
            store.store(page)
            window = projection_window_intersecting(
                page,
                start_byte=payload.source.selector.start_byte,
                end_byte=payload.source.selector.end_byte,
            )
            if window is not None:
                _refuse_evidence_from_projection(
                    source_id=source_id,
                    block_id=window.block_id,
                    start_byte=payload.source.selector.start_byte,
                    end_byte=payload.source.selector.end_byte,
                )
    if page is None:
        cited = projection_window_cited(envelope, store=store, memo=memo)
        if cited is not None:
            resolved, window = cited
            _refuse_evidence_from_projection(
                source_id=resolved.source_id or source_id,
                block_id=window.block_id,
                start_byte=resolved.start_byte,
                end_byte=resolved.end_byte,
            )
        resolved_source = resolve_cited_source_window(envelope, store=store, memo=memo)
        if resolved_source is not None and resolved_source.whole_source:
            page = resolved_source.content
    cited_source = (
        envelope.source.source_identity
        if isinstance(envelope.source, ExternalSourceReference)
        else source_id
    )
    if cited_source is None:
        return
    # The one fold every other consumer reads -- `next`, coverage and the
    # detach refusal -- and not the publication road alone: since `publish_to`
    # is gone, every new block is declaration-road, and reading only the
    # publication registrations would leave this gate blind to exactly the
    # blocks the instance now mints.
    registered = source_projection_blocks(instance, cited_source)
    if registered is None:
        _refuse(
            "cruxible.projection.window_unverifiable",
            "source",
            f"Source {cited_source!r} may carry projection blocks and the registration "
            "fold cannot be read, so this span cannot be proved outside them.",
            repair_kind="restore_registry",
            repair_description="Restore the instance exhaust so registrations can be read.",
        )
    expected = registered
    if not expected:
        return
    if page is None:
        _refuse(
            "cruxible.projection.window_unverifiable",
            "source",
            f"Source {cited_source!r} registers projection blocks and its whole bytes "
            "were not presented, so this span cannot be proved outside them.",
            repair_kind="present_source_content",
            repair_description=(
                "Cite through the SDK or `authoring bind`, which hand over the page "
                "(`source_content_base64`) when it declares a block."
            ),
        )
    stamped = {window.block_id for window in stamped_projection_windows(page)}
    missing = [block_id for block_id in expected if block_id not in stamped]
    if missing:
        _refuse(
            "cruxible.projection.window_unverifiable",
            "source",
            f"The bytes presented for source {cited_source!r} carry no stamped window for "
            f"registered block {missing[0]!r}, so they are not the page this instance "
            "governs and this span cannot be proved outside its projection blocks.",
            repair_kind="present_source_content",
            repair_description=(
                "Present the governed page itself, markers included, or repin the block "
                "the page no longer carries."
            ),
        )


def _refuse_evidence_from_projection(
    *,
    source_id: str | None,
    block_id: str,
    start_byte: int,
    end_byte: int,
) -> NoReturn:
    _refuse(
        "cruxible.projection.evidence_from_projection",
        "source",
        f"Bytes [{start_byte}, {end_byte}) of source {source_id!r} lie inside projection "
        f"block {block_id!r}. A projection block is never evidence, whatever the citation "
        "role: cite the Claims it reflects, or prose outside the block.",
        repair_kind="cite_outside_the_block",
        repair_description=(
            "Cite the accepted Claims the block reflects, or select prose outside it."
        ),
    )


def _claim_pin(*, role: str, target: ArtifactIdentity, artifact_digest: str) -> ArtifactPin:
    def derive() -> ArtifactPin:
        value = ArtifactPin(role=role, target=target, artifact_digest=artifact_digest)
        if not seal(value):
            raise TypeError("a carried Claim pin must be immutable")
        return value

    return request_derive(
        ("core.claim_pin", role, target.kind, target.name, artifact_digest), derive
    )


def _lower_claim(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    payload: ClaimAuthoringPayloadV1 | None = None,
    claim_identity: str | None = None,
    claim_index: _ClaimPredicateIndex | None = None,
    derivation_procedure: ArtifactPin | None = None,
    captures: ProducerReceiptResolverProtocol,
    subject_creation_key: bool = False,
    key_paths: Mapping[tuple[str, str], str] | None = None,
) -> LoweredAuthoring:
    """Lower one authored Claim against the tree it is being written onto.

    `payload` and `claim_identity` are explicit so a change-set member lowers the
    same way a singular Claim intent does: the member carries its own payload and
    its own minted Claim ID, and `base_tree` is the staged tree its siblings have
    already written into, so dependency installs, the slot law and predecessor
    lookup all see the rest of the same change.

    `key_paths` lands each Subject the Claim names by key (``subject-key-v1``)
    on the path ``_key_reference_paths`` resolved for its change; a singular
    Claim intent resolves its own.

    `subject_creation_key` marks a key Claim a Subject creation writes: the
    creation covers it (Claim law v2 revision 9), so an existing Capture it
    cites as evidence need not satisfy one of the key ClaimType's rules.
    """

    authored = intent.payload if payload is None else payload
    assert isinstance(authored, ClaimAuthoringPayloadV1)
    payload = authored
    derivation = payload.derivation if isinstance(payload, ClaimAuthoringPayload) else None
    if payload.statement.role == "derivation" or derivation is not None:
        if (
            derivation is None
            or derivation_procedure is None
            or derivation.procedure.target != derivation_procedure.target
            or derivation.procedure.artifact_digest != derivation_procedure.artifact_digest
        ):
            _refuse(
                "cruxible.authoring.derivation_requires_execution",
                "derivation",
                "Derived Claims must be produced by the executing Procedure's proposal "
                "terminal; ordinary authoring cannot assert or carry execution provenance.",
                repair_kind="run_procedure",
                repair_description="Run the authorized Procedure to propose this derivation.",
            )
    claim_id = intent.semantic_identity if claim_identity is None else claim_identity
    type_path = claim_type_path(payload.statement.predicate)
    if _subject_key_addresses(payload):
        if key_paths is None:
            key_paths = _key_reference_paths(
                instance, base=base, base_tree=base_tree, members=(payload,), creations={}
            ).paths
        payload = _with_keys_landed(payload, key_paths)
    elif key_paths:
        payload = _with_keys_landed(payload, key_paths)
    payload = _with_aliases_resolved(instance, base, base_tree, payload)
    candidate_base_tree, dependency_paths = _install_claim_dependencies(
        payload,
        base_tree=base_tree,
        light=base.compiler == LIGHT_ARTIFACTS_COMPILER,
    )
    type_content = candidate_base_tree.get(type_path)
    if type_content is None:
        _refuse(
            "cruxible.authoring.claim_type_not_found",
            "statement.predicate",
            "The predicate has no accepted ClaimType at the intent base.",
            repair_kind="replace_predicate",
            repair_description="Choose an accepted ClaimType predicate.",
        )
    claim_type = parse_claim_type(type_content, path=type_path)
    descriptor = payload.statement.predicate in DESCRIPTOR_PREDICATES
    subject_identity, subject_digest_value = _referent(
        candidate_base_tree, payload.statement.subject, descriptor=descriptor
    )
    statement_object = _exact_object(instance, payload.statement.object)
    # A none's shape is judged against the ClaimType's inverse by the Claim law.
    if not isinstance(statement_object, NoneClaimObject) and (
        statement_object.kind != claim_type.object_kind
    ):
        _refuse(
            "cruxible.claim.object_kind_mismatch",
            "statement.object",
            "The Claim object kind differs from its accepted ClaimType.",
            repair_kind="replace_object",
            repair_description=(
                f"Use a {claim_type.object_kind} object for ClaimType {claim_type.predicate!r}."
            ),
            replacement={"required_object_kind": claim_type.object_kind},
        )
    statement_object = _canonical_object(statement_object, claim_type)
    qualifier = payload.statement.qualifier
    object_referent: tuple[ArtifactIdentity, str] | None = None
    # A none of a Subject (an object-keyed inverse) pins that Subject as its object.
    named_object = (
        statement_object.of if isinstance(statement_object, NoneClaimObject) else statement_object
    )
    if isinstance(named_object, SubjectClaimObject):
        object_path = named_object.address.artifact_path
        object_content = candidate_base_tree.get(object_path)
        object_name = object_path.removeprefix("subjects/").removesuffix(".json")
        if object_content is None:
            _refuse(
                "cruxible.authoring.object_subject_not_found",
                "statement.object.address",
                f"The object Subject {object_name!r} is not accepted at the intent base.",
                repair_kind="create_subject",
                repair_description=(
                    "Name an accepted Subject, or create it in the same change set with a "
                    "subject member naming its kind and key: "
                    f"{subject_creation_shape(object_name.partition('/')[0])}."
                ),
                replacement={"subject": object_name},
            )
        if not object_path.startswith("subjects/"):
            _refuse(
                "cruxible.claim.object_subject_kind_forbidden",
                "statement.object.address",
                "A subject-valued Claim object must name an accepted Subject artifact.",
                repair_kind="replace_object_subject",
                repair_description="Choose an accepted Subject of an admitted kind.",
            )
        object_subject = parse_subject(object_content, path=object_path)
        # The Claim law judges a descriptor relation's object as semantic.subject.
        if not admits_object_subject(
            claim_type.predicate,
            claim_type.allowed_object_subject_kinds,
            object_subject.subject_kind,
        ):
            _refuse(
                "cruxible.claim.object_subject_kind_forbidden",
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
    if claim_index is None:
        claim_index = _ClaimPredicateIndex(candidate_base_tree)
    existing = claim_index.claims_for(statement)
    slot_claims = tuple(
        claim for claim in existing if claim.statement.qualifier == statement.qualifier
    )
    expected = {item.identity.name for item in slot_claims}
    dispositionable = {item.identity.name for item in existing}
    supplied = {item.claim_id for item in payload.existing_claim_dispositions}
    inferred = {payload.revises} if payload.revises in expected else set()
    required_ids = expected - inferred
    if not required_ids.issubset(supplied) or not supplied.issubset(dispositionable):
        required = tuple(sorted(slot_claims, key=lambda item: item.identity.name.encode("ascii")))
        _refuse(
            "cruxible.authoring.existing_claim_dispositions_incomplete",
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
        if isinstance(predecessor, ClaimArtifact):
            _refuse(
                "cruxible.authoring.claim_terminal",
                "revises",
                "An attributed retired Claim is terminal and cannot be authored again.",
                repair_kind="omit_revises",
                repair_description="Mint a new Claim lineage instead of reviving a retired one.",
                replacement=None,
            )
    elif payload.revises is not None:
        _refuse(
            "cruxible.authoring.claim_predecessor_not_found",
            "revises",
            "The requested Claim lineage does not exist at the intent base.",
            repair_kind="omit_revises",
            repair_description="Omit revises to mint a new Claim lineage.",
            replacement=None,
        )

    public_base = AcceptedCoordinate.from_internal(base)
    built_capture: CaptureBuildResult | DirectCaptureBuildResult | None = None
    accepted_contract: AcceptedCaptureContract | None = None
    accepted_producer_digests: dict[str, str] = {}
    install_contract = True
    contract: CaptureContractAny
    light_contracts = base.compiler == LIGHT_ARTIFACTS_COMPILER
    coordinator_contract = (
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
        if light_contracts
        else COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1
    )
    foreign_contract = (
        foreign_source_capture_contract if light_contracts else foreign_source_capture_contract_v1
    )
    if isinstance(payload.source, SelfSourceBody):
        profile_path = capture_contract_path(coordinator_contract.identity.name)
        previous_profile_bytes = candidate_base_tree.get(profile_path)
        previous_profile = (
            None
            if previous_profile_bytes is None
            else parse_capture_contract(previous_profile_bytes, path=profile_path)
        )
        contract = (
            builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, previous_profile)
            if light_contracts
            else previous_profile or coordinator_contract
        )
        install_contract = previous_profile != contract
        built_capture = build_coordinator_self_source_capture(
            store=instance.body_store(),
            actor_id=actor_id,
            claim_id=claim_id,
            body=payload.source.content,
            accepted_contract=contract,
            observed_at=observed_at,
            accepted_coordinate=public_base,
        )
        citation_role: CitationRole = "copy"
        citation_origin: CitationOrigin = "self_source"
    elif isinstance(payload.source, ExistingCaptureCitationSource):
        install_contract = False
        store = instance.body_store()
        # The request may already have verified this Capture for a sibling Claim.
        verified = captures.recall_verified(payload.source.capture_digest)
        envelope = (
            verified.envelope
            if verified is not None
            else _read_existing_capture(store, payload.source.capture_digest)
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
                producer_receipt_resolver=captures,
                # A Capture the accepted predecessor already cites is not new
                # evidence; the Claim law judges whether the revision cites it
                # a new way.
                require_production_proof=not (
                    predecessor is not None
                    and isinstance(predecessor.backing, ClaimBacking)
                    and payload.source.capture_digest in predecessor.backing.capture_digests
                ),
            )
        except CaptureProductionUnproven as exc:
            _refuse_unproduced_capture(exc)
        except (CruxibleError, ValueError):
            _refuse(
                "cruxible.authoring.existing_capture_invalid",
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
                "cruxible.claim.self_source_capture_unbound",
                "source.capture_digest",
                "The Claim-bound Capture belongs to another Claim or has mismatched "
                "family signals.",
                repair_kind="replace_capture",
                repair_description="Use a shareable observed Capture or one bound to this Claim.",
            )
        if classification == "not_shareable":
            _refuse(
                "cruxible.authoring.capture_not_shareable",
                "source.capture_digest",
                "Only verified observed Captures are shareable across Claims.",
                repair_kind="replace_capture",
                repair_description="Use an observed Capture or author new source evidence.",
            )
        assert payload.citation_role is not None
        if classification == "claim_bound" and payload.citation_role != "copy":
            _refuse(
                "cruxible.authoring.existing_capture_not_admitted",
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
        assert isinstance(source, WorkingSelectionObservation)
        # A source contract improved through a successor is captured under its
        # accepted head, which is then already installed; the first capture of a
        # source installs its deterministic contract.
        head_path = capture_contract_path(foreign_contract(source.source_id).identity.name)
        head_bytes = candidate_base_tree.get(head_path)
        head = None if head_bytes is None else parse_capture_contract(head_bytes, path=head_path)
        if not light_contracts:
            head = (
                head
                if head is not None and head.lifecycle.state == "live"
                else foreign_contract(source.source_id)
            )
        elif head is not None and head.lifecycle.state == "live":
            successor = builtin_contract_for_write(
                foreign_source_capture_contract(source.source_id), head
            )
            install_contract = successor != head
            head = successor
        try:
            built_selection = build_working_selection_capture(
                accepted_contract=None if head is None or head.lifecycle.state != "live" else head,
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
        except (CruxibleError, ValueError) as exc:
            # The capture contracts refusing the observation -- a selector that
            # carries a locator, a selection over its byte budget -- is a
            # refusal of THIS request, and it used to leave as an unhandled
            # server fault the author could only read in the daemon log.
            _refuse(
                "cruxible.authoring.working_selection_refused",
                "source.selector",
                f"The working selection cannot be captured: {exc}",
                repair_kind="revise_selection",
                repair_description=(
                    "Anchor on prose the source rules admit and grow the window with "
                    "surrounding lines; a URL may appear in an anchor but never in "
                    "another selector field."
                ),
            )
        built_capture = built_selection
        contract = built_selection.contract
        assert payload.citation_role is not None
        citation_role = payload.citation_role
        citation_origin = "independent"

    capture_digest_value = (
        payload.source.capture_digest
        if isinstance(payload.source, ExistingCaptureCitationSource)
        else built_capture.capture_digest
    )
    capture_envelope = (
        envelope
        if isinstance(payload.source, ExistingCaptureCitationSource)
        else built_capture.envelope
    )

    _refuse_citation_into_projection_window(
        instance,
        payload=payload,
        envelope=capture_envelope,
        memo=(
            captures.recall_verified(payload.source.capture_digest)
            if isinstance(payload.source, ExistingCaptureCitationSource)
            else None
        ),
    )

    identity = ArtifactIdentity(kind="Claim", name=claim_id)
    citation = build_claim_citation(
        identity,
        capture_digest=capture_digest_value,
        role=citation_role,
        origin=citation_origin,
    )
    # Under `replace` a revision that changes what it states carries exactly
    # the evidence it cites now; one that states the same thing again, and every
    # revision under `accumulate`, keeps everything its predecessors cited.
    inherited = predecessor
    if (
        predecessor is not None
        and effective_revision_evidence(claim_type) == "replace"
        and statement
        != predecessor.statement.model_copy(
            update={"claim_type_digest": statement.claim_type_digest}
        )
    ):
        inherited = None
    predecessor_citations = (
        inherited.backing.citations
        if inherited is not None and isinstance(inherited.backing, ClaimBacking)
        else ()
    )
    # A re-bind (Claim law v2 revision 10): the same statement, with the named
    # citations replaced by this payload's. What only they backed -- their
    # Captures, those Captures' content mappings and contract versions -- leaves
    # with them; everything else is inherited as for any same-statement revision.
    rebound = _rebound_backing(
        instance.body_store(),
        payload=payload,
        statement=statement,
        predecessor=predecessor if inherited is not None else None,
        citations=predecessor_citations,
    )
    if rebound is not None:
        predecessor_citations = rebound.kept
    located = None
    located_citation = None
    located_contract = None
    located_install = False
    if payload.located_at is not None:
        location = payload.located_at
        if (
            not isinstance(payload.source, SelfSourceBody)
            or location.selected_content != payload.source.content
        ):
            _refuse(
                "cruxible.authoring.location_mismatch",
                "located_at",
                "A location must cite the same bytes as the Claim's own words.",
                repair_kind="observe_section",
                repair_description="Observe the authored section again.",
            )
        located_path = capture_contract_path(foreign_contract(location.source_id).identity.name)
        previous_contract = candidate_base_tree.get(located_path)
        located_contract = (
            None
            if previous_contract is None
            else parse_capture_contract(previous_contract, path=located_path)
        )
        located_successor = (
            builtin_contract_for_write(
                foreign_source_capture_contract(location.source_id), located_contract
            )
            if light_contracts
            else located_contract or foreign_contract(location.source_id)
        )
        located_install = located_successor != located_contract
        located_contract = located_successor
        try:
            located = build_working_selection_capture(
                store=instance.body_store(),
                actor_id=actor_id,
                claim_id=claim_id,
                rationale=payload.rationale,
                observed_at=observed_at,
                accepted_coordinate=public_base,
                source_id=location.source_id,
                coordinate=location.coordinate.model_dump(mode="json"),
                selector=location.selector.model_dump(mode="json"),
                selected_content=location.selected_content,
                accepted_contract=located_contract,
            )
        except (CruxibleError, ValueError) as exc:
            _refuse(
                "cruxible.authoring.working_selection_refused",
                "located_at",
                f"The located selection cannot be captured: {exc}",
                repair_kind="revise_selection",
                repair_description="Select admitted prose outside projection blocks.",
            )
        located_contract = located.contract
        _refuse_citation_into_projection_window(
            instance,
            payload=payload.model_copy(update={"source": location}),
            envelope=located.envelope,
        )
        located_citation = build_claim_citation(
            identity,
            capture_digest=located.capture_digest,
            role="copy",
            origin="independent",
        )
    capture_digests = tuple(
        sorted(
            {
                *(
                    ()
                    if inherited is None
                    else (
                        item
                        for item in inherited.backing.capture_digests
                        if rebound is None or item not in rebound.captures
                    )
                ),
                capture_digest_value,
                *(() if located is None else (located.capture_digest,)),
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
        _claim_pin(
            role="capture-contract",
            target=contract.identity,
            artifact_digest=capture_contract_digest(contract).tagged,
        ),
        _claim_pin(
            role="claim-type",
            target=claim_type.identity,
            artifact_digest=claim_type_digest(claim_type).tagged,
        ),
        _claim_pin(
            role="subject",
            target=subject_identity,
            artifact_digest=subject_digest_value,
        ),
    ]
    if located_contract is not None:
        pins.append(
            _claim_pin(
                role="capture-contract",
                target=located_contract.identity,
                artifact_digest=capture_contract_digest(located_contract).tagged,
            )
        )
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
                _claim_pin(
                    role=producer_identity.kind.casefold(),
                    target=producer_identity,
                    artifact_digest=producer_digest_value,
                )
            )
    if object_referent is not None:
        pins.append(
            _claim_pin(
                role="object-subject",
                target=object_referent[0],
                artifact_digest=object_referent[1],
            )
        )
    if derivation is not None:
        try:
            if statement.role != "derivation":
                raise ValueError("only derivation Claims may carry reducer bindings")
            for pin in (derivation.procedure, *derivation.inputs):
                pin_path = (
                    procedure_path(pin.target.name)
                    if pin.target.kind == "Procedure"
                    else claim_path(pin.target.name)
                )
                content = base_tree.get(pin_path)
                if content is None:
                    raise ValueError("a derivation binding is absent from the accepted base")
                if pin.target.kind == "Procedure":
                    reducer = parse_procedure(content, path=pin_path)
                    identity_matches = reducer.identity == pin.target
                    digest = procedure_artifact_digest(reducer).tagged
                else:
                    basis_claim = parse_claim(content, path=pin_path)
                    identity_matches = basis_claim.identity == pin.target
                    digest = claim_artifact_digest(basis_claim).tagged
                if not identity_matches or digest != pin.artifact_digest:
                    raise ValueError("a derivation binding changed from its admitted version")
                pins.append(pin)
        except (ValueError, CruxibleError) as exc:
            _refuse(
                "cruxible.authoring.derivation_unverified",
                "derivation",
                str(exc),
                repair_kind="rebind_derivation",
                repair_description="Read the basis again at the accepted base.",
            )
    claim = ClaimArtifactV2(
        identity=identity,
        statement=statement,
        backing=ClaimBacking(
            referent_context=context,
            capture_digests=capture_digests,
            citations=merge_claim_citations(
                predecessor_citations,
                (citation,) + (() if located_citation is None else (located_citation,)),
            ),
            input_claim_digests=(
                tuple(sorted(pin.artifact_digest for pin in derivation.inputs))
                if derivation is not None
                else ()
            ),
            reducer_digest=(
                derivation.procedure.artifact_digest if derivation is not None else None
            ),
            source_mappings=_merge_mappings(
                ()
                if inherited is None
                else tuple(
                    item
                    for item in inherited.backing.source_mappings
                    if rebound is None
                    or not any(span.content_digest in rebound.commitments for span in item.spans)
                ),
                (source_mapping,),
            ),
        ),
        pins=_merge_pins(
            ()
            if inherited is None
            else tuple(
                pin
                for pin in inherited.pins
                if rebound is None
                or pin.role != "capture-contract"
                or pin.artifact_digest not in rebound.contract_versions
            ),
            tuple(pins),
        ),
        lifecycle=ArtifactLifecycle(
            predecessor_digest=(
                None if predecessor is None else claim_artifact_digest(predecessor).tagged
            )
        ),
    )
    if (
        isinstance(payload.source, ExistingCaptureCitationSource)
        and citation_role == "evidence"
        and not subject_creation_key
    ):
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
                "cruxible.authoring.existing_capture_not_admitted",
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
    candidate_tree = fork_tree(candidate_base_tree)
    contract_member_path = capture_contract_path(contract.identity.name)
    contract_bytes = render_capture_contract(contract)
    claim_bytes = render_claim(claim)
    # The Claim is final: take the one sealed parse of its bytes that evaluation
    # and projection share, so its statement and artifact digests are taken once
    # in this process. A Claim its own law would refuse stays as built here; the
    # law reports it.
    final: ClaimArtifactAny = claim
    try:
        from cruxible_core.compiler.sealed_members import sealed_claim_member

        final = sealed_claim_member(claim_bytes, path=path).claim
    except ClaimFormatError:
        pass
    if install_contract:
        candidate_tree[contract_member_path] = contract_bytes
    if located_install and located_contract is not None:
        candidate_tree[located_path] = render_capture_contract(located_contract)
    candidate_tree[path] = claim_bytes
    member_paths = {path}
    if install_contract:
        member_paths.add(contract_member_path)
    if located_install and located_contract is not None:
        member_paths.add(located_path)
    member_paths.update(dependency_paths)
    changed = tuple(
        (member_path, candidate_tree[member_path])
        for member_path in sorted(
            member_paths,
            key=lambda item: item.encode("utf-8"),
        )
        if base_tree.get(member_path) != candidate_tree[member_path]
    )
    # Re-observation (identity-design-1009): the same canonical statement whose
    # citation association -- the exact verified Capture contract and observed
    # content cited, its role and its origin -- the Claim already holds is a
    # no-op: no revision, no citation growth. The re-observation journal admits
    # exactly these (same contract and content), so preflight and submit agree.
    # It is the very Capture again, or another Capture of the same content (a
    # re-run reading the same bytes) cited the same way. A new association on
    # already-cited content (another role, origin or contract) is kept,
    # and new content under the same statement refreshes the evidence by
    # revising the Claim, never by writing a second one.
    # A disposition that takes no stance (not_tested: the other slot Claims a
    # many-valued restatement keeps) changes nothing about the Claim restated.
    same_statement = (
        predecessor is not None
        and final.statement == predecessor.statement
        and all(item.disposition == "not_tested" for item in payload.existing_claim_dispositions)
        and not dependency_paths
    )
    recited = same_statement and citation in predecessor_citations
    reobserved = (
        same_statement
        and not recited
        and (
            capture_envelope.capture_contract_digest,
            observed_content(capture_envelope).digest,
            citation.role,
            citation.origin,
        )
        in _cited_associations(instance, predecessor_citations)
    )
    idempotent = (
        isinstance(payload.source, ExistingCaptureCitationSource) and bool(recited)
    ) or bool(reobserved)
    if idempotent and located is not None:
        idempotent = (
            located.envelope.capture_contract_digest,
            observed_content(located.envelope).digest,
            "copy",
            "independent",
        ) in _cited_associations(instance, predecessor_citations)
    if idempotent:
        candidate_tree = fork_tree(candidate_base_tree)
        changed = ()
    resolved_artifact_digest = (
        claim_artifact_digest(predecessor).tagged
        if idempotent and predecessor is not None
        else claim_artifact_digest(final).tagged
    )
    return LoweredAuthoring(
        proposed_tree=candidate_tree,
        resolved_authoring={
            "artifact_digest": resolved_artifact_digest,
            "capture_digest": capture_digest_value,
            "citation_id": citation.citation_id,
            **(
                {
                    "outcome": (
                        "cruxible.authoring.content_already_cited"
                        if reobserved
                        else "cruxible.authoring.existing_capture_already_associated"
                    ),
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
            "predecessor_digest": final.lifecycle.predecessor_digest,
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


def _acquisition_policy_pin(
    payload: ProcedureAuthoringPayload,
    *,
    accepted: dict[str, tuple[str, str]],
    candidates: dict[str, tuple[str, str]],
    candidate_identities: frozenset[str],
) -> ArtifactPin | None:
    """Resolve the Procedure's named acquisition policy into its exact pin.

    The author names the policy the way they name any other artifact -- by its
    semantic name -- and lowering owns the digest, so a caller can neither
    supply one nor pin a policy this instance has not accepted. A change set may
    accept the policy and the Procedure together: the same candidate reference
    every other Procedure member uses resolves it.
    """

    name = payload.acquisition_policy if isinstance(payload, ProcedureAuthoringPayload) else None
    if name is None:
        return None
    try:
        target = ArtifactIdentity(kind="SourceAcquisitionPolicy", name=name)
    except ValueError:
        _refuse(
            "cruxible.authoring.artifact_reference_invalid",
            "acquisition_policy",
            "The named SourceAcquisitionPolicy identity is not canonical.",
            repair_kind="replace_reference",
            repair_description="Name an accepted SourceAcquisitionPolicy by its semantic name.",
        )
    reference: AuthoringArtifactReference | AuthoringCandidateReference = (
        AuthoringCandidateReference(role=ACQUISITION_POLICY_PIN_ROLE, target=target)
        if target.qualified in candidate_identities
        else AuthoringArtifactReference(role=ACQUISITION_POLICY_PIN_ROLE, target=target)
    )
    resolved = _resolve_authoring_references(
        reference.model_dump(mode="json"),
        accepted=accepted,
        candidates=candidates,
        candidate_identities=candidate_identities,
        location="acquisition_policy",
    )
    return ArtifactPin.model_validate(resolved)


def _resolve_authoring_references(
    value: object,
    *,
    accepted: dict[str, tuple[str, str]],
    candidates: dict[str, tuple[str, str]] | None = None,
    candidate_identities: frozenset[str] = frozenset(),
    owned_contracts: dict[str, ProcedureOwnedContract] | None = None,
    location: str = "definition",
) -> object:
    if isinstance(value, dict):
        if value.get("tag") == "playbill-authoring-artifact-reference-v1":
            try:
                reference = AuthoringArtifactReference.model_validate(value)
            except ValidationError as exc:
                _refuse(
                    "cruxible.authoring.artifact_reference_invalid",
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
                    "cruxible.authoring.artifact_reference_unresolved",
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
                candidate_reference = AuthoringCandidateReference.model_validate(value)
            except ValidationError as exc:
                _refuse(
                    "cruxible.authoring.candidate_reference_invalid",
                    location,
                    "The change-set candidate reference is invalid: "
                    + " | ".join(_validation_error_lines(exc, root=location)),
                    repair_kind="replace_reference",
                    repair_description="Use a valid candidate member reference.",
                )
            if candidate_reference.target.kind == "Procedure":
                _refuse(
                    "cruxible.authoring.candidate_procedure_reference_forbidden",
                    location,
                    "A Procedure cannot candidate-reference another Procedure in change-set v1.",
                    repair_kind="replace_reference",
                    repair_description="Reference a non-Procedure member of this change set.",
                )
            if candidate_reference.target.qualified not in candidate_identities:
                _refuse(
                    "cruxible.authoring.candidate_reference_outside_change_set",
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
                    "cruxible.authoring.carried_contract_unresolved",
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
                "cruxible.authoring.caller_artifact_digest_forbidden",
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


def _reference_identities(value: object) -> set[ArtifactIdentity]:
    """Collect only this graph's authoring references; resolution owns refusals."""
    identities: set[ArtifactIdentity] = set()
    if isinstance(value, dict):
        if value.get("tag") in (
            "playbill-authoring-artifact-reference-v1",
            "playbill-authoring-candidate-reference-v1",
        ):
            try:
                identities.add(ArtifactIdentity.model_validate(value.get("target")))
            except ValidationError:
                # Preserve the resolver's field-specific invalid-reference diagnostic.
                pass
        else:
            for member in value.values():
                identities.update(_reference_identities(member))
    elif isinstance(value, list):
        for member in value:
            identities.update(_reference_identities(member))
    return identities


def _reference_path(identity: ArtifactIdentity) -> str | None:
    """Every projected identity has one path, including exact Provider builds.

    Kind-specific path validators reject identities the generic wire grammar
    permits but no accepted artifact can own. Unknown kinds likewise remain
    unresolved. Canonical identity/path validation prevents a second placement
    of the same identity; no population index or sibling enumeration is needed.
    """
    singletons = {
        APPROVAL_POLICY_IDENTITY: APPROVAL_POLICY_PATH,
        PROCEDURE_RUNTIME_POLICY_IDENTITY: PROCEDURE_RUNTIME_POLICY_PATH,
    }
    if identity.qualified in singletons:
        return singletons[identity.qualified]
    paths: dict[str, Callable[[str], str]] = {
        "Blueprint": blueprint_path,
        "CaptureContract": capture_contract_path,
        "Claim": claim_path,
        "Contract": contract_path_for_identity,
        "ClaimAttestation": attestation_path,
        "ClaimType": claim_type_path,
        "document": document_path,
        "ExhaustPromotion": exhaust_promotion_path,
        "Line": line_spec_path,
        "Procedure": procedure_path,
        "ProcedureMandate": procedure_mandate_path,
        "Provider": provider_path,
        "ProviderInterface": provider_interface_path,
        "QueryDefinition": query_definition_path,
        "ResolutionContract": resolution_contract_path,
        "SourceAcquisitionPolicy": acquisition_policy_path,
        "SubjectKind": subject_kind_path,
        "Trigger": trigger_path,
    }
    try:
        if identity.kind == "Subject":
            kind, subject_id = identity.name.split("/", 1)
            return subject_path(kind, subject_id)
        path_of = paths.get(identity.kind)
        return None if path_of is None else path_of(identity.name)
    except (CruxibleError, ValueError):
        return None


def _reference_blobs(
    tree: Mapping[str, bytes],
    identities: Iterable[ArtifactIdentity],
    *,
    base: AcceptedProjectionCoordinate,
) -> Mapping[str, bytes]:
    """Read exact paths, with work bounded by the Procedure's reference set."""
    codec = artifact_codec_for_compiler(base.compiler)
    selected: dict[str, bytes] = {}
    for identity in sorted(identities, key=lambda item: item.qualified.encode("utf-8")):
        path = _reference_path(identity)
        if path is None:
            continue
        path = artifact_path_for_codec(path, codec)
        content = tree.get(path)
        if content is not None:
            selected[path] = content
    return selected


def _parse_reference_tree(
    instance: PlaybillInstance,
    tree: Mapping[str, bytes],
    *,
    base: AcceptedProjectionCoordinate,
) -> ParsedProjectionTree:
    """Parse the selected blobs a Procedure's references resolve against.

    The body-metadata resolver is not optional here. A Document is registered
    with its body's digest and nothing else, so a referenced Document still
    asks the managed CAS for its metadata. Unrelated Documents are never read.
    """

    try:
        return parse_projection_tree(
            tree,
            registry=projection_registry_for_compiler(base.compiler),
            artifact_kinds=artifact_kinds_for_compiler(base.compiler),
            artifact_codec=artifact_codec_for_compiler(base.compiler),
            by_name=base.compiler == LIGHT_ARTIFACTS_COMPILER,
            bodies=instance.body_store(),
        )
    except CruxibleError as exc:
        # An accepted tree this instance cannot read is a fault of the
        # instance, not of the payload -- but the author still asked a question
        # and is owed a typed answer with a repair rather than a bare 500.
        _refuse(
            "cruxible.authoring.lowering_invalid",
            "definition",
            f"The accepted artifacts this Procedure resolves against cannot be read: {exc}",
            repair_kind="restore_accepted_projection",
            repair_description=(
                "Restore the instance's body store so the accepted tree parses, then compile again."
            ),
        )


def _same_revision_content(candidate: BaseModel, previous: BaseModel) -> bool:
    """Compare authored content before introducing a successor link.

    Lifecycle state remains significant; only the link we generate while
    lowering is excluded. Preserve the original bytes for unchanged artifacts.
    """
    exclude = {"lifecycle": {"predecessor_digest"}}
    return candidate.model_dump(mode="json", exclude=exclude) == previous.model_dump(
        mode="json", exclude=exclude
    )


def _refuse_unserved_procedure(definition: dict[str, object]) -> None:
    try:
        refuse_unserved_procedure_nodes(definition)
    except AuthoringInputError as exc:
        _refuse(
            exc.code,
            exc.field_path,
            exc.message,
            repair_kind="replace_definition",
            repair_description=exc.repair,
        )


def _lower_procedure(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    accepted_reference_tree: Mapping[str, bytes] | None = None,
    candidate_identities: frozenset[str] = frozenset(),
    candidate_paths: tuple[str, ...] = (),
) -> LoweredAuthoring:
    payload = intent.payload
    assert isinstance(payload, ProcedureAuthoringPayload)
    if payload.retire:
        target = procedure_path(str(payload.definition["name"]))
        if target in base_tree:
            retained_predecessor = parse_procedure(base_tree[target], path=target)
            retired = retained_predecessor.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        state="retired",
                        predecessor_digest=procedure_artifact_digest(retained_predecessor).tagged,
                    )
                }
            )
            return _procedure_lowered(
                instance, retired, retained_predecessor, path=target, base=base, base_tree=base_tree
            )
    if "procedure_spec" in payload.definition:
        from cruxible_client.contracts.procedures.authored import ProcedureSpec, ProcedureSpecError
        from cruxible_core.authoring.procedure_spec import resolve_procedure_spec

        try:
            spec = ProcedureSpec.model_validate(payload.definition["procedure_spec"])
            retiring = payload.retire
            payload = resolve_procedure_spec(
                instance,
                base,
                base_tree,
                spec,
                candidate_paths,
                named=base.compiler == LIGHT_ARTIFACTS_COMPILER,
            )
            payload = payload.model_copy(update={"retire": retiring})
        except ProcedureSpecError as exc:
            _refuse(
                exc.code,
                exc.field_path,
                exc.message,
                repair_kind="replace_definition",
                repair_description=exc.repair,
            )
        except ValueError as exc:
            _refuse(
                "cruxible.authoring.procedure_definition_invalid",
                "$.steps",
                str(exc),
                repair_kind="replace_definition",
                repair_description="Repair the authored Procedure.",
            )
    if not payload.retire:
        _refuse_unserved_procedure(payload.definition)
    if payload.definition.get("graph_format") == 7:
        return _lower_named_procedure(instance, payload, base=base, base_tree=base_tree)
    source_authoring = "source_request" in payload.definition
    accepted: dict[str, tuple[str, str]] = {}
    candidate_artifacts: dict[str, tuple[str, str]] = {}
    if source_authoring:
        from cruxible_client.contracts.procedures.source_compiler import SourceCompileError
        from cruxible_client.contracts.procedures.source_requests import ProcedureSourceRequest
        from cruxible_core.authoring.procedure_source import (
            accepted_build_resolver,
            resolve_indexed_source,
        )
        from cruxible_core.indexes.evaluated_state import EvaluationRows

        try:
            if set(payload.definition) != {"name", "source_request"}:
                raise ValueError("Source authoring accepts a name and source_request only")
            source_request = ProcedureSourceRequest.model_validate(
                payload.definition["source_request"]
            )
            if source_request.name != payload.definition["name"]:
                raise ValueError("The Procedure name must match its source request")
            with instance.bind_accepted_projection(base) as projection:
                rows = EvaluationRows(projection)
                # Only already-staged non-Procedure siblings enter this selection.
                # Child Procedures continue to resolve at the accepted base.
                edits = {path: base_tree[path] for path in candidate_paths if path in base_tree}
                selected = rows.overlay(edits) if edits else rows
                # A bare provider name in a selection binds its most recently
                # registered live build at the base; the definition pins it exactly.
                compiled = resolve_indexed_source(
                    source_request,
                    selected,
                    builds=accepted_build_resolver(instance, base, rows),
                )
                policy = getattr(payload, "acquisition_policy", None)
                if policy is not None:
                    identity = "SourceAcquisitionPolicy:" + policy
                    for view, destination in ((rows, accepted), (selected, candidate_artifacts)):
                        row = view.connection.execute(
                            f"SELECT path,artifact_digest FROM {view.table('artifact_lookup')} "
                            "WHERE identity=?",
                            (identity,),
                        ).fetchone()
                        if row is not None:
                            destination[identity] = (row[0], row[1])
            payload = type(payload)(
                definition=compiled.definition.model_dump(mode="json", by_alias=True),
                activation_policy=payload.activation_policy,
                owned_contracts=tuple(
                    ProcedureOwnedContract(
                        identity=ArtifactIdentity(kind="Contract", name=contract.name),
                        schema=contract.schema_,
                    )
                    for contract in compiled.contracts
                ),
                acquisition_policy=payload.acquisition_policy,
                retire=payload.retire,
            )
            if base.compiler == LIGHT_ARTIFACTS_COMPILER:
                from cruxible_client.contracts.procedures.names import named_graph

                # The compiler's transient graph may retain a native Query.
                # Select graph 7 before validating a serialized graph-6 model,
                # whose frozen source bindings cannot contain v3 Queries.
                payload = payload.model_copy(update={"definition": named_graph(payload.definition)})
                _refuse_unserved_procedure(payload.definition)
                return _lower_named_procedure(instance, payload, base=base, base_tree=base_tree)
        except (SourceCompileError, ValueError) as exc:
            diagnostic = exc.diagnostic if isinstance(exc, SourceCompileError) else None
            _refuse(
                diagnostic.code if diagnostic else "cruxible.authoring.procedure_source_invalid",
                "definition.source_request",
                (
                    f"{diagnostic.span.filename}:{diagnostic.span.line}:"
                    f"{diagnostic.span.column + 1}: {diagnostic.message}"
                    if diagnostic
                    else str(exc)
                ),
                repair_kind="replace_definition",
                repair_description="Repair the indicated source and prepare again.",
            )
    else:
        references = _reference_identities(payload.definition)
        if payload.acquisition_policy is not None:
            try:
                references.add(
                    ArtifactIdentity(
                        kind="SourceAcquisitionPolicy", name=payload.acquisition_policy
                    )
                )
            except ValueError:
                # _acquisition_policy_pin retains the typed invalid-name refusal.
                pass
        parsed = _parse_reference_tree(
            instance,
            _reference_blobs(
                base_tree if accepted_reference_tree is None else accepted_reference_tree,
                references,
                base=base,
            ),
            base=base,
        )
        duplicates: set[str] = set()
        for envelope in parsed.envelopes:
            if envelope.identity in accepted:
                duplicates.add(envelope.identity)
            accepted[envelope.identity] = (envelope.path, envelope.artifact_digest)
        for duplicate_identity in duplicates:
            accepted.pop(duplicate_identity, None)
        if candidate_identities:
            candidate_parsed = _parse_reference_tree(
                instance,
                _reference_blobs(
                    base_tree,
                    (item for item in references if item.qualified in candidate_identities),
                    base=base,
                ),
                base=base,
            )
            for envelope in candidate_parsed.envelopes:
                if envelope.identity in candidate_identities:
                    candidate_artifacts[envelope.identity] = (
                        envelope.path,
                        envelope.artifact_digest,
                    )
    owned_contracts = (
        {contract.identity.name: contract for contract in payload.owned_contracts}
        if isinstance(payload, ProcedureAuthoringPayload)
        else None
    )
    resolved_definition = (
        payload.definition
        if source_authoring
        else _resolve_authoring_references(
            payload.definition,
            accepted=accepted,
            candidates=candidate_artifacts,
            candidate_identities=candidate_identities,
            owned_contracts=owned_contracts,
        )
    )
    if not source_authoring:
        # Authoring carries names; only this transient compiler input has exact
        # implementation metadata. Governed graph7 drops these mirrors.
        from copy import deepcopy

        from cruxible_client.contracts.provider_interfaces import interface_definition_digest

        resolved_definition = deepcopy(resolved_definition)
        for node in cast(Any, resolved_definition).get("nodes", ()):
            for occurrence in (node, *node.get("body", ())):
                if (
                    occurrence.get("kind") not in {"call", "source"}
                    and occurrence.get("operation") != "call"
                ):
                    continue
                interface_ref = occurrence.get("interface")
                provider_ref = occurrence.get("provider")
                if not isinstance(interface_ref, dict) or not isinstance(provider_ref, dict):
                    continue
                interface_path = provider_interface_path(interface_ref["target"]["name"])
                registration = parse_provider_interface(
                    base_tree[interface_path], path=interface_path
                )
                occurrence["interface_digest"] = interface_definition_digest(registration)
                if "slot_name" in provider_ref:
                    for declaration in cast(Any, resolved_definition).get("pin_slots", ()):
                        if declaration["slot_name"] == provider_ref["slot_name"]:
                            declaration.setdefault("pin_role", "provider")
                            declaration.setdefault("artifact_kind", "Provider")
                            declaration["interface_digest"] = occurrence["interface_digest"]
                    continue
                provider_member_path = provider_path(provider_ref["target"]["name"])
                provider_value = parse_provider(
                    base_tree[provider_member_path], path=provider_member_path
                )
                if not isinstance(provider_value, ProviderV2):
                    _refuse(
                        "cruxible.authoring.provider_runtime_required",
                        "definition",
                        "Provider occurrence requires an installed runtime build.",
                        repair_kind="replace_binding",
                        repair_description="Select an installed provider build.",
                    )
                matches = tuple(
                    row
                    for row in provider_value.implementations
                    if row.interface_id == registration.interface_id
                    and row.interface_digest == interface_definition_digest(registration)
                )
                if len(matches) != 1:
                    _refuse(
                        "cruxible.authoring.provider_implementation_ambiguous",
                        "definition",
                        "Select one fitting provider implementation",
                        repair_kind="replace_binding",
                        repair_description=(
                            "Bind a provider implementing the named interface exactly once."
                        ),
                    )
                occurrence["interface_digest"] = matches[0].interface_digest
                occurrence["implementation_digest"] = matches[0].implementation_digest
    definition: ProcedureDefinition
    try:
        definition = ProcedureDefinition.model_validate(resolved_definition)
    except (ProcedureGraphFormatError, ValidationError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ProcedureGraphFormatError)
            else " | ".join(_validation_error_lines(exc, root="definition"))
        )
        _refuse(
            "cruxible.authoring.procedure_definition_invalid",
            "definition",
            "The lowered Procedure definition is invalid: " + message,
            repair_kind="replace_definition",
            repair_description="Repair the indicated definition field.",
        )
    identity = ArtifactIdentity(kind="Procedure", name=definition.name)
    path = procedure_path(definition.name)
    predecessor: ProcedureArtifact | None = None
    if path in base_tree:
        predecessor = parse_procedure(base_tree[path], path=path)
    declared_pins = {
        binding for binding in iter_pin_bindings(definition) if isinstance(binding, ArtifactPin)
    }
    # Validate an explicit name at authoring, but runs resolve its accepted
    # value at admission; the Procedure never pins the policy's bytes.
    _acquisition_policy_pin(
        payload,
        accepted=accepted,
        candidates=candidate_artifacts,
        candidate_identities=candidate_identities,
    )
    pins = tuple(
        sorted(
            declared_pins,
            key=lambda item: (
                item.role.encode("utf-8"),
                item.target.qualified.encode("utf-8"),
                item.artifact_digest.encode("ascii"),
            ),
        )
    )
    if isinstance(payload, ProcedureAuthoringPayload):
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
                "cruxible.authoring.procedure_definition_invalid",
                "owned_contracts",
                "The Procedure declares owned Contracts that its graph does not reference: "
                + ", ".join(unreferenced_contracts),
                repair_kind="replace_contracts_or_definition",
                repair_description=(
                    "Remove each unused owned Contract or reference it from the Procedure graph."
                ),
            )

    if isinstance(payload, ProcedureAuthoringPayload) and definition.budget.max_items is not None:

        def carries_list(contract: ProcedureOwnedContract) -> bool:
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
                "cruxible.authoring.procedure_definition_invalid",
                "definition.budget.max_items",
                "The lowered Procedure definition declares max_items but none of "
                "its pinned Contracts declares a list field.",
                repair_kind="replace_definition",
                repair_description=(
                    "Declare a list field on a pinned Contract or remove budget.max_items."
                ),
            )
    lifecycle = ArtifactLifecycle(
        state="retired" if payload.retire else "live",
        predecessor_digest=(None if predecessor is None else _envelope_digest(predecessor)),
    )
    if (
        definition.open_slots or definition.pin_slots
    ) and base.compiler != LIGHT_ARTIFACTS_COMPILER:
        _refuse(
            "cruxible.authoring.procedure_definition_invalid",
            "definition",
            "Provider slots require compiler revision 36 and graph 7.",
            repair_kind="upgrade_compiler",
            repair_description="Upgrade the compiler and author a Procedure with slots.",
        )
    try:
        format_arguments: dict[str, Any] = {"artifact_format": "playbill-procedure-v2"}
        procedure = ProcedureArtifact(
            **format_arguments,
            identity=identity,
            definition=definition,
            definition_digest=compute_procedure_definition_digest(definition).tagged,
            pins=pins,
            owned_contracts=payload.owned_contracts,
            activation_policy=payload.activation_policy,
            blueprint=None if predecessor is None else predecessor.blueprint,
            lifecycle=lifecycle,
        )
    except ValidationError as exc:
        _refuse(
            "cruxible.authoring.procedure_definition_invalid",
            "procedure",
            "The lowered Procedure artifact is invalid: "
            + " | ".join(_validation_error_lines(exc, root="procedure")),
            repair_kind="replace_definition_or_contracts",
            repair_description=(
                "Repair the Procedure definition or its owned Contract declarations."
            ),
        )
    if isinstance(procedure, ProcedureArtifact):
        procedure = procedure.model_copy(update={"acquisition_policy": payload.acquisition_policy})
    return _procedure_lowered(
        instance, procedure, predecessor, path=path, base=base, base_tree=base_tree
    )


def _lower_named_procedure(
    instance: PlaybillInstance,
    payload: ProcedureAuthoringPayload,
    *,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
) -> LoweredAuthoring:
    """Validate the authored named tree directly, without an exact-pin graph."""
    from cruxible_client.contracts.procedures.artifacts import NamedProcedureArtifact
    from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition

    if base.compiler != LIGHT_ARTIFACTS_COMPILER:
        _refuse(
            "cruxible.authoring.procedure_definition_invalid",
            "definition.graph_format",
            "Named graph 7 requires compiler revision 36.",
            repair_kind="upgrade_compiler",
            repair_description="Upgrade the compiler.",
        )
    try:
        definition = NamedProcedureDefinition.model_validate(payload.definition)
        path = procedure_path(definition.name)
        predecessor = parse_procedure(base_tree[path], path=path) if path in base_tree else None
        if payload.acquisition_policy is not None:
            policy_path = acquisition_policy_path(payload.acquisition_policy)
            policy = parse_acquisition_policy(base_tree[policy_path], path=policy_path)
            if policy.lifecycle.state != "live":
                raise ValueError("The named acquisition policy must be live")
        procedure = NamedProcedureArtifact.model_validate(
            dict(
                identity=ArtifactIdentity(kind="Procedure", name=definition.name),
                definition=definition,
                definition_digest=compute_procedure_definition_digest(definition).tagged,
                owned_contracts=payload.owned_contracts,
                activation_policy=payload.activation_policy,
                acquisition_policy=payload.acquisition_policy,
                lifecycle=ArtifactLifecycle(
                    state="retired" if payload.retire else "live",
                    predecessor_digest=None
                    if predecessor is None
                    else procedure_artifact_digest(predecessor).tagged,
                ),
            )
        )
    except (ValueError, KeyError, ProcedureGraphFormatError) as exc:
        _refuse(
            "cruxible.authoring.procedure_definition_invalid",
            "definition",
            str(exc),
            repair_kind="replace_definition",
            repair_description="Repair the named Procedure definition.",
        )
    return _procedure_lowered(
        instance,
        procedure,
        predecessor,
        path=path,
        base=base,
        base_tree=base_tree,
    )


def _envelope_digest(envelope: ProcedureArtifact) -> str:
    return procedure_artifact_digest(envelope).tagged


def _procedure_lowered(
    instance: PlaybillInstance,
    procedure: ProcedureArtifact,
    predecessor: ProcedureArtifact | None,
    *,
    path: str,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
) -> LoweredAuthoring:
    if base.compiler == LIGHT_ARTIFACTS_COMPILER and int(procedure.definition.graph_format) != 7:
        named = name_procedure(procedure)
        assert isinstance(named, ProcedureArtifact)
        procedure = named
    candidate_tree = fork_tree(base_tree)
    if predecessor is not None and _same_revision_content(procedure, predecessor):
        procedure = predecessor
        procedure_bytes = base_tree[path]
    else:
        procedure_bytes = render_procedure(procedure)
    candidate_tree[path] = procedure_bytes
    changed = () if base_tree.get(path) == procedure_bytes else ((path, procedure_bytes),)
    return LoweredAuthoring(
        proposed_tree=candidate_tree,
        resolved_authoring={
            "artifact_digest": _envelope_digest(procedure),
            "changed_members": _encoded_members(changed),
            "definition": procedure.definition.model_dump(mode="json", by_alias=True),
            "definition_digest": procedure.definition_digest,
            "identity": procedure.identity.model_dump(mode="json"),
            "pins": [pin.model_dump(mode="json") for pin in procedure.pins],
            "predecessor_digest": procedure.lifecycle.predecessor_digest,
        },
        changed_members=changed,
        idempotent=not changed and base.git_oid == instance.accepted_coordinate().git_oid,
    )


def _pin_query_contract_ref(
    ref: ContractRef, *, tree: Mapping[str, bytes], label: str
) -> ContractRef:
    """Pin ``ref`` to its accepted digest, the way a nested Contract pin is lowered.

    An author writes ``name@N`` unpinned; lowering resolves it against the
    candidate tree the same way :func:`render_contract_member` pins a
    Contract's own nested refs, so the stored QueryDefinition never carries a
    parameter_contract or row_contract without an exact digest.
    """

    target_path = contract_path(ref.name, ref.version)
    content = tree.get(target_path)
    if content is None:
        _refuse(
            "cruxible.query_definition.contract_unresolved",
            "query_definition",
            f"The {label} Contract {ref.spelling} is not accepted.",
            repair_kind="replace_input",
            repair_description=f"Accept {ref.spelling} first, or in the same change set.",
        )
    digest = contract_digest(parse_contract(content, path=target_path)).tagged
    if ref.digest is not None and ref.digest != digest:
        _refuse(
            "cruxible.query_definition.contract_unresolved",
            "query_definition",
            f"The {label} Contract {ref.spelling} is accepted at another digest.",
            repair_kind="replace_input",
            repair_description=(
                f"Write {ref.spelling} without a digest; lowering pins the accepted version."
            ),
        )
    return ref.model_copy(update={"digest": digest})


def _render_non_procedure_member(
    payload: ResolutionContractAuthoringPayload
    | AttestationAuthoringPayload
    | SubjectAuthoringPayload
    | QueryDefinitionAuthoringPayload
    | ClaimTypeAuthoringPayload
    | ApprovalPolicyAuthoringPayload
    | ProcedureRuntimePolicyAuthoringPayload
    | CaptureContractAuthoringPayload
    | SourceAcquisitionPolicyAuthoringPayload,
    *,
    tree: Mapping[str, bytes] | None = None,
    light: bool = False,
    query_paths: tuple[str, ...] | None = None,
) -> tuple[str, bytes, str]:
    if isinstance(payload, ResolutionContractAuthoringPayload):
        from cruxible_client.contracts.resolution_contracts import (
            render_resolution_contract,
            resolution_contract_digest,
            resolution_contract_path,
        )

        contract_value = payload.resolution_contract
        return (
            resolution_contract_path(contract_value.identity.name),
            render_resolution_contract(contract_value),
            resolution_contract_digest(contract_value).tagged,
        )
    if isinstance(payload, AttestationAuthoringPayload):
        from cruxible_client.contracts.accepted_attestations import (
            attestation_artifact_digest,
            attestation_identity,
            attestation_path,
            render_accepted_attestation,
        )

        value = payload.attestation
        return (
            attestation_path(attestation_identity(value).name),
            render_accepted_attestation(value),
            attestation_artifact_digest(value).tagged,
        )
    if isinstance(payload, CaptureContractAuthoringPayload):
        if isinstance(payload.capture_contract, CaptureContractInputRecord):
            record = payload.capture_contract
            path = capture_contract_path(record.name)
            previous_bytes = None if tree is None else tree.get(path)
            previous = (
                None
                if previous_bytes is None
                else parse_capture_contract(previous_bytes, path=path)
            )
            contract = lower_capture_contract_input(record, predecessor=previous, named=light)
        else:
            contract = payload.capture_contract
        return (
            capture_contract_path(contract.identity.name),
            render_capture_contract(contract),
            capture_contract_digest(contract).tagged,
        )
    if isinstance(payload, SourceAcquisitionPolicyAuthoringPayload):
        policy = payload.acquisition_policy
        return (
            acquisition_policy_path(policy.identity.name),
            render_acquisition_policy(policy),
            acquisition_policy_digest(policy).tagged,
        )
    if isinstance(payload, ClaimTypeAuthoringPayload):
        definition = payload.claim_type
        return (
            claim_type_path(definition.predicate),
            render_claim_type(definition),
            claim_type_digest(definition).tagged,
        )
    if isinstance(payload, SubjectAuthoringPayload):
        shell = payload.subject
        path = subject_path(shell.subject_kind, shell.subject_id)
        if tree is not None and path not in tree:
            _refuse_unassigned_subject(shell.subject_kind, shell.subject_id, element="subject")
        return (path, render_subject(shell), subject_digest(shell).tagged)
    if isinstance(payload, QueryDefinitionAuthoringPayload):
        from cruxible_client.contracts.query.authored import (
            QuerySpecError,
            QuerySpecInput,
            lower_query_spec,
        )
        from cruxible_core.service.discovery.authored_query import (
            AuthoredQueryResolver,
            candidate_query_vocabulary,
        )
        from cruxible_core.service.read_refusals import ReadRefusalError

        source = payload.query_definition
        query: QueryDefinition
        if isinstance(source, QuerySpecInput):
            try:
                query = lower_query_spec(
                    source,
                    AuthoredQueryResolver(
                        candidate_query_vocabulary(tree or {}, paths=query_paths)
                    ),
                    named=light,
                )
            except (QuerySpecError, ReadRefusalError) as exc:
                _refuse(
                    exc.error_code,
                    exc.field_path or "query_definition",
                    str(exc),
                    repair_kind="replace_input",
                    repair_description="Correct the authored path using the candidate vocabulary.",
                )
            except ValidationError as exc:
                _refuse(
                    "cruxible.authoring.query_definition_invalid",
                    "$",
                    str(exc),
                    repair_kind="replace_input",
                    repair_description=(
                        "Correct the authored fields or budgets to satisfy the query law."
                    ),
                )
        else:
            query = source
        if light:
            from cruxible_client.contracts.query.definitions import NamedQueryDefinition

            if not isinstance(query, NamedQueryDefinition):
                query = NamedQueryDefinition.model_validate(
                    {
                        **query.model_dump(mode="python", exclude={"pins", "artifact_format"}),
                        "artifact_format": "cruxible-query-definition-v3",
                    }
                )
        previous_query: QueryDefinition | None = None
        previous_content = None
        if isinstance(source, QuerySpecInput) and tree is not None:
            previous_content = tree.get(query_definition_path(query.identity.name))
            if previous_content is not None:
                previous_query = parse_query_definition(
                    previous_content, path=query_definition_path(query.identity.name)
                )
                if previous_query.lifecycle.state == "retired":
                    _refuse(
                        "cruxible.query_definition.lifecycle_invalid",
                        "$.query",
                        "A retired QueryDefinition cannot be revived or revised.",
                        repair_kind="replace_input",
                        repair_description="Author a query under a new identity.",
                    )
                query = query.model_copy(
                    update={
                        "lifecycle": ArtifactLifecycle(
                            predecessor_digest=query_definition_digest(previous_query).tagged
                        )
                    }
                )
        if tree is not None:
            pins = {} if light else {(pin.role, pin.target.qualified): pin for pin in query.pins}
            for predicate in query.referenced_predicates:
                target_path = claim_type_path(predicate)
                content = tree.get(target_path)
                if content is None:
                    _refuse(
                        "cruxible.authoring.claim_type_missing",
                        "query_definition",
                        f"Query predicate {predicate!r} is absent from the candidate vocabulary.",
                        repair_kind="replace_input",
                        repair_description="Add the ClaimType or correct the predicate.",
                    )
                target = parse_claim_type(content, path=target_path)
                if target.lifecycle.state != "live":
                    _refuse(
                        "cruxible.authoring.claim_type_missing",
                        "query_definition",
                        "Query predicates must reference live ClaimTypes.",
                        repair_kind="replace_input",
                        repair_description="Choose a live ClaimType.",
                    )
                if light:
                    continue
                pin = ArtifactPin(
                    role=CLAIM_TYPE_PIN_ROLE,
                    target=target.identity,
                    artifact_digest=claim_type_digest(target).tagged,
                )
                key = (pin.role, pin.target.qualified)
                if key in pins and pins[key] != pin:
                    _refuse(
                        "cruxible.authoring.query_pin_mismatch",
                        "query_definition.pins",
                        "Explicit query pin differs from the resolved definition.",
                        repair_kind="replace_input",
                        repair_description="Use the accepted pin or omit it for base resolution.",
                    )
                pins[key] = pin
            for attr, label in (("parameter_contract", "parameter"), ("row_contract", "row")):
                ref = getattr(query, attr)
                if ref is not None:
                    query = query.model_copy(
                        update={attr: _pin_query_contract_ref(ref, tree=tree, label=label)}
                    )
            if not light:
                body = query.model_dump(mode="json")
                body["pins"] = [
                    p.model_dump(mode="json")
                    for p in sorted(
                        pins.values(), key=lambda p: (p.role, p.target.qualified, p.artifact_digest)
                    )
                ]
                query = QueryDefinition.model_validate(body)
        if previous_query is not None and _same_revision_content(query, previous_query):
            assert previous_content is not None
            return (
                query_definition_path(query.identity.name),
                previous_content,
                query_definition_digest(previous_query).tagged,
            )
        return (
            query_definition_path(query.identity.name),
            render_query_definition(query),
            query_definition_digest(query).tagged,
        )
    if isinstance(payload, ApprovalPolicyAuthoringPayload):
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


def _line_provider_resolver(
    instance: PlaybillInstance, base: AcceptedProjectionCoordinate, tree: Mapping[str, bytes]
) -> Callable[[str], str]:
    from cruxible_core.providers.builds import resolve_in_tree

    return lambda name: (
        resolve_in_tree(instance, base, tree, name.removeprefix("Provider:")).resolved
    )


def _render_line_member(
    payload: LineAuthoringPayload,
    *,
    tree: Mapping[str, bytes],
    light: bool = False,
    resolve_provider: Callable[[str], str] | None = None,
) -> tuple[str, bytes, str]:
    """Lower one Line decision into the exact LineSpec its accepted closure pins.

    The Procedure and the acquisition policy are named; both must be present in
    the staged tree -- accepted at the base or authored earlier in the same
    set -- and lowering pins their exact digests. A Line embeds no trigger; a
    ``trigger_input`` Line declares the exact event its Source input accepts,
    read from that Source's pinned CaptureContract.
    """

    procedure_target = procedure_path(payload.procedure_name)
    procedure_content = tree.get(procedure_target)
    if procedure_content is None:
        _refuse(
            "cruxible.authoring.line_procedure_missing",
            "procedure_name",
            "Line authoring requires the named accepted or same-ChangeSet Procedure.",
            repair_kind="replace_procedure_name",
            repair_description="Use a Procedure name present at the authoring coordinate.",
        )
    procedure = parse_procedure(procedure_content, path=procedure_target)
    policy_pin: ArtifactPin | None = None
    if payload.acquisition_policy_name is None:
        # Line law resolves the reserved default at the candidate coordinate.
        pass
    else:
        policy_target = acquisition_policy_path(payload.acquisition_policy_name)
        policy_content = tree.get(policy_target)
        if policy_content is None:
            _refuse(
                "cruxible.authoring.line_acquisition_policy_missing",
                "acquisition_policy_name",
                "Line authoring requires the named accepted or same-ChangeSet "
                "SourceAcquisitionPolicy.",
                repair_kind="replace_acquisition_policy_name",
                repair_description=(
                    "Use a SourceAcquisitionPolicy name present at the authoring coordinate, "
                    "or omit it when the Procedure has no Source nodes."
                ),
            )
        policy = parse_acquisition_policy(policy_content, path=policy_target)
        policy_pin = ArtifactPin(
            role=ACQUISITION_POLICY_PIN_ROLE,
            target=policy.identity,
            artifact_digest=acquisition_policy_digest(policy).tagged,
        )
    exhaust_taps = sorted(
        node.node_id for node in procedure.definition.nodes if isinstance(node, ExhaustTapNode)
    )
    if exhaust_taps:
        _refuse(
            "cruxible.authoring.line_exhaust_tap_unsupported",
            "procedure_name",
            f"Procedure {payload.procedure_name!r} reads exhaust through {exhaust_taps}; no v1 "
            "run path admits an exhaust_tap node, so a Line over it could never run.",
            repair_kind="replace_procedure_name",
            repair_description="Name a Procedure without exhaust_tap nodes.",
        )
    procedure_pin = ArtifactPin(
        role="procedure",
        target=procedure.identity,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    _check_line_parameters(payload, procedure=procedure, procedure_path_value=procedure_target)
    caps = procedure.definition.hard_caps
    budgets = (
        {
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
            "max_provider_calls": caps.max_provider_calls,
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
        }
        if payload.budgets is None
        else dict(payload.budgets)
    )
    path = line_spec_path(payload.name)
    previous_content = tree.get(path)
    predecessor_digest = None
    if previous_content is not None:
        previous = parse_line_spec(previous_content, path=path)
        predecessor_digest = line_spec_digest(previous).tagged
    trigger_event: CaptureEventSelector | None = None
    trigger_pins: tuple[ArtifactPin, ...] = ()
    if payload.trigger_input is not None:
        sources = [
            node
            for node in procedure.definition.nodes
            if getattr(node, "as_", None) == payload.trigger_input
        ]
        contract = (
            sources[0].capture_contract
            if len(sources) == 1 and isinstance(sources[0], SourceNode)
            else None
        )
        if isinstance(contract, ArtifactNameRef):
            contract_path = capture_contract_path(contract.target.name)
            accepted_contract = parse_capture_contract(tree[contract_path], path=contract_path)
            contract = ArtifactPin(
                role="capture-contract",
                target=contract.target,
                artifact_digest=capture_contract_digest(accepted_contract).tagged,
            )
        if not isinstance(contract, ArtifactPin):
            _refuse(
                "cruxible.authoring.line_trigger_input_invalid",
                "trigger_input",
                f"trigger_input {payload.trigger_input!r} must name exactly one "
                "Source input of the Procedure that pins its CaptureContract exactly.",
                repair_kind="replace_trigger_input",
                repair_description="Name the `as` alias of one Source node, or omit it.",
            )
        assert isinstance(contract, ArtifactPin)
        trigger_event = CaptureEventSelector(
            capture_contract_identity=contract.target,
            capture_contract_digest=contract.artifact_digest,
        )
        trigger_pins = (
            ArtifactPin(
                role="trigger-capture-contract",
                target=contract.target,
                artifact_digest=contract.artifact_digest,
            ),
        )
    line_fields = dict(
        identity=ArtifactIdentity(kind="Line", name=payload.name),
        occurrence_epoch=payload.occurrence_epoch,
        procedure=procedure_pin,
        parameters=payload.parameters,
        acquisition_policy=policy_pin,
        max_authority=(
            payload.max_authority or RUNG_AUTHORITY[procedure.definition.terminal_capability]
        ),
        trigger_input=payload.trigger_input,
        trigger_event=trigger_event,
        budgets=budgets,
        epsilon=payload.epsilon,
        pins=tuple(
            sorted(
                (
                    procedure_pin,
                    *(() if policy_pin is None else (policy_pin,)),
                    *trigger_pins,
                ),
                key=lambda pin: (
                    pin.role.encode("utf-8"),
                    pin.target.qualified.encode("utf-8"),
                    pin.artifact_digest.encode("ascii"),
                ),
            )
        ),
        lifecycle=ArtifactLifecycle(
            state="retired" if payload.retire else "live",
            predecessor_digest=predecessor_digest,
        ),
    )
    line = LineSpec.model_validate(line_fields)
    if light:
        line = name_line(line)
        bindings = {}
        for slot, name in payload.bindings.items():
            selected = (
                name.removeprefix("Provider:")
                if resolve_provider is None
                else resolve_provider(name)
            )
            bindings[slot] = ArtifactNameRef(
                target=ArtifactIdentity(kind="Provider", name=selected)
            )
        line = line.model_copy(update={"bindings": bindings})
    elif payload.bindings:
        _refuse(
            "cruxible.line.compiler_unsupported",
            "bindings",
            "Line slot bindings require compiler revision 36.",
            repair_kind="upgrade_compiler",
            repair_description="Upgrade the compiler.",
        )
    if previous_content is not None and _same_revision_content(line, previous):
        return path, previous_content, line_spec_digest(previous).tagged
    return path, render_line_spec(line), line_spec_digest(line).tagged


def _resolve_trigger_authoring_schedule(
    schedule: AuthoringTriggerSchedule,
    *,
    tree: Mapping[str, bytes],
) -> TriggerSchedule:
    """Read one canonical contract artifact; never consult Captures or journals.

    All authoring adapters preserve the typed schedule until this shared
    candidate-overlay boundary. Stored schedules and runtime matching stay exact.
    """

    if isinstance(schedule, CaptureLandingByNameSchedule):
        name = schedule.capture_contract
        logical_sources = schedule.logical_sources
    elif isinstance(schedule, WindowCloseAuthoringSchedule):
        if isinstance(schedule.window, FixedWindow):
            return WindowCloseSchedule(window=schedule.window)
        name = schedule.window.capture_contract
        logical_sources = schedule.window.logical_sources
    else:
        return schedule
    capture_path = capture_contract_path(name)
    content = tree.get(capture_path)
    try:
        contract = None if content is None else parse_capture_contract(content, path=capture_path)
    except CaptureFormatError:
        contract = None
    if contract is None or contract.lifecycle.state != "live":
        _refuse(
            "cruxible.authoring.trigger_capture_missing",
            "schedule",
            f"{capture_path} must contain a live, kind-correct CaptureContract at this base.",
            repair_kind="replace_schedule",
            repair_description=(
                "Declare or install the named live CaptureContract in this change set, "
                "or name an existing live CaptureContract."
            ),
        )
    event = CaptureEventSelector(
        capture_contract_identity=contract.identity,
        capture_contract_digest=capture_contract_digest(contract).tagged,
        logical_sources=logical_sources,
    )
    if isinstance(schedule, CaptureLandingByNameSchedule):
        return CaptureLandingSchedule(event=event)
    return WindowCloseSchedule(
        window=CaptureEventWindow(event=event, duration_seconds=schedule.window.duration_seconds)
    )


def _resolved_trigger_schedule(content: bytes, *, path: str) -> dict[str, object]:
    """Expose the exact event identity and digest in prepare/preview output."""

    schedule = parse_trigger(content, path=path).schedule
    selector = schedule_capture_selector(schedule)
    return {} if selector is None else {"capture_contract": selector.model_dump(mode="json")}


def _render_trigger_member(
    payload: TriggerAuthoringPayload,
    *,
    tree: Mapping[str, bytes],
    light: bool = False,
) -> tuple[str, bytes, str]:
    """Lower one Trigger decision; a Line target is named by identity, never pinned.

    A live Trigger's Line must be present in the staged tree -- accepted at the
    base or authored earlier in the same set -- and an event schedule's exact
    CaptureContract must be too. Whether the Line is live and accepts the event
    is the Trigger law's to judge at acceptance.
    """

    path = trigger_path(payload.name)
    previous_content = tree.get(path)
    previous = None if previous_content is None else parse_trigger(previous_content, path=path)
    if payload.retire:
        if previous is None:
            _refuse(
                "cruxible.authoring.trigger_missing",
                "name",
                "Retiring a Trigger requires its accepted or same-ChangeSet predecessor.",
                repair_kind="replace_trigger_name",
                repair_description="Name an existing Trigger to retire.",
            )
        if previous.lifecycle.state == "retired":
            assert previous_content is not None
            return path, previous_content, trigger_digest(previous).tagged
        retired = previous.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired",
                    predecessor_digest=trigger_digest(previous).tagged,
                ),
            }
        )
        if light:
            retired = name_trigger(retired)
        return path, render_trigger(retired), trigger_digest(retired).tagged
    if payload.line_name is not None:
        if line_spec_path(payload.line_name) not in tree:
            _refuse(
                "cruxible.authoring.trigger_line_missing",
                "line_name",
                "Trigger authoring requires the named accepted or same-ChangeSet Line.",
                repair_kind="replace_line_name",
                repair_description="Use a Line name present at the authoring coordinate.",
            )
        target: TriggerTarget = LineTarget(
            line=ArtifactRef(
                role=TRIGGER_LINE_REF_ROLE,
                target=ArtifactIdentity(kind="Line", name=payload.line_name),
            )
        )
    else:
        assert payload.action is not None
        target = ActionTarget(action=payload.action)
    schedule = _resolve_trigger_authoring_schedule(payload.schedule, tree=tree)
    selector = schedule_capture_selector(schedule)
    if selector is not None and payload.line_name:
        line_path = line_spec_path(payload.line_name)
        line = parse_line_spec(tree[line_path], path=line_path)
        if line.trigger_event is not None and not schedule_satisfies_input(
            schedule, CaptureEventInput(event=line.trigger_event), by_name=light
        ):
            _refuse(
                "cruxible.trigger.event_not_accepted",
                "schedule",
                f"Resolved contract {selector.capture_contract_identity.qualified} digest "
                f"{selector.capture_contract_digest} differs from Line Source contract "
                f"{line.trigger_event.capture_contract_identity.qualified} digest "
                f"{line.trigger_event.capture_contract_digest}, or sources "
                f"{selector.logical_sources!r} exceed the Line's accepted sources "
                f"{line.trigger_event.logical_sources!r}.",
                repair_kind="replace_schedule",
                repair_description=(
                    "Name the live CaptureContract matching the Line Source, or re-author "
                    "the Line against the resolved CaptureContract."
                ),
            )
    trigger = Trigger(
        identity=ArtifactIdentity(kind="Trigger", name=payload.name),
        schedule=schedule,
        target=target,
        pins=trigger_schedule_pins(schedule),
        lifecycle=ArtifactLifecycle(
            state="live",
            predecessor_digest=None if previous is None else trigger_digest(previous).tagged,
        ),
    )
    if light:
        trigger = name_trigger(trigger)
    if previous_content is not None and previous is not None:
        if _same_revision_content(trigger, previous):
            return path, previous_content, trigger_digest(previous).tagged
    return path, render_trigger(trigger), trigger_digest(trigger).tagged


def _render_subject_kind_member(
    payload: SubjectKindAuthoringPayload,
    *,
    tree: Mapping[str, bytes],
) -> tuple[str, bytes, str]:
    """Lower one SubjectKind decision as the successor of the accepted one, if any.

    Whether its key ClaimTypes qualify is the SubjectKind law's to judge at
    acceptance, against the candidate (so a key ClaimType of the same set counts).
    """

    path = subject_kind_path(payload.name)
    previous_content = tree.get(path)
    previous = None if previous_content is None else parse_subject_kind(previous_content, path=path)
    rule: SubjectKindKeys | SubjectKindMinted = (
        SubjectKindMinted()
        if payload.minted
        else SubjectKindKeys(keys=payload.keys, uniqueness=payload.uniqueness or "unique")
    )
    declared = SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=payload.name),
        description=payload.description,
        identity_rule=rule,
        lifecycle=ArtifactLifecycle(
            state="retired" if payload.retire else "live",
            predecessor_digest=None if previous is None else subject_kind_digest(previous).tagged,
        ),
    )
    if previous_content is not None and previous is not None:
        if _same_revision_content(declared, previous):
            return path, previous_content, subject_kind_digest(previous).tagged
    return path, render_subject_kind(declared), subject_kind_digest(declared).tagged


def _render_contract_member(
    payload: ContractAuthoringPayload,
    *,
    tree: Mapping[str, bytes],
) -> tuple[str, bytes, str]:
    """Lower one Contract version; contract_lowering owns the pinning and succession."""

    from cruxible_core.authoring.contract_lowering import (
        ContractLoweringRefusal,
        render_contract_member,
    )

    try:
        return render_contract_member(payload, tree=tree)
    except ContractLoweringRefusal as exc:
        _refuse(
            exc.code,
            "definition",
            exc.message,
            repair_kind="replace_definition",
            repair_description=exc.repair,
        )


def _contract_fields_summary(
    procedure: ProcedureArtifact, contract: ArtifactPin | ArtifactNameRef
) -> dict[str, str]:
    for owned in procedure.owned_contracts:
        if owned.identity == contract.target and (
            isinstance(contract, ArtifactNameRef)
            or procedure_owned_contract_digest(owned).tagged == contract.artifact_digest
        ):
            return {
                name: field.type + (" (optional)" if field.optional else "")
                for name, field in sorted(owned.contract_schema.fields.items())
            }
    return {}


def _check_line_parameters(
    payload: LineAuthoringPayload,
    *,
    procedure: ProcedureArtifact,
    procedure_path_value: str,
) -> None:
    """Refuse Line parameters the Procedure's input contract would refuse at run time.

    Every Line run hands `parameters` to the Procedure as its input record, and
    the executor validates that record against the Procedure's own input
    Contract. The same validator runs here, so a mismatch refuses when the Line
    is authored rather than on every later run.
    """

    contract = procedure.definition.contract_in
    if not isinstance(contract, ArtifactPin | ArtifactNameRef):
        return
    accepted = AcceptedProcedure(
        path=procedure_path_value,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    try:
        OwnedProcedureContractValidator(accepted).validate_contract(
            contract=contract,
            payload=normalize_canonical(payload.parameters),
            direction="input",
        )
    except ProcedureContractValidationError as exc:
        field_path = exc.field_path
        _refuse(
            "cruxible.authoring.line_parameters_refused",
            "parameters" if not field_path else f"parameters.{field_path}",
            f"Line parameters are the input record of Procedure {payload.procedure_name!r}, "
            f"and its input contract {contract.target.name!r} refuses them: {exc}",
            repair_kind="replace_parameters",
            repair_description=(
                "Set parameters to a record the Procedure's input contract accepts; "
                "the replacement lists its fields and types."
            ),
            replacement=_contract_fields_summary(procedure, contract),
        )


def _mandate_alias_resolver(
    instance: PlaybillInstance,
    base: AcceptedProjectionCoordinate,
    tree: Mapping[str, bytes],
) -> Callable[[str, str], str]:
    """Resolve a Subject reference a mandate names through its aliases, at ``base``.

    A reference already in ``tree`` stands as written; one that names no Subject
    resolves to the one live Subject of its kind an alias names; several refuse
    ``cruxible.authoring.alias_ambiguous`` naming each.
    """

    def resolve(value: str, element: str) -> str:
        parts = split_reference(value)
        if parts is None:
            return value
        _prefix, kind, subject_id = parts
        if _live_subject_in(tree, f"subjects/{kind}/{subject_id}.json"):
            return value
        with instance.bind_accepted_projection(base) as projection:
            found = resolve_reference_text(projection.typed.connection, value)
        if found is None:
            return value
        spelled, resolution = found
        if resolution.ambiguous:
            _refuse(
                "cruxible.authoring.alias_ambiguous",
                element,
                f"{kind}/{subject_id} names no Subject, and an alias of {kind} names "
                f"{len(resolution.candidates)}: {', '.join(resolution.candidates)}.",
                repair_kind="replace_subject",
                repair_description="Name one of the listed Subjects as kind/id.",
                replacement={"candidates": list(resolution.candidates)},
            )
        return spelled

    return resolve


def _render_procedure_mandate_member(
    payload: ProcedureMandateAuthoringPayload,
    *,
    tree: Mapping[str, bytes],
    resolve: Callable[[str, str], str] | None = None,
) -> tuple[str, bytes, str]:
    if payload.grants == "propose":
        # A propose grant lets a run propose; review decides. Settle-only
        # fields are refused by name here rather than as a raw model dump.
        settle_only = [
            name
            for name, present in (
                ("scope", bool(payload.scope)),
                ("condition", payload.condition is not None),
                ("subject_scope", payload.subject_scope is not None),
                ("subject_creation", bool(payload.subject_creation)),
            )
            if present
        ]
        if settle_only:
            _refuse(
                "cruxible.authoring.procedure_mandate_propose_scope",
                settle_only[0],
                "A propose grant carries no settle scope, condition, subject scope or subject "
                f"creation; this one sets {', '.join(settle_only)}.",
                repair_kind="remove_settle_fields",
                repair_description=(
                    f"Remove {', '.join(settle_only)}, or grant settle (grants: settle) to "
                    "let the run settle within that scope."
                ),
            )
    target_path = procedure_path(payload.procedure_name)
    target_content = tree.get(target_path)
    if target_content is None:
        _refuse(
            "cruxible.authoring.procedure_mandate_procedure_missing",
            "procedure_name",
            "ProcedureMandate authoring requires the named accepted or same-ChangeSet Procedure.",
            repair_kind="replace_procedure_name",
            repair_description="Use a Procedure name present at the authoring coordinate.",
        )
    procedure = parse_procedure(target_content, path=target_path)
    path = procedure_mandate_path(payload.name)
    previous_content = tree.get(path)
    predecessor_digest = None
    previous = None
    if previous_content is not None:
        previous = parse_procedure_mandate_any(previous_content, path=path)
        predecessor_digest = procedure_mandate_digest(previous).tagged
    scope: list[MandateClaimScope] = []
    for index, item in enumerate(payload.scope):
        type_path = claim_type_path(item.claim_type)
        type_content = tree.get(type_path)
        if type_content is None:
            _refuse(
                "cruxible.authoring.procedure_mandate_claim_type_missing",
                f"scope[{index}].claim_type",
                f"Settle scope ClaimType {item.claim_type!r} is not accepted or staged.",
                repair_kind="replace_claim_type",
                repair_description="Name a ClaimType present at the authoring coordinate.",
            )
        claim_type = parse_claim_type(type_content, path=type_path)
        scope.append(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=claim_type.identity,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                ),
                change_kinds=tuple(
                    kind for kind in MANDATE_CHANGE_KIND_ORDER if kind in item.change_kinds
                ),
                binding_subject_role=item.binding_subject_role,
            )
        )
    condition = None
    if payload.condition is not None:
        query_path = query_definition_path(payload.condition.query_name)
        query_content = tree.get(query_path)
        if query_content is None:
            _refuse(
                "cruxible.authoring.procedure_mandate_condition_query_missing",
                "condition.query_name",
                f"Condition query {payload.condition.query_name!r} is not accepted or staged.",
                repair_kind="replace_query_name",
                repair_description="Name a QueryDefinition present at the authoring coordinate.",
            )
        query = parse_query_definition(query_content, path=query_path)
        fixed = dict(payload.condition.fixed_parameters)
        if resolve is not None:
            # A fixed Subject reference names its Subject through its aliases.
            for declared in query.parameters:
                value = fixed.get(declared.name)
                if declared.value_type == "subject_reference" and isinstance(value, str):
                    fixed[declared.name] = resolve(
                        value, f"condition.fixed_parameters.{declared.name}"
                    )
        condition = MandateCondition(
            query=ArtifactPin(
                role="condition-query",
                target=query.identity,
                artifact_digest=query_definition_digest(query).tagged,
            ),
            binding_parameter=payload.condition.binding_parameter,
            fixed_parameters=fixed,
            required_fields=tuple(sorted(set(payload.condition.required_fields))),
            fallback=payload.condition.fallback,
        )
    try:
        mandate = ProcedureMandate(
            identity=ArtifactIdentity(kind="ProcedureMandate", name=payload.name),
            procedure=ArtifactPin(
                role="procedure",
                target=procedure.identity,
                artifact_digest=procedure_artifact_digest(procedure).tagged,
            ),
            grants=payload.grants,
            resource_ceiling=payload.resource_ceiling,
            namespace=payload.namespace,
            valid_from=payload.valid_from,
            expires_at=payload.expires_at,
            scope=tuple(sorted(scope, key=lambda item: item.claim_type.target.qualified.encode())),
            subject_scope=(
                None
                if payload.subject_scope is None
                else tuple(
                    sorted(
                        {
                            address
                            if resolve is None or address.selector.scheme != "artifact-v1"
                            else SemanticAddress.whole_artifact(
                                resolve(address.artifact_path, f"subject_scope[{index}]")
                            )
                            for index, address in enumerate(payload.subject_scope)
                        },
                        key=lambda item: item.artifact_path,
                    )
                )
            ),
            condition=condition,
            subject_creation=tuple(
                sorted(set(payload.subject_creation), key=lambda item: item.encode("utf-8"))
            ),
            suspended=payload.suspended,
            lifecycle=ArtifactLifecycle(
                state="retired" if payload.retire else "live",
                predecessor_digest=predecessor_digest,
            ),
        )
    except ValidationError as exc:
        _refuse(
            "cruxible.authoring.procedure_mandate_invalid",
            "procedure_mandate",
            "The ProcedureMandate is invalid: "
            + " | ".join(_validation_error_lines(exc, root="procedure_mandate")),
            repair_kind="revise_procedure_mandate",
            repair_description="Revise the field the message names and compile again.",
        )
    if (
        previous_content is not None
        and previous is not None
        and _same_revision_content(mandate, previous)
    ):
        return path, previous_content, procedure_mandate_digest(previous).tagged
    return path, render_procedure_mandate(mandate), procedure_mandate_digest(mandate).tagged


def _authored_query_paths(
    payload: object,
    instance: PlaybillInstance,
    base: AcceptedProjectionCoordinate,
    tree: Mapping[str, bytes],
) -> tuple[str, ...] | None:
    from cruxible_client.contracts.query.authored import QuerySpecInput
    from cruxible_core.service.discovery.authored_query import candidate_vocabulary_paths

    if isinstance(payload, QueryDefinitionAuthoringPayload) and isinstance(
        payload.query_definition, QuerySpecInput
    ):
        return candidate_vocabulary_paths(instance, base, tree)
    return None


def _lower_non_procedure(
    *,
    payload: ResolutionContractAuthoringPayload
    | AttestationAuthoringPayload
    | SubjectAuthoringPayload
    | QueryDefinitionAuthoringPayload
    | ApprovalPolicyAuthoringPayload
    | ProcedureRuntimePolicyAuthoringPayload
    | CaptureContractAuthoringPayload
    | SourceAcquisitionPolicyAuthoringPayload,
    base_tree: Mapping[str, bytes],
    base: AcceptedProjectionCoordinate,
    instance: PlaybillInstance,
) -> LoweredAuthoring:
    path, content, digest = _render_non_procedure_member(
        payload,
        tree=base_tree,
        light=base.compiler == LIGHT_ARTIFACTS_COMPILER,
        query_paths=_authored_query_paths(payload, instance, base, base_tree),
    )
    candidate_tree = fork_tree(base_tree)
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


@dataclass(frozen=True)
class _Creation:
    """One Subject a change creates, named by core from its kind's rule."""

    kind: str
    subject_id: str
    path: str
    #: The canonical key in key order; empty for a minted kind.
    key: KeyTuple
    declared: SubjectKind
    #: How core named it (the binding the candidate boundary re-derives); None
    #: when the creation resolved to an existing Subject.
    assignment: SubjectAssignment | None = None
    #: The key already names a live Subject: the creation resolves to it and
    #: writes nothing (create-by-key is an idempotent upsert).
    resolved: bool = False


SUBJECT_KEY_CLAIM_ID_DOMAIN = "cruxible-subject-key-claim-id-v1"


def subject_key_claim_id(*, operation: str, member: str, predicate: str) -> str:
    """The Claim ID one key part of a created Subject lowers into, from its operation."""

    digest = typed_digest(
        Sha256Value,
        SUBJECT_KEY_CLAIM_ID_DOMAIN,
        {"member": member, "operation": operation, "predicate": predicate},
    ).tagged.removeprefix("sha256:")
    return f"CLM-{digest[:32]}"


def _declared_in_change(
    kind: str,
    *,
    members: tuple[AuthoringChangeSetMember, ...],
    base_tree: Mapping[str, bytes],
) -> SubjectKind | None:
    """The live SubjectKind of ``kind`` this change declares, else the accepted one."""

    for member in members:
        if isinstance(member, SubjectKindAuthoringPayload) and member.name == kind:
            path, content, _digest = _render_subject_kind_member(member, tree=base_tree)
            declared = parse_subject_kind(content, path=path)
            return declared if declared.lifecycle.state == "live" else None
    return live_subject_kind(base_tree, kind)


def _claim_type_in_change(
    predicate: str,
    *,
    members: tuple[AuthoringChangeSetMember, ...],
    base_tree: Mapping[str, bytes],
) -> ClaimType | None:
    """The ClaimType of ``predicate`` this change defines or succeeds, else the accepted one."""

    for member in members:
        if (
            isinstance(member, ClaimTypeAuthoringPayload)
            and member.claim_type.predicate == predicate
        ):
            return member.claim_type
        if isinstance(member, ClaimTypeSuccessionMember) and member.predicate == predicate:
            return member.successor
    content = base_tree.get(claim_type_path(predicate))
    return None if content is None else parse_claim_type(content, path=claim_type_path(predicate))


def members_identity_overlay(
    connection: sqlite3.Connection,
    *,
    members: tuple[AuthoringChangeSetMember, ...],
    base_tree: Mapping[str, bytes],
) -> IdentityOverlay:
    """What a change's own members do to Subject identity, before any is lowered.

    Subject successions (a retirement among them), and the key and alias Claims
    its Claim members add, revise or retire: creation by key resolves against
    the state the change leaves (review F-007), as the candidate boundary judges
    it.
    """

    from cruxible_core.indexes.typed_state import statement_vocabulary_terms

    overlay = IdentityOverlay()
    removed: set[str] = set()
    added: list[tuple[str, str, object]] = []
    for member in members:
        if isinstance(member, SubjectAuthoringPayload):
            shell = member.subject
            overlay.subjects[subject_path(shell.subject_kind, shell.subject_id)] = (
                shell.lifecycle.state == "live"
            )
        elif isinstance(member, ClaimRetirementMember):
            removed.add(member.retires)
        elif isinstance(member, ClaimAuthoringPayloadV1):
            if member.revises is not None:
                removed.add(member.revises)
            statement = member.statement
            # Alias rows come from the one row function accepted vocabulary
            # extraction uses (any qualifier), so this overlay cannot diverge
            # from what the accepted Claim will index (review delta-3 F-001).
            overlay.aliases_added.update(
                (target, term)
                for _identity, target, basis, term, _value in statement_vocabulary_terms(
                    "pending", statement
                )
                if basis == "alias" and target.startswith("subjects/")
            )
            address = statement.subject
            # Key values: only an unqualified literal is a key part.
            if (
                address.selector.scheme == "artifact-v1"
                and address.artifact_path.startswith("subjects/")
                and statement.qualifier is None
                and isinstance(statement.object, LiteralClaimObject)
                and statement.predicate != "semantic.alias"
            ):
                added.append((address.artifact_path, statement.predicate, statement.object.value))
    declared = _DeclaredInChange(members=members, base_tree=base_tree)
    affected: dict[str, tuple[str, ...]] = {}
    for claim_id in sorted(removed):
        row = connection.execute(
            "SELECT subject_path, predicate FROM claims WHERE identity=?", (f"Claim:{claim_id}",)
        ).fetchone()
        if row is None:
            continue
        subject, predicate = str(row[0]), str(row[1])
        for (term,) in connection.execute(
            "SELECT term FROM vocabulary_terms WHERE source_identity=? AND basis='alias'",
            (f"Claim:{claim_id}",),
        ):
            overlay.aliases_removed.add((subject, str(term)))
        kind_rule = declared(subject_kind_of(subject)[0])
        if kind_rule is not None and predicate in kind_rule.keys:
            affected[subject] = kind_rule.keys
    added_values: dict[tuple[str, str], list[object]] = {}
    for subject, predicate, value in added:
        added_values.setdefault((subject, predicate), []).append(value)
        kind_rule = declared(subject_kind_of(subject)[0])
        if kind_rule is not None and predicate in kind_rule.keys:
            affected[subject] = kind_rule.keys
    # Each key ClaimType is read once, and each owner's added values were grouped
    # above: the overlay is linear in the change, not owners times members.
    claim_types: dict[str, ClaimType | None] = {}
    for subject, keys in affected.items():
        parts: list[tuple[str, SubjectKeyValue]] = []
        for predicate in keys:
            values: list[object] = [
                value
                for identity, value in live_key_claims(connection, subject, predicate)
                if identity.removeprefix("Claim:") not in removed
            ]
            if predicate not in claim_types:
                claim_types[predicate] = _claim_type_in_change(
                    predicate, members=members, base_tree=base_tree
                )
            claim_type = claim_types[predicate]
            if claim_type is not None:
                form = effective_canonical_form(claim_type)
                for value in added_values.get((subject, predicate), ()):
                    try:
                        values.append(canonicalize_literal(value, form))
                    except CanonicalFormDidNotConverge:
                        values.append(None)
            if len(values) != 1 or key_value_problem(values[0]) is not None:
                break
            parts.append((predicate, cast(SubjectKeyValue, values[0])))
        else:
            overlay.keys[subject] = tuple(parts)
            continue
        overlay.keys[subject] = None
    return overlay


class _DeclaredInChange:
    """The live SubjectKind of each kind as a change sees it, read once per kind."""

    def __init__(
        self, *, members: tuple[AuthoringChangeSetMember, ...], base_tree: Mapping[str, bytes]
    ) -> None:
        self._members = members
        self._base_tree = base_tree
        self._known: dict[str, SubjectKind | None] = {}

    def __call__(self, kind: str) -> SubjectKind | None:
        if kind not in self._known:
            self._known[kind] = _declared_in_change(
                kind, members=self._members, base_tree=self._base_tree
            )
        return self._known[kind]


def _single_text(member: SubjectCreationAuthoringPayload, declared: SubjectKind) -> str | None:
    """A one-key creation's text as written, read as an alias of the kind first."""

    if len(declared.keys) != 1:
        return None
    raw = (
        member.key_value
        if member.key_value is not None
        else (member.key[0].value if len(member.key) == 1 else None)
    )
    return raw if isinstance(raw, str) else None


def _assign_creations(
    instance: PlaybillInstance,
    *,
    operation: str,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    members: tuple[AuthoringChangeSetMember, ...],
) -> dict[str, _Creation]:
    """Name every Subject the change creates, as core does (identity D1).

    A keyed creation resolves through the shared resolver (``IdentityView``)
    over the parent overlaid with the change itself (``members_identity_overlay``): a
    one-key creation's text names a Subject an alias of the kind names; else
    its canonical key's live holders -- none, create; one, reuse it and create
    nothing; several, refuse ``key_ambiguous``. Only ``create_new`` on a
    ``hint`` kind creates a further holder (refused on a ``unique`` kind).

    Ids, in canonical-key order: a first holder takes the readable rendering of
    its key, suffixed with the key's digest when the rendering is an existing
    Subject's id, a live alias of the kind, or an id this change already
    assigned. Ordinary creations precede create_new members of the same key,
    with member identity breaking ties; a further holder takes the rendering and a digest of the
    operation and member; a minted kind's Subject an id minted from the
    operation and member. Each records how (``SubjectAssignment``), so the
    candidate boundary re-derives it on every route.
    """

    creations: dict[str, _Creation] = {}
    keyed: list[tuple[str, int, SubjectCreationAuthoringPayload, SubjectKind, KeyTuple]] = []
    for index, member in enumerate(members):
        if not isinstance(member, SubjectCreationAuthoringPayload):
            continue
        element = f"members[{index}]"
        kind = member.subject_kind
        if not admits_subject_kinds(base.compiler):
            _refuse(
                SUBJECT_COMPILER_UPGRADE_REQUIRED,
                f"{element}.subject_kind",
                f"This instance's compiler predates SubjectKinds, so no new {kind} Subject "
                "can be created.",
                repair_kind="upgrade_compiler",
                repair_description=compiler_upgrade_repair(kind),
                replacement={"subject_kind": kind},
            )
        declared = _declared_in_change(kind, members=members, base_tree=base_tree)
        if declared is None:
            _refuse(
                SUBJECT_KIND_UNDECLARED,
                f"{element}.subject_kind",
                f"No accepted SubjectKind declares {kind}, so no new Subject of it can be created.",
                repair_kind="author_subject_kind",
                repair_description=undeclared_kind_repair(kind),
                replacement={"subject_kind": kind},
            )
        identity = member.member_identity
        if declared.minted:
            if member.key or member.key_value is not None:
                _refuse(
                    "cruxible.subject.key_invalid",
                    f"{element}.key",
                    f"{kind} is minted: core names its Subjects, so a creation gives no key.",
                    repair_kind="drop_key",
                    repair_description="Remove the key.",
                )
            subject_id = minted_subject_id(operation=operation, member=identity)
            creations[identity] = _Creation(
                kind=kind,
                subject_id=subject_id,
                path=subject_path(kind, subject_id),
                key=(),
                declared=declared,
                assignment=SubjectAssignment(rule="minted", operation=operation, member=identity),
            )
            continue

        def claim_type_of(predicate: str) -> ClaimType | None:
            return _claim_type_in_change(predicate, members=members, base_tree=base_tree)

        given: dict[str, object] = {part.predicate: part.value for part in member.key}
        if member.key_value is not None:
            if len(declared.keys) != 1:
                _refuse(
                    "cruxible.subject.key_invalid",
                    f"{element}.key_value",
                    f"{kind} is named by {len(declared.keys)} key parts "
                    f"({', '.join(declared.keys)}); a bare key value names only a one-key kind.",
                    repair_kind="replace_key",
                    repair_description="Give each key part by name.",
                    replacement={"keys": list(declared.keys)},
                )
            given = {declared.keys[0]: member.key_value}
        try:
            key = canonical_key(declared, given, claim_type=claim_type_of)
        except SubjectKeyInvalid as exc:
            _refuse(
                "cruxible.subject.key_invalid",
                f"{element}.key",
                f"{kind}: {exc}",
                repair_kind="replace_key",
                repair_description=(
                    f"Give one value for each key of {kind}: " + ", ".join(declared.keys)
                ),
                replacement={"keys": list(declared.keys)},
            )
        keyed.append((key_encoding(key), index, member, declared, key))
    if not keyed:
        return creations
    names: dict[str, set[str]] = {}
    with instance.bind_accepted_projection(base) as projection:
        connection = projection.typed.connection
        view = IdentityView(
            connection, members_identity_overlay(connection, members=members, base_tree=base_tree)
        )
        for _encoding, index, member, declared, key in sorted(
            keyed,
            key=lambda item: (
                item[3].subject_kind,
                key_order(item[4]),
                item[2].create_new,
                item[2].member_identity,
            ),
        ):
            kind = member.subject_kind
            element = f"members[{index}]"
            if member.create_new and declared.unique:
                _refuse(
                    "cruxible.subject.create_new_refused",
                    f"{element}.create_new",
                    f"{kind} keys are unique: a key names one Subject, so create_new does not "
                    "apply.",
                    repair_kind="drop_create_new",
                    repair_description=(
                        "Drop create_new: the creation then resolves to the Subject holding the "
                        "key."
                    ),
                )
            resolution = view.resolve_creation(
                kind,
                key,
                single_text=_single_text(member, declared),
                create_new=member.create_new,
            )
            refs = tuple(f"{kind}/{subject_kind_of(path)[1]}" for path in resolution.holders)
            if resolution.ambiguous:
                _refuse(
                    "cruxible.subject.key_ambiguous"
                    if resolution.via == "key"
                    else "cruxible.authoring.alias_ambiguous",
                    f"{element}.key",
                    (
                        f"{len(refs)} live {kind} Subjects hold this key: {', '.join(refs)}; "
                        "name one by id."
                        if resolution.via == "key"
                        else f"An alias of {kind} spelled as this key names {len(refs)} "
                        f"Subjects: {', '.join(refs)}; name one by id."
                    ),
                    repair_kind="replace_subject",
                    repair_description="Name one of the listed Subjects by id.",
                    replacement={"candidates": list(refs)},
                )
            if resolution.path is not None:
                creations[member.member_identity] = _Creation(
                    kind=kind,
                    subject_id=subject_kind_of(resolution.path)[1],
                    path=resolution.path,
                    key=key,
                    declared=declared,
                    resolved=True,
                )
                continue
            assigned = names.setdefault(kind, set())
            if resolution.holders:
                # create_new on a hint kind: a further holder of the key, named from
                # the operation and the member so it never reuses the key's rendering.
                subject_id = distinct_holder_id(
                    key_values(key), operation=operation, member=member.member_identity
                )
                assignment: SubjectAssignment | None = SubjectAssignment(
                    rule="further_holder", operation=operation, member=member.member_identity
                )
            else:
                try:
                    subject_id = assign_creation_id(view, kind, key, names=assigned)
                except SubjectIdUnassignable as exc:
                    _refuse(
                        "cruxible.subject.key_invalid",
                        f"{element}.key",
                        str(exc),
                        repair_kind="replace_key",
                        repair_description="Give a key whose rendering is free.",
                    )
                assignment = None
            assigned.add(subject_id)
            path = subject_path(kind, subject_id)
            view.add_creation(path, key)
            creations[member.member_identity] = _Creation(
                kind=kind,
                subject_id=subject_id,
                path=path,
                key=key,
                declared=declared,
                assignment=assignment,
            )
    return creations


def subject_creation_paths(
    instance: PlaybillInstance,
    *,
    operation: str,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    members: tuple[AuthoringChangeSetMember, ...],
    include_operation_dependent: bool = True,
) -> dict[str, tuple[str, bool]]:
    """Where each Subject creation in ``members`` lands, as lowering will name it.

    Member identity -> (Subject path, whether the key named an existing Subject
    and nothing is created). ``operation`` is the intent id lowering will run
    under, and ``members`` the whole change (its other members are the overlay
    the creations resolve against); the assignment is the one
    ``lower_authoring`` makes. A write preview can omit operation-derived
    assignments with ``include_operation_dependent=False``; its minted and
    further-holder Subjects remain unassigned in public outcomes. Procedure
    terminals use the complete mapping to name sibling Subject items by key.
    """

    return {
        identity: (creation.path, creation.resolved)
        for identity, creation in _assign_creations(
            instance,
            operation=operation,
            base=base,
            base_tree=base_tree,
            members=members,
        ).items()
        if include_operation_dependent or creation.assignment is None
    }


SUBJECT_CREATION_REFERENCE_UNRESOLVED = "cruxible.authoring.subject_creation_unresolved"
#: A key reference that names one Subject over the base and another once the
#: change's own identity changes (key or alias Claims, creations) apply.
IDENTITY_REFERENCE_UNSTABLE = "cruxible.authoring.identity_reference_unstable"


def _key_slot(address: SemanticAddress) -> tuple[str, str]:
    return address.artifact_path, address.selector.value


def _subject_key_addresses(
    member: AuthoringChangeSetMember,
) -> tuple[tuple[str, SemanticAddress], ...]:
    """The Subjects a Claim member names by key (``subject-key-v1``), by field."""

    if not isinstance(member, ClaimAuthoringPayloadV1):
        return ()
    statement = member.statement
    found: list[tuple[str, SemanticAddress]] = []
    if statement.subject.selector.scheme == "subject-key-v1":
        found.append(("statement.subject", statement.subject))
    value = statement.object
    if isinstance(value, SubjectClaimObject) and value.address.selector.scheme == "subject-key-v1":
        found.append(("statement.object.address", value.address))
    return tuple(found)


def _with_keys_landed(
    payload: _PayloadV1T, key_paths: Mapping[tuple[str, str], str]
) -> _PayloadV1T:
    """The Claim with each Subject it names by key named by the path that key resolved to."""

    def landed(address: SemanticAddress) -> SemanticAddress:
        path = key_paths.get(_key_slot(address))
        return address if path is None else SemanticAddress.whole_artifact(path)

    statement = payload.statement
    subject = landed(statement.subject)
    value = statement.object
    if isinstance(value, SubjectClaimObject):
        target = landed(value.address)
        if target != value.address:
            value = SubjectClaimObject(address=target)
    if subject == statement.subject and value is statement.object:
        return payload
    return payload.model_copy(
        update={"statement": statement.model_copy(update={"subject": subject, "object": value})}
    )


def _key_reference_paths(
    instance: PlaybillInstance,
    *,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    members: tuple[AuthoringChangeSetMember, ...],
    creations: Mapping[str, _Creation],
) -> KeyReferences:
    """Resolve every Subject the change's Claims name by kind and key, once.

    Mirrors a Procedure v3 ``SubjectKeyReference``: a key a creation member of the
    same change carries names the Subject that member lands on; any other
    resolves through the one resolver over the base (exact live id, alias, key,
    exact retired id for a one-key text). None refuses ``key_missing`` (create it
    with a subject member in the same set), several ``key_ambiguous``. Each is
    then resolved again over the final state (the base with the change's own
    identity changes and creations); a different answer refuses
    ``identity_reference_unstable``.
    """

    references = [
        (index, field, address)
        for index, member in enumerate(members)
        for field, address in _subject_key_addresses(member)
    ]
    text_references = [
        (index, field, address)
        for index, member in enumerate(members)
        if isinstance(member, ClaimAuthoringPayloadV1)
        for field, address in (
            ("statement.subject", member.statement.subject),
            *(
                (("statement.object.address", member.statement.object.address),)
                if isinstance(member.statement.object, SubjectClaimObject)
                else ()
            ),
        )
        if creations
        and address.selector.scheme == "artifact-v1"
        and address.artifact_path.startswith("subjects/")
    ]
    if not references and not text_references:
        return KeyReferences(paths={})
    created: dict[tuple[str, str], str] = {}
    for creation in creations.values():
        if creation.key:
            created.setdefault((creation.kind, key_encoding(creation.key)), creation.path)

    def claim_type_of(predicate: str) -> ClaimType | None:
        return _claim_type_in_change(predicate, members=members, base_tree=base_tree)

    frozen: dict[tuple[str, str], tuple[str, KeyTuple, str | None, str]] = {}
    text_paths: dict[tuple[str, str], str] = {}
    with instance.bind_accepted_projection(base) as projection:
        connection = projection.typed.connection
        view = IdentityView(connection, key_reader=tree_key_reader(base_tree))

        # Match creation assignment's change-local identity effects, including
        # aliases that make a creation reuse an existing Subject. Only genuinely
        # new Subjects gain a key here: alias reuse leaves the held key unchanged.
        def text_key(kind: str, text: str) -> KeyTuple | None:
            declared = _declared_in_change(kind, members=members, base_tree=base_tree)
            if declared is None or len(declared.keys) != 1:
                return None
            try:
                return canonical_key(declared, {declared.keys[0]: text}, claim_type=claim_type_of)
            except SubjectKeyInvalid:
                return None

        created_view = IdentityView(
            connection,
            members_identity_overlay(connection, members=members, base_tree=base_tree),
            key_reader=text_key,
        )
        for creation in creations.values():
            if not creation.resolved:
                created_view.add_creation(creation.path, creation.key)
        for index, field, address in text_references:
            kind, _, rest = address.artifact_path.removeprefix("subjects/").partition("/")
            resolution = created_view.resolve(kind, rest.removesuffix(".json"))
            if resolution.ambiguous:
                _refuse(
                    "cruxible.subject.key_ambiguous"
                    if resolution.via == "key"
                    else "cruxible.authoring.alias_ambiguous",
                    f"members[{index}].{field}" if len(members) > 1 else field,
                    f"{kind} reference names several Subjects: " + ", ".join(resolution.candidates),
                    repair_kind="replace_subject",
                    repair_description="Name one of the listed Subjects as kind/id.",
                    replacement={"candidates": list(resolution.candidates)},
                )
            if resolution.path is not None:
                text_paths[_key_slot(address)] = resolution.path
        for index, field, address in references:
            slot = _key_slot(address)
            if slot in frozen:
                continue
            element = f"members[{index}].{field}" if len(members) > 1 else field
            reference = address.subject_key_reference
            assert reference is not None
            kind, given = reference
            declared = _declared_in_change(kind, members=members, base_tree=base_tree)
            if declared is None or declared.minted:
                _refuse(
                    "cruxible.subject.key_invalid",
                    element,
                    f"{kind} has no declared keys, so no Subject of it is named by key.",
                    repair_kind="replace_subject",
                    repair_description="Name the Subject as kind/id.",
                )
            try:
                given = key_reference_mapping(declared, given)
                key = canonical_key(declared, given, claim_type=claim_type_of)
            except (SubjectKeyInvalid, ValueError) as exc:
                _refuse(
                    "cruxible.subject.key_invalid",
                    element,
                    f"{kind}: {exc}",
                    repair_kind="replace_key",
                    repair_description=(
                        f"Give a key mapping for {kind}: "
                        + repr({predicate: "<value>" for predicate in declared.keys})
                    ),
                    replacement={
                        "keys": list(declared.keys),
                        "key": {predicate: "<value>" for predicate in declared.keys},
                    },
                )
            values = tuple(given.values())
            single = (
                values[0]
                if len(key) == 1 and len(values) == 1 and isinstance(values[0], str)
                else None
            )
            path = created.get((kind, key_encoding(key)))
            if path is None:
                resolution = view.resolve_key_reference(kind, key, single_text=single)
                if resolution.ambiguous:
                    holders = [f"subjects/{item}.json" for item in resolution.candidates]
                    _refuse(
                        "cruxible.subject.key_ambiguous"
                        if resolution.via == "key"
                        else "cruxible.authoring.alias_ambiguous",
                        element,
                        key_ambiguous_message(kind, holders)
                        if resolution.via == "key"
                        else f"An alias of {kind} names {len(holders)} Subjects: "
                        + ", ".join(resolution.candidates)
                        + "; name one by id.",
                        repair_kind="replace_subject",
                        repair_description="Name one of the listed Subjects by id.",
                        replacement={"candidates": list(resolution.candidates)},
                    )
                if resolution.path is None:
                    _refuse(
                        SUBJECT_KEY_MISSING,
                        element,
                        f"No live {kind} Subject holds the key {dict(given)!r}.",
                        repair_kind="create_subject",
                        repair_description=(
                            "Add a subject member creating it to the same change set: "
                            + subject_creation_shape(kind)
                        ),
                    )
                path = resolution.path
            frozen[slot] = (kind, key, single, path)

        by_digest = {
            identity.rpartition("@")[2]: creation.path for identity, creation in creations.items()
        }

        def landed(address: SemanticAddress) -> SemanticAddress:
            if _key_slot(address) in text_paths:
                return SemanticAddress.whole_artifact(text_paths[_key_slot(address)])
            if address.selector.scheme == "subject-key-v1":
                return SemanticAddress.whole_artifact(frozen[_key_slot(address)][3])
            if address.selector.scheme == "subject-creation-v1":
                path = by_digest.get(address.selector.value)
                return address if path is None else SemanticAddress.whole_artifact(path)
            # Ordinary kind/<text> shorthand lands where lowering will land it
            # (_with_aliases_resolved: an alias or a single key over the base), so
            # an identity Claim stated through it is overlaid on that Subject.
            return _shorthand_landed(view, base_tree, address)

        final = tuple(_with_landed_addresses(member, landed) for member in members)
        _refuse_unstable_key_references(
            connection,
            base_tree=base_tree,
            references=[
                (index, field, address, *frozen[_key_slot(address)])
                for index, field, address in references
            ],
            final_members=final,
            creations=creations,
            single_member=len(members) == 1,
        )
    return KeyReferences(
        paths={**text_paths, **{slot: item[3] for slot, item in frozen.items()}},
        frozen=tuple(
            (index, field, address, *frozen[_key_slot(address)])
            for index, field, address in references
        ),
    )


FrozenKeyReference: TypeAlias = tuple[int, str, SemanticAddress, str, KeyTuple, str | None, str]
"""(member index, field, address, kind, canonical key, one-key text, frozen path)."""


@dataclass(frozen=True)
class KeyReferences:
    """Each key reference of a change, by selector, and what it was frozen to."""

    paths: Mapping[tuple[str, str], str]
    frozen: tuple[FrozenKeyReference, ...] = ()


def _shorthand_landed(
    view: IdentityView, tree: Mapping[str, bytes], address: SemanticAddress
) -> SemanticAddress:
    """Where lowering lands an ordinary ``kind/<text>`` address (``_with_aliases_resolved``)."""

    path = address.artifact_path
    if (
        address.selector.scheme != "artifact-v1"
        or not path.startswith("subjects/")
        or _live_subject_in(tree, path)
    ):
        return address
    kind, _, rest = path.removeprefix("subjects/").partition("/")
    text = rest.removesuffix(".json")
    if not kind or not text:
        return address
    resolution = view.resolve(kind, text)
    if (
        resolution.ambiguous
        or resolution.via not in {"alias", "key"}
        or resolution.path is None
        or resolution.path not in tree
    ):
        return address
    return SemanticAddress.whole_artifact(resolution.path)


def _with_landed_addresses(
    member: AuthoringChangeSetMember,
    landed: Callable[[SemanticAddress], SemanticAddress],
) -> AuthoringChangeSetMember:
    if not isinstance(member, ClaimAuthoringPayloadV1):
        return member
    statement = member.statement
    subject = landed(statement.subject)
    value = statement.object
    if isinstance(value, SubjectClaimObject):
        target = landed(value.address)
        if target != value.address:
            value = SubjectClaimObject(address=target)
    if subject == statement.subject and value is statement.object:
        return member
    return member.model_copy(
        update={"statement": statement.model_copy(update={"subject": subject, "object": value})}
    )


def _refuse_unstable_key_references(
    connection: sqlite3.Connection,
    *,
    base_tree: Mapping[str, bytes],
    references: Sequence[FrozenKeyReference],
    final_members: tuple[AuthoringChangeSetMember, ...],
    creations: Mapping[str, _Creation],
    single_member: bool,
) -> None:
    """Refuse a key reference the change's own identity changes resolve elsewhere.

    ``final_members`` name every Subject concretely, as the change lands them;
    the final state is the base with their key and alias Claims, retirements and
    the change's creations overlaid.
    """

    if not references:
        return
    after = IdentityView(
        connection,
        members_identity_overlay(connection, members=final_members, base_tree=base_tree),
        key_reader=tree_key_reader(base_tree),
    )
    for creation in creations.values():
        if creation.key and not creation.resolved:
            after.add_creation(creation.path, creation.key)
    for index, element, address, kind, key, single, path in references:
        now = after.resolve_key_reference(kind, key, single_text=single)
        if now.path == path and not now.ambiguous:
            continue
        reference = address.subject_key_reference
        assert reference is not None
        _refuse(
            IDENTITY_REFERENCE_UNSTABLE,
            element if single_member else f"members[{index}].{element}",
            f"The {kind} key {reference[1]!r} names {path} at the base, and "
            f"{now.path or list(now.candidates) or 'no Subject'} once this change's "
            "own identity changes (key or alias Claims) apply.",
            repair_kind="split_change_set",
            repair_description=(
                "Put the identity change (an alias or key Claim) and the references "
                "that depend on it in separate change sets."
            ),
        )


def _recheck_key_references_lowered(
    instance: PlaybillInstance,
    *,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    staged_tree: Mapping[str, bytes],
    members: tuple[AuthoringChangeSetMember, ...],
    claim_identities: Mapping[str, str],
    creations: Mapping[str, _Creation],
    references: KeyReferences,
) -> None:
    """Check every frozen key reference again against what lowering actually wrote.

    Each Claim member is read back from the lowered tree, so its Subject and
    Subject value are the ones lowering resolved (alias, key, creation): the
    final identity state is built from the change as it lands, not as predicted.
    """

    if not references.frozen:
        return
    lowered: list[AuthoringChangeSetMember] = []
    for member in members:
        if isinstance(member, ClaimAuthoringPayloadV1):
            path = claim_path(claim_identities[authoring_member_identity(member)])
            content = staged_tree.get(path)
            if content is not None:
                written = parse_claim(content, path=path).statement
                value = member.statement.object
                if isinstance(written.object, SubjectClaimObject):
                    value = SubjectClaimObject(address=written.object.address)
                member = member.model_copy(
                    update={
                        "statement": member.statement.model_copy(
                            update={"subject": written.subject, "object": value}
                        )
                    }
                )
        lowered.append(member)
    with instance.bind_accepted_projection(base) as projection:
        _refuse_unstable_key_references(
            projection.typed.connection,
            base_tree=base_tree,
            references=references.frozen,
            final_members=tuple(lowered),
            creations=creations,
            single_member=len(members) == 1,
        )


def _naming_creations(
    payload: ClaimAuthoringPayloadV1,
    creations: Mapping[str, _Creation],
    *,
    element: str,
) -> ClaimAuthoringPayloadV1:
    """The Claim with each Subject it names by creation member named by its Subject's path.

    A ``subject-creation-v1`` address names the Subject one creation member of
    the same change lands on -- the one core created, or the existing one its
    key named (review F-002) -- never a path predicted from raw key values.
    """

    by_digest = {identity.rpartition("@")[2]: creation for identity, creation in creations.items()}

    def landed(address: SemanticAddress, field_name: str) -> SemanticAddress:
        if address.selector.scheme != "subject-creation-v1":
            return address
        creation = by_digest.get(address.selector.value)
        if creation is None or address.artifact_path != subject_kind_path(creation.kind):
            _refuse(
                SUBJECT_CREATION_REFERENCE_UNRESOLVED,
                f"{element}.{field_name}",
                "The Claim names a Subject by a creation member this change does not hold "
                f"({address.artifact_path}, {address.selector.value}).",
                repair_kind="replace_subject",
                repair_description=(
                    "Name the Subject by the reference its creation member returned in this "
                    "change set, or by an existing kind/id."
                ),
            )
        return SemanticAddress.whole_artifact(creation.path)

    statement = payload.statement
    subject = landed(statement.subject, "statement.subject")
    value = statement.object
    if isinstance(value, SubjectClaimObject):
        target = landed(value.address, "statement.object.address")
        if target != value.address:
            value = SubjectClaimObject(address=target)
    if subject == statement.subject and value is statement.object:
        return payload
    return payload.model_copy(
        update={"statement": statement.model_copy(update={"subject": subject, "object": value})}
    )


def _key_claim_role(claim_type: ClaimType) -> ClaimRole | None:
    """The role a key Claim takes: the ClaimType's default role, or its only authorable one."""

    if claim_type.default_role is not None:
        return claim_type.default_role
    permitted = tuple(role for role in claim_type.permitted_roles if role != "derivation")
    return permitted[0] if len(permitted) == 1 else None


def _stage_subject_creation(
    instance: PlaybillInstance,
    *,
    member: SubjectCreationAuthoringPayload,
    creation: _Creation,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    staged_tree: Mapping[str, bytes],
    claim_index: _ClaimPredicateIndex,
    derivation_procedure: ArtifactPin | None,
    captures: ProducerReceiptResolverProtocol,
) -> StagedMember:
    """Write the created Subject and one key Claim per key part.

    Each key Claim cites the creation's source (its rationale as the author's own
    words when none) and lowers like any Claim member, so its value is stored in
    its ClaimType's canonical form. The creation covers it (Claim law v2
    revision 9): its origin supports it, so the key ClaimType needs no evidence
    rule admitting what it cites. A later revision is judged by its policy.
    """

    if creation.resolved:
        # The Subject the key names: accepted, or (staged later or earlier) created
        # by a sibling member of this change holding the same key.
        content = staged_tree.get(creation.path)
        return (
            fork_tree(staged_tree),
            {
                **(
                    {}
                    if content is None
                    else {
                        "artifact_digest": subject_digest(
                            parse_subject(content, path=creation.path)
                        ).tagged
                    }
                ),
                "subject": f"{creation.kind}/{creation.subject_id}",
                "resolved": True,
            },
            set(),
            {},
        )
    shell = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{creation.kind}/{creation.subject_id}"),
        subject_kind=creation.kind,
        subject_id=creation.subject_id,
        assignment=creation.assignment,
    )
    tree = fork_tree(staged_tree)
    tree[creation.path] = render_subject(shell)
    extra: set[str] = set()
    key_claims: list[str] = []
    rationale = member.rationale or (
        f"{creation.kind}/{creation.subject_id} is named by its key: "
        + ", ".join(f"{predicate}={value!r}" for predicate, value in creation.key)
        + "."
    )
    for predicate, value in creation.key:
        claim_type = parse_claim_type(
            tree[claim_type_path(predicate)], path=claim_type_path(predicate)
        )
        role = member.role or _key_claim_role(claim_type)
        if role is None:
            _refuse(
                "cruxible.subject.key_role_required",
                "role",
                f"Key ClaimType {predicate!r} permits several roles and declares no "
                "default_role, so the key Claim's role is not implied.",
                repair_kind="set_role",
                repair_description=(
                    "Pass role, or declare default_role on the key ClaimType: "
                    + ", ".join(claim_type.permitted_roles)
                ),
            )
        claim_id = subject_key_claim_id(
            operation=intent.intent_id, member=member.member_identity, predicate=predicate
        )
        statement = AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(creation.path),
            predicate=predicate,
            object=LiteralClaimObject(value=value),
            role=role,
        )
        # The author's own words lower as a write's ``--because`` does (payload v2),
        # so the key Claim is self-asserted evidence its ClaimType admits.
        payload: ClaimAuthoringPayloadV1 = (
            ClaimAuthoringPayload(
                statement=statement,
                rationale=rationale,
                source=member.source,
                citation_role=member.citation_role,
                dependency_drafts=ClaimDependencyDrafts(),
            )
            if member.source is not None and not isinstance(member.source, SelfSourceBody)
            else ClaimAuthoringPayloadV2(
                statement=statement,
                rationale=rationale,
                source=member.source
                or SelfSourceBody(
                    content_base64=base64.b64encode(rationale.encode("utf-8")).decode("ascii")
                ),
                dependency_drafts=ClaimDependencyDrafts(),
            )
        )
        lowered = _lower_claim(
            instance,
            intent=intent,
            actor_id=actor_id,
            base=base,
            base_tree=tree,
            payload=payload,
            claim_identity=claim_id,
            claim_index=claim_index,
            derivation_procedure=derivation_procedure,
            captures=captures,
            subject_creation_key=True,
        )
        tree = fork_tree(lowered.proposed_tree)
        extra.update(path for path, _content in lowered.changed_members)
        key_claims.append(claim_id)
    return (
        tree,
        {
            "artifact_digest": subject_digest(shell).tagged,
            "subject": f"{creation.kind}/{creation.subject_id}",
            "key_claims": key_claims,
        },
        extra,
        {},
    )


MEMBER_STAGING_ORDER = (
    "definition",
    "claim_type_succession",
    "query",
    "subject_creation",
    "claim",
    "procedure",
    "procedure_mandate",
    "line",
    "trigger",
    "claim_retirement",
)


def _member_stage(member: AuthoringChangeSetMember) -> str:
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

    if isinstance(member, QueryDefinitionAuthoringPayload):
        return "query"
    if isinstance(member, SubjectCreationAuthoringPayload):
        return "subject_creation"
    if isinstance(member, ClaimAuthoringPayloadV1):
        return "claim"
    if isinstance(member, ClaimTypeSuccessionMember):
        return "claim_type_succession"
    if isinstance(member, ClaimRetirementMember):
        return "claim_retirement"
    if isinstance(member, ProcedureAuthoringPayload):
        return "procedure"
    if isinstance(member, ProcedureMandateAuthoringPayload):
        return "procedure_mandate"
    if isinstance(member, LineAuthoringPayload):
        return "line"
    if isinstance(member, TriggerAuthoringPayload):
        return "trigger"
    return "definition"


def _contract_member_refs(member: ContractAuthoringPayload) -> tuple[ContractRef, ...]:
    found: list[ContractRef] = []
    for item in member.fields.values():
        found.extend(type_refs(item.type))
    return tuple(found)


def _stage_member_order(
    stage_indices: list[int], members: tuple[AuthoringChangeSetMember, ...]
) -> list[int]:
    """One stage's members, in staging order.

    Members arrive canonically byte-sorted by semantic identity (one intent,
    one wire form), which is the wrong order to *stage* in whenever a
    Contract in this set pins another Contract in the same set: lowering
    renders each member against only the members already staged, so a
    dependency staged later than its dependent would refuse pin_unresolved
    even though both are valid and acyclic. Contract members of this stage
    are staged in dependency order (a stable topological sort); every other
    member keeps its canonical position.
    """

    contract_indices = [
        i for i in stage_indices if isinstance(members[i], ContractAuthoringPayload)
    ]
    if len(contract_indices) <= 1:
        return stage_indices
    by_key: dict[tuple[str, int], int] = {}
    for i in contract_indices:
        member = members[i]
        assert isinstance(member, ContractAuthoringPayload)
        by_key[(member.name, member.version)] = i
    deps: dict[int, set[int]] = {i: set() for i in contract_indices}
    for i in contract_indices:
        member = members[i]
        assert isinstance(member, ContractAuthoringPayload)
        for ref in _contract_member_refs(member):
            dep = by_key.get((ref.name, ref.version))
            if dep is not None and dep != i:
                deps[i].add(dep)
    remaining = set(contract_indices)
    resolved: set[int] = set()
    ordered: list[int] = []
    while remaining:
        ready = sorted(i for i in remaining if deps[i] <= resolved)
        if not ready:
            cyclic = sorted(
                f"{members[i].name}@{members[i].version}"  # type: ignore[union-attr]
                for i in remaining
            )
            _refuse(
                "cruxible.authoring.contract_dependency_cycle",
                "members",
                "These Contracts in the same change set pin each other in a cycle: "
                + ", ".join(cyclic),
                repair_kind="split_change_set",
                repair_description=(
                    "Break the cycle, or accept one side first in its own change set."
                ),
            )
        ordered.extend(ready)
        resolved.update(ready)
        remaining -= set(ready)
    ordered_set = set(contract_indices)
    reordered = iter(ordered)
    return [next(reordered) if i in ordered_set else i for i in stage_indices]


def _member_primary_path(
    member: AuthoringChangeSetMember,
    *,
    claim_identities: Mapping[str, str],
    creations: Mapping[str, _Creation] | None = None,
) -> str:
    """Return the one artifact path this member is authoring."""

    if isinstance(member, SubjectCreationAuthoringPayload):
        assert creations is not None
        return creations[member.member_identity].path
    if isinstance(member, ClaimAuthoringPayloadV1):
        return claim_path(claim_identities[authoring_member_identity(member)])
    if isinstance(member, ClaimTypeSuccessionMember):
        return claim_type_path(member.predicate)
    if isinstance(member, ClaimRetirementMember):
        return claim_path(member.claim_id)
    if isinstance(member, ProcedureAuthoringPayload):
        return procedure_path(str(member.definition["name"]))
    if isinstance(member, ProcedureMandateAuthoringPayload):
        return procedure_mandate_path(member.name)
    if isinstance(member, LineAuthoringPayload):
        return line_spec_path(member.name)
    if isinstance(member, TriggerAuthoringPayload):
        return trigger_path(member.name)
    if isinstance(member, SubjectKindAuthoringPayload):
        return subject_kind_path(member.name)
    if isinstance(member, ContractAuthoringPayload):
        return contract_path(member.name, member.version)
    if isinstance(member, QueryDefinitionAuthoringPayload):
        return query_definition_path(member.query_definition.identity.name)
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
        offending_element=f"members[{index}].{error.offending_element.removeprefix('$.')}",
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
        return f"cruxible.authoring.{self.kind}_singleton_required"


CHANGE_SET_SINGLETON_ONLY_MEMBERS: tuple[ChangeSetSingletonOnlyMember, ...] = (
    ChangeSetSingletonOnlyMember(
        payload=ApprovalPolicyAuthoringPayload,
        kind="approval_policy",
        artifact="ApprovalPolicy",
    ),
    ChangeSetSingletonOnlyMember(
        payload=ProcedureRuntimePolicyAuthoringPayload,
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
    base_tree: Mapping[str, bytes],
    derivation_procedure: ArtifactPin | None = None,
    captures: ProducerReceiptResolverProtocol,
) -> LoweredAuthoring:
    payload = intent.payload
    assert isinstance(payload, ChangeSetAuthoringPayload)
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
    creations = _assign_creations(
        instance,
        operation=intent.intent_id,
        base=base,
        base_tree=base_tree,
        members=payload.members,
    )
    key_references = _key_reference_paths(
        instance, base=base, base_tree=base_tree, members=payload.members, creations=creations
    )
    key_paths = key_references.paths
    sibling_member_by_claim_id = {
        claim_identities[authoring_member_identity(member)]: index
        for index, member in enumerate(payload.members)
        if isinstance(member, ClaimAuthoringPayloadV1)
    }
    owner_by_path: dict[str, int] = {}
    primary_paths: list[str] = []
    for index, member in enumerate(payload.members):
        path = _member_primary_path(member, claim_identities=claim_identities, creations=creations)
        primary_paths.append(path)
        owner = owner_by_path.get(path)
        if owner is not None:
            _refuse(
                "cruxible.authoring.change_set_member_path_collision",
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

    staged_tree: MutableMapping[str, bytes] = fork_tree(base_tree)
    claim_index = _ClaimPredicateIndex(staged_tree)
    member_paths: set[str] = set()
    installed_by: dict[str, set[int]] = {}
    resolved: list[dict[str, object]] = []
    candidate_identities = frozenset(
        authoring_member_identity(member)
        for member in payload.members
        if not isinstance(member, ProcedureAuthoringPayload)
    )
    candidate_paths = tuple(
        primary_paths[i]
        for i, member in enumerate(payload.members)
        if not isinstance(member, ProcedureAuthoringPayload)
    )
    for stage in MEMBER_STAGING_ORDER:
        stage_indices = [
            index for index, member in enumerate(payload.members) if _member_stage(member) == stage
        ]
        for index in _stage_member_order(stage_indices, payload.members):
            member = payload.members[index]
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
                        claim_index=claim_index,
                        path=path,
                        members=payload.members,
                        claim_identities=claim_identities,
                        candidate_identities=candidate_identities,
                        candidate_paths=candidate_paths,
                        re_author_siblings=re_author_siblings.get(index, {}),
                        derivation_procedure=derivation_procedure,
                        captures=captures,
                        creations=creations,
                        key_paths=key_paths,
                    )
                except AuthoringLoweringError as error:
                    raise _rescope_member_error(
                        error,
                        index=index,
                        sibling_member_by_claim_id=sibling_member_by_claim_id,
                    ) from error
                if isinstance(member, ClaimTypeSuccessionMember):
                    _refuse_contended_succession_paths(
                        index=index,
                        changed_paths=extra_paths,
                        owner_by_path=owner_by_path,
                        re_authored=frozenset(re_author_siblings.get(index, {}).values()),
                        members=payload.members,
                    )
                sibling_resolved.update(staged_siblings)
                claim_index.advance(staged_tree, {path, *extra_paths})
                member_paths.update(extra_paths)
                for extra_path in extra_paths:
                    installed_by.setdefault(extra_path, set()).add(index)
            member_resolved["identity"] = authoring_member_identity(member)
            member_resolved["member"] = index
            member_resolved["path"] = path
            resolved.append(member_resolved)
    _recheck_key_references_lowered(
        instance,
        base=base,
        base_tree=base_tree,
        staged_tree=staged_tree,
        members=payload.members,
        claim_identities=claim_identities,
        creations=creations,
        references=key_references,
    )
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
        idempotent=not changed and base.git_oid == instance.accepted_coordinate().git_oid,
        subject_creations=assigned_creations(creations),
    )


StagedMember: TypeAlias = tuple[
    MutableMapping[str, bytes],
    dict[str, object],
    set[str],
    dict[int, dict[str, object]],
]
"""The staged tree, what this member resolved to, the extra paths it wrote, and
what any sibling member it consumed resolved to."""


def _stage_change_set_member(
    instance: PlaybillInstance,
    *,
    member: AuthoringChangeSetMember,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    staged_tree: Mapping[str, bytes],
    claim_index: _ClaimPredicateIndex,
    path: str,
    members: tuple[AuthoringChangeSetMember, ...],
    claim_identities: Mapping[str, str],
    candidate_identities: frozenset[str],
    candidate_paths: tuple[str, ...],
    re_author_siblings: Mapping[str, int],
    derivation_procedure: ArtifactPin | None = None,
    captures: ProducerReceiptResolverProtocol,
    creations: Mapping[str, _Creation] | None = None,
    key_paths: Mapping[tuple[str, str], str] | None = None,
) -> StagedMember:
    """Write one member into the staged tree and report what it resolved to."""

    if isinstance(member, SubjectCreationAuthoringPayload):
        assert creations is not None
        return _stage_subject_creation(
            instance,
            member=member,
            creation=creations[member.member_identity],
            intent=intent,
            actor_id=actor_id,
            base=base,
            staged_tree=staged_tree,
            claim_index=claim_index,
            derivation_procedure=derivation_procedure,
            captures=captures,
        )
    if isinstance(member, ClaimAuthoringPayloadV1):
        claim_id = claim_identities[authoring_member_identity(member)]
        lowered = _lower_claim(
            instance,
            intent=intent,
            actor_id=actor_id,
            base=base,
            base_tree=staged_tree,
            payload=_naming_creations(member, creations or {}, element=path),
            claim_identity=claim_id,
            claim_index=claim_index,
            derivation_procedure=derivation_procedure,
            captures=captures,
            key_paths=key_paths or {},
        )
        member_resolved = dict(lowered.resolved_authoring)
        member_resolved["claim_id"] = claim_id
        extra = {member_path for member_path, _content in lowered.changed_members}
        return fork_tree(lowered.proposed_tree), member_resolved, extra, {}
    if isinstance(member, ClaimTypeSuccessionMember):
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
            captures=captures,
            key_paths=key_paths or {},
        )
    if isinstance(member, ClaimRetirementMember):
        tree, resolved, extra = _stage_claim_retirement(
            instance,
            member=member,
            base=base,
            staged_tree=staged_tree,
            path=path,
        )
        return tree, resolved, extra, {}
    if isinstance(member, ProcedureAuthoringPayload):
        lowered = _lower_procedure(
            instance,
            intent=intent.model_copy(update={"payload": member}),
            base=base,
            base_tree=staged_tree,
            accepted_reference_tree=base_tree,
            candidate_identities=candidate_identities,
            candidate_paths=candidate_paths,
        )
        return fork_tree(lowered.proposed_tree), dict(lowered.resolved_authoring), set(), {}
    if isinstance(member, ProcedureMandateAuthoringPayload):
        mandate_path, content, digest = _render_procedure_mandate_member(
            member,
            tree=staged_tree,
            resolve=_mandate_alias_resolver(instance, base, staged_tree),
        )
        candidate_tree = fork_tree(staged_tree)
        candidate_tree[mandate_path] = content
        return candidate_tree, {"artifact_digest": digest}, set(), {}
    if isinstance(member, LineAuthoringPayload):
        line_path, content, digest = _render_line_member(
            member,
            tree=staged_tree,
            light=base.compiler == LIGHT_ARTIFACTS_COMPILER,
            resolve_provider=_line_provider_resolver(instance, base, staged_tree),
        )
        candidate_tree = fork_tree(staged_tree)
        candidate_tree[line_path] = content
        return candidate_tree, {"artifact_digest": digest}, set(), {}
    if isinstance(member, TriggerAuthoringPayload):
        trigger_member_path, content, digest = _render_trigger_member(
            member,
            tree=staged_tree,
            light=base.compiler == LIGHT_ARTIFACTS_COMPILER,
        )
        candidate_tree = fork_tree(staged_tree)
        candidate_tree[trigger_member_path] = content
        return (
            candidate_tree,
            {
                "artifact_digest": digest,
                **_resolved_trigger_schedule(content, path=trigger_member_path),
            },
            set(),
            {},
        )
    if isinstance(member, SubjectKindAuthoringPayload):
        kind_path, content, digest = _render_subject_kind_member(member, tree=staged_tree)
        candidate_tree = fork_tree(staged_tree)
        candidate_tree[kind_path] = content
        return candidate_tree, {"artifact_digest": digest}, set(), {}
    if isinstance(member, ContractAuthoringPayload):
        contract_member_path, content, digest = _render_contract_member(member, tree=staged_tree)
        candidate_tree = fork_tree(staged_tree)
        candidate_tree[contract_member_path] = content
        return candidate_tree, {"artifact_digest": digest}, set(), {}
    _path, content, digest = _render_non_procedure_member(
        member,
        tree=staged_tree,
        light=base.compiler == LIGHT_ARTIFACTS_COMPILER,
        query_paths=_authored_query_paths(member, instance, base, staged_tree),
    )
    candidate_tree = fork_tree(staged_tree)
    candidate_tree[path] = content
    builtin_paths: set[str] = set()
    if isinstance(member, ClaimTypeAuthoringPayload) and base.compiler == LIGHT_ARTIFACTS_COMPILER:
        from cruxible_core.claims.claim_type_inputs import install_builtin_capture_dependencies

        builtin_paths = install_builtin_capture_dependencies(member.claim_type, candidate_tree)
    return candidate_tree, {"artifact_digest": digest}, builtin_paths, {}


def _resolve_re_author_siblings(
    members: tuple[AuthoringChangeSetMember, ...],
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
        if not isinstance(member, ClaimTypeSuccessionMember):
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
                    "cruxible.authoring.claim_type_succession_re_author_invalid",
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
                    "cruxible.authoring.claim_type_succession_re_author_invalid",
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
                    "cruxible.authoring.claim_type_succession_re_author_invalid",
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
                        "cruxible.authoring.claim_type_succession_re_author_invalid",
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
                    "cruxible.authoring.claim_type_succession_re_author_invalid",
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
    members: tuple[AuthoringChangeSetMember, ...],
) -> None:
    """Refuse a set that settles one artifact twice: in a succession and as a member.

    A succession rewrites every dependent of the ClaimType it succeeds. Another
    member that authors one of those same paths -- a `ClaimRetirementMember`
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
            "cruxible.authoring.change_set_member_path_collision",
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
    member: ClaimTypeSuccessionMember,
    intent: AuthoringIntentV1,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    staged_tree: Mapping[str, bytes],
    path: str,
    members: tuple[AuthoringChangeSetMember, ...],
    claim_identities: Mapping[str, str],
    re_author_siblings: Mapping[str, int],
    captures: ProducerReceiptResolverProtocol,
    key_paths: Mapping[tuple[str, str], str] | None = None,
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

    try:
        type_path, predecessor, successor = resolve_claim_type_succession(
            staged_tree,
            member.successor,
            identity_rules=identity_rules_supported(base.compiler),
        )
    except ClaimTypeMigrationError as error:
        _refuse(
            "cruxible.authoring.claim_type_succession_invalid",
            "successor",
            str(error),
            repair_kind="replace_successor",
            repair_description=(
                "Name the ClaimType this set succeeds and pin its exact current digest."
            ),
        )
    try:
        inventory = claim_type_migration_inventory(
            staged_tree, root=successor.identity, by_name=base.compiler == LIGHT_ARTIFACTS_COMPILER
        )
    except ClaimTypeMigrationError as error:
        _refuse(
            "cruxible.authoring.claim_type_succession_closure_unsupported",
            "dependents",
            str(error),
            repair_kind="split_change_set",
            repair_description=(
                "Settle the dependents this succession cannot reach through their own "
                "governed change first."
            ),
        )
    required = {item.identity.qualified: item for item in inventory}
    if member.carry_all:
        # The server fills the closure: every member the author did not name is
        # carried to the successor as it stands, retired Claims included, so a
        # closure too large to list in a repair is still one member to write.
        named = {item.identity.qualified for item in member.dependents}
        member = member.model_copy(
            update={
                "dependents": tuple(
                    sorted(
                        (
                            *member.dependents,
                            *(
                                ClaimTypeSuccessionDependent(
                                    identity=item.identity, disposition="successor"
                                )
                                for item in inventory
                                if item.identity.qualified not in named
                            ),
                        ),
                        key=lambda item: item.identity.qualified.encode("utf-8"),
                    )
                )
            }
        )
    supplied = {item.identity.qualified: item for item in member.dependents}
    if set(required) != set(supplied):
        _refuse(
            "cruxible.authoring.claim_type_succession_closure_incomplete",
            "dependents",
            "A ClaimType succession must disposition its exact reverse-pin closure.",
            repair_kind="replace_dependents",
            repair_description=(
                # A succession dependent has no digest field: it is named by
                # identity alone and the succession re-reads the bytes itself.
                # `current_artifact_digest` rides in `required_dependents` as an
                # informational read, never as something to copy back.
                "Disposition exactly the listed dependents, each named by the exact identity "
                "given; `missing` names the ones this member omits and `unexpected` the ones it "
                "names outside the closure. Or set carry_all: true to carry every dependent "
                "this member does not name to the successor. A truncated list is continued "
                "by `cruxible claim-type migrate` in preflight mode."
            ),
            replacement={
                **_closure_difference(required, supplied),
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
                    "cruxible.authoring.claim_type_succession_object_kind_change",
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
    lowering_tree = fork_tree(staged_tree)
    lowering_tree[type_path] = render_claim_type(successor)
    working = fork_tree(staged_tree)
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
                captures=captures,
                key_paths=key_paths or {},
            )
        except AuthoringLoweringError as error:
            _refuse(
                "cruxible.authoring.claim_type_succession_re_author_refused",
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
        ClaimTypeDependentDisposition(
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
            "cruxible.authoring.claim_type_succession_dependent_invalid",
            "dependents",
            str(error),
            repair_kind="replace_dependents",
            repair_description=(
                "Re-read the closure this succession owes and disposition it exactly."
            ),
        )
    # The migration builder still materializes its closure result. Reattach
    # only its changed members to the shared parent, preserving unaffected
    # contender memberships for the following ordinary Claim members.
    shared_candidate = fork_tree(working)
    for changed_path, content in candidate_tree.items():
        if working.get(changed_path) != content:
            shared_candidate[changed_path] = content
            if changed_path != path:
                extra.add(changed_path)
    resolved: dict[str, object] = {
        "artifact_digest": claim_type_digest(successor).tagged,
        "predecessor_digest": claim_type_digest(predecessor).tagged,
        "dependents": [item.model_dump(mode="json") for item in normalized],
    }
    return shared_candidate, resolved, extra, sibling_resolved


def _stage_claim_retirement(
    instance: PlaybillInstance,
    *,
    member: ClaimRetirementMember,
    base: AcceptedProjectionCoordinate,
    staged_tree: Mapping[str, bytes],
    path: str,
) -> tuple[MutableMapping[str, bytes], dict[str, object], set[str]]:
    """Retire one Claim and its live closure inside the same change set."""

    content = staged_tree.get(path)
    if content is None:
        _refuse(
            "cruxible.authoring.claim_predecessor_not_found",
            "retires",
            "The Claim named for retirement does not exist in this change set's tree.",
            repair_kind="replace_retires",
            repair_description="Retire a Claim accepted at the intent base.",
        )
    claim = parse_claim(content, path=path)
    if claim.lifecycle.state != "live":
        _refuse(
            "cruxible.authoring.claim_terminal",
            "retires",
            "A retired Claim cannot be retired again.",
            repair_kind="drop_member",
            repair_description="Remove this retirement member; the Claim is already retired.",
        )
    root = ClaimRetireDependent(
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
            "cruxible.authoring.claim_retirement_closure_unsupported",
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
            "cruxible.authoring.claim_retirement_closure_incomplete",
            "dependents",
            "A retirement member must carry its exact live Claim closure.",
            repair_kind="replace_dependents",
            repair_description=(
                "Carry exactly the listed dependents at their exact digests; `missing` names "
                "the ones this member omits and `unexpected` the ones it names outside the "
                "closure."
            ),
            replacement={
                **_closure_difference(
                    expected,
                    (
                        identity
                        for identity, digest in supplied.items()
                        if expected.get(identity) == digest
                    ),
                ),
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
            "cruxible.authoring.claim_retirement_stale",
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
    shared_candidate = fork_tree(staged_tree)
    for changed_path in {path, *extra}:
        shared_candidate[changed_path] = candidate_tree[changed_path]
    return (
        shared_candidate,
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
            # The member's file digest: the one every later layer takes of it.
            "content_sha256": file_digest(content).tagged,
            "path": path,
        }
        for path, content in members
    ]


@timed_phase("lower")
def lower_authoring(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor_id: str,
    derivation_procedure: ArtifactPin | None = None,
    captures: ProducerReceiptResolverProtocol | None = None,
) -> LoweredAuthoring:
    """Resolve one intent against its immutable base without submitting anything.

    Every Subject the lowering creates is checked for a collision with an
    accepted Subject of its kind (``_refuse_subject_collisions``).
    """

    lowered = _lower_authoring(
        instance,
        intent=intent,
        actor_id=actor_id,
        derivation_procedure=derivation_procedure,
        captures=captures,
    )
    _refuse_subject_collisions(instance, intent=intent, lowered=lowered)
    return lowered


_DISTINCT_FROM = "semantic.distinct_from"


def lowered_authority(operation: str, lowered: LoweredAuthoring) -> SubjectAssignmentAuthority:
    """The creations one lowering assigned, as the boundary's authority for its own intent."""

    recorded = {item.record for item in lowered.subject_creations}
    return lambda named, creation: named == operation and creation in recorded


def assigned_creations(
    creations: Mapping[str, _Creation],
) -> tuple[AssignedCreation, ...]:
    """Each creation member's actual assignment: rule, outcome, key and id (review F-001)."""

    found: list[AssignedCreation] = []
    for member, creation in sorted(creations.items()):
        assignment = creation.assignment
        rule: Literal["first_holder", "further_holder", "minted"] = (
            "first_holder" if assignment is None else assignment.rule
        )
        found.append(
            AssignedCreation(
                record=RecordedCreation(
                    member=member,
                    kind=creation.kind,
                    key=key_encoding(creation.key) if creation.key else None,
                    rule=rule,
                    outcome="reused" if creation.resolved else "created",
                    subject_id=creation.subject_id,
                ),
                path=creation.path,
                key=creation.key,
            )
        )
    return tuple(found)


def _refuse_subject_collisions(
    instance: PlaybillInstance, *, intent: AuthoringIntentV1, lowered: LoweredAuthoring
) -> None:
    """Refuse early a new Subject that collides with a live Subject of its kind.

    The authoritative check is candidate evaluation's (admission and
    settlement, ``indexes.subject_aliases.candidate_creation_collisions``);
    this is the same check run on the lowered candidate, so the author learns
    which member to repair before anything is proposed. A Subject this lowering
    creates -- a Subject member, a Claim's Subject draft, a write's added
    Subject, a Procedure item's Subject -- collides when its id's canonical
    token equals another live Subject's id, canonical token or alias of the
    same kind, accepted or added by this change. The author resolves it by
    naming the existing Subject, by adding an alias to it, or by asserting
    ``semantic.distinct_from`` between the two in the same change, which
    exempts that pair.
    """

    if not any(
        path.startswith(("subjects/", "claims/", "subject-kinds/"))
        for path, _content in lowered.changed_members
    ):
        return
    base = instance.resolve_accepted_coordinate(
        git_oid=intent.base_coordinate.git_oid,
        semantic_root=intent.base_coordinate.semantic_root,
        generation_root=intent.base_coordinate.generation_root,
    )
    current_tree = instance.immutable_tree_at(base.git_oid)
    scope = tuple(path for path, _content in lowered.changed_members)
    for issue in boundary_identity_issues(
        base.compiler,
        current_tree=current_tree,
        candidate_tree=lowered.proposed_tree,
        scope=scope,
        # The creations this lowering made are its own intent's (review F-001).
        assignment_authority=lowered_authority(intent.intent_id, lowered),
    ):
        kind, _subject_id = subject_kind_of(issue.path)
        _refuse(
            issue.code,
            issue.path
            if issue.path not in lowered.member_by_path
            else f"members[{lowered.member_by_path[issue.path]}]",
            issue.message,
            repair_kind=(
                "author_subject_kind"
                if issue.code == SUBJECT_KIND_UNDECLARED
                else "upgrade_compiler"
                if issue.code == SUBJECT_COMPILER_UPGRADE_REQUIRED
                else "use_existing_subject"
            ),
            repair_description=(
                undeclared_kind_repair(kind)
                if issue.code == SUBJECT_KIND_UNDECLARED
                else compiler_upgrade_repair(kind)
                if issue.code == SUBJECT_COMPILER_UPGRADE_REQUIRED
                else issue.message
            ),
            replacement={"subject_kind": kind, "existing": list(issue.existing)},
        )
    if not any(path.startswith("subjects/") for path in scope):
        return
    collisions = candidate_creation_collisions(
        current_tree=current_tree,
        candidate_tree=lowered.proposed_tree,
        scope=scope,
    )
    for path, others in collisions:
        kind, _, rest = path.removeprefix("subjects/").partition("/")
        subject_id = rest.removesuffix(".json")
        _refuse(
            "cruxible.authoring.subject_collision",
            path
            if path not in lowered.member_by_path
            else f"members[{lowered.member_by_path[path]}]",
            subject_collision_message(path, others),
            repair_kind="use_existing_subject",
            repair_description=(
                f"Name {others[0]} instead, add a semantic.alias to it, or "
                f"assert {_DISTINCT_FROM} between {kind}/{subject_id} and it."
            ),
            replacement={"existing": list(others)},
        )


def _lower_authoring(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor_id: str,
    derivation_procedure: ArtifactPin | None = None,
    captures: ProducerReceiptResolverProtocol | None = None,
) -> LoweredAuthoring:
    """Resolve one intent against its immutable base without submitting anything.

    Only the Procedure terminal supplies `derivation_procedure`, from the actual
    run. It is not a wire field or inferred from the intent's ID or payload.
    Proposal admission separately checks authority over the exact lowered bytes.

    `captures` is the request's Capture verification context; without one the
    lowering builds its own, shared by every Claim it lowers, so N Claims citing
    one Capture read that Capture's run once.
    """

    base = instance.resolve_accepted_coordinate(
        git_oid=intent.base_coordinate.git_oid,
        semantic_root=intent.base_coordinate.semantic_root,
        generation_root=intent.base_coordinate.generation_root,
    )
    base_tree = instance.immutable_tree_at(base.git_oid)
    if captures is None:
        captures = instance.capture_verification_context()
    if isinstance(intent.payload, ClaimAuthoringPayloadV1):
        return _lower_claim(
            instance,
            intent=intent,
            actor_id=actor_id,
            base=base,
            base_tree=base_tree,
            derivation_procedure=derivation_procedure,
            captures=captures,
        )
    if isinstance(intent.payload, ProcedureAuthoringPayload):
        return _lower_procedure(instance, intent=intent, base=base, base_tree=base_tree)
    if isinstance(intent.payload, ProcedureMandateAuthoringPayload):
        path, content, digest = _render_procedure_mandate_member(
            intent.payload,
            tree=base_tree,
            resolve=_mandate_alias_resolver(instance, base, base_tree),
        )
        candidate_tree = fork_tree(base_tree)
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
            idempotent=not changed and base.git_oid == instance.accepted_coordinate().git_oid,
        )
    if isinstance(intent.payload, ChangeSetAuthoringPayload):
        return _lower_change_set(
            instance,
            intent=intent,
            actor_id=actor_id,
            base=base,
            base_tree=base_tree,
            derivation_procedure=derivation_procedure,
            captures=captures,
        )
    if isinstance(intent.payload, ContractAuthoringPayload):
        path, content, digest = _render_contract_member(intent.payload, tree=base_tree)
        candidate_tree = fork_tree(base_tree)
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
            idempotent=not changed and base.git_oid == instance.accepted_coordinate().git_oid,
        )
    if isinstance(
        intent.payload,
        LineAuthoringPayload | TriggerAuthoringPayload | SubjectKindAuthoringPayload,
    ):
        path, content, digest = (
            _render_line_member(
                intent.payload,
                tree=base_tree,
                light=base.compiler == LIGHT_ARTIFACTS_COMPILER,
                resolve_provider=_line_provider_resolver(instance, base, base_tree),
            )
            if isinstance(intent.payload, LineAuthoringPayload)
            else _render_trigger_member(
                intent.payload,
                tree=base_tree,
                light=base.compiler == LIGHT_ARTIFACTS_COMPILER,
            )
            if isinstance(intent.payload, TriggerAuthoringPayload)
            else _render_subject_kind_member(intent.payload, tree=base_tree)
        )
        candidate_tree = fork_tree(base_tree)
        candidate_tree[path] = content
        changed = () if base_tree.get(path) == content else ((path, content),)
        return LoweredAuthoring(
            proposed_tree=candidate_tree,
            resolved_authoring={
                "artifact_digest": digest,
                **(
                    _resolved_trigger_schedule(content, path=path)
                    if isinstance(intent.payload, TriggerAuthoringPayload)
                    else {}
                ),
                "changed_members": _encoded_members(changed),
                "identity": authoring_member_identity(intent.payload),
            },
            changed_members=changed,
            idempotent=not changed and base.git_oid == instance.accepted_coordinate().git_oid,
        )
    return _lower_non_procedure(
        payload=intent.payload, base_tree=base_tree, base=base, instance=instance
    )


__all__ = [
    "AuthoringLoweringError",
    "LoweredAuthoring",
    "lower_authoring",
]
