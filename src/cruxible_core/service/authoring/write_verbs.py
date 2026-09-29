"""The write verbs: ``set``, ``retire`` and ``write``, lowered onto one change set.

Every write is resolved against the current accepted head, checked against the
accepted vocabulary before anything is lowered (R04), and lowered to an ordinary
authoring change set. That change set goes through the existing coordinator --
create, preflight, submit -- and, when the approval policy and the caller's tier
allow it, activation. There is no second write path: a ``set`` is exactly the
Claim member an author would have written by hand, with the Claim it revises,
the dispositions the slot law demands and the Subjects it needs filled in.

What the verbs decide for the caller:

- a field resolves by the shared naming rule (``field_names``), so the names
  ``orient`` advertises are the names a write takes;
- ``set`` on a single-value field revises the live Claim, and refuses
  ``playbill.write.slot_changed`` when the slot moved since the caller's read
  coordinate (decision a);
- the default evidence is the writer's ``because`` as self evidence (decision
  b); for an exact-content field the value is its own evidence, byte for byte;
- a Subject of a known kind that does not exist yet is added to the same change
  set (decision c); a Subject-valued value never is;
- ``accept="if_allowed"`` activates in the same call when the approval policy
  and the caller's tier allow it (decision d); otherwise the outcome names who
  may approve and the exact call on the caller's surface;
- ``dry_run`` takes the same path up to the commit and writes nothing (R12).
"""

from __future__ import annotations

