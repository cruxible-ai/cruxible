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

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    approval_policy_digest,
    render_approval_policy,
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.authoring.inputs import (
    AuthoringInputError,
    refuse_unserved_procedure_nodes,
)
from cruxible_client.contracts.authoring.models import (
    MAX_REPAIR_BYTES,
    ApprovalPolicyAuthoringPayload,
    AttestationAuthoringPayload,
    AuthoringChangeSetMember,
    AuthoringClaimStatement,
    AuthoringExactContentObject,
    AuthoringIntent,
    AuthoringNoneClaimObject,
    AuthoringTriggerSchedule,
    CaptureContractAuthoringPayload,
    CaptureLandingByNameSchedule,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimRetirementMember,
    ClaimTypeAuthoringPayload,
    ClaimTypeSuccessionDependent,
    ClaimTypeSuccessionMember,
    ContractAuthoringPayload,
    ExistingCaptureCitationSource,
    LineAuthoringPayload,
    NoteAuthoringPayload,
    ProcedureAuthoringPayload,
    ProcedureMandateAuthoringPayload,
    ProcedureRuntimePolicyAuthoringPayload,
    QueryDefinitionAuthoringPayload,
    RepairAlternative,
    ResolutionContractAuthoringPayload,
    RunbookAuthoringPayload,
    SelfSourceBody,
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
    ClaimBacking,
    ClaimCitation,
    ClaimFormatError,
    ClaimReferentContext,
    ClaimRetireDependent,
    ClaimStatement,
    LiteralClaimObject,
    NoneClaimObject,
    SubjectClaimObject,
    _statement_with_claim_type_digest,
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
from cruxible_client.contracts.errors import CruxibleError, FormatError
from cruxible_client.contracts.notes import (
    Note,
    note_digest,
    note_identity,
    note_path,
    parse_note,
    render_note,
)
from cruxible_client.contracts.procedure_mandates import (
    MandateClaimScope,
    MandateCondition,
    ProcedureMandate,
    parse_procedure_mandate_any,
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
    ProcedureArtifact,
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.contracts import (
    AcceptedProcedureContractValidator,
    ProcedureContractValidationError,
    validate_line_parameters,
)
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest,
)
from cruxible_client.contracts.procedures.line_specs import (
    RUNG_AUTHORITY,
    LineSpec,
    caps_line_budgets,
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureDefinition,
    ProcedureHardCaps,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventNameSelector,
    CaptureEventNameWindow,
    FixedWindow,
)
from cruxible_client.contracts.providers import (
    parse_provider,
    provider_digest,
    provider_path,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinition,
    parse_query_definition,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.shapes import (
    ContractRef,
    claim_has_line_break,
    claim_value_kind,
    contract_digest,
    contract_path,
    map_type_refs,
    parse_contract,
    subject_value_kinds,
    tree_contract_resolver,
    type_refs,
)
from cruxible_client.contracts.source_references import (
    ExternalSourceReference,
    LedgerSourceReference,
)
from cruxible_client.contracts.subject_kinds import (
    KeyClaimTypeShape,
    SubjectEnd,
    SubjectIdUnassignable,
    SubjectKeyPartValue,
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
)
from cruxible_core.authoring.registrations import source_projection_blocks
from cruxible_core.claims.claim_retirement import (
    ChangeRetirementClosures,
    ClaimRetireError,
    build_claim_retirement_candidate,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    build_claim_type_migration_candidate,
    claim_type_migration_inventory,
    resolve_claim_type_succession,
)
from cruxible_core.claims.enum_resolution import tree_enum_resolver
from cruxible_core.derived.derived_state import CandidateTree, SnapshotTree, fork_tree
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.indexes.subject_aliases import (
    candidate_creation_collisions,
    resolve_reference_text,
    resolve_subject_ref,
    split_reference,
    subject_collision_message,
)
from cruxible_core.indexes.subject_identity import (
    SUBJECT_KEY_MISSING,
    SUBJECT_KIND_UNDECLARED,
    IdentityOverlay,
    IdentityView,
    KeyTuple,
    RecordedCreation,
    SubjectAssignmentAuthority,
    SubjectKeyInvalid,
    assign_creation_id,
    candidate_identity_issues,
    canonical_key,
    claim_type_key_shape,
    creation_order,
    key_ambiguous_message,
    key_encoding,
    key_values,
    kind_depths,
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


def _staged_capture_contract_by_digest(
    base_tree: Mapping[str, bytes],
    staged_paths: Iterable[str],
    contract_digest: str,
) -> AcceptedCaptureContract | None:
    """A CaptureContract this same lowering call just staged, by exact digest.

    ``staged_paths`` is this one Claim's own newly-installed dependency paths
    (``_install_claim_dependencies``'s ``dependency_paths``) -- bounded by
    this change, never the whole tree -- so checking each is a keyed lookup
    over a handful of entries, not a catalog walk.
    """

    for path in staged_paths:
        if not path.startswith("capture-contracts/"):
            continue
        content = base_tree.get(path)
        if content is None:
            continue
        contract = parse_capture_contract(content, path=path)
        if capture_contract_digest(contract).tagged == contract_digest:
            return AcceptedCaptureContract(
                path=path, contract=contract, artifact_digest=contract_digest
            )
    return None


def _capture_contract_at_base(
    instance: PlaybillInstance,
    *,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    contract_digest: str,
    staged_paths: Iterable[str] = (),
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
        # Or this exact write's own working-selection lowering just staged it
        # (a foreign-source dependency this same member installs for the
        # first time): neither accepted nor enumerated, a keyed check over
        # this change's own small set of newly-staged paths.
        staged = _staged_capture_contract_by_digest(base_tree, staged_paths, contract_digest)
        if staged is not None:
            return staged
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


def _line_budgets_within_caps(budgets: Any, caps: ProcedureHardCaps) -> Any:
    """A Line's operating budget, each unstated required limit the cap, within the caps.

    The Line owns the budget; the Procedure only declares caps.
    """

    filled = budgets.filled(caps)
    widened = filled.widened(caps)
    if widened:
        _refuse(
            "cruxible.authoring.line_budget_exceeds_caps",
            "budgets",
            "A Line's operating budget may narrow but never widen its Procedure's hard "
            f"caps; it widens {', '.join(widened)}.",
            repair_kind="replace_budgets",
            repair_description="Set each budget to at most the Procedure's hard cap.",
        )
    return filled


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


def _claim_object(
    value: object,
) -> LiteralClaimObject | SubjectClaimObject | NoneClaimObject:
    if isinstance(value, AuthoringNoneClaimObject):
        return NoneClaimObject(of=value.of)
    assert isinstance(value, LiteralClaimObject | SubjectClaimObject)
    return value


_ClaimPayloadT = TypeVar("_ClaimPayloadT", bound=ClaimAuthoringPayload)
_ObjectT = TypeVar("_ObjectT")


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
    payload: _ClaimPayloadT,
) -> _ClaimPayloadT:
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
    drafted = payload.dependency_drafts.subject is not None
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
    payload: _ClaimPayloadT,
) -> _ClaimPayloadT:
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
    value: LiteralClaimObject | SubjectClaimObject | NoneClaimObject,
    claim_type: ClaimType,
) -> LiteralClaimObject | SubjectClaimObject | NoneClaimObject:
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
        if not isinstance(member, ClaimAuthoringPayload):
            return member
        member = cast(ClaimAuthoringPayload, _with_landed_addresses(member, mapped_reference))
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
    """Two authored members state one statement once canonicalized."""

    error_code = "cruxible.authoring.duplicate_statement"


def _same_predicate_claims(
    tree: Mapping[str, bytes], statement: ClaimStatement
) -> tuple[ClaimArtifact, ...]:
    """Return every live Claim on this statement's (subject, predicate).

    Wider than the contended slot: these are the Claims an author may
    disposition voluntarily, not the ones the law demands.
    """
    claims: list[ClaimArtifact] = []
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
) -> tuple[ClaimArtifact, ...]:
    """Return the live Claims contending for this statement's own slot.

    The slot is ``(subject, predicate, qualifier)`` — the same partition the
    resolution policy and the curation detectors already use. Matching on
    subject+predicate alone made every distinct qualifier and every
    cardinality=many sibling demand a disposition it does not contend with.
    """
    claims: list[ClaimArtifact] = []
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
        self._claims: dict[tuple[SemanticAddress, str], dict[str, ClaimArtifact]] | None = None
        self._keys: dict[str, tuple[SemanticAddress, str]] = {}
        # Models belong to this lowering only. Shared roots retain immutable
        # bytes; repeated siblings avoid parsing the same requested bytes again.
        self._parsed: dict[str, tuple[bytes, ClaimArtifact]] = {}

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

    def claims_for(self, statement: ClaimStatement) -> tuple[ClaimArtifact, ...]:
        if isinstance(self._tree, SnapshotTree | CandidateTree):
            requested: list[ClaimArtifact] = []
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
    payload: ClaimAuthoringPayload,
    statement: ClaimStatement,
    predecessor: ClaimArtifact | None,
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
    if predecessor is None or statement != _statement_with_claim_type_digest(
        predecessor.statement, statement.claim_type_digest
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
    payload: ClaimAuthoringPayload,
    *,
    base_tree: Mapping[str, bytes],
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
    if payload.dependency_drafts.empty:
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
        claim_type_draft = _pin_claim_type_value(claim_type_draft, base_tree)
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
    payload: ClaimAuthoringPayload | NoteAuthoringPayload,
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


def _claim_pin(
    *,
    role: str,
    target: ArtifactIdentity,
    artifact_digest: str,
    previous: tuple[ArtifactPin, ...] = (),
) -> ArtifactPin:
    for pin in previous:
        if pin.role == role and pin.target == target and pin.artifact_digest == artifact_digest:
            return pin

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
    intent: AuthoringIntent,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
    payload: ClaimAuthoringPayload | None = None,
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
    assert isinstance(authored, ClaimAuthoringPayload)
    payload = authored
    derivation = payload.derivation
    if payload.procedure_evidence is not None and derivation_procedure is None:
        _refuse(
            "cruxible.authoring.derivation_requires_execution",
            "procedure_evidence",
            "Only the executing Claims terminal can bind a claim-evidence-v2 receipt.",
            repair_kind="run_procedure",
            repair_description="Run the declared Procedure.",
        )
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
    if isinstance(payload.statement.object, AuthoringExactContentObject):
        _refuse(
            "cruxible.claim.exact_content_retired",
            "statement.object",
            "Prose lives in Documents; cite a Document section",
            repair_kind="cite_document_section",
            repair_description="Put prose in a Document and cite a Document section.",
        )
    if (
        isinstance(payload.statement.object, LiteralClaimObject)
        and isinstance(payload.statement.object.value, str)
        and claim_has_line_break(payload.statement.object.value)
    ):
        _refuse(
            "cruxible.claim.multiline_string",
            "statement.object",
            "String Claims cannot contain newlines",
            repair_kind="cite_document_section",
            repair_description="Prose lives in Documents; cite a Document section.",
        )
    statement_object = _claim_object(payload.statement.object)
    # A none's shape is judged against the ClaimType's inverse by the Claim law.
    if not isinstance(statement_object, NoneClaimObject) and (
        statement_object.kind != claim_value_kind(claim_type.value)
    ):
        _refuse(
            "cruxible.claim.object_kind_mismatch",
            "statement.object",
            "The Claim object kind differs from its accepted ClaimType.",
            repair_kind="replace_object",
            repair_description=(
                f"Use a {claim_value_kind(claim_type.value)} object "
                f"for ClaimType {claim_type.predicate!r}."
            ),
            replacement={"required_object_kind": claim_value_kind(claim_type.value)},
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
            subject_value_kinds(claim_type.value),
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
                replacement={"allowed_subject_kinds": list(subject_value_kinds(claim_type.value))},
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
    predecessor: ClaimArtifact | None = None
    if path in candidate_base_tree:
        predecessor = parse_claim(candidate_base_tree[path], path=path)
        if predecessor.retirement is not None:
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
    if isinstance(payload.source, SelfSourceBody):
        profile_path = capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name)
        previous_profile_bytes = candidate_base_tree.get(profile_path)
        previous_profile = (
            None
            if previous_profile_bytes is None
            else parse_capture_contract(previous_profile_bytes, path=profile_path)
        )
        contract = builtin_contract_for_write(
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, previous_profile
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
            staged_paths=dependency_paths,
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
        head_path = capture_contract_path(
            foreign_source_capture_contract(source.source_id).identity.name
        )
        head_bytes = candidate_base_tree.get(head_path)
        head = None if head_bytes is None else parse_capture_contract(head_bytes, path=head_path)
        if head is not None and head.lifecycle.state == "live":
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

    if isinstance(payload.source, ExistingCaptureCitationSource):
        capture_digest_value = payload.source.capture_digest
        capture_envelope = envelope
    else:
        assert built_capture is not None
        capture_digest_value = built_capture.capture_digest
        capture_envelope = built_capture.envelope

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

    identity = (
        predecessor.identity
        if predecessor is not None
        else ArtifactIdentity(kind="Claim", name=claim_id)
    )
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
        != _statement_with_claim_type_digest(predecessor.statement, statement.claim_type_digest)
    ):
        inherited = None
    predecessor_citations = () if inherited is None else inherited.backing.citations
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
        located_path = capture_contract_path(
            foreign_source_capture_contract(location.source_id).identity.name
        )
        previous_contract = candidate_base_tree.get(located_path)
        located_contract = (
            None
            if previous_contract is None
            else parse_capture_contract(previous_contract, path=located_path)
        )
        located_successor = builtin_contract_for_write(
            foreign_source_capture_contract(location.source_id), located_contract
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
            previous=() if predecessor is None else predecessor.pins,
        ),
        _claim_pin(
            role="claim-type",
            target=claim_type.identity,
            artifact_digest=claim_type_digest(claim_type).tagged,
            previous=() if predecessor is None else predecessor.pins,
        ),
        _claim_pin(
            role="subject",
            target=subject_identity,
            artifact_digest=subject_digest_value,
            previous=() if predecessor is None else predecessor.pins,
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
                    raise ValueError(
                        f"derivation binding {pin.target.qualified} changed "
                        f"from {pin.artifact_digest} to {digest}"
                    )
                pins.append(pin)
        except (ValueError, CruxibleError) as exc:
            _refuse(
                "cruxible.authoring.derivation_unverified",
                "derivation",
                str(exc),
                repair_kind="rebind_derivation",
                repair_description="Read the basis again at the accepted base.",
            )
    claim = ClaimArtifact(
        identity=identity,
        statement=statement,
        backing=ClaimBacking(
            procedure_evidence=payload.procedure_evidence,
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
            historical_capture_contract=lambda digest: (
                _staged_capture_contract_by_digest(candidate_base_tree, dependency_paths, digest)
                or instance.accepted_capture_contract_version(public_base, digest)
            ),
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
    final: ClaimArtifact = claim
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
    intent: AuthoringIntent,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
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
    if "procedure_spec" not in payload.definition:
        retired_form = (
            "the retired @procedure Python source (source_request)"
            if "source_request" in payload.definition
            else "a graph definition"
        )
        _refuse(
            "cruxible.authoring.procedure_authoring_form_retired",
            "definition",
            f"A Procedure is authored as one typed ProcedureSpec, not as {retired_form}.",
            repair_kind="replace_definition",
            repair_description=(
                "Submit the Procedure as a ProcedureSpec ({procedure, input, steps, ...})."
            ),
        )
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
            " | ".join(_validation_error_lines(exc, root="procedure_spec"))
            if isinstance(exc, ValidationError)
            else str(exc),
            repair_kind="replace_definition",
            repair_description="Repair the authored Procedure.",
        )
    if not payload.retire:
        _refuse_unserved_procedure(payload.definition)
    return _lower_named_procedure(instance, payload, base=base, base_tree=base_tree)


def _lower_named_procedure(
    instance: PlaybillInstance,
    payload: ProcedureAuthoringPayload,
    *,
    base: AcceptedProjectionCoordinate,
    base_tree: Mapping[str, bytes],
) -> LoweredAuthoring:
    """Validate the authored named tree into its one governed envelope.

    Each boundary the spec declared inline becomes the governed Contract
    ``<procedure>.<role>@N``, co-proposed in the same tree: a successor whose
    fields are unchanged keeps version N; changed fields are version N+1.
    """
    generated_files, definition_data = _co_propose_boundary_contracts(payload, base_tree=base_tree)
    try:
        definition = ProcedureDefinition.model_validate(
            _pin_slot_contracts(definition_data, base_tree)
        )
    except (ValueError, KeyError, ProcedureGraphFormatError) as exc:
        message = (
            " | ".join(_validation_error_lines(exc, root="definition"))
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        _refuse(
            "cruxible.authoring.procedure_definition_invalid",
            "definition",
            "The lowered Procedure definition is invalid: " + message,
            repair_kind="replace_definition",
            repair_description="Repair the indicated definition field.",
        )
    path = procedure_path(definition.name)
    predecessor = parse_procedure(base_tree[path], path=path) if path in base_tree else None
    try:
        procedure = ProcedureArtifact.model_validate(
            dict(
                identity=ArtifactIdentity(kind="Procedure", name=definition.name),
                definition=definition,
                definition_digest=compute_procedure_definition_digest(definition).tagged,
                activation_policy=payload.activation_policy,
                lifecycle=ArtifactLifecycle(
                    state="retired" if payload.retire else "live",
                    predecessor_digest=None
                    if predecessor is None
                    else procedure_artifact_digest(predecessor).tagged,
                ),
            )
        )
    except ValidationError as exc:
        _refuse(
            "cruxible.authoring.procedure_definition_invalid",
            "procedure",
            "The lowered Procedure artifact is invalid: "
            + " | ".join(_validation_error_lines(exc, root="procedure")),
            repair_kind="replace_definition",
            repair_description="Repair the Procedure definition or its boundary fields.",
        )
    return _procedure_lowered(
        instance,
        procedure,
        predecessor,
        path=path,
        base=base,
        base_tree=base_tree,
        co_proposed=generated_files,
    )


def _co_propose_boundary_contracts(
    payload: ProcedureAuthoringPayload, *, base_tree: Mapping[str, bytes]
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Each inline boundary's governed Contract and the definition that names it."""

    from cruxible_client.contracts.procedures.authored import (
        CONTRACT_ROLES,
        generated_contract_name,
    )
    from cruxible_core.authoring.contract_lowering import (
        ContractLoweringRefusal,
        render_contract_member,
    )

    definition = dict(payload.definition)
    files: dict[str, bytes] = {}
    for role, fields in sorted(payload.generated.items()):
        name = generated_contract_name(str(definition["name"]), role)
        latest = 0
        while contract_path(name, latest + 1) in base_tree:
            latest += 1
        version = latest + 1
        try:
            if latest:
                # Unchanged fields keep the accepted version: it is the same shape.
                try:
                    reused = render_contract_member(
                        ContractAuthoringPayload(name=name, version=latest, fields=fields),
                        tree=base_tree,
                    )
                except ContractLoweringRefusal as exc:
                    if exc.code != "cruxible.contract.immutable":
                        raise
                else:
                    version = latest
                    files[reused[0]] = reused[1]
            if version != latest:
                rendered = render_contract_member(
                    ContractAuthoringPayload(name=name, version=version, fields=fields),
                    tree=base_tree,
                )
                files[rendered[0]] = rendered[1]
        except ContractLoweringRefusal as exc:
            _refuse(
                exc.code,
                f"procedure_spec.{role}",
                f"The Procedure's {role} Contract {name}@{version}: {exc.message}",
                repair_kind="replace_definition",
                repair_description=exc.repair,
            )
        definition[CONTRACT_ROLES[role]] = {
            "target": {"kind": "Contract", "name": f"{name}/v{version}"}
        }
    return files, definition


def _pin_slot_contracts(definition: Any, tree: Mapping[str, bytes]) -> Any:
    """The definition with every slot Contract pinned to the version accepted in ``tree``.

    A slot is ``{in, out, effect}``, authored ``name@N``; lowering owns the
    digest, so an author can neither supply one nor pin a Contract this instance
    has not accepted (``cruxible.authoring.slot_contract_unresolved``).
    """

    from copy import deepcopy

    from cruxible_client.contracts.shapes import (
        contract_digest,
        contract_path,
        parse_contract,
        parse_contract_ref,
    )

    if not isinstance(definition, Mapping):
        return definition
    result = deepcopy(dict(definition))
    # A post_inbox node's item Contract is named ``name@N`` and pinned here too.
    result["nodes"] = list(result.get("nodes") or ())
    for index, node in enumerate(result["nodes"]):
        if not isinstance(node, Mapping) or node.get("kind") != "post_inbox":
            continue
        value = node.get("contract")
        ref = parse_contract_ref(value) if isinstance(value, str) else None
        if ref is None and isinstance(value, Mapping) and value.get("digest") is None:
            ref = parse_contract_ref(f"{value.get('name')}@{value.get('version')}")
        if ref is None:
            continue
        path = contract_path(ref.name, ref.version)
        content = tree.get(path)
        if content is None:
            _refuse(
                "cruxible.authoring.slot_contract_unresolved",
                f"definition.nodes[{index}].contract",
                f"post_inbox {node.get('node_id')!r} names {ref.spelling}, which is not accepted.",
                repair_kind="replace_reference",
                repair_description="Accept the Contract first, or name an accepted version.",
            )
        digest = contract_digest(parse_contract(content, path=path)).tagged
        result["nodes"][index] = {
            **node,
            "contract": {"name": ref.name, "version": ref.version, "digest": digest},
        }
    if not result.get("slots"):
        return result
    for name, slot in dict(result["slots"]).items():
        if not isinstance(slot, Mapping):
            continue
        pinned = dict(slot)
        for side in ("in", "out"):
            value = slot.get(side)
            ref = parse_contract_ref(value) if isinstance(value, str) else None
            if ref is None and isinstance(value, Mapping):
                ref = parse_contract_ref(f"{value.get('name')}@{value.get('version')}")
            if ref is None:
                continue
            path = contract_path(ref.name, ref.version)
            content = tree.get(path)
            if content is None:
                _refuse(
                    "cruxible.authoring.slot_contract_unresolved",
                    f"definition.slots.{name}.{side}",
                    f"Slot {name!r} names {ref.spelling}, which is not accepted.",
                    repair_kind="replace_reference",
                    repair_description="Accept the Contract first, or name an accepted version.",
                )
            digest = contract_digest(parse_contract(content, path=path)).tagged
            stated = value.get("digest") if isinstance(value, Mapping) else None
            if stated is not None and stated != digest:
                _refuse(
                    "cruxible.authoring.slot_contract_unresolved",
                    f"definition.slots.{name}.{side}",
                    f"Slot {name!r} pins {ref.spelling} at {stated}; the accepted is {digest}.",
                    repair_kind="replace_reference",
                    repair_description="Name the Contract version without a digest.",
                )
            pinned[side] = {"name": ref.name, "version": ref.version, "digest": digest}
        result["slots"][name] = pinned
    return result


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
    co_proposed: Mapping[str, bytes] | None = None,
) -> LoweredAuthoring:
    candidate_tree = fork_tree(base_tree)
    if predecessor is not None and _same_revision_content(procedure, predecessor):
        procedure = predecessor
        procedure_bytes = base_tree[path]
    else:
        procedure_bytes = render_procedure(procedure)
    candidate_tree[path] = procedure_bytes
    written = {**dict(co_proposed or {}), path: procedure_bytes}
    for member_path, content in written.items():
        candidate_tree[member_path] = content
    changed = tuple(
        (member_path, content)
        for member_path, content in sorted(written.items())
        if base_tree.get(member_path) != content
    )
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


def _pin_claim_type_value(definition: ClaimType, tree: Mapping[str, bytes]) -> ClaimType:
    if not type_refs(definition.value):
        return definition
    return definition.model_copy(
        update={
            "value": map_type_refs(
                definition.value,
                lambda ref: _pin_query_contract_ref(ref, tree=tree, label="ClaimType.value"),
            )
        }
    )


def _render_non_procedure_member(
    payload: ResolutionContractAuthoringPayload
    | AttestationAuthoringPayload
    | SubjectAuthoringPayload
    | QueryDefinitionAuthoringPayload
    | ClaimTypeAuthoringPayload
    | ApprovalPolicyAuthoringPayload
    | ProcedureRuntimePolicyAuthoringPayload
    | CaptureContractAuthoringPayload,
    *,
    tree: Mapping[str, bytes] | None = None,
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
            contract = lower_capture_contract_input(record, predecessor=previous, named=True)
        else:
            contract = payload.capture_contract
        return (
            capture_contract_path(contract.identity.name),
            render_capture_contract(contract),
            capture_contract_digest(contract).tagged,
        )
    if isinstance(payload, ClaimTypeAuthoringPayload):
        definition = (
            _pin_claim_type_value(payload.claim_type, tree)
            if tree is not None
            else payload.claim_type
        )
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
            for attr, label in (("parameter_contract", "parameter"), ("row_contract", "row")):
                ref = getattr(query, attr)
                if ref is not None:
                    query = query.model_copy(
                        update={attr: _pin_query_contract_ref(ref, tree=tree, label=label)}
                    )
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
) -> Callable[[Mapping[str, Any], Mapping[str, str]], dict[str, Any]]:
    """Resolve a Line's slot bindings through the base's signature index.

    ``(slots, requested) -> {slot: LineBinding}``: each requested
    ``provider[/build][#operation]`` names its build and operation; a slot left
    out is fitted (exactly one live operation must serve its signature).
    """

    del tree
    from cruxible_core.providers.operation_index import resolve_slot_bindings

    def resolve(slots: Mapping[str, Any], requested: Mapping[str, str]) -> dict[str, Any]:
        with instance.bind_accepted_projection(base) as projection:
            return resolve_slot_bindings(projection.typed.connection, slots, requested)

    return resolve


def _carried_line_mandates(
    instance: PlaybillInstance,
    base: AcceptedProjectionCoordinate,
    tree: Mapping[str, bytes],
    line_path: str,
    content: bytes,
) -> dict[str, bytes]:
    """Propose-only successors of the live Mandates granting the Line this replaces.

    A grant never follows a changed Line (ruling C3): each Mandate pinning the
    accepted Line version takes a successor pinning the new one, propose-only,
    in the same change. A Mandate the change already authors is left to it.
    Found by Line digest in the base's Mandate index: O(Mandates of the Line).
    """

    from cruxible_client.contracts.procedures.line_specs import line_spec_digest
    from cruxible_core.providers.automation_carry import carry_mandate, parse_carry_line

    previous = tree.get(line_path)
    if previous is None or previous == content:
        return {}
    old = parse_carry_line(previous, path=line_path)
    old_digest = line_spec_digest(old).tagged
    new_digest = line_spec_digest(parse_carry_line(content, path=line_path)).tagged
    with instance.bind_accepted_projection(base) as projection:
        rows = projection.typed.connection.execute(
            "SELECT path FROM procedure_mandates WHERE line_identity=? AND line_digest=? "
            "AND lifecycle='live' ORDER BY path",
            (old.identity.qualified, old_digest),
        ).fetchall()
        accepted = {str(row[0]): projection.typed.member_bytes(str(row[0])) for row in rows}
    carried: dict[str, bytes] = {}
    for path, accepted_bytes in accepted.items():
        if tree.get(path) != accepted_bytes:
            continue  # authored in this change; its own law judges it
        successor, _row = carry_mandate(path, accepted_bytes, {old_digest: new_digest})
        if successor is not None:
            carried[path] = successor
    return carried


def _render_line_member(
    payload: LineAuthoringPayload,
    *,
    tree: Mapping[str, bytes],
    resolve_provider: Callable[[Mapping[str, Any], Mapping[str, str]], dict[str, Any]]
    | None = None,
) -> tuple[str, bytes, str]:
    """Lower one Line decision into the exact Line its accepted closure pins.

    The Procedure is named; it must be present in the staged tree -- accepted at
    the base or authored earlier in the same set -- and lowering pins its exact
    version. Every slot (an invoked child's included) is bound to one exact
    ``{build, operation}``: as the author names it, or fitted. A Line embeds no
    trigger; a ``trigger_input`` Line declares the exact event its Source input
    accepts, read from that Source's CaptureContract.
    """

    from cruxible_client.contracts.procedures.windows import CaptureEventNameSelector
    from cruxible_core.providers.operation_index import line_capture_output_map, line_slot_map

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
    procedure_pin = ArtifactPin(
        role="procedure",
        target=procedure.identity,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    _check_line_parameters(
        payload, procedure=procedure, procedure_path_value=procedure_target, tree=tree
    )

    def child(identity: ArtifactIdentity) -> Any:
        target = procedure_path(identity.name)
        content = tree.get(target)
        return None if content is None else parse_procedure(content, path=target).definition

    try:
        aliases = line_capture_output_map(procedure.definition, child)
    except ValueError as exc:
        _refuse(
            "cruxible.authoring.line_procedure_missing",
            "procedure_name",
            str(exc),
            repair_kind="replace_procedure_name",
            repair_description="Name a Procedure whose invoked children are accepted.",
        )
    unknown = sorted(set(payload.acquisition) - aliases)
    if unknown:
        _refuse(
            "cruxible.authoring.line_acquisition_unknown_source",
            "acquisition",
            f"Line acquisition names no capture output of {payload.procedure_name!r}: {unknown}.",
            repair_kind="replace_acquisition",
            repair_description=(
                "Key acquisition rules by capture output <alias>.<field> (an invoked "
                f"child's: <invoke node path>.<alias>.<field>): {sorted(aliases)}."
            ),
        )

    try:
        slots = line_slot_map(procedure.definition, child)
    except ValueError as exc:
        _refuse(
            "cruxible.authoring.line_procedure_missing",
            "procedure_name",
            str(exc),
            repair_kind="replace_procedure_name",
            repair_description="Name a Procedure whose invoked children are accepted.",
        )
    if slots and resolve_provider is None:
        _refuse(
            "cruxible.line.compiler_unsupported",
            "bindings",
            "Line slot bindings need the accepted signature index.",
            repair_kind="upgrade_compiler",
            repair_description="Author the Line through an instance.",
        )
    bindings = {} if not slots else cast(Any, resolve_provider)(slots, dict(payload.bindings))
    caps = procedure.definition.hard_caps
    budgets = (
        caps_line_budgets(caps)
        if payload.budgets is None
        else _line_budgets_within_caps(payload.budgets, caps)
    )
    path = line_spec_path(payload.name)
    previous_content = tree.get(path)
    predecessor_digest = None
    previous = None
    if previous_content is not None:
        previous = parse_line_spec(previous_content, path=path)
        predecessor_digest = line_spec_digest(previous).tagged
    trigger_event: CaptureEventNameSelector | None = None
    if payload.trigger_input is not None:
        from cruxible_client.contracts.procedures.capture_outputs import (
            TriggerTargetRefused,
            trigger_capture_output,
        )
        from cruxible_client.contracts.provider_operations import capture_list_fields

        contract: ArtifactPin | ArtifactNameRef | None = None
        try:
            output = trigger_capture_output(procedure.definition, payload.trigger_input)
        except TriggerTargetRefused as exc:
            _refuse(
                "cruxible.authoring.line_" + exc.code,
                "trigger_input",
                str(exc),
                repair_kind="replace_trigger_input",
                repair_description=(
                    "Name one capture output as <alias>.<field> (or the alias of a call "
                    "with one capture field), or omit it."
                ),
            )
        else:
            call = next(
                node
                for node in procedure.definition.nodes
                if isinstance(node, CallNode) and node.node_id == output.node_path
            )
            slot = procedure.definition.slots.get(call.slot)
            out = None if slot is None else tree_contract_resolver(tree)(slot.out)
            if out is not None and output.field in {
                name for name, _kind, _cap in capture_list_fields(out)
            }:
                # A capture_landing Trigger lands one Capture; a list field's
                # members land together or not at all (D-R37 a7).
                _refuse(
                    "cruxible.authoring.line_trigger_target_list",
                    "trigger_input",
                    f"trigger_input {payload.trigger_input!r} names a list of Captures; a "
                    "Trigger substitutes one Capture, never a list field.",
                    repair_kind="replace_trigger_input",
                    repair_description="Name a single capture output, or omit trigger_input.",
                )
            contract = call.captures[output.field]
        assert isinstance(contract, ArtifactPin | ArtifactNameRef)
        trigger_event = CaptureEventNameSelector.model_validate(
            {"capture_contract_identity": contract.target}
        )
    line = LineSpec.model_validate(
        dict(
            identity=ArtifactIdentity(kind="Line", name=payload.name),
            occurrence_epoch=payload.occurrence_epoch,
            procedure=procedure_pin,
            parameters=payload.parameters,
            bindings=bindings,
            acquisition=payload.acquisition,
            findings=payload.findings,
            max_authority=(
                payload.max_authority or RUNG_AUTHORITY[procedure.definition.terminal_capability]
            ),
            trigger_input=payload.trigger_input,
            trigger_event=trigger_event,
            budgets=budgets,
            epsilon=payload.epsilon,
            lifecycle=ArtifactLifecycle(
                state="retired" if payload.retire else "live",
                predecessor_digest=predecessor_digest,
            ),
        )
    )
    if (
        previous is not None
        and previous_content is not None
        and _same_revision_content(line, previous)
    ):
        return path, previous_content, line_spec_digest(previous).tagged
    return path, render_line_spec(line), line_spec_digest(line).tagged


def _resolve_trigger_authoring_schedule(
    schedule: AuthoringTriggerSchedule,
    *,
    tree: Mapping[str, bytes],
) -> TriggerSchedule:
    """Read one canonical contract artifact; never consult Captures or journals.

    All authoring adapters preserve the typed schedule until this shared
    candidate-overlay boundary. Stored schedules name the contract; runtime
    verifies each exact retained event.
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
    event = CaptureEventNameSelector(
        capture_contract_identity=contract.identity,
        logical_sources=logical_sources,
    )
    if isinstance(schedule, CaptureLandingByNameSchedule):
        return CaptureLandingSchedule(event=event)
    return WindowCloseSchedule(
        window=CaptureEventNameWindow(
            event=event, duration_seconds=schedule.window.duration_seconds
        )
    )


def _resolved_trigger_schedule(content: bytes, *, path: str) -> dict[str, object]:
    """Expose the resolved event identity in prepare/preview output."""

    schedule = parse_trigger(content, path=path).schedule
    selector = schedule_capture_selector(schedule)
    return {} if selector is None else {"capture_contract": selector.model_dump(mode="json")}


def _render_trigger_member(
    payload: TriggerAuthoringPayload,
    *,
    tree: Mapping[str, bytes],
) -> tuple[str, bytes, str]:
    """Lower one Trigger decision; a Line target is named by identity, never pinned.

    A live Trigger's Line must be present in the staged tree -- accepted at the
    base or authored earlier in the same set -- and an event schedule's named
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
        return path, render_trigger(retired), trigger_digest(retired).tagged
    if payload.runbook_name is not None:
        from cruxible_core.authoring.runbook_lowering import render_runbook_trigger

        return render_runbook_trigger(payload, tree=tree)
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
            line=ArtifactNameRef(
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
            schedule, CaptureEventInput(event=line.trigger_event)
        ):
            _refuse(
                "cruxible.trigger.event_not_accepted",
                "schedule",
                f"Resolved contract {selector.capture_contract_identity.qualified} "
                f"differs from Line Source contract "
                f"{line.trigger_event.capture_contract_identity.qualified}, or sources "
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
        lifecycle=ArtifactLifecycle(
            state="live",
            predecessor_digest=None if previous is None else trigger_digest(previous).tagged,
        ),
    )
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
        else SubjectKindKeys(parts=payload.key_rules(), uniqueness=payload.uniqueness or "unique")
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
    contract: ArtifactPin | ArtifactNameRef, tree: Mapping[str, bytes]
) -> dict[str, str]:
    from cruxible_client.contracts.shapes import contract_ref_for_identity

    found = tree_contract_resolver(tree)(contract_ref_for_identity(contract.target))
    if found is None:
        return {}
    return {name: field.spelling for name, field in sorted(found.fields.items())}


def _check_line_parameters(
    payload: LineAuthoringPayload,
    *,
    procedure: ProcedureArtifact,
    procedure_path_value: str,
    tree: Mapping[str, bytes],
) -> None:
    """Refuse Line parameters the Procedure would refuse at run time.

    Every Line run hands `parameters` (over the Procedure's defaults) to the
    Procedure as its parameters, with an empty input; the executor checks both
    against the Procedure's params and input Contracts. The same check runs
    here, so a mismatch refuses when the Line is authored rather than on every
    later run.
    """

    definition = procedure.definition
    try:
        validate_line_parameters(
            AcceptedProcedureContractValidator(
                tree_contract_resolver(tree), tree_enum_resolver(tree)
            ),
            definition,
            payload.parameters,
        )
    except ProcedureContractValidationError as exc:
        field_path = exc.field_path
        contract = definition.parameter_contract
        _refuse(
            "cruxible.authoring.line_parameters_refused",
            "parameters" if not field_path else f"parameters.{field_path}",
            f"Line parameters are the parameters of Procedure {payload.procedure_name!r}, "
            f"and it refuses them: {exc}",
            repair_kind="replace_parameters",
            repair_description=(
                "Set parameters to a record the Procedure's params Contract accepts; "
                "the replacement lists its fields and types."
            ),
            replacement=None if contract is None else _contract_fields_summary(contract, tree),
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
                ("subjects", payload.subjects is not None),
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
    target_path = line_spec_path(payload.line_name)
    target_content = tree.get(target_path)
    if target_content is None:
        _refuse(
            "cruxible.authoring.procedure_mandate_line_missing",
            "line_name",
            "Mandate authoring requires the named accepted or same-ChangeSet Line.",
            repair_kind="replace_line_name",
            repair_description="Use a Line name present at the authoring coordinate.",
        )
    granted_line = parse_line_spec(target_content, path=target_path)
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
                changes=item.changes,
                binding_subject_role=item.binding_subject_role,
                value=item.value,
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
            value_parameter=payload.condition.value_parameter,
            before_parameter=payload.condition.before_parameter,
            fixed_parameters=fixed,
            required_fields=tuple(sorted(set(payload.condition.required_fields))),
            fallback=payload.condition.fallback,
        )
    try:
        mandate = ProcedureMandate(
            identity=ArtifactIdentity(kind="ProcedureMandate", name=payload.name),
            line=ArtifactPin(
                role="line",
                target=granted_line.identity,
                artifact_digest=line_spec_digest(granted_line).tagged,
            ),
            grants=payload.grants,
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
            subjects=payload.subjects,
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
    | CaptureContractAuthoringPayload,
    base_tree: Mapping[str, bytes],
    base: AcceptedProjectionCoordinate,
    instance: PlaybillInstance,
) -> LoweredAuthoring:
    path, content, digest = _render_non_procedure_member(
        payload,
        tree=base_tree,
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
    #: The kind's creation layer: 0 without Subject-valued key parts, else one
    #: above its ends' (``kind_depths``). Ends are named, and staged, first.
    layer: int = 0
    #: The Subject it lands on holds ``key``: it is new, or the key named it
    #: (not an alias of a Subject holding another key).
    holds_key: bool = True


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
        elif isinstance(member, ClaimAuthoringPayload):
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
            # Key values: an unqualified literal, or (a pair's end) a whole Subject.
            if (
                address.selector.scheme == "artifact-v1"
                and address.artifact_path.startswith("subjects/")
                and statement.qualifier is None
                and statement.predicate != "semantic.alias"
            ):
                if isinstance(statement.object, LiteralClaimObject):
                    added.append(
                        (address.artifact_path, statement.predicate, statement.object.value)
                    )
                elif isinstance(statement.object, SubjectClaimObject):
                    end = _whole_subject_end(statement.object.address)
                    if end is not None:
                        added.append((address.artifact_path, statement.predicate, end))
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
        parts: list[tuple[str, SubjectKeyPartValue]] = []
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
                    if isinstance(value, SubjectEnd):
                        values.append(value)
                        continue
                    try:
                        values.append(canonicalize_literal(value, form))
                    except CanonicalFormDidNotConverge:
                        values.append(None)
            if len(values) != 1 or (
                not isinstance(values[0], SubjectEnd) and key_value_problem(values[0]) is not None
            ):
                break
            parts.append((predicate, cast(SubjectKeyPartValue, values[0])))
        else:
            overlay.keys[subject] = tuple(parts)
            continue
        overlay.keys[subject] = None
    return overlay


def _whole_subject_end(address: SemanticAddress) -> SubjectEnd | None:
    """The whole Subject an address names, as a key part's canonical value, else None."""

    if address.selector.scheme != "artifact-v1" or address.selector.value:
        return None
    try:
        return SubjectEnd(path=address.artifact_path)
    except ValueError:
        return None


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

    Creations are named in layers (``kind_depths``): kinds with no
    Subject-valued key part first, then their pairs, then pairs of pairs. A
    pair's end -- the Subject itself, the Subject its kind's key names, or the
    Subject a creation member of this change lands on -- is landed on its path
    once the layer below is named, so the pair's key is final before it is
    resolved: two pair creations whose ends land on one Subject land on one
    pair, and a pair of existing ends that already exists is landed on.

    Ids, in the one creation order (``creation_order``: layer, kind, canonical
    key; ordinary creations before create_new members of the same key, member
    identity breaking ties): a first holder takes the readable rendering of its
    key, suffixed with the key's digest when the rendering is an existing
    Subject's id, a live alias of the kind, or an id this change already
    assigned; a further holder takes the rendering and a digest of the
    operation and member; a minted kind's Subject an id minted from the
    operation and member. Each records how (``SubjectAssignment``), so the
    candidate boundary re-derives it on every route, in the same order.
    """

    creations: dict[str, _Creation] = {}
    keyed: list[tuple[int, SubjectCreationAuthoringPayload, SubjectKind]] = []
    declared_of = _DeclaredInChange(members=members, base_tree=base_tree)
    for index, member in enumerate(members):
        if not isinstance(member, SubjectCreationAuthoringPayload):
            continue
        element = f"members[{index}]"
        kind = member.subject_kind
        declared = declared_of(kind)
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
        if member.key_value is not None and len(declared.keys) != 1:
            _refuse(
                "cruxible.subject.key_invalid",
                f"{element}.key_value",
                f"{kind} is named by {len(declared.keys)} key parts "
                f"({', '.join(declared.keys)}); a bare key value names only a one-key kind.",
                repair_kind="replace_key",
                repair_description="Give each key part by name.",
                replacement={"keys": list(declared.keys)},
            )
        keyed.append((index, member, declared))
    if not keyed:
        return creations
    claim_types: dict[str, ClaimType | None] = {}

    def claim_type_of(predicate: str) -> ClaimType | None:
        if predicate not in claim_types:
            claim_types[predicate] = _claim_type_in_change(
                predicate, members=members, base_tree=base_tree
            )
        return claim_types[predicate]

    def key_shape_of(predicate: str) -> KeyClaimTypeShape | None:
        found = claim_type_of(predicate)
        return None if found is None else claim_type_key_shape(found)

    layers = kind_depths(
        {declared.subject_kind for _index, _member, declared in keyed},
        subject_kind_of=declared_of,
        key_shape_of=key_shape_of,
    )
    by_layer: dict[int, list[tuple[int, SubjectCreationAuthoringPayload, SubjectKind]]] = {}
    for item in keyed:
        by_layer.setdefault(layers.get(item[2].subject_kind, 0), []).append(item)
    creators = {
        member.member_identity.rpartition("@")[2]: member
        for member in members
        if isinstance(member, SubjectCreationAuthoringPayload)
    }
    names: dict[str, set[str]] = {}
    with instance.bind_accepted_projection(base) as projection:
        connection = projection.typed.connection
        view = IdentityView(
            connection,
            members_identity_overlay(connection, members=members, base_tree=base_tree),
            key_reader=tree_key_reader(base_tree),
        )
        base_view = IdentityView(connection, key_reader=tree_key_reader(base_tree))
        # The keys a creation of this change holds, for an end named by key.
        created_keys: set[tuple[str, str]] = set()
        for layer in sorted(by_layer):
            ordered: list[
                tuple[
                    tuple[object, ...], int, SubjectCreationAuthoringPayload, SubjectKind, KeyTuple
                ]
            ] = []
            for index, member, declared in by_layer[layer]:
                kind = member.subject_kind
                element = f"members[{index}]"
                given: dict[str, object] = (
                    {declared.keys[0]: member.key_value}
                    if member.key_value is not None
                    else {
                        part.predicate: (
                            _key_part_end(
                                view,
                                part.value,
                                base_view=base_view,
                                element=f"{element}.key",
                                creators=creators,
                                creations=creations,
                                created_keys=created_keys,
                                declared_of=declared_of,
                                claim_type_of=claim_type_of,
                            )
                            if isinstance(part.value, SubjectClaimObject)
                            else part.value
                        )
                        for part in member.key
                    }
                )
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
                order = creation_order(layer, kind, key, member.member_identity)
                ordered.append(
                    ((*order[:3], member.create_new, order[3]), index, member, declared, key)
                )
            for _order, index, member, declared, key in sorted(ordered, key=lambda item: item[0]):
                creation = _assign_keyed_creation(
                    view,
                    operation=operation,
                    index=index,
                    member=member,
                    declared=declared,
                    key=key,
                    names=names.setdefault(member.subject_kind, set()),
                    layer=layer,
                )
                creations[member.member_identity] = creation
                if creation.holds_key:
                    # A creation that landed through an alias holds that
                    # Subject's own key, not the one it asked for.
                    created_keys.add((creation.kind, key_encoding(key)))
    return creations


def _key_part_end(
    view: IdentityView,
    value: SubjectClaimObject,
    *,
    base_view: IdentityView,
    element: str,
    creators: Mapping[str, SubjectCreationAuthoringPayload],
    creations: Mapping[str, _Creation],
    created_keys: set[tuple[str, str]],
    declared_of: Callable[[str], SubjectKind | None],
    claim_type_of: Callable[[str], ClaimType | None],
) -> SubjectEnd:
    """The end a Subject-valued key part names, landed on its path (the part's canonical value).

    The forms a Claim object lands through: the Subject itself (or the one live
    Subject its alias or single key names), the Subject a creation member of
    the same change lands on, or the Subject its kind's key names: the one
    holder of the key among the accepted Subjects and this change's creations
    (several refuse ``key_ambiguous``; a creation member reference picks one).
    An end's layer is named before its pair's, so a creation it names already
    has its path.
    """

    address = value.address
    scheme = address.selector.scheme
    path: str | None = None
    if scheme == "subject-creation-v1":
        creator = creators.get(address.selector.value)
        if creator is None or address.artifact_path != subject_kind_path(creator.subject_kind):
            _refuse(
                SUBJECT_CREATION_REFERENCE_UNRESOLVED,
                element,
                "A key part names a Subject by a creation member this change does not hold "
                f"({address.artifact_path}, {address.selector.value}).",
                repair_kind="replace_subject",
                repair_description=(
                    "Name the end by the reference its creation member returned in this change "
                    "set, or by an existing kind/id."
                ),
            )
        assigned = creations.get(creator.member_identity)
        if assigned is None:
            # Not named below this pair's layer: a creation of another kind
            # than the part's end (the key canonicalization says which).
            _refuse(
                "cruxible.subject.key_invalid",
                element,
                f"A key part names the {creator.subject_kind} this change creates, which is not "
                "an end of the pair's kind.",
                repair_kind="replace_key",
                repair_description="Name a Subject of the kind the key part takes.",
            )
        path = assigned.path
    elif scheme == "subject-key-v1":
        reference = address.subject_key_reference
        assert reference is not None
        kind, raw = reference
        declared = declared_of(kind)
        if declared is None or declared.minted:
            _refuse(
                "cruxible.subject.key_invalid",
                element,
                f"{kind} has no declared keys, so no Subject of it is named by key.",
                repair_kind="replace_subject",
                repair_description="Name the end as kind/id, or by its creation member.",
            )
        try:
            key = canonical_key(
                declared, key_reference_mapping(declared, raw), claim_type=claim_type_of
            )
        except (SubjectKeyInvalid, ValueError) as exc:
            _refuse(
                "cruxible.subject.key_invalid",
                element,
                f"{kind}: {exc}",
                repair_kind="replace_key",
                repair_description=f"Give each key of {kind}: " + ", ".join(declared.keys),
                replacement={"keys": list(declared.keys)},
            )
        # An end named by key is the one Subject the reference names, every
        # reading counted: the key's holders (accepted ones and each creation of
        # this change; creations landing on one Subject are one), and, for a bare
        # one-key text, the Subject that text names as an id or alias. Several
        # holders, or readings that disagree, refuse key_ambiguous: a whole-Subject
        # address, a structured key or a creation member reference picks one.
        text = raw if isinstance(raw, str) and len(key) == 1 else None
        now = _end_by_key(view, kind, key, text, element=element)
        if (kind, key_encoding(key)) in created_keys:
            # A creation of this change holds the key: its layer is named.
            path = now
        else:
            # Frozen at the base, as a Claim's key reference is; the change's own
            # identity changes must not move it (``identity_reference_unstable``).
            path = _end_by_key(base_view, kind, key, text, element=element)
            if path is not None and now != path:
                _refuse(
                    IDENTITY_REFERENCE_UNSTABLE,
                    element,
                    f"The {kind} key {raw!r} names {path} at the base, and "
                    f"{now or 'no Subject'} once this change's own identity changes "
                    "(key or alias Claims) apply.",
                    repair_kind="split_change_set",
                    repair_description=(
                        "Put the identity change (an alias or key Claim) and the pair that names "
                        "the end by key in separate change sets."
                    ),
                )
        if path is None:
            _refuse(
                SUBJECT_KEY_MISSING,
                element,
                f"No live {kind} Subject holds the key {raw!r}, and no creation of this "
                "change does.",
                repair_kind="create_subject",
                repair_description=(
                    "Add a subject member creating the end to the same change set: "
                    + subject_creation_shape(kind)
                ),
            )
    else:
        kind, subject_id = subject_kind_of(address.artifact_path)
        resolution = view.resolve(kind, subject_id)
        if resolution.ambiguous:
            _refuse(
                "cruxible.subject.key_ambiguous"
                if resolution.via == "key"
                else "cruxible.authoring.alias_ambiguous",
                element,
                f"{kind}/{subject_id} names several Subjects: " + ", ".join(resolution.candidates),
                repair_kind="replace_subject",
                repair_description="Name one of the listed Subjects as kind/id.",
                replacement={"candidates": list(resolution.candidates)},
            )
        if resolution.path is None:
            _refuse(
                "cruxible.authoring.object_subject_not_found",
                element,
                f"The key part names {kind}/{subject_id}, which names no Subject; a key part "
                "never creates the end it names.",
                repair_kind="create_subject",
                repair_description=(
                    "Name an accepted Subject, or create the end in the same change set and name "
                    "it by its creation member: " + subject_creation_shape(kind)
                ),
                replacement={"subject": f"{kind}/{subject_id}"},
            )
        path = resolution.path
    return SubjectEnd(path=path)


def _end_by_key(
    view: IdentityView,
    kind: str,
    key: KeyTuple,
    text: str | None,
    *,
    element: str,
) -> str | None:
    """The one Subject an end named by key names in ``view``, refusing any ambiguity.

    The key's live holders, and (for a bare one-key ``text``) the live Subject
    whose id it is, else the one an alias of the kind names. Several holders,
    several alias targets, or an id/alias reading that names another Subject
    than the key's holder refuse ``key_ambiguous`` with every candidate: a key
    reference never silently picks one reading (review F2). None when nothing
    is named.
    """

    held = view.holders(kind, key)
    named: tuple[str, ...] = ()
    if text is not None:
        exact = f"subjects/{kind}/{text}.json"
        named = (exact,) if view.live(exact) else view.alias_targets(kind, text)
    found = sorted({*held, *named})
    if len(found) > 1:
        refs = [path.removeprefix("subjects/").removesuffix(".json") for path in found]
        _refuse(
            "cruxible.subject.key_ambiguous",
            element,
            f"The {kind} key reference names {len(found)} Subjects: {', '.join(refs)}"
            + (
                f" (holders of the key, or named by {text!r} as an id or alias)"
                if named
                else " (each holds the key)"
            )
            + ".",
            repair_kind="replace_subject",
            repair_description=(
                "Name the end as kind/id, by a structured key ({part: value}), or by the "
                "creation member that creates the one you mean."
            ),
            replacement={"candidates": refs},
        )
    return found[0] if found else None


def _assign_keyed_creation(
    view: IdentityView,
    *,
    operation: str,
    index: int,
    member: SubjectCreationAuthoringPayload,
    declared: SubjectKind,
    key: KeyTuple,
    names: set[str],
    layer: int,
) -> _Creation:
    """Resolve one keyed creation and name it, laying a new Subject over ``view``."""

    kind = member.subject_kind
    element = f"members[{index}]"
    if member.create_new and declared.unique:
        _refuse(
            "cruxible.subject.create_new_refused",
            f"{element}.create_new",
            f"{kind} keys are unique: a key names one Subject, so create_new does not apply.",
            repair_kind="drop_create_new",
            repair_description=(
                "Drop create_new: the creation then resolves to the Subject holding the key."
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
        return _Creation(
            kind=kind,
            subject_id=subject_kind_of(resolution.path)[1],
            path=resolution.path,
            key=key,
            declared=declared,
            resolved=True,
            layer=layer,
            holds_key=resolution.via == "key",
        )
    assignment: SubjectAssignment | None
    if resolution.holders:
        # create_new on a hint kind: a further holder of the key, named from
        # the operation and the member so it never reuses the key's rendering.
        subject_id = distinct_holder_id(
            key_values(key), operation=operation, member=member.member_identity
        )
        assignment = SubjectAssignment(
            rule="further_holder", operation=operation, member=member.member_identity
        )
    else:
        try:
            subject_id = assign_creation_id(view, kind, key, names=names)
        except SubjectIdUnassignable as exc:
            _refuse(
                "cruxible.subject.key_invalid",
                f"{element}.key",
                str(exc),
                repair_kind="replace_key",
                repair_description="Give a key whose rendering is free.",
            )
        assignment = None
    names.add(subject_id)
    path = subject_path(kind, subject_id)
    view.add_creation(path, key)
    return _Creation(
        kind=kind,
        subject_id=subject_id,
        path=path,
        key=key,
        declared=declared,
        assignment=assignment,
        layer=layer,
    )


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


def _object_subject(value: object) -> tuple[str, SemanticAddress] | None:
    """The Subject a Claim object names, by field: its value, or the X of a none of X."""

    if isinstance(value, SubjectClaimObject):
        return "statement.object.address", value.address
    if isinstance(value, AuthoringNoneClaimObject | NoneClaimObject) and isinstance(
        value.of, SubjectClaimObject
    ):
        return "statement.object.of.address", value.of.address
    return None


def _with_object_subject(value: _ObjectT, address: SemanticAddress) -> _ObjectT:
    """``value`` naming ``address`` where it names a Subject (``_object_subject``)."""

    if isinstance(value, SubjectClaimObject):
        return cast(_ObjectT, SubjectClaimObject(address=address))
    return cast(_ObjectT, AuthoringNoneClaimObject(of=SubjectClaimObject(address=address)))


def _subject_key_addresses(
    member: AuthoringChangeSetMember,
) -> tuple[tuple[str, SemanticAddress], ...]:
    """The Subjects a Claim member names by key (``subject-key-v1``), by field."""

    if not isinstance(member, ClaimAuthoringPayload):
        return ()
    statement = member.statement
    found: list[tuple[str, SemanticAddress]] = []
    if statement.subject.selector.scheme == "subject-key-v1":
        found.append(("statement.subject", statement.subject))
    named = _object_subject(statement.object)
    if named is not None and named[1].selector.scheme == "subject-key-v1":
        found.append(named)
    return tuple(found)


def _with_keys_landed(
    payload: _ClaimPayloadT, key_paths: Mapping[tuple[str, str], str]
) -> _ClaimPayloadT:
    """The Claim with each Subject it names by key named by the path that key resolved to."""

    def landed(address: SemanticAddress) -> SemanticAddress:
        path = key_paths.get(_key_slot(address))
        return address if path is None else SemanticAddress.whole_artifact(path)

    statement = payload.statement
    subject = landed(statement.subject)
    value = statement.object
    named = _object_subject(value)
    if named is not None:
        target = landed(named[1])
        if target != named[1]:
            value = _with_object_subject(value, target)
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
        if isinstance(member, ClaimAuthoringPayload)
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
    if not isinstance(member, ClaimAuthoringPayload):
        return member
    statement = member.statement
    subject = landed(statement.subject)
    value = statement.object
    named = _object_subject(value)
    if named is not None:
        target = landed(named[1])
        if target != named[1]:
            value = _with_object_subject(value, target)
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
        if isinstance(member, ClaimAuthoringPayload):
            path = claim_path(claim_identities[authoring_member_identity(member)])
            content = staged_tree.get(path)
            if content is not None:
                written = parse_claim(content, path=path).statement
                value = member.statement.object
                landed = _object_subject(written.object)
                if landed is not None:
                    value = _with_object_subject(value, landed[1])
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
    payload: ClaimAuthoringPayload,
    creations: Mapping[str, _Creation],
    *,
    element: str,
) -> ClaimAuthoringPayload:
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
    intent: AuthoringIntent,
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
        + ", ".join(
            f"{predicate}="
            + (
                value.path.removeprefix("subjects/").removesuffix(".json")
                if isinstance(value, SubjectEnd)
                else repr(value)
            )
            for predicate, value in creation.key
        )
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
        # A pair's end is a link Claim to the end Subject; a literal part, a literal.
        statement = AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(creation.path),
            predicate=predicate,
            object=(
                SubjectClaimObject(address=SemanticAddress.whole_artifact(value.path))
                if isinstance(value, SubjectEnd)
                else LiteralClaimObject(value=value)
            ),
            role=role,
        )
        # The author's own words lower as a write's ``--because`` does, so the
        # key Claim is self-asserted evidence its ClaimType admits.
        payload = (
            ClaimAuthoringPayload(
                statement=statement,
                rationale=rationale,
                source=member.source,
                citation_role=member.citation_role,
            )
            if member.source is not None and not isinstance(member.source, SelfSourceBody)
            else ClaimAuthoringPayload(
                statement=statement,
                rationale=rationale,
                source=member.source
                or SelfSourceBody(
                    content_base64=base64.b64encode(rationale.encode("utf-8")).decode("ascii")
                ),
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
    "note",
    "procedure",
    "line",
    "procedure_mandate",
    "trigger",
    "claim_retirement",
)
# One change set's retirement closures share one overlay of the tree the first
# retirement sees (ChangeRetirementClosures): sound only while nothing but
# retirements is staged after that tree.
if MEMBER_STAGING_ORDER[-1] != "claim_retirement":
    raise AssertionError("claim_retirement must be the last member staging pass")


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
    if isinstance(member, ClaimAuthoringPayload):
        return "claim"
    if isinstance(member, NoteAuthoringPayload):
        return "note"
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
        # A version also depends on its own predecessor version when both are
        # staged in this set: lowering computes predecessor_digest from the
        # tree, so version N-1 must already be staged before version N. This
        # edge is implicit (the author's field refs never name it), and
        # canonical identity order gets it wrong whenever a version number's
        # digit count changes (".../v10" sorts before ".../v2").
        predecessor = by_key.get((member.name, member.version - 1))
        if predecessor is not None:
            deps[i].add(predecessor)
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
    if isinstance(member, ClaimAuthoringPayload):
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
    if isinstance(member, NoteAuthoringPayload):
        return note_path(member.note_name)
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
    intent: AuthoringIntent,
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
        if isinstance(member, ClaimAuthoringPayload)
    }
    owner_by_path: dict[str, int] = {}
    primary_paths: list[str] = []
    for index, member in enumerate(payload.members):
        path = _member_primary_path(member, claim_identities=claim_identities, creations=creations)
        primary_paths.append(path)
        if (
            isinstance(member, SubjectCreationAuthoringPayload)
            and creations[member.member_identity].resolved
        ):
            # A creation that lands on a Subject (accepted, or created by a
            # sibling member) writes nothing: several members may name one
            # Subject, as two pair creations with one end do.
            continue
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
    # Every retirement member's closure over one overlay (ChangeRetirementClosures).
    retirement_closures = ChangeRetirementClosures(
        instance, coordinate=AcceptedCoordinate.from_internal(base)
    )
    try:
        for stage in MEMBER_STAGING_ORDER:
            stage_indices = [
                index
                for index, member in enumerate(payload.members)
                if _member_stage(member) == stage
            ]
            if stage == "subject_creation":
                # A pair's key Claims name its ends: ends are staged first, layer
                # by layer (each kind's creation layer), as they were named.
                stage_indices.sort(
                    key=lambda index: (
                        creations[
                            cast(
                                SubjectCreationAuthoringPayload, payload.members[index]
                            ).member_identity
                        ].layer
                    )
                )
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
                            retirement_closures=retirement_closures,
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
    finally:
        retirement_closures.close()
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
    intent: AuthoringIntent,
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
    retirement_closures: ChangeRetirementClosures,
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
    if isinstance(member, ClaimAuthoringPayload):
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
    if isinstance(member, NoteAuthoringPayload):
        return _stage_note(
            instance,
            member=member,
            intent=intent,
            actor_id=actor_id,
            base=base,
            staged_tree=staged_tree,
            path=path,
        )
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
            member=member,
            staged_tree=staged_tree,
            path=path,
            derivation_procedure=derivation_procedure,
            closures=retirement_closures,
        )
        return tree, resolved, extra, {}
    if isinstance(member, ProcedureAuthoringPayload):
        lowered = _lower_procedure(
            instance,
            intent=intent.model_copy(update={"payload": member}),
            base=base,
            base_tree=staged_tree,
            candidate_paths=candidate_paths,
        )
        # The Procedure's co-proposed boundary Contracts are this member's
        # changes too, exactly as a single-Procedure lowering reports them.
        extra = {member_path for member_path, _content in lowered.changed_members} - {path}
        return fork_tree(lowered.proposed_tree), dict(lowered.resolved_authoring), extra, {}
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
            resolve_provider=_line_provider_resolver(instance, base, staged_tree),
        )
        candidate_tree = fork_tree(staged_tree)
        candidate_tree[line_path] = content
        for mandate_path, mandate in _carried_line_mandates(
            instance, base, staged_tree, line_path, content
        ).items():
            candidate_tree[mandate_path] = mandate
        return candidate_tree, {"artifact_digest": digest}, set(), {}
    if isinstance(member, TriggerAuthoringPayload):
        trigger_member_path, content, digest = _render_trigger_member(
            member,
            tree=staged_tree,
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
        query_paths=_authored_query_paths(member, instance, base, staged_tree),
    )
    candidate_tree = fork_tree(staged_tree)
    candidate_tree[path] = content
    builtin_paths: set[str] = set()
    if isinstance(member, ClaimTypeAuthoringPayload):
        from cruxible_core.claims.claim_type_inputs import install_builtin_capture_dependencies

        builtin_paths = install_builtin_capture_dependencies(member.claim_type, candidate_tree)
    return candidate_tree, {"artifact_digest": digest}, builtin_paths, {}


def _stage_note(
    instance: PlaybillInstance,
    *,
    member: NoteAuthoringPayload,
    intent: AuthoringIntent,
    actor_id: str,
    base: AcceptedProjectionCoordinate,
    staged_tree: Mapping[str, bytes],
    path: str,
) -> StagedMember:
    """Write one note: the section's workspace Capture and the link, or its retirement.

    The section is captured exactly as a section citation captures it (the
    source's foreign-source CaptureContract, installed on first use), so a
    note's prose is retained under the same contract and projection-window
    gate as a Claim's cited section. O(the section's bytes + its Subjects).
    """

    previous_bytes = staged_tree.get(path)
    previous = None if previous_bytes is None else parse_note(previous_bytes, path=path)
    predecessor = None if previous is None else note_digest(previous).tagged
    candidate_tree = fork_tree(staged_tree)
    extra: set[str] = set()
    if not member.about:
        if previous is None or previous.lifecycle.state != "live":
            _refuse(
                "cruxible.note.not_live",
                "note",
                f"Note {member.note_name} is not live here, so there is nothing to unlink.",
                repair_kind="omit_member",
                repair_description="Leave the unlink out, or link the section first.",
            )
        note = previous.model_copy(
            update={"lifecycle": ArtifactLifecycle(state="retired", predecessor_digest=predecessor)}
        )
    elif member.source is None:
        # Links only: the note keeps the section bytes it already retains.
        if previous is None or previous.lifecycle.state != "live":
            _refuse(
                "cruxible.note.not_live",
                "note",
                f"Note {member.note_name} is not live here; link its section with an "
                "observation first.",
                repair_kind="observe_section",
                repair_description="Link the section through the writer's adapter.",
            )
        note = previous
        if previous.about != member.about:
            note = previous.model_copy(
                update={
                    "about": member.about,
                    "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor),
                }
            )
    else:
        source = member.source
        assert source is not None and source.selector.section is not None
        about = member.about
        head_path = capture_contract_path(
            foreign_source_capture_contract(source.source_id).identity.name
        )
        head_bytes = staged_tree.get(head_path)
        head = None if head_bytes is None else parse_capture_contract(head_bytes, path=head_path)
        install = True
        if head is not None and head.lifecycle.state == "live":
            successor = builtin_contract_for_write(
                foreign_source_capture_contract(source.source_id), head
            )
            install = successor != head
            head = successor
        try:
            built = build_working_selection_capture(
                accepted_contract=None if head is None or head.lifecycle.state != "live" else head,
                store=instance.body_store(),
                actor_id=actor_id,
                claim_id=member.note_name,
                rationale=(
                    intent.payload.rationale
                    if isinstance(intent.payload, ChangeSetAuthoringPayload)
                    else None
                )
                or "",
                observed_at=_observed_at(intent.canonical_timestamp),
                accepted_coordinate=AcceptedCoordinate.from_internal(base),
                source_id=source.source_id,
                coordinate=source.coordinate.model_dump(mode="json"),
                selector=source.selector.model_dump(mode="json"),
                selected_content=source.selected_content,
            )
        except (CruxibleError, ValueError) as exc:
            _refuse(
                "cruxible.authoring.working_selection_refused",
                "source.selector",
                f"The note's section cannot be captured: {exc}",
                repair_kind="revise_selection",
                repair_description="Link a section of admitted prose outside projection blocks.",
            )
        _refuse_citation_into_projection_window(instance, payload=member, envelope=built.envelope)
        note = Note(
            identity=note_identity(source.source_id, source.selector.section.path),
            source_id=source.source_id,
            section=source.selector.section.path,
            capture_digest=built.capture_digest,
            section_digest=source.selected_bytes_digest,
            about=about,
            lifecycle=ArtifactLifecycle(predecessor_digest=predecessor),
        )
        if (
            previous is not None
            and previous.lifecycle.state == "live"
            and (previous.section_digest, previous.about) == (note.section_digest, note.about)
        ):
            # The same section bytes linked to the same Subjects: the live note
            # already says this (a new Capture differs only by observation time).
            note = previous
        elif install:
            contract_path = capture_contract_path(built.contract.identity.name)
            candidate_tree[contract_path] = render_capture_contract(built.contract)
            extra.add(contract_path)
    candidate_tree[path] = render_note(note)
    return candidate_tree, {"artifact_digest": note_digest(note).tagged}, extra, {}


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
            assert isinstance(sibling, ClaimAuthoringPayload)
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
    intent: AuthoringIntent,
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
            staged_tree, root=successor.identity, by_name=True
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
    if claim_value_kind(successor.value) != claim_value_kind(predecessor.value):
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
                        "predecessor_object_kind": claim_value_kind(predecessor.value),
                        "successor_object_kind": claim_value_kind(successor.value),
                    },
                )
    # Two trees, deliberately. A re-authoring sibling is lowered against the
    # SUCCESSOR vocabulary, or its own object-kind and schema laws would be
    # judged by the ClaimType this set is replacing. The builder, meanwhile,
    # takes the tree with the PREDECESSOR still installed: what it migrates
    # every dependent away from is exactly the digest it reads there.
    lowering_tree = fork_tree(staged_tree)
    successor = _pin_claim_type_value(successor, lowering_tree)
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
        assert isinstance(sibling, ClaimAuthoringPayload)
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
    *,
    member: ClaimRetirementMember,
    staged_tree: Mapping[str, bytes],
    path: str,
    derivation_procedure: ArtifactPin | None = None,
    closures: ChangeRetirementClosures,
) -> tuple[MutableMapping[str, bytes], dict[str, object], set[str]]:
    """Retire one Claim and its live closure inside the same change set.

    Its closure is the one ``claim_retirement_inventory`` computes over
    ``staged_tree``, answered by the change set's shared ``closures``.
    """

    for witness in (member.absence, *(dependent.absence for dependent in member.dependents)):
        if witness is not None and (
            derivation_procedure is None
            or witness.procedure_digest != derivation_procedure.artifact_digest
        ):
            _refuse(
                "cruxible.authoring.absence_requires_execution",
                "absence",
                "Only the executing Claims terminal can bind an absence witness.",
                repair_kind="run_procedure",
                repair_description="Run the declared Procedure with a complete enumeration.",
            )
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
        absence=member.absence,
        effective_until=member.effective_until,
    )
    try:
        inventory = closures.inventory(tree=staged_tree, claim=claim)
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
    closures.record(before=staged_tree, after=shared_candidate, results=retirements)
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
    intent: AuthoringIntent,
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
    instance: PlaybillInstance, *, intent: AuthoringIntent, lowered: LoweredAuthoring
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
    for issue in candidate_identity_issues(
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
                else "use_existing_subject"
            ),
            repair_description=(
                undeclared_kind_repair(kind)
                if issue.code == SUBJECT_KIND_UNDECLARED
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
    intent: AuthoringIntent,
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
    if isinstance(intent.payload, RunbookAuthoringPayload):
        from cruxible_core.authoring.runbook_lowering import lower_runbook

        return lower_runbook(instance, payload=intent.payload, base=base, base_tree=base_tree)
    if isinstance(intent.payload, ClaimAuthoringPayload):
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
                resolve_provider=_line_provider_resolver(instance, base, base_tree),
            )
            if isinstance(intent.payload, LineAuthoringPayload)
            else _render_trigger_member(
                intent.payload,
                tree=base_tree,
            )
            if isinstance(intent.payload, TriggerAuthoringPayload)
            else _render_subject_kind_member(intent.payload, tree=base_tree)
        )
        candidate_tree = fork_tree(base_tree)
        candidate_tree[path] = content
        changed = () if base_tree.get(path) == content else ((path, content),)
        if isinstance(intent.payload, LineAuthoringPayload):
            carried = _carried_line_mandates(instance, base, base_tree, path, content)
            for mandate_path, mandate in carried.items():
                candidate_tree[mandate_path] = mandate
            changed = (*changed, *sorted(carried.items()))
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