import base64
import json
import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from cruxible_client.contracts import PlaybillAcceptedCoordinate as ClientCoordinate
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringChangeSetMemberV1,
    AuthoringClaimStatementV1,
    AuthoringExactContentObjectV1,
    AuthoringExistingClaimDispositionV1,
    AuthoringIntentV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ClaimRetirementMemberV1,
    ExistingCaptureCitationSourceV1,
    PreflightResultV1,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
    authoring_member_identity,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    foreign_source_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import (
    ClaimRetireDependentV1,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
    literal_satisfies_schema,
    new_claim_id,
    parse_claim,
)
from cruxible_client.contracts.errors import (
    CanonicalEncodingError,
    PlaybillError,
    ReadRefusalError,
    WriteRefusalError,
)
from cruxible_client.contracts.get_reads import (
    PlaybillGetCoordinateV1,
    PlaybillReadSurface,
    summary_value,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_path
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.write import (
    AddChange,
    ApprovalNeeded,
    ApprovalReason,
    CaptureEvidence,
    ChangeOutcome,
    FileEvidence,
    PlaybillWriteRequestV1,
    RetireChange,
    SelfEvidence,
    SetChange,
    SlotRef,
    WriteOutcome,
    WriteProposalRef,
    WriteRefusal,
    WriteStatus,
    WriteWarning,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import ComputedPreflight
from cruxible_core.claims.claim_retirement import ClaimRetireError, claim_retirement_inventory
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.exact_content import ExactContentReader
from cruxible_core.service.discovery.field_names import resolve_field_in
from cruxible_core.service.discovery.query_values import read_live_values, subjects_of_kind
from cruxible_core.service.discovery.query_vocabulary import (
    PredicateInfo,
    QueryVocabulary,
    load_query_vocabulary,
)
from cruxible_core.service.read_refusals import nearest, resolve_read_coordinate
from cruxible_core.storage.cas import BodyAccessContext

_AUTHORABLE_ROLES = ("normative", "observation", "environment_binding")
_MAX_CANDIDATES = 8


@dataclass(frozen=True)
class WriteCaller:
    """Who is writing, and whether their tier admits activating what they propose."""

    actor: AuthenticatedActor
    may_activate: bool


def _refuse(
    code: str,
    message: str,
    *,
    change: int | None = None,
    candidates: Sequence[str] = (),
    repair: str | None = None,
    field_path: str | None = None,
) -> WriteRefusalError:
    return WriteRefusalError(
        code,
        message,
        change=change,
        candidates=tuple(candidates)[:_MAX_CANDIDATES],
        repair_line=repair,
        field_path=field_path,
    )


def _bare(claim: str) -> str:
    return claim.removeprefix("Claim:")


def _compact(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> PlaybillGetCoordinateV1:
    public = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=public) as history:
        generation = int(history.sequence)
    return PlaybillGetCoordinateV1(git_oid=public.git_oid[:12], generation=generation)


def _full(coordinate: AcceptedProjectionCoordinate | AcceptedCoordinate) -> ClientCoordinate:
    public = (
        coordinate
        if isinstance(coordinate, AcceptedCoordinate)
        else AcceptedCoordinate.from_internal(coordinate)
    )
    return ClientCoordinate.model_validate(public.model_dump(mode="json"))


# -- rendering calls on the caller's surface (R07) -------------------------------


def _render_proposal_call(
    surface: PlaybillReadSurface,
    verb: Literal["approve", "activate"],
    proposal_id: str,
    *,
    signer: str | None = None,
) -> str:
    if surface == "cli":
        if verb == "approve":
            who = signer or "<approver>"
            return (
                f"cruxible playbill proposal approve {proposal_id} --signer-id {who} "
                f"--key <{who}.ed25519>"
            )
        return f"cruxible playbill proposal activate {proposal_id}"
    if surface == "sdk":
        handle = f"pb.proposal({json.dumps(proposal_id)})"
        if verb == "approve":
            who = signer or "approver"
            return f"{handle}.approve(signer=<{who} signer>, reviewed={handle}.review())"
        return f"{handle}.accept()"
    if verb == "approve":
        arguments = f"proposal_id={json.dumps(proposal_id)}"
        if signer is not None:
            arguments += f", signer_id={json.dumps(signer)}"
        return f"cruxible_playbill_approve({arguments})"
    return f"cruxible_playbill_activate(proposal_id={json.dumps(proposal_id)})"


def _render_get(surface: PlaybillReadSurface, ref: str) -> str:
    if surface == "cli":
        return f"cruxible playbill get {shlex.quote(ref)}"
    if surface == "sdk":
        return f"pb.get({json.dumps(ref)})"
    return f"cruxible_playbill_get(ref={json.dumps(ref)})"


def _render_commit(surface: PlaybillReadSurface, git_oid: str) -> str:
    if surface == "cli":
        return f"Run the same command without --dry-run, adding --at {git_oid}"
    if surface == "sdk":
        return f"Call it again with dry_run=False, at={json.dumps(git_oid)}"
    return f'Call it again with dry_run=false and at="{git_oid}"'


# -- planning ------------------------------------------------------------------------


@dataclass(frozen=True)
class _SlotClaim:
    claim_id: str
    digest: str
    value: object


@dataclass
class _Planned:
    """One change resolved against accepted state, before its member is built."""

    index: int
    op: Literal["set", "add", "retire"]
    outcome: dict[str, Any]
    member: AuthoringChangeSetMemberV1 | None = None
    slot: tuple[str, str] | None = None
    revises: str | None = None
    # Live Claims of the slot at the head that the new Claim must disposition.
    existing: tuple[str, ...] = ()
    retires: str | None = None
    change: SetChange | AddChange | None = None
    claim_type: ClaimType | None = None
    # The CaptureContract the evidence was captured under, by name, when known
    # before submit; a cited Capture's contract is read from its envelope.
    used_contract: str | None = None


@dataclass
class _Plan:
    changes: list[_Planned] = field(default_factory=list)
    subjects: dict[str, SubjectShell] = field(default_factory=dict)
    retire_notes: list[str] = field(default_factory=list)


class _Planner:
    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        head: AcceptedProjectionCoordinate,
        read_at: AcceptedProjectionCoordinate,
        request: PlaybillWriteRequestV1,
    ) -> None:
        self.instance = instance
        self.head = head
        self.read_at = read_at
        self.request = request
        self.vocabulary: QueryVocabulary = load_query_vocabulary(instance, head)
        self.exact = ExactContentReader(instance)
        self.plan = _Plan()
        self._subject_exists: dict[str, bool] = {}
        self._read_sequence: int | None = None

    # -- accepted state ----------------------------------------------------------

    def subject_exists(self, path: str) -> bool:
        if path not in self._subject_exists:
            self._subject_exists[path] = self.instance.blob_at(self.head.git_oid, path) is not None
        return self._subject_exists[path]

    def slot_claims(self, subject_path_value: str, predicate: str) -> tuple[_SlotClaim, ...]:
        """The live unqualified Claims of one slot at the head, with their shown values."""

        with self.instance.bind_accepted_projection(self.head) as projection:
            rows = projection.typed.connection.execute(
                "SELECT identity, artifact_digest FROM claims WHERE subject_path=? "
                "AND predicate=? AND qualifier IS NULL AND lifecycle='live' ORDER BY identity",
                (subject_path_value, predicate),
            ).fetchall()
        wanted = {str(row[0]): str(row[1]) for row in rows}
        if not wanted:
            return ()
        values = {
            item.identity: item
            for item in read_live_values(
                self.instance,
                self.head,
                subject_paths=(subject_path_value,),
                predicates=(predicate,),
            )
        }
        found: list[_SlotClaim] = []
        for identity, digest in sorted(wanted.items()):
            live = values.get(identity)
            shown: object = None
            if live is not None:
                shown = self.exact.value(str(live.value), live.span) if live.exact else live.value
            found.append(_SlotClaim(claim_id=_bare(identity), digest=digest, value=shown))
        return tuple(found)

    def slot_history(self, subject_path_value: str, predicate: str) -> tuple[str, ...]:
        """Every Claim (any lifecycle) the head holds in one slot."""

        with self.instance.bind_accepted_projection(self.head) as projection:
            rows = projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE subject_path=? AND predicate=? "
                "AND qualifier IS NULL ORDER BY identity",
                (subject_path_value, predicate),
            ).fetchall()
        return tuple(_bare(str(row[0])) for row in rows)

    def read_sequence(self) -> int:
        if self._read_sequence is None:
            with self.instance.accepted_history_reader(
                at=AcceptedCoordinate.from_internal(self.read_at)
            ) as history:
                self._read_sequence = int(history.sequence)
        return self._read_sequence

    def check_slot_unchanged(
        self,
        *,
        index: int,
        subject: str,
        field_name: str,
        subject_path_value: str,
        predicate: str,
        live: tuple[_SlotClaim, ...],
    ) -> None:
        """Refuse when the slot moved after the writer's read coordinate (decision a, Q05)."""

        if self.read_at.git_oid == self.head.git_oid:
            return
        since = self.read_sequence()
        moved: list[tuple[str, int, str | None]] = []
        with self.instance.accepted_history_reader(
            at=AcceptedCoordinate.from_internal(self.head)
        ) as history:
            for claim_id in self.slot_history(subject_path_value, predicate):
                location = history.latest_member(claim_path(claim_id))
                if location is None or location.sequence <= since:
                    continue
                generation = history.generation(location.sequence)
                moved.append((claim_id, int(location.sequence), generation.actor_id))
        if not moved:
            return
        live_ids = {item.claim_id: item for item in live}
        shown = [item for item in moved if item[0] in live_ids] or moved
        claim_id, generation_number, actor = max(shown, key=lambda item: item[1])
        value = live_ids[claim_id].value if claim_id in live_ids else None
        state = f"now holds {value!r} as {claim_id}" if claim_id in live_ids else "was retired"
        raise _refuse(
            "playbill.write.slot_changed",
            (
                f"{subject} {field_name} changed after your read: it {state}, "
                f"accepted in generation {generation_number} by {actor or 'unknown'}"
            ),
            change=index,
            candidates=tuple(item.claim_id for item in live),
            repair="Re-set it to replace it, or set it with contend: true to contest it",
        )

    # -- vocabulary --------------------------------------------------------------

    def resolve_subject(self, subject: str, *, index: int, path: str) -> tuple[str, str, str]:
        kind, _, subject_id = subject.partition("/")
        if kind not in self.vocabulary.kinds:
            raise _refuse(
                "playbill.write.unknown_kind",
                f"no accepted Subject kind is named {kind!r}",
                change=index,
                candidates=nearest(kind, self.vocabulary.kinds),
                repair="Use one of the listed kinds; orient names every kind",
                field_path=path,
            )
        return kind, subject_id, subject_path(kind, subject_id)

    def resolve_field(self, kind: str, name: str, *, index: int, path: str) -> PredicateInfo:
        applicable = {kind: {info.predicate: info for info in self.vocabulary.predicates_of(kind)}}
        found = resolve_field_in(name, applicable)
        if len(found) == 1:
            return self.vocabulary.predicates[found[0]]
        shown = [self.vocabulary.field_name(info, (kind,)) for info in applicable[kind].values()]
        if found:
            raise _refuse(
                "playbill.write.ambiguous_field",
                f"{name!r} names more than one field of {kind}",
                change=index,
                candidates=found,
                repair="Name the predicate in full",
                field_path=path,
            )
        message = (
            f"predicate {name!r} does not apply to {kind}"
            if name in self.vocabulary.predicates
            else f"{kind} has no field {name!r}"
        )
        raise _refuse(
            "playbill.write.unknown_field",
            message,
            change=index,
            candidates=nearest(name, (*shown, *applicable[kind])),
            repair=f"Use a field of {kind}; orient kind={kind} lists them",
            field_path=path,
        )

    def field_name(self, info: PredicateInfo, kind: str) -> str:
        return self.vocabulary.field_name(info, (kind,))

    def role(
        self,
        claim_type: ClaimType,
        requested: str | None,
        *,
        index: int,
        field_name: str,
    ) -> ClaimRole:
        permitted = tuple(role for role in claim_type.permitted_roles if role in _AUTHORABLE_ROLES)
        if requested is not None:
            if requested not in permitted:
                raise _refuse(
                    "playbill.write.role_not_permitted",
                    f"{field_name} does not permit role {requested!r}",
                    change=index,
                    candidates=permitted,
                    repair=f"Use one of: {', '.join(permitted)}",
                    field_path=f"changes[{index}].role",
                )
            return cast(ClaimRole, requested)
        if len(permitted) == 1:
            return permitted[0]
        raise _refuse(
            "playbill.write.role_required",
            f"{field_name} permits {len(permitted)} roles, so the role is not implied",
            change=index,
            candidates=permitted,
            repair=f"Pass role as one of: {', '.join(permitted)}",
            field_path=f"changes[{index}].role",
        )

    # -- values ------------------------------------------------------------------

    def literal(
        self, info: PredicateInfo, value: object, *, index: int, field_name: str
    ) -> LiteralClaimObject:
        path = f"changes[{index}].value"
        if isinstance(value, float):
            if not value.is_integer():
                raise _refuse(
                    "playbill.write.value_type_mismatch",
                    f"{value!r} is a fractional number, which accepted values cannot hold",
                    change=index,
                    repair="Pass an integer, or a string when the field takes one",
                    field_path=path,
                )
            value = int(value)
        if info.members:
            if value not in info.members:
                spelled = ", ".join(info.members)
                raise _refuse(
                    "playbill.write.value_not_member",
                    f"{value!r} is not a member of {field_name}; its members are: {spelled}",
                    change=index,
                    candidates=nearest(str(value), info.members) or info.members,
                    repair=f"Use one of: {spelled}",
                    field_path=path,
                )
        schema = info.claim_type.literal_schema
        if schema is None or not literal_satisfies_schema(value, schema):
            expected = info.value_type if info.value_type != "json" else "value"
            raise _refuse(
                "playbill.write.value_type_mismatch",
                f"{value!r} is not a valid {expected} for {field_name}",
                change=index,
                repair=f"Pass a {expected} that the field's schema admits",
                field_path=path,
            )
        try:
            return LiteralClaimObject(value=value)
        except (CanonicalEncodingError, ValueError) as exc:
            raise _refuse(
                "playbill.write.value_type_mismatch",
                f"{value!r} cannot be stored: {exc}",
                change=index,
                field_path=path,
            ) from exc

    def subject_value(
        self,
        info: PredicateInfo,
        value: object,
        *,
        index: int,
        field_name: str,
        added: Mapping[str, SubjectShell],
    ) -> tuple[SubjectClaimObject, str]:
        path = f"changes[{index}].value"
        allowed = tuple(info.claim_type.allowed_object_subject_kinds)
        kind, _, subject_id = str(value).partition("/") if isinstance(value, str) else ("", "", "")
        if not kind or not subject_id:
            raise _refuse(
                "playbill.write.value_type_mismatch",
                f"{field_name} takes a Subject as kind/id, not {value!r}",
                change=index,
                repair=f"Pass a Subject of kind {' or '.join(allowed)} as kind/id",
                field_path=path,
            )
        if kind not in allowed:
            raise _refuse(
                "playbill.write.value_kind_not_admitted",
                f"{field_name} takes a Subject of kind {', '.join(allowed)}, not {kind!r}",
                change=index,
                candidates=allowed,
                repair=f"Pass a Subject of kind {' or '.join(allowed)}",
                field_path=path,
            )
        try:
            target = subject_path(kind, subject_id)
        except (PlaybillError, ValueError) as exc:
            raise _refuse(
                "playbill.write.value_type_mismatch",
                f"{value!r} is not a Subject reference: {exc}",
                change=index,
                field_path=path,
            ) from exc
        if not self.subject_exists(target) and target not in added:
            with self.instance.bind_accepted_projection(self.head) as projection:
                known = subjects_of_kind(projection.typed.connection, kind).values()
            raise _refuse(
                "playbill.write.value_subject_not_found",
                f"{field_name} names Subject {value!r}, which does not exist; a value "
                "never creates the Subject it names",
                change=index,
                candidates=nearest(str(value), known),
                repair=f"Name an existing {kind}, or set a field of {value} first to add it",
                field_path=path,
            )
        return SubjectClaimObject(address=SemanticAddress.whole_artifact(target)), str(value)

    # -- evidence ----------------------------------------------------------------

    def source(
        self,
        info: PredicateInfo,
        change: SetChange | AddChange,
        *,
        role: str,
        index: int,
        field_name: str,
        exact: bytes | None,
    ) -> tuple[Any, Literal["evidence", "copy"] | None]:
        """The Claim's source and citation role for this change's evidence."""

        evidence = change.evidence
        path = f"changes[{index}].evidence"
        if isinstance(evidence, CaptureEvidence):
            return ExistingCaptureCitationSourceV1(capture_digest=evidence.capture), "evidence"
        if isinstance(evidence, FileEvidence):
            if evidence.observation is None:
                raise _refuse(
                    "playbill.write.file_evidence_unobserved",
                    f"file evidence {evidence.file!r} was not read on the writer's side; "
                    "the daemon never reads workspace files",
                    change=index,
                    repair=(
                        "Pass it through the CLI (--evidence-file), the SDK or the MCP "
                        "adapter, which read the file and send what they observed"
                    ),
                    field_path=path,
                )
            return evidence.observation, "evidence"
        if exact is not None:
            if isinstance(evidence, SelfEvidence) and evidence.self.encode("utf-8") != exact:
                raise _refuse(
                    "playbill.write.exact_content_evidence_mismatch",
                    f"{field_name} is exact content, so its self evidence is the value itself; "
                    "the evidence given differs from the value",
                    change=index,
                    repair="Leave evidence out: the value's own text is its evidence",
                    field_path=path,
                )
            body = exact
        else:
            text = evidence.self if isinstance(evidence, SelfEvidence) else self.request.because
            body = text.encode("utf-8")
        return SelfSourceBodyV1(content_base64=base64.b64encode(body).decode("ascii")), None

    # -- changes -----------------------------------------------------------------

    def claim_change(self, index: int, change: SetChange | AddChange) -> _Planned:
        prefix = f"changes[{index}]"
        kind, subject_id, path = self.resolve_subject(
            change.subject, index=index, path=f"{prefix}.subject"
        )
        info = self.resolve_field(kind, change.field, index=index, path=f"{prefix}.field")
        claim_type = info.claim_type
        name = self.field_name(info, kind)
        label = f"{change.subject} {name}"
        if isinstance(change, SetChange) and claim_type.cardinality == "many":
            raise _refuse(
                "playbill.write.field_is_many",
                f"{name} holds many values, so set has nothing to replace",
                change=index,
                repair="Use add to add a value, or retire one first",
                field_path=f"{prefix}.op",
            )
        if isinstance(change, AddChange) and claim_type.cardinality == "one":
            raise _refuse(
                "playbill.write.field_is_single",
                f"{name} holds one value, so add would contend with it",
                change=index,
                repair="Use set to replace the value (contend: true to contest it)",
                field_path=f"{prefix}.op",
            )
        role = self.role(claim_type, change.role, index=index, field_name=name)
        exact: bytes | None = None
        shown_after: object = change.value
        statement_object: Any
        if claim_type.object_kind == "exact_content":
            if not isinstance(change.value, str) or not change.value:
                raise _refuse(
                    "playbill.write.value_type_mismatch",
                    f"{name} is exact content, so its value is text",
                    change=index,
                    repair="Pass the text itself as the value",
                    field_path=f"{prefix}.value",
                )
            exact = change.value.encode("utf-8")
            statement_object = AuthoringExactContentObjectV1(
                content_base64=base64.b64encode(exact).decode("ascii")
            )
        elif claim_type.object_kind == "subject":
            statement_object, shown_after = self.subject_value(
                info,
                change.value,
                index=index,
                field_name=name,
                added=self.plan.subjects,
            )
        else:
            statement_object = self.literal(info, change.value, index=index, field_name=name)
            shown_after = statement_object.value
        source, citation_role = self.source(
            info, change, role=role, index=index, field_name=name, exact=exact
        )
        if not self.subject_exists(path) and path not in self.plan.subjects:
            if kind not in claim_type.allowed_subject_kinds:  # pragma: no cover - resolve_field
                raise _refuse(
                    "playbill.write.unknown_kind",
                    f"{name} does not apply to Subjects of kind {kind!r}",
                    change=index,
                )
            self.plan.subjects[path] = SubjectShell(
                identity=ArtifactIdentity(kind="Subject", name=f"{kind}/{subject_id}"),
                subject_kind=kind,
                subject_id=subject_id,
            )
        live = self.slot_claims(path, info.predicate) if self.subject_exists(path) else ()
        revises: str | None = None
        before: object = None
        contenders: tuple[str, ...] = ()
        if isinstance(change, SetChange):
            if change.contend:
                contenders = tuple(item.claim_id for item in live)
            else:
                self.check_slot_unchanged(
                    index=index,
                    subject=change.subject,
                    field_name=name,
                    subject_path_value=path,
                    predicate=info.predicate,
                    live=live,
                )
                remaining = [item for item in live if item.claim_id not in self._retiring()]
                if len(remaining) > 1:
                    raise _refuse(
                        "playbill.write.slot_contested",
                        f"{label} holds {len(remaining)} competing values, so set cannot "
                        "tell which to replace",
                        change=index,
                        candidates=tuple(item.claim_id for item in remaining),
                        repair=(
                            "Retire all but one of them in the same write, or set with "
                            "contend: true to add one more"
                        ),
                    )
                if remaining:
                    revises = remaining[0].claim_id
                    before = remaining[0].value
        else:
            for item in live:
                if item.value == shown_after:
                    raise _refuse(
                        "playbill.write.value_already_present",
                        f"{label} already holds {shown_after!r} as {item.claim_id}",
                        change=index,
                        candidates=(item.claim_id,),
                        repair="Leave it out; the value is already there",
                    )
        statement = AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact(path),
            predicate=info.predicate,
            qualifier=None,
            object=statement_object,
            role=role,
        )
        values: dict[str, Any] = {
            "statement": statement,
            "rationale": self.request.because,
            "source": source,
            "citation_role": citation_role,
            "revises": revises,
            "dependency_drafts": ClaimDependencyDraftsV1(),
        }
        member: ClaimAuthoringPayloadV1 = (
            ClaimAuthoringPayloadV3(**values)
            if isinstance(source, ExistingCaptureCitationSourceV1)
            else ClaimAuthoringPayloadV2(**values)
        )
        used_contract = (
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name
            if isinstance(source, SelfSourceBodyV1)
            else foreign_source_capture_contract(source.source_id).identity.name
            if isinstance(change.evidence, FileEvidence)
            else None
        )
        return _Planned(
            change=change,
            claim_type=claim_type,
            used_contract=used_contract,
            index=index,
            op=change.op,
            outcome={
                "subject": change.subject,
                "field": name,
                "predicate": info.predicate,
                "before": summary_value(before),
                "after": summary_value(shown_after),
                "revises": revises,
                "contenders_created": contenders,
            },
            member=member,
            slot=(path, info.predicate),
            revises=revises,
            existing=tuple(item.claim_id for item in live if item.claim_id != revises),
        )

    def _retiring(self) -> set[str]:
        return {item.retires for item in self.plan.changes if item.retires is not None}

    def retire_target(
        self, index: int, change: RetireChange
    ) -> tuple[str, str | None, str | None, str | None, object]:
        """The live Claim a retire names: (claim, subject, field, predicate, value)."""

        prefix = f"changes[{index}].target"
        target = change.target
        if isinstance(target, SlotRef):
            kind, _subject_id, path = self.resolve_subject(
                target.subject, index=index, path=f"{prefix}.subject"
            )
            info = self.resolve_field(kind, target.field, index=index, path=f"{prefix}.field")
            name = self.field_name(info, kind)
            live = self.slot_claims(path, info.predicate) if self.subject_exists(path) else ()
            if not live:
                raise _refuse(
                    "playbill.write.slot_empty",
                    f"{target.subject} {name} holds no live value to retire",
                    change=index,
                    repair=f"Read it first: {_render_get(self.request.surface, target.subject)}",
                    field_path=prefix,
                )
            if len(live) > 1:
                raise _refuse(
                    "playbill.write.slot_ambiguous",
                    f"{target.subject} {name} holds {len(live)} values; name the one to retire",
                    change=index,
                    candidates=tuple(item.claim_id for item in live),
                    repair="Retire one of the listed Claims by ID",
                    field_path=prefix,
                )
            self.check_slot_unchanged(
                index=index,
                subject=target.subject,
                field_name=name,
                subject_path_value=path,
                predicate=info.predicate,
                live=live,
            )
            return live[0].claim_id, target.subject, name, info.predicate, live[0].value
        claim_id = _bare(target)
        content = self.instance.blob_at(self.head.git_oid, claim_path(claim_id))
        if content is None:
            raise _refuse(
                "playbill.write.claim_not_found",
                f"no accepted Claim {claim_id}",
                change=index,
                repair="Name a live Claim; get on the Subject lists its Claim IDs",
                field_path=prefix,
            )
        claim = parse_claim(content, path=claim_path(claim_id))
        if claim.lifecycle.state != "live":
            raise _refuse(
                "playbill.write.claim_not_live",
                f"{claim_id} is already {claim.lifecycle.state}",
                change=index,
                repair="Leave it out; there is nothing left to retire",
                field_path=prefix,
            )
        subject = claim.statement.subject.artifact_path.removeprefix("subjects/").removesuffix(
            ".json"
        )
        kind = subject.split("/", 1)[0]
        retired_info = self.vocabulary.predicates.get(claim.statement.predicate)
        name = (
            claim.statement.predicate
            if retired_info is None
            else self.field_name(retired_info, kind)
        )
        if self.read_at.git_oid != self.head.git_oid:
            with self.instance.accepted_history_reader(
                at=AcceptedCoordinate.from_internal(self.head)
            ) as history:
                location = history.latest_member(claim_path(claim_id))
                if location is not None and location.sequence > self.read_sequence():
                    actor = history.generation(location.sequence).actor_id
                    raise _refuse(
                        "playbill.write.slot_changed",
                        f"{claim_id} changed after your read: revised in generation "
                        f"{location.sequence} by {actor or 'unknown'}",
                        change=index,
                        candidates=(claim_id,),
                        repair="Read it again, then retire it at the new coordinate",
                    )
        value: object = None
        if claim.statement.qualifier is None and retired_info is not None:
            for item in self.slot_claims(
                claim.statement.subject.artifact_path, retired_info.predicate
            ):
                if item.claim_id == claim_id:
                    value = item.value
        return claim_id, subject, name, claim.statement.predicate, value

    def retire_change(self, index: int, change: RetireChange) -> _Planned:
        claim_id, subject, name, predicate, value = self.retire_target(index, change)
        content = self.instance.blob_at(self.head.git_oid, claim_path(claim_id))
        assert content is not None
        claim = parse_claim(content, path=claim_path(claim_id))
        tree = self.instance.immutable_tree_at(self.head.git_oid)
        try:
            inventory = claim_retirement_inventory(
                self.instance,
                tree=tree,
                coordinate=AcceptedCoordinate.from_internal(self.head),
                claim=claim,
            )
        except ClaimRetireError as exc:
            raise _refuse(
                "playbill.write.retirement_closure_unsupported",
                f"{claim_id} cannot be retired here: {exc}",
                change=index,
                repair="Retire what depends on it through its own governed change first",
            ) from exc
        dependents = tuple(
            sorted(
                (
                    ClaimRetireDependentV1(
                        artifact_identity=item.artifact_identity,
                        predecessor_digest=item.predecessor_digest,
                        reason=change.reason,
                    )
                    for item in inventory
                ),
                key=lambda item: item.artifact_identity.qualified.encode("utf-8"),
            )
        )
        if change.because is not None:
            self.plan.retire_notes.append(f"Retire {claim_id}: {change.because}")
        return _Planned(
            index=index,
            op="retire",
            outcome={
                "subject": subject,
                "field": name,
                "predicate": predicate,
                "before": summary_value(value),
                "after": None,
                "claim": claim_id,
                "retired": tuple(item.artifact_identity.name for item in dependents),
            },
            member=ClaimRetirementMemberV1(
                claim_ref=claim_id, reason=change.reason, dependents=dependents
            ),
            retires=claim_id,
        )

    def build(self) -> _Plan:
        # Retirements first, so a set in the same write sees what it leaves.
        order = sorted(
            enumerate(self.request.changes),
            key=lambda item: (not isinstance(item[1], RetireChange), item[0]),
        )
        touched: dict[str, int] = {}
        slots: dict[tuple[str, str], int] = {}
        for index, change in order:
            if isinstance(change, RetireChange):
                planned = self.retire_change(index, change)
            else:
                planned = self.claim_change(index, change)
            for claim in (planned.retires, planned.revises):
                if claim is None:
                    continue
                if claim in touched:
                    raise _refuse(
                        "playbill.write.claim_changed_twice",
                        f"changes {touched[claim]} and {index} both change {claim}",
                        change=index,
                        repair="Keep one change per Claim in a write",
                    )
                touched[claim] = index
            if isinstance(change, SetChange) and planned.slot is not None:
                if planned.slot in slots:
                    raise _refuse(
                        "playbill.write.slot_set_twice",
                        f"changes {slots[planned.slot]} and {index} both set "
                        f"{planned.outcome['subject']} {planned.outcome['field']}",
                        change=index,
                        repair="Keep one set per field in a write",
                    )
                slots[planned.slot] = index
            self.plan.changes.append(planned)
        self.plan.changes.sort(key=lambda item: item.index)
        return self.plan


# -- lowering the plan onto one change set ----------------------------------------


@dataclass(frozen=True)
class _Lowered:
    payload: ChangeSetAuthoringPayloadV1
    claim_ids: tuple[str, ...]
    identity_by_change: dict[int, str]


def _with_dispositions(
    member: ClaimAuthoringPayloadV1, claim_ids: Sequence[str]
) -> ClaimAuthoringPayloadV1:
    return member.model_copy(
        update={
            "existing_claim_dispositions": tuple(
                AuthoringExistingClaimDispositionV1(claim_id=claim_id, disposition="not_tested")
                for claim_id in sorted(set(claim_ids), key=lambda item: item.encode("ascii"))
            )
        }
    )


def _lower(plan: _Plan, *, because: str) -> _Lowered:
    """Fold the plan into one change set, filling every disposition the slot law demands.

    A new Claim must disposition every live Claim of its slot when it is staged.
    Lowering stages Claim members in their canonical (identity) order, so a
    sibling added earlier in the same set is live by then: two ``add`` changes
    on one many-valued field need the second to disposition the first. The
    sibling has no ID until the coordinator mints it, so the IDs are minted
    here, in the coordinator's own order, and handed to it.
    """

    drafts: list[tuple[int | None, AuthoringChangeSetMemberV1]] = [
        (None, SubjectAuthoringPayloadV1(subject=shell)) for shell in plan.subjects.values()
    ]
    for item in plan.changes:
        assert item.member is not None
        drafts.append((item.index, item.member))
    by_identity: dict[str, tuple[int | None, AuthoringChangeSetMemberV1]] = {}
    for index, member in drafts:
        identity = authoring_member_identity(member)
        if identity in by_identity:
            raise _refuse(
                "playbill.write.duplicate_change",
                f"change {index} repeats change {by_identity[identity][0]}",
                change=index,
                repair="Leave the repeated change out",
            )
        by_identity[identity] = (index, member)
    ordered = sorted(by_identity, key=lambda item: item.encode("utf-8"))
    planned_by_index = {item.index: item for item in plan.changes}
    claim_ids: list[str] = []
    minted: dict[str, str] = {}
    for identity in ordered:
        index, member = by_identity[identity]
        if isinstance(member, ClaimAuthoringPayloadV1) and member.revises is None:
            minted[identity] = new_claim_id()
            claim_ids.append(minted[identity])
    siblings: dict[tuple[str, str], list[str]] = {}
    members: list[AuthoringChangeSetMemberV1] = []
    identity_by_change: dict[int, str] = {}
    for identity in ordered:
        index, member = by_identity[identity]
        if index is not None:
            identity_by_change[index] = identity
        if isinstance(member, ClaimAuthoringPayloadV1) and index is not None:
            planned = planned_by_index[index]
            assert planned.slot is not None
            earlier = siblings.setdefault(planned.slot, [])
            member = _with_dispositions(member, (*planned.existing, *earlier))
            if identity in minted:
                earlier.append(minted[identity])
        members.append(member)
    rationale = "\n\n".join((because.strip(), *plan.retire_notes))
    return _Lowered(
        payload=ChangeSetAuthoringPayloadV1(members=tuple(members), rationale=rationale),
        claim_ids=tuple(claim_ids),
        identity_by_change=identity_by_change,
    )


def _minted_claims(intent: AuthoringIntentV1) -> dict[str, str]:
    return {item.member_identity: item.claim_id for item in intent.change_set_claim_identities}


def _change_outcomes(
    plan: _Plan, lowered: _Lowered, intent: AuthoringIntentV1 | None
) -> tuple[ChangeOutcome, ...]:
    minted = {} if intent is None else _minted_claims(intent)
    outcomes: list[ChangeOutcome] = []
    for item in plan.changes:
        values = dict(item.outcome)
        if item.op != "retire":
            identity = lowered.identity_by_change[item.index]
            values["claim"] = item.revises or minted.get(identity)
        outcomes.append(ChangeOutcome(op=item.op, **values))
    return tuple(outcomes)


# -- verdicts (R05: a write that lands uncovered is never silent) -------------------


_VALUE_READ_ACCESS = BodyAccessContext(principal_id="playbill-write-verdict", can_read_body=True)


def _claim_id_of_path(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".json")


def _candidate_verdicts(candidate: object) -> dict[str, str]:
    """Each written Claim's verdict as the candidate evaluation found it."""

    found: dict[str, str] = {}
    for evaluation in getattr(candidate, "law_evidence", ()):
        evidence = evaluation.result.get("claim_evidence")
        if not evaluation.path.startswith("claims/") or not isinstance(evidence, Mapping):
            continue
        verdict = evidence.get("initial_verdict")
        if isinstance(verdict, str):
            found[_claim_id_of_path(evaluation.path)] = verdict
    return found


def _accepted_verdicts(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate, plan: _Plan
) -> dict[str, str]:
    """Each written Claim's verdict at the accepted coordinate, by the read machinery."""

    from cruxible_client.contracts.claim_reads import ClaimValuesRequestV1
    from cruxible_core.service.claims.claim_reads import service_read_claim_values

    slots = [item.slot for item in plan.changes if item.slot is not None and item.op != "retire"]
    if not slots:
        return {}
    result = service_read_claim_values(
        instance,
        request=ClaimValuesRequestV1(
            at=_full(coordinate),
            subject_paths=tuple(sorted({slot[0] for slot in slots})),
            predicates=tuple(sorted({slot[1] for slot in slots})),
        ),
    )
    return {_bare(row.claim_id): row.verdict for row in result.values}


def _used_contract(
    instance: PlaybillInstance, planned: _Planned, names: CaptureContractNames
) -> str | None:
    if planned.used_contract is not None:
        return planned.used_contract
    change = planned.change
    if change is None or not isinstance(change.evidence, CaptureEvidence):
        return None
    try:
        envelope = parse_capture_envelope(
            instance.body_store().read(change.evidence.capture, access=_VALUE_READ_ACCESS)
        )
    except (PlaybillError, ValueError):
        return None
    return names.name(envelope.capture_contract_digest)


def _render_evidence_repair(
    surface: PlaybillReadSurface,
    change: SetChange | AddChange,
    *,
    because: str,
    contracts: Sequence[str],
) -> str:
    placeholder = f"<digest of a Capture under {' or '.join(contracts) or 'an admitted contract'}>"
    value = change.value
    if surface == "cli":
        if isinstance(change, SetChange):
            return (
                f"cruxible playbill set {shlex.quote(change.subject)} "
                f"{shlex.quote(change.field)} {shlex.quote(str(value))} "
                f"--because {shlex.quote(because)} --capture {placeholder}"
            )
        return (
            "cruxible playbill write FILE, with this change carrying "
            f'"evidence": {{"kind": "capture", "capture": "{placeholder}"}}'
        )
    if surface == "sdk":
        verb = "set" if isinstance(change, SetChange) else "add"
        call = (
            f"pb.{verb}({json.dumps(change.subject)}, {json.dumps(change.field)}, "
            f"{json.dumps(value)}, because={json.dumps(because)}, "
            f"evidence=CaptureEvidence(capture={json.dumps(placeholder)}))"
        )
        return (
            call
            if verb == "set"
            else f"pb.changes(because={json.dumps(because)}).{call[3:]}.write()"
        )
    evidence = json.dumps({"kind": "capture", "capture": placeholder})
    if isinstance(change, SetChange):
        return (
            f"cruxible_playbill_set(subject={json.dumps(change.subject)}, "
            f"field={json.dumps(change.field)}, value={json.dumps(value)}, "
            f"because={json.dumps(because)}, evidence={evidence})"
        )
    item = {
        "op": "add",
        "subject": change.subject,
        "field": change.field,
        "value": value,
        "evidence": {"kind": "capture", "capture": placeholder},
    }
    return f"cruxible_playbill_write(changes=[{json.dumps(item)}], because={json.dumps(because)})"


def _with_verdicts(
    instance: PlaybillInstance,
    *,
    head: AcceptedProjectionCoordinate,
    plan: _Plan,
    changes: tuple[ChangeOutcome, ...],
    verdicts: Mapping[str, str],
    surface: PlaybillReadSurface,
    because: str,
) -> tuple[tuple[ChangeOutcome, ...], tuple[WriteWarning, ...]]:
    """Attach each written Claim's verdict, and warn plainly when it is not supported."""

    planned_by_index = {item.index: item for item in plan.changes}
    updated: list[ChangeOutcome] = []
    warnings: list[WriteWarning] = []
    names: CaptureContractNames | None = None
    for position, outcome in enumerate(changes):
        planned = plan.changes[position]
        verdict = (
            None if outcome.op == "retire" or outcome.claim is None else verdicts.get(outcome.claim)
        )
        updated.append(outcome.model_copy(update={"verdict": verdict}))
        if verdict is None or verdict == "supported":
            continue
        planned = planned_by_index[planned.index]
        change = planned.change
        claim_type = planned.claim_type
        if names is None:
            with instance.bind_accepted_projection(head) as projection:
                names = CaptureContractNames(instance, head, connection=projection.typed.connection)
                admitted = names.admitted(claim_type) if claim_type is not None else ()
        else:
            admitted = names.admitted(claim_type) if claim_type is not None else ()
        used = _used_contract(instance, planned, names)
        label = f"{outcome.subject} {outcome.field}"
        if verdict == "uncovered" and used is not None and used not in admitted:
            message = (
                f"{label} was written, but its verdict is uncovered: evidence not admitted. "
                f"The ClaimType admits {', '.join(admitted) or 'no CaptureContract'}; "
                f"your evidence used {used}."
            )
        elif verdict == "uncovered":
            message = (
                f"{label} was written, but its verdict is uncovered: no admitted evidence backs it."
            )
        else:
            message = f"{label} was written, but its verdict is {verdict}."
        repair = (
            _render_evidence_repair(surface, change, because=because, contracts=admitted)
            if change is not None and verdict == "uncovered"
            else None
        )
        warnings.append(
            WriteWarning(
                code="playbill.write.verdict_not_supported",
                change=planned.index,
                claim=outcome.claim,
                verdict=verdict,
                message=message,
                admitted_contracts=admitted,
                used_contract=used,
                repair=repair,
            )
        )
    return tuple(updated), tuple(warnings)


def _first_repair(warnings: Sequence[WriteWarning]) -> str | None:
    return next((item.repair for item in warnings if item.repair is not None), None)


# -- refusals as outcomes -------------------------------------------------------------


def _refusal(error: WriteRefusalError | ReadRefusalError) -> WriteRefusal:
    if isinstance(error, WriteRefusalError):
        return WriteRefusal(
            code=error.error_code,
            message=error.detail,
            change=error.change,
            field_path=error.field_path,
            candidates=error.candidates,
            repair=error.repair_line,
        )
    return WriteRefusal(
        code=error.error_code,
        message=str(error).split(": ", 1)[-1],
        field_path=error.field_path or "at",
        candidates=error.candidates,
        repair=error.repair_line,
    )


def _preflight_refusal(result: PreflightResultV1, lowered: _Lowered | None = None) -> WriteRefusal:
    diagnostics = result.frontier.diagnostics
    if not diagnostics:
        blocked = result.frontier.blocked_checks
        code = blocked[0].blocked_by[0] if blocked and blocked[0].blocked_by else "refused"
        return WriteRefusal(code=code, message="The change set did not pass preflight.")
    first = diagnostics[0]
    change: int | None = None
    element = first.offending_element or ""
    if lowered is not None and element.startswith("members["):
        position = element[len("members[") :].split("]", 1)[0]
        if position.isdigit():
            identity = authoring_member_identity(lowered.payload.members[int(position)])
            change = next(
                (index for index, value in lowered.identity_by_change.items() if value == identity),
                None,
            )
    repair = first.repairs[0].description if first.repairs else None
    return WriteRefusal(
        code=first.code,
        message=first.message,
        change=change,
        field_path=element or None,
        repair=repair,
    )


# -- approval -------------------------------------------------------------------------


def _eligible_approvers(
    instance: PlaybillInstance, head: AcceptedProjectionCoordinate, *, creator: str
) -> tuple[str, ...]:
    generation = instance.generation_for_semantic_root(head.semantic_root)
    return tuple(
        record.principal_id
        for record in generation.principals.principals
        if record.kind == "ordinary"
        and record.status == "active"
        and record.principal_id != creator
    )


def _approval(
    instance: PlaybillInstance,
    head: AcceptedProjectionCoordinate,
    *,
    reason: ApprovalReason,
    minimum: int,
    caller: WriteCaller,
    surface: PlaybillReadSurface,
    proposal_id: str,
) -> ApprovalNeeded:
    approvers = (
        _eligible_approvers(instance, head, creator=caller.actor.actor_id)
        if reason == "independent_approval_required"
        else ()
    )
    return ApprovalNeeded(
        reason=reason,
        minimum_approvals=minimum,
        eligible_approvers=approvers,
        approve=(
            _render_proposal_call(
                surface,
                "approve",
                proposal_id,
                signer=approvers[0] if approvers else None,
            )
            if reason == "independent_approval_required"
            else None
        ),
        activate=_render_proposal_call(surface, "activate", proposal_id),
    )


def _approval_reason(
    *, requires_approval: bool, caller: WriteCaller, accept: str
) -> ApprovalReason | None:
    if requires_approval:
        return "independent_approval_required"
    if accept == "never":
        return "accept_never"
    if not caller.may_activate:
        return "activation_not_permitted"
    return None


# -- the verb -------------------------------------------------------------------------


def _coordinator(
    instance: PlaybillInstance, claim_ids: Sequence[str]
) -> AuthoringIntentCoordinator:
    pending: Iterator[str] = iter(tuple(claim_ids))

    def mint() -> str:
        # The coordinator mints change-set Claim IDs in member order; the plan
        # already minted them in that order so sibling dispositions could name them.
        return next(pending, None) or new_claim_id()

    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentCoordinator.for_instance(instance).store,
        claim_id_factory=mint,
    )


def service_playbill_write(
    instance: PlaybillInstance,
    *,
    request: PlaybillWriteRequestV1,
    caller: WriteCaller,
) -> WriteOutcome:
    """Resolve, check and lower one write, then preview it or carry it to acceptance."""

    instance.require_writable()
    head = instance.accepted_coordinate()
    refused: WriteStatus = "would_refuse" if request.dry_run else "refused"

    def refuse(refusal: WriteRefusal) -> WriteOutcome:
        return WriteOutcome(
            status=refused,
            coordinate=_compact(instance, head),
            refusal=refusal,
        )

    try:
        read_at = resolve_read_coordinate(instance, request.at) if request.at is not None else head
        plan = _Planner(instance, head=head, read_at=read_at, request=request).build()
        lowered = _lower(plan, because=request.because)
    except (WriteRefusalError, ReadRefusalError) as error:
        return refuse(_refusal(error))
    coordinator = _coordinator(instance, lowered.claim_ids)
    timestamp = canonical_candidate_timestamp(utc_now())
    subjects_added = tuple(
        f"{shell.subject_kind}/{shell.subject_id}" for shell in plan.subjects.values()
    )
    if request.dry_run:
        return _dry_run(
            instance,
            head=head,
            coordinator=coordinator,
            caller=caller,
            request=request,
            plan=plan,
            lowered=lowered,
            timestamp=timestamp,
            subjects_added=subjects_added,
        )
    view = coordinator.create(
        actor=caller.actor,
        payload=lowered.payload,
        canonical_timestamp=timestamp,
    )
    submitted = coordinator.submit(view.intent.intent_id, actor=caller.actor)
    intent = submitted.intent
    changes = _change_outcomes(plan, lowered, intent)
    status = submitted.status
    if status.state in {"preflight_refused", "conflicted_after_rebase"} or (
        status.proposal_id is None and status.state != "accepted"
    ):
        refusal = (
            _preflight_refusal(intent.last_preflight, lowered)
            if status.state == "preflight_refused" and intent.last_preflight is not None
            else WriteRefusal(
                code="playbill.write.head_moved",
                message="Accepted state moved while this write was being submitted.",
                repair="Run the same write again; it is checked against the new head",
            )
        )
        return WriteOutcome(
            status="refused",
            changes=changes,
            subjects_added=subjects_added,
            coordinate=_compact(instance, head),
            refusal=refusal,
        )
    if status.state == "accepted":
        return _accepted(
            instance,
            plan=plan,
            head=head,
            accepted=status.accepted_generation,
            request=request,
            changes=changes,
            subjects_added=subjects_added,
            proposal=None,
        )
    assert status.proposal_id is not None
    proposal_id = status.proposal_id
    evaluated_at = _evaluated_head(instance, intent, head)
    if evaluated_at.git_oid != head.git_oid and request.at is not None:
        # The head moved between the checks above and the submit: the slots are
        # checked again against the head the proposal was evaluated at.
        try:
            _Planner(instance, head=evaluated_at, read_at=read_at, request=request).build()
        except (WriteRefusalError, ReadRefusalError) as error:
            return WriteOutcome(
                status="refused",
                changes=changes,
                subjects_added=subjects_added,
                proposal=WriteProposalRef(proposal_id=proposal_id, state=status.state),
                coordinate=_compact(instance, evaluated_at),
                refusal=_refusal(error),
            )
    reason = _approval_reason(
        requires_approval=status.state == "awaiting_external_approval",
        caller=caller,
        accept=request.accept,
    )
    if reason is None and status.state == "ready_to_activate":
        from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

        receipt = service_activate_playbill_proposal(
            instance, proposal_id=proposal_id, activated_by=caller.actor.actor_id
        )
        if receipt.status == "accepted" and receipt.accepted_coordinate is not None:
            return _accepted(
                instance,
                plan=plan,
                head=evaluated_at,
                accepted=AcceptedCoordinate.model_validate(
                    receipt.accepted_coordinate.model_dump(mode="json")
                ),
                request=request,
                changes=changes,
                subjects_added=subjects_added,
                proposal=WriteProposalRef(proposal_id=proposal_id, state="accepted"),
            )
        return WriteOutcome(
            status="refused",
            changes=changes,
            subjects_added=subjects_added,
            proposal=WriteProposalRef(proposal_id=proposal_id, state="conflicted_after_rebase"),
            coordinate=_compact(instance, instance.accepted_coordinate()),
            refusal=WriteRefusal(
                code="playbill.write.head_moved",
                message="Another change was accepted first, so this one was not activated.",
                repair="Run the same write again; it is checked against the new head",
            ),
        )
    approval = _approval(
        instance,
        evaluated_at,
        reason=reason or "independent_approval_required",
        minimum=1 if status.state == "awaiting_external_approval" else 0,
        caller=caller,
        surface=request.surface,
        proposal_id=proposal_id,
    )
    changes, warnings = _with_verdicts(
        instance,
        head=evaluated_at,
        plan=plan,
        changes=changes,
        verdicts=(
            {}
            if status.candidate_digest is None
            else _candidate_verdicts(
                instance.proposal_evidence().read_candidate(status.candidate_digest)
            )
        ),
        surface=request.surface,
        because=request.because,
    )
    return WriteOutcome(
        status="awaiting_approval",
        changes=changes,
        subjects_added=subjects_added,
        proposal=WriteProposalRef(proposal_id=proposal_id, state=status.state),
        coordinate=_compact(instance, evaluated_at),
        approval=approval,
        warnings=warnings,
        next=approval.approve or approval.activate,
    )


def _evaluated_head(
    instance: PlaybillInstance,
    intent: AuthoringIntentV1,
    fallback: AcceptedProjectionCoordinate,
) -> AcceptedProjectionCoordinate:
    """The head the submitted candidate was evaluated at: its preflight's coordinate."""

    preflight = intent.last_preflight
    if preflight is None:
        return fallback
    at = preflight.certificate.accepted_coordinate
    if at.git_oid == fallback.git_oid:
        return fallback
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _accepted(
    instance: PlaybillInstance,
    *,
    plan: _Plan,
    head: AcceptedProjectionCoordinate,
    accepted: AcceptedCoordinate | None,
    request: PlaybillWriteRequestV1,
    changes: tuple[ChangeOutcome, ...],
    subjects_added: tuple[str, ...],
    proposal: WriteProposalRef | None,
) -> WriteOutcome:
    coordinate = (
        instance.accepted_coordinate()
        if accepted is None
        else instance.resolve_accepted_coordinate(
            git_oid=accepted.git_oid,
            semantic_root=accepted.semantic_root,
            generation_root=accepted.generation_root,
            compiler_digest=accepted.compiler_digest,
        )
    )
    changes, warnings = _with_verdicts(
        instance,
        head=coordinate,
        plan=plan,
        changes=changes,
        verdicts=_accepted_verdicts(instance, coordinate, plan),
        surface=request.surface,
        because=request.because,
    )
    first = next((item.subject for item in changes if item.subject is not None), None)
    return WriteOutcome(
        status="accepted",
        changes=changes,
        subjects_added=subjects_added,
        proposal=proposal,
        coordinate=_compact(instance, coordinate),
        base=_compact(instance, head),
        accepted_coordinate=_full(coordinate) if request.full_coordinate else None,
        warnings=warnings,
        next=_first_repair(warnings)
        or (None if first is None else _render_get(request.surface, first)),
    )


def _dry_run(
    instance: PlaybillInstance,
    *,
    head: AcceptedProjectionCoordinate,
    coordinator: AuthoringIntentCoordinator,
    caller: WriteCaller,
    request: PlaybillWriteRequestV1,
    plan: _Plan,
    lowered: _Lowered,
    timestamp: str,
    subjects_added: tuple[str, ...],
) -> WriteOutcome:
    """Preview the write on its own path: preflight and evaluation, then stop (R12)."""

    intent, computed = coordinator.preview(
        actor=caller.actor, payload=lowered.payload, canonical_timestamp=timestamp
    )
    changes = _change_outcomes(plan, lowered, intent)
    compact = _compact(instance, head)
    if computed.result.verdict != "passed":
        return WriteOutcome(
            status="would_refuse",
            changes=changes,
            subjects_added=subjects_added,
            coordinate=compact,
            refusal=_preflight_refusal(computed.result, lowered),
        )
    changes, warnings = _with_verdicts(
        instance,
        head=head,
        plan=plan,
        changes=changes,
        verdicts=(
            {}
            if computed.evaluation is None or computed.evaluation.candidate is None
            else _candidate_verdicts(computed.evaluation.candidate)
        ),
        surface=request.surface,
        because=request.because,
    )
    requires_approval = _requires_approval(computed)
    reason = _approval_reason(
        requires_approval=requires_approval, caller=caller, accept=request.accept
    )
    commit = _render_commit(request.surface, compact.git_oid)
    if reason is None:
        return WriteOutcome(
            status="would_accept",
            changes=changes,
            subjects_added=subjects_added,
            coordinate=compact,
            accepted_coordinate=_full(head) if request.full_coordinate else None,
            warnings=warnings,
            next=commit,
        )
    approvers = (
        _eligible_approvers(instance, head, creator=caller.actor.actor_id)
        if reason == "independent_approval_required"
        else ()
    )
    return WriteOutcome(
        status="would_await_approval",
        changes=changes,
        subjects_added=subjects_added,
        coordinate=compact,
        accepted_coordinate=_full(head) if request.full_coordinate else None,
        approval=ApprovalNeeded(
            reason=reason,
            minimum_approvals=1 if requires_approval else 0,
            eligible_approvers=approvers,
            activate="Submit it first; the outcome then names the activate call",
        ),
        warnings=warnings,
        next=commit,
    )


def _requires_approval(computed: ComputedPreflight) -> bool:
    evaluation = computed.evaluation
    if evaluation is None or evaluation.candidate is None:
        return False
    return bool(cast(Any, evaluation.candidate).approval_requirements)


__all__ = ["WriteCaller", "service_playbill_write"]
