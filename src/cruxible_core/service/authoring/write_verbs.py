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
  ``cruxible.write.slot_changed`` when the slot moved since the caller's read
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
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import BaseModel

from cruxible_client.contracts import AcceptedCoordinate as ClientCoordinate
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringChangeSetMember,
    AuthoringClaimStatement,
    AuthoringExactContentObject,
    AuthoringExistingClaimDisposition,
    AuthoringIntentV1,
    AuthoringReferenceExpectation,
    AuthoringSlotExpectation,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    ClaimRetirementMember,
    ExistingCaptureCitationSource,
    PreflightResult,
    SelfSourceBody,
    SubjectAuthoringPayload,
    authoring_member_identity,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    COORDINATOR_SELF_SOURCE_CONTRACT_ID,
    DIRECT_SELF_ASSERTED_CONTRACT_ID,
    classify_capture_reuse,
    foreign_source_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import ClaimType, effective_evidence_requirement
from cruxible_client.contracts.claims import (
    ClaimRetireDependent,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
    literal_schema_violation,
    new_claim_id,
    parse_claim,
)
from cruxible_client.contracts.errors import (
    CanonicalEncodingError,
    CruxibleError,
    ReadRefusalError,
    SettlementIntegrityError,
    WriteRefusalError,
)
from cruxible_client.contracts.get_display import exact_content_marker_text
from cruxible_client.contracts.get_reads import ReadSurface
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.read_values import (
    ExactContentRef,
    TruncatedText,
    summary_value,
    whole_value_read,
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
    ContractEvidence,
    ExpectedValue,
    FileEvidence,
    NewerCaptureNotCitableWarning,
    RetireChange,
    SelfEvidence,
    SetChange,
    SlotRef,
    VerdictNotSupportedWarning,
    WriteOutcome,
    WriteProposalRef,
    WriteRefusal,
    WriteRequest,
    WriteStatus,
    WriteWarning,
    capture_handle,
    subject_reference,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import ComputedPreflight
from cruxible_core.claims.claim_retirement import ClaimRetireError, claim_retirement_inventory
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import compact_coordinate, preview_guards
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.exact_content import ExactContentReader
from cruxible_core.service.discovery.field_names import resolve_field_in
from cruxible_core.service.discovery.query_values import read_live_values, subjects_of_kind
from cruxible_core.service.discovery.query_vocabulary import (
    PredicateInfo,
    QueryVocabulary,
    load_query_vocabulary,
)
from cruxible_core.service.identity import require_authoring_principal
from cruxible_core.service.read_refusals import nearest, resolve_read_coordinate
from cruxible_core.storage.cas import BodyAccessContext

_AUTHORABLE_ROLES = ("normative", "observation", "environment_binding")
# The contracts a Claim's own authoring produces; they never satisfy `captured`.
_OWN_WORDS_CONTRACTS = frozenset(
    {DIRECT_SELF_ASSERTED_CONTRACT_ID, COORDINATOR_SELF_SOURCE_CONTRACT_ID}
)
_MAX_CANDIDATES = 8
# How many of a contract's Captures about the Subject, newest first, are tried
# for the newest one that verifies.
_MAX_CONTRACT_CAPTURES = 32
# How many stored objects one contract lookup examines for its Captures.
_CONTRACT_SCAN_BUDGET = 16_384
_CONTRACT_QUALIFIER = "CaptureContract:"


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


def _full(coordinate: AcceptedProjectionCoordinate | AcceptedCoordinate) -> ClientCoordinate:
    public = (
        coordinate
        if isinstance(coordinate, AcceptedCoordinate)
        else AcceptedCoordinate.from_internal(coordinate)
    )
    return ClientCoordinate.model_validate(public.model_dump(mode="json"))


# -- rendering calls on the caller's surface (R07) -------------------------------


def _render_proposal_call(
    surface: ReadSurface,
    verb: Literal["approve", "activate"],
    proposal_id: str,
    *,
    signer: str | None = None,
) -> str:
    if surface == "cli":
        if verb == "approve":
            who = signer or "<approver>"
            return (
                f"cruxible proposal approve {proposal_id} --signer-id {who} --key <{who}.ed25519>"
            )
        return f"cruxible proposal activate {proposal_id}"
    if surface == "sdk":
        handle = f"cx.proposal({json.dumps(proposal_id)})"
        if verb == "approve":
            who = signer or "approver"
            return f"{handle}.approve(signer=<{who} signer>, reviewed={handle}.review())"
        return f"{handle}.activate()"
    if verb == "approve":
        arguments = f"proposal_id={json.dumps(proposal_id)}"
        if signer is not None:
            arguments += f", signer_id={json.dumps(signer)}"
        return f"cruxible_proposal_approve({arguments})"
    return f"cruxible_proposal_activate(proposal_id={json.dumps(proposal_id)})"


def _render_get(surface: ReadSurface, ref: str) -> str:
    if surface == "cli":
        return f"cruxible get {shlex.quote(ref)}"
    if surface == "sdk":
        return f"cx.get({json.dumps(ref)})"
    return f"cruxible_get(ref={json.dumps(ref)})"


def _render_commit(surface: ReadSurface, git_oid: str) -> str:
    if surface == "cli":
        return f"Run the same command without --dry-run, adding --at {git_oid}"
    if surface == "sdk":
        return f"Call it again with dry_run=False, at={json.dumps(git_oid)}"
    return f'Call it again with dry_run=false and at="{git_oid}"'


def requires_captured_evidence(claim_type: ClaimType) -> bool:
    """Whether a ClaimType refuses a write backed only by the writer's own words.

    Decision b refuses such a write with ``cruxible.write.evidence_required``: a
    v7 ClaimType whose ``evidence_requirement`` is ``captured``. Under ``self``
    (every earlier ClaimType) a policy that merely does not admit self evidence
    lets the write land ``uncovered``, with a warning (R05); under ``none`` the
    writer's words are the origin and support the Claim.
    """

    return effective_evidence_requirement(claim_type) == "captured"


_INTEGER_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)")


def _coerce_text(value: object, info: PredicateInfo) -> object:
    """Read a canonical text spelling of a number or boolean as that value.

    The CLI passes every value as text, and an agent may too. A field whose
    schema is an integer or a boolean takes the one canonical spelling of such
    a value (``42``, ``-3``, ``true``, ``false``); any other text stays text and
    is refused by the schema check with the type it needed.
    """

    if not isinstance(value, str):
        return value
    if info.value_type in ("integer", "decimal") and _INTEGER_TEXT.fullmatch(value):
        return int(value)
    if info.value_type == "boolean" and value in ("true", "false"):
        return value == "true"
    return value


def _named(subject: str | None) -> str:
    """A change's Subject, once the write's default subject has been filled in."""

    assert subject is not None, "the default subject is filled in before planning"
    return subject


def _with_default_subject(request: WriteRequest) -> WriteRequest:
    """Give every change that names no Subject the write's own ``subject``.

    A change's own subject overrides the default. A change with neither
    refuses ``cruxible.write.subject_required`` before anything is planned.
    """

    changes: list[SetChange | AddChange | RetireChange] = []
    for index, change in enumerate(request.changes):
        if isinstance(change, RetireChange):
            target = change.target
            if isinstance(target, SlotRef) and target.subject is None:
                owner = _default_subject(request, index=index, path=f"changes[{index}].target")
                change = change.model_copy(
                    update={"target": target.model_copy(update={"subject": owner})}
                )
        elif change.subject is None:
            owner = _default_subject(request, index=index, path=f"changes[{index}]")
            change = change.model_copy(update={"subject": owner})
        changes.append(change)
    return request.model_copy(update={"changes": tuple(changes)})


def _default_subject(request: WriteRequest, *, index: int, path: str) -> str:
    if request.subject is None:
        raise _refuse(
            "cruxible.write.subject_required",
            f"change {index} names no Subject, and the write has no default subject",
            change=index,
            repair=(
                "Name the Subject on the change as kind/id, or give the write a "
                "top-level subject for every change that names none"
            ),
            field_path=f"{path}.subject",
        )
    return request.subject


_EXACT_BYTES_REPAIR = (
    "Cite evidence committed as exact bytes: a span of the record's source as file "
    "evidence (--evidence-file PATH#ANCHOR), or a Capture a Procedure or Line stored "
    "with an exact-bytes commitment. The external record reader commits records as "
    "canonical values, so reading the same record again does not help"
)


def _commitment_kind(item: Any) -> str:
    return (
        "a canonical value"
        if item.envelope.commitment.digest_kind == "canonical_value"
        else (f"{item.envelope.commitment.digest_kind.replace('_', ' ')}")
    )


def _distinct_handles(digests: Sequence[str], *, at_least: int) -> tuple[str, ...]:
    """Each digest's shortest handle, at least ``at_least`` hex, that tells them apart."""

    length = max(12, at_least)
    while length < 64 and len({capture_handle(item, length=length) for item in digests}) < len(
        digests
    ):
        length += 1
    return tuple(capture_handle(item, length=length) for item in digests)


_BRIEF_MAX = 80


def _brief(value: object) -> str:
    """A value as a refusal quotes it: its repr, cut to a readable length."""

    text = repr(value)
    return text if len(text) <= _BRIEF_MAX else f"{text[: _BRIEF_MAX - 1]}\u2026"


def _not_whole(value: object) -> bool:
    """Whether a shown value is, or holds, something other than the value itself.

    A ``TruncatedText`` preview or an ``ExactContentRef`` marker: neither is a
    value to expect, so a refusal names the read of the whole value instead.
    """

    return isinstance(value, TruncatedText | ExactContentRef) or (
        isinstance(value, list) and any(_not_whole(item) for item in value)
    )


def _brief_shown(value: object) -> str:
    """A shown value as a refusal quotes it: a preview by its head and length."""

    if isinstance(value, TruncatedText):
        return f"{_brief(value.preview)} ({value.length} chars)"
    if isinstance(value, ExactContentRef):
        return exact_content_marker_text(value)
    if isinstance(value, list):
        return "[" + ", ".join(_brief_shown(item) for item in value) + "]"
    return repr(value)


def _value_key(value: object) -> str:
    """One value's comparison key: ``1``, ``true`` and ``"1"`` stay distinct."""

    return canonical_json(value.model_dump(mode="json") if isinstance(value, BaseModel) else value)


# -- planning ------------------------------------------------------------------------


@dataclass(frozen=True)
class _SlotClaim:
    claim_id: str
    digest: str
    value: object


@dataclass(frozen=True)
class _SlotPin:
    """One slot a change depends on, with its live Claims when it was planned."""

    subject_path: str
    predicate: str
    qualifier: str | None
    live: tuple[str, ...]


@dataclass
class _Planned:
    """One change resolved against accepted state, before its member is built."""

    index: int
    op: Literal["set", "add", "retire"]
    outcome: dict[str, Any]
    member: AuthoringChangeSetMember | None = None
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
    # The slot this change was planned against, pinned through admission.
    pin: _SlotPin | None = None
    # The Capture the evidence cites, by digest, once a handle or contract resolved.
    capture: str | None = None


@dataclass
class _Plan:
    changes: list[_Planned] = field(default_factory=list)
    subjects: dict[str, SubjectShell] = field(default_factory=dict)
    retire_notes: list[str] = field(default_factory=list)
    # What planning chose for the writer and says so: see WriteWarning.
    notes: list[WriteWarning] = field(default_factory=list)


class _Planner:
    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        head: AcceptedProjectionCoordinate,
        read_at: AcceptedProjectionCoordinate,
        request: WriteRequest,
    ) -> None:
        self.instance = instance
        self.head = head
        self.read_at = read_at
        self.request = request
        self.vocabulary: QueryVocabulary = load_query_vocabulary(instance, head)
        self.exact = ExactContentReader(instance)
        self.plan = _Plan()
        self._subject_exists: dict[str, bool] = {}
        self._verified: dict[str, bool] = {}
        self._read_sequence: int | None = None

    # -- accepted state ----------------------------------------------------------

    def subject_exists(self, path: str) -> bool:
        if path not in self._subject_exists:
            self._subject_exists[path] = self.instance.blob_at(self.head.git_oid, path) is not None
        return self._subject_exists[path]

    def slot_claims(
        self, subject_path_value: str, predicate: str, qualifier: str | None = None
    ) -> tuple[_SlotClaim, ...]:
        """The live Claims of one slot at the head, with their shown values."""

        with self.instance.bind_accepted_projection(self.head) as projection:
            rows = projection.typed.connection.execute(
                "SELECT identity, artifact_digest FROM claims WHERE subject_path=? "
                "AND predicate=? AND qualifier IS ? AND lifecycle='live' ORDER BY identity",
                (subject_path_value, predicate, qualifier),
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

    def slot_pin(
        self, subject_path_value: str, predicate: str, qualifier: str | None = None
    ) -> _SlotPin:
        """The slot's live Claim IDs at the head, however they are qualified or valued."""

        with self.instance.bind_accepted_projection(self.head) as projection:
            rows = projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE subject_path=? AND predicate=? "
                "AND qualifier IS ? AND lifecycle='live'",
                (subject_path_value, predicate, qualifier),
            ).fetchall()
        return _SlotPin(
            subject_path=subject_path_value,
            predicate=predicate,
            qualifier=qualifier,
            live=tuple(
                sorted({_bare(str(row[0])) for row in rows}, key=lambda item: item.encode("ascii"))
            ),
        )

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
            "cruxible.write.slot_changed",
            (
                f"{subject} {field_name} changed after your read: it {state}, "
                f"accepted in generation {generation_number} by {actor or 'unknown'}"
            ),
            change=index,
            candidates=tuple(item.claim_id for item in live),
            repair="Re-set it to replace it, or set it with contend: true to contest it",
        )

    def expected_keys(
        self, info: PredicateInfo, expect: ExpectedValue, *, index: int, field_name: str
    ) -> dict[str, object]:
        """The values ``expect`` names, checked like values and keyed for comparison."""

        path = f"changes[{index}].expect"
        items = expect if isinstance(expect, tuple) else (expect,)
        found: dict[str, object] = {}
        for item in items:
            shown: object
            if info.claim_type.object_kind == "literal":
                shown = self.literal(
                    info, item, index=index, field_name=field_name, path=path
                ).value
            elif isinstance(item, str) and item:
                # A Subject as kind/id (``@kind/id`` names the same one), or exact
                # content as its text: compared as written, so a value no Claim
                # holds is simply not what it holds.
                shown = (
                    subject_reference(item) if info.claim_type.object_kind == "subject" else item
                )
            else:
                taken = (
                    "a Subject as kind/id" if info.claim_type.object_kind == "subject" else "text"
                )
                raise _refuse(
                    "cruxible.write.value_type_mismatch",
                    f"{field_name} takes {taken}, so expect {item!r} can never match it",
                    change=index,
                    repair=f"Pass expect as {taken}",
                    field_path=path,
                )
            found[_value_key(shown)] = shown
        return found

    def check_expected(
        self,
        *,
        index: int,
        label: str,
        info: PredicateInfo,
        field_name: str,
        expect: ExpectedValue | None,
        live: tuple[_SlotClaim, ...],
    ) -> None:
        """Compare-and-set: refuse unless the slot holds exactly the values expected.

        The comparison is by value, at the head the write is planned at. When it
        holds, the slot expectation the plan already pins carries exactly the
        live Claim IDs that matched, so a Claim joining or leaving the slot after
        this check still refuses the write at admission or settlement.
        """

        if expect is None:
            return
        wanted = self.expected_keys(info, expect, index=index, field_name=field_name)
        holds = {_value_key(item.value): item.value for item in live}
        if set(wanted) == set(holds):
            return
        current = [summary_value(item.value) for item in live]
        now = (
            "holds no value"
            if not current
            else f"holds {_brief_shown(current[0])}"
            if len(current) == 1
            else f"holds {_brief_shown(current)}"
        )
        expected = [summary_value(value) for value in wanted.values()]
        spelled = expected[0] if isinstance(expect, str | int | float | bool) else expected
        repair_value: object = None if not current else current[0] if len(current) == 1 else current
        # A preview or a marker is never a value to expect: name the whole read.
        cut = [
            item.claim_id for item, shown in zip(live, current, strict=True) if _not_whole(shown)
        ]
        raise _refuse(
            "cruxible.write.slot_changed",
            f"{label} {now}, not {_brief_shown(spelled)} as expected",
            change=index,
            candidates=tuple(item.claim_id for item in live),
            repair=(
                "Read it again; to write over what it holds now, expect its whole value, "
                f'which get({cut[0]}, detail="evidence") reads'
                if cut
                else "Read it again; to write over what it holds now, expect "
                + ("[]" if repair_value is None else json.dumps(repair_value))
            ),
            field_path=f"changes[{index}].expect",
        )

    # -- vocabulary --------------------------------------------------------------

    def resolve_subject(self, subject: str, *, index: int, path: str) -> tuple[str, str, str]:
        kind, _, subject_id = subject.partition("/")
        if kind not in self.vocabulary.kinds:
            raise _refuse(
                "cruxible.write.unknown_kind",
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
                "cruxible.write.ambiguous_field",
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
            "cruxible.write.unknown_field",
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
                    "cruxible.write.role_not_permitted",
                    f"{field_name} does not permit role {requested!r}",
                    change=index,
                    candidates=permitted,
                    repair=f"Use one of: {', '.join(permitted)}",
                    field_path=f"changes[{index}].role",
                )
            return cast(ClaimRole, requested)
        if claim_type.default_role is not None and claim_type.default_role in permitted:
            return claim_type.default_role
        if len(permitted) == 1:
            return permitted[0]
        raise _refuse(
            "cruxible.write.role_required",
            f"{field_name} permits {len(permitted)} roles and declares no default_role, "
            "so the role is not implied",
            change=index,
            candidates=permitted,
            repair=(
                f"Pass role as one of: {', '.join(permitted)}; or declare default_role on "
                "the ClaimType"
            ),
            field_path=f"changes[{index}].role",
        )

    # -- values ------------------------------------------------------------------

    def literal(
        self,
        info: PredicateInfo,
        value: object,
        *,
        index: int,
        field_name: str,
        path: str | None = None,
    ) -> LiteralClaimObject:
        path = path or f"changes[{index}].value"
        value = _coerce_text(value, info)
        if isinstance(value, float):
            if not value.is_integer():
                raise _refuse(
                    "cruxible.write.value_type_mismatch",
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
                    "cruxible.write.value_not_member",
                    f"{value!r} is not a member of {field_name}; its members are: {spelled}",
                    change=index,
                    candidates=nearest(str(value), info.members) or info.members,
                    repair=f"Use one of: {spelled}",
                    field_path=path,
                )
        schema = info.claim_type.literal_schema
        violated = (
            "the field declares no literal schema"
            if schema is None
            else literal_schema_violation(value, schema)
        )
        if violated is not None:
            expected = info.value_type if info.value_type != "json" else "value"
            raise _refuse(
                "cruxible.write.value_type_mismatch",
                f"{_brief(value)} is not a valid {expected} for {field_name}: {violated}",
                change=index,
                repair=f"Pass a {expected} that the field's schema admits ({violated})",
                field_path=path,
            )
        try:
            return LiteralClaimObject(value=value)
        except (CanonicalEncodingError, ValueError) as exc:
            raise _refuse(
                "cruxible.write.value_type_mismatch",
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
        value = subject_reference(value)
        kind, _, subject_id = str(value).partition("/") if isinstance(value, str) else ("", "", "")
        if not kind or not subject_id:
            raise _refuse(
                "cruxible.write.value_type_mismatch",
                f"{field_name} takes a Subject as kind/id, not {value!r}",
                change=index,
                repair=f"Pass a Subject of kind {' or '.join(allowed)} as kind/id",
                field_path=path,
            )
        if kind not in allowed:
            raise _refuse(
                "cruxible.write.value_kind_not_admitted",
                f"{field_name} takes a Subject of kind {', '.join(allowed)}, not {kind!r}",
                change=index,
                candidates=allowed,
                repair=f"Pass a Subject of kind {' or '.join(allowed)}",
                field_path=path,
            )
        try:
            target = subject_path(kind, subject_id)
        except (CruxibleError, ValueError) as exc:
            raise _refuse(
                "cruxible.write.value_type_mismatch",
                f"{value!r} is not a Subject reference: {exc}",
                change=index,
                field_path=path,
            ) from exc
        if not self.subject_exists(target) and target not in added:
            with self.instance.bind_accepted_projection(self.head) as projection:
                known = subjects_of_kind(projection.typed.connection, kind).values()
            raise _refuse(
                "cruxible.write.value_subject_not_found",
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
        capture: str | None,
    ) -> tuple[Any, Literal["evidence", "copy"] | None]:
        """The Claim's source and citation role for this change's evidence."""

        evidence = change.evidence
        path = f"changes[{index}].evidence"
        if capture is not None:
            return ExistingCaptureCitationSource(capture_digest=capture), "evidence"
        if isinstance(evidence, FileEvidence):
            if evidence.observation is None:
                raise _refuse(
                    "cruxible.write.file_evidence_unobserved",
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
                    "cruxible.write.exact_content_evidence_mismatch",
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
        if requires_captured_evidence(info.claim_type):
            with self.instance.bind_accepted_projection(self.head) as projection:
                admitted = tuple(
                    name
                    for name in CaptureContractNames(
                        self.instance, self.head, connection=projection.typed.connection
                    ).admitted(info.claim_type)
                    if name not in _OWN_WORDS_CONTRACTS
                )
            raise _refuse(
                "cruxible.write.evidence_required",
                f"{field_name} declares evidence_requirement 'captured' "
                f"({', '.join(admitted)}); your own words cannot back it",
                change=index,
                candidates=admitted,
                repair=(
                    "Pass evidence as a capture digest or file evidence captured under "
                    f"{' or '.join(admitted)}"
                ),
                field_path=path,
            )
        return SelfSourceBody(content_base64=base64.b64encode(body).decode("ascii")), None

    # -- Captures by handle or by contract ----------------------------------------

    def cited_capture(
        self, change: SetChange | AddChange, *, subject_path_value: str, index: int
    ) -> str | None:
        """The digest of the Capture this change's evidence cites, if it cites one."""

        evidence = change.evidence
        path = f"changes[{index}].evidence"
        if isinstance(evidence, CaptureEvidence):
            if evidence.capture.startswith("sha256:"):
                return evidence.capture
            return self.capture_by_handle(evidence.capture, index=index, path=f"{path}.capture")
        if isinstance(evidence, ContractEvidence):
            return self.capture_by_contract(
                evidence.contract,
                subject=_named(change.subject),
                subject_path_value=subject_path_value,
                index=index,
                path=f"{path}.contract",
            )
        return None

    def _verified_capture(self, digest: str) -> bool:
        """Whether ``digest`` is a Capture this instance holds that verifies at the head."""

        from cruxible_core.service.evidence.capture_reads import (
            CaptureReadInvalid,
            verify_accepted_capture,
        )

        if digest not in self._verified:
            try:
                verified = verify_accepted_capture(
                    self.instance, self.head, digest, access=_VALUE_READ_ACCESS
                )
            except (CaptureReadInvalid, ReadRefusalError, CruxibleError, ValueError):
                self._verified[digest] = False
            else:
                self._verified[digest] = not isinstance(verified, str)
        return self._verified[digest]

    def _nearest_captures(self, near: Sequence[str], hex_prefix: str) -> list[str]:
        """Verified Captures near an unknown handle, at most a few.

        ``near`` is what the bounded scan kept as sharing the longest prefix;
        with none of those verifying, the accepted Captures on either side of
        the handle in digest order, which the index answers without a scan.
        """

        found = [digest for digest in near if self._verified_capture(digest)]
        if not found:
            key = "sha256:" + hex_prefix
            half = _MAX_CANDIDATES // 2
            with self.instance.bind_accepted_projection(self.head) as projection:
                connection = projection.typed.connection
                after = connection.execute(
                    "SELECT capture_digest FROM captures WHERE capture_digest >= ? "
                    "ORDER BY capture_digest LIMIT ?",
                    (key, half),
                ).fetchall()
                before = connection.execute(
                    "SELECT capture_digest FROM captures WHERE capture_digest < ? "
                    "ORDER BY capture_digest DESC LIMIT ?",
                    (key, half),
                ).fetchall()
            found = sorted(str(row[0]) for row in (*before, *after))
        return found[:_MAX_CANDIDATES]

    def capture_by_handle(self, handle: str, *, index: int, path: str) -> str:
        """Resolve ``CAP-<hex>`` to one Capture at the head, as ``get`` and ``read_capture`` do.

        ``resolve_capture_handle`` is the one resolver: a Capture an accepted
        Claim cites, or one the instance holds that verifies against its
        contract accepted at the head -- citing one for the first time is the
        common case. The lookup is bounded: when the store holds more under the
        prefix than one lookup examines, it refuses rather than call a partial
        answer unique.
        """

        from cruxible_core.service.evidence.capture_reads import (
            CAPTURE_HANDLE_MAX_VERIFIED,
            CaptureHandleAmbiguous,
            CaptureHandleExhausted,
            CaptureHandleResolved,
            resolve_capture_handle,
        )

        hex_prefix = handle.removeprefix("CAP-")
        resolution = resolve_capture_handle(
            self.instance,
            self.head,
            hex_prefix,
            verified=self._verified_capture,
            nearest=_MAX_CANDIDATES,
        )
        if isinstance(resolution, CaptureHandleResolved):
            return resolution.digest
        if isinstance(resolution, CaptureHandleExhausted):
            # Out of budget, a Capture past the limit could match too: the scan
            # never calls what it verified so far unique.
            what = (
                "objects under it than one lookup examines"
                if resolution.reason == "scan_budget"
                else f"more than {CAPTURE_HANDLE_MAX_VERIFIED} Captures under it to verify"
            )
            raise _refuse(
                "cruxible.write.capture_scan_exhausted",
                f"{handle} was not resolved: the body store holds {what}",
                change=index,
                repair="Pass a longer handle, or the full sha256 digest",
                field_path=path,
            )
        if isinstance(resolution, CaptureHandleAmbiguous):
            raise _refuse(
                "cruxible.write.capture_ambiguous",
                f"{handle} is the prefix of more than one Capture",
                change=index,
                candidates=_distinct_handles(resolution.candidates, at_least=len(hex_prefix) + 1),
                repair="Pass a longer handle, or the full sha256 digest",
                field_path=path,
            )
        raise _refuse(
            "cruxible.write.capture_not_found",
            f"no Capture this instance holds has the handle {handle}",
            change=index,
            candidates=_distinct_handles(
                self._nearest_captures(resolution.nearest, hex_prefix), at_least=12
            ),
            repair=(
                "Name a Capture by the CAP- handle a run, a capture or get with "
                "detail=evidence printed, or by its full sha256 digest"
            ),
            field_path=path,
        )

    def capture_by_contract(
        self,
        name: str,
        *,
        subject: str,
        subject_path_value: str,
        index: int,
        path: str,
    ) -> str:
        """The newest verified, citable Capture of one contract about one Subject."""

        from cruxible_core.service.evidence.capture_reads import (
            CaptureReadInvalid,
            RetainedCapture,
            retained_captures,
            verify_accepted_capture,
        )

        bare = name.removeprefix(_CONTRACT_QUALIFIER)
        qualified = _CONTRACT_QUALIFIER + bare
        with self.instance.bind_accepted_projection(self.head) as projection:
            connection = projection.typed.connection
            known = [
                str(row[0]).removeprefix(_CONTRACT_QUALIFIER)
                for row in connection.execute("SELECT identity FROM capture_contracts")
            ]
            if bare not in known:
                raise _refuse(
                    "cruxible.write.unknown_contract",
                    f"no accepted CaptureContract is named {bare!r}",
                    change=index,
                    candidates=nearest(bare, known),
                    repair="Name a CaptureContract the field admits; orient lists them",
                    field_path=path,
                )
            versions = CaptureContractNames(self.instance, self.head).lineage(qualified)
            marks = ",".join("?" for _ in versions)
            cited = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT u.capture_digest FROM citation_uses u "
                    "JOIN claims c ON c.identity = u.owner_key "
                    "JOIN captures p ON p.capture_digest = u.capture_digest "
                    f"WHERE u.owner_kind = 'Claim' AND c.subject_path = ? "
                    f"AND p.contract_digest IN ({marks})",
                    (subject_path_value, *versions),
                )
            }
        # Every retained Capture of the contract counts, cited or not.
        inventory = retained_captures(
            self.instance, budget=_CONTRACT_SCAN_BUDGET, contract_digests=versions
        )
        if not inventory.complete:
            raise _refuse(
                "cruxible.write.capture_scan_exhausted",
                f"the newest Capture of {bare} about {subject} was not found: the body "
                "store holds more objects than one lookup examines",
                change=index,
                repair="Cite the Capture by its CAP- handle or its full sha256 digest",
                field_path=path,
            )
        address = SemanticAddress.whole_artifact(subject_path_value).model_dump(mode="json")

        def about_subject(envelope: Any) -> bool:
            selector = getattr(envelope.source, "selector", None)
            return isinstance(selector, Mapping) and selector.get("semantic_subject") == address

        bound = sorted(
            (
                item
                for item in inventory.captures
                if item.digest in cited or about_subject(item.envelope)
            ),
            key=lambda item: (item.envelope.observed_at, item.digest),
            reverse=True,
        )
        store = self.instance.body_store()
        # A Claim maps a byte span onto what it cites, so only an exact-bytes
        # commitment can back one. A newer Capture committed any other way is
        # never skipped silently: it is named, as a refusal or as a warning.
        uncitable: RetainedCapture | None = None
        for item in bound[:_MAX_CONTRACT_CAPTURES]:
            try:
                verified = verify_accepted_capture(
                    self.instance, self.head, item.digest, access=_VALUE_READ_ACCESS
                )
            except (CaptureReadInvalid, ReadRefusalError):
                continue
            if isinstance(verified, str):
                continue
            reuse = classify_capture_reuse(
                verified.envelope, contract=verified.contract, store=store, claim_id=""
            )
            if reuse != "shareable":
                continue
            if item.envelope.commitment.digest_kind != "exact_bytes":
                uncitable = uncitable or item
                continue
            if uncitable is not None and uncitable.envelope.observed_at > item.envelope.observed_at:
                self.plan.notes.append(
                    NewerCaptureNotCitableWarning(
                        change=index,
                        capture=capture_handle(uncitable.digest),
                        message=(
                            f"{subject}: the newest Capture of {bare}, "
                            f"{capture_handle(uncitable.digest)}, is committed as "
                            f"{_commitment_kind(uncitable)}, which no Claim can cite; the "
                            f"write cites the older {capture_handle(item.digest)} instead"
                        ),
                        repair=_EXACT_BYTES_REPAIR,
                    )
                )
            return item.digest
        if uncitable is not None:
            raise _refuse(
                "cruxible.write.contract_capture_not_citable",
                f"the newest Capture of {bare} about {subject}, "
                f"{capture_handle(uncitable.digest)}, is committed as "
                f"{_commitment_kind(uncitable)}, which no Claim can cite: a Claim maps a "
                "byte span onto its evidence, and only an exact-bytes commitment has bytes",
                change=index,
                candidates=(capture_handle(uncitable.digest),),
                repair=_EXACT_BYTES_REPAIR,
                field_path=path,
            )
        raise _refuse(
            "cruxible.write.contract_capture_not_found",
            f"no verified Capture of {bare} is about {subject}",
            change=index,
            repair=(
                f"Capture {subject} under {bare} first (a Procedure or Line that produces it, "
                "or --evidence-file / file evidence from its source), or cite a Capture "
                "by its CAP- handle or digest"
            ),
            field_path=path,
        )

    # -- changes -----------------------------------------------------------------

    def claim_change(self, index: int, change: SetChange | AddChange) -> _Planned:
        prefix = f"changes[{index}]"
        subject = _named(change.subject)
        kind, subject_id, path = self.resolve_subject(
            subject, index=index, path=f"{prefix}.subject"
        )
        info = self.resolve_field(kind, change.field, index=index, path=f"{prefix}.field")
        claim_type = info.claim_type
        name = self.field_name(info, kind)
        label = f"{subject} {name}"
        if isinstance(change, SetChange) and claim_type.cardinality == "many":
            raise _refuse(
                "cruxible.write.field_is_many",
                f"{name} holds many values, so set has nothing to replace",
                change=index,
                repair="Use add to add a value, or retire one first",
                field_path=f"{prefix}.op",
            )
        if isinstance(change, AddChange) and claim_type.cardinality == "one":
            raise _refuse(
                "cruxible.write.field_is_single",
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
                    "cruxible.write.value_type_mismatch",
                    f"{name} is exact content, so its value is text",
                    change=index,
                    repair="Pass the text itself as the value",
                    field_path=f"{prefix}.value",
                )
            exact = change.value.encode("utf-8")
            statement_object = AuthoringExactContentObject(
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
        capture = self.cited_capture(change, subject_path_value=path, index=index)
        source, citation_role = self.source(
            info, change, role=role, index=index, field_name=name, exact=exact, capture=capture
        )
        if not self.subject_exists(path) and path not in self.plan.subjects:
            if kind not in claim_type.allowed_subject_kinds:  # pragma: no cover - resolve_field
                raise _refuse(
                    "cruxible.write.unknown_kind",
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
            self.check_expected(
                index=index,
                label=label,
                info=info,
                field_name=name,
                expect=change.expect,
                live=live,
            )
            if change.contend:
                contenders = tuple(item.claim_id for item in live)
            else:
                self.check_slot_unchanged(
                    index=index,
                    subject=subject,
                    field_name=name,
                    subject_path_value=path,
                    predicate=info.predicate,
                    live=live,
                )
                remaining = [item for item in live if item.claim_id not in self._retiring()]
                if len(remaining) > 1:
                    raise _refuse(
                        "cruxible.write.slot_contested",
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
            # A Claim retired in this same write is not live after it, so the
            # value it holds is stated anew rather than reported as present.
            retiring = self._retiring()
            present = next(
                (
                    item
                    for item in live
                    if item.value == shown_after and item.claim_id not in retiring
                ),
                None,
            )
            if present is not None and change.expect_absent:
                raise _refuse(
                    "cruxible.write.value_already_present",
                    f"{label} already holds {summary_value(present.value)!r} as "
                    f"{present.claim_id}, and the add expected it absent",
                    change=index,
                    candidates=(present.claim_id,),
                    repair="Leave it out, or drop expect_absent to accept it as already done",
                    field_path=f"changes[{index}].expect_absent",
                )
            if present is not None:
                # Adding what is already there is done already: an idempotent
                # success, with nothing to submit for this change.
                return _Planned(
                    index=index,
                    op=change.op,
                    outcome={
                        "subject": subject,
                        "field": name,
                        "predicate": info.predicate,
                        "before": summary_value(
                            present.value,
                            read_whole=whole_value_read(present.claim_id, self.head.git_oid),
                        ),
                        "after": summary_value(present.value),
                        "claim": present.claim_id,
                        "already_live": True,
                    },
                    slot=(path, info.predicate),
                    change=change,
                    claim_type=claim_type,
                )
        statement = AuthoringClaimStatement(
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
            "dependency_drafts": ClaimDependencyDrafts(),
        }
        member: ClaimAuthoringPayloadV1 = (
            ClaimAuthoringPayload(**values)
            if isinstance(source, ExistingCaptureCitationSource)
            else ClaimAuthoringPayloadV2(**values)
        )
        used_contract = (
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name
            if isinstance(source, SelfSourceBody)
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
                "subject": subject,
                "field": name,
                "predicate": info.predicate,
                # before is the revised Claim at the planning head; after is
                # the value this write sent, so it names no whole read.
                "before": summary_value(
                    before,
                    read_whole=None
                    if revises is None
                    else whole_value_read(revises, self.head.git_oid),
                ),
                "after": summary_value(shown_after),
                "revises": revises,
                "contenders_created": contenders,
                "capture": None if capture is None else capture_handle(capture),
            },
            member=member,
            capture=capture,
            slot=(path, info.predicate),
            revises=revises,
            existing=tuple(item.claim_id for item in live if item.claim_id != revises),
            pin=self.slot_pin(path, info.predicate),
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
            owner = _named(target.subject)
            kind, _subject_id, path = self.resolve_subject(
                owner, index=index, path=f"{prefix}.subject"
            )
            info = self.resolve_field(kind, target.field, index=index, path=f"{prefix}.field")
            name = self.field_name(info, kind)
            live = self.slot_claims(path, info.predicate) if self.subject_exists(path) else ()
            # A slot that moved since the read is named as moved first, before
            # what it holds now makes the retire empty or ambiguous.
            self.check_expected(
                index=index,
                label=f"{owner} {name}",
                info=info,
                field_name=name,
                expect=change.expect,
                live=live,
            )
            self.check_slot_unchanged(
                index=index,
                subject=owner,
                field_name=name,
                subject_path_value=path,
                predicate=info.predicate,
                live=live,
            )
            if not live:
                raise _refuse(
                    "cruxible.write.slot_empty",
                    f"{owner} {name} holds no live value to retire",
                    change=index,
                    repair=f"Read it first: {_render_get(self.request.surface, owner)}",
                    field_path=prefix,
                )
            if len(live) > 1:
                raise _refuse(
                    "cruxible.write.slot_ambiguous",
                    f"{owner} {name} holds {len(live)} values; name the one to retire",
                    change=index,
                    candidates=tuple(item.claim_id for item in live),
                    repair="Retire one of the listed Claims by ID",
                    field_path=prefix,
                )
            return live[0].claim_id, owner, name, info.predicate, live[0].value
        claim_id = _bare(target)
        content = self.instance.blob_at(self.head.git_oid, claim_path(claim_id))
        if content is None:
            raise _refuse(
                "cruxible.write.claim_not_found",
                f"no accepted Claim {claim_id}",
                change=index,
                repair="Name a live Claim; get on the Subject lists its Claim IDs",
                field_path=prefix,
            )
        claim = parse_claim(content, path=claim_path(claim_id))
        if claim.lifecycle.state != "live":
            raise _refuse(
                "cruxible.write.claim_not_live",
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
                        "cruxible.write.slot_changed",
                        f"{claim_id} changed after your read: revised in generation "
                        f"{location.sequence} by {actor or 'unknown'}",
                        change=index,
                        candidates=(claim_id,),
                        repair="Read it again, then retire it at the new coordinate",
                    )
        live = self.slot_claims(
            claim.statement.subject.artifact_path,
            claim.statement.predicate,
            claim.statement.qualifier,
        )
        if change.expect is not None:
            if retired_info is None:
                raise _refuse(
                    "cruxible.write.unknown_field",
                    f"{claim_id} states {claim.statement.predicate}, which no live ClaimType "
                    "defines, so its values cannot be compared",
                    change=index,
                    repair="Retire it without expect",
                    field_path=f"changes[{index}].expect",
                )
            self.check_expected(
                index=index,
                label=f"{subject} {name}",
                info=retired_info,
                field_name=name,
                expect=change.expect,
                live=live,
            )
        value = next((item.value for item in live if item.claim_id == claim_id), None)
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
                "cruxible.write.retirement_closure_unsupported",
                f"{claim_id} cannot be retired here: {exc}",
                change=index,
                repair="Retire what depends on it through its own governed change first",
            ) from exc
        dependents = tuple(
            sorted(
                (
                    ClaimRetireDependent(
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
                "before": summary_value(
                    value, read_whole=whole_value_read(claim_id, self.head.git_oid)
                ),
                "after": None,
                "claim": claim_id,
                "retired": tuple(item.artifact_identity.name for item in dependents),
            },
            member=ClaimRetirementMember(
                retires=claim_id, reason=change.reason, dependents=dependents
            ),
            retires=claim_id,
            pin=self.slot_pin(
                claim.statement.subject.artifact_path,
                claim.statement.predicate,
                claim.statement.qualifier,
            ),
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
                        "cruxible.write.claim_changed_twice",
                        f"changes {touched[claim]} and {index} both change {claim}",
                        change=index,
                        repair="Keep one change per Claim in a write",
                    )
                touched[claim] = index
            if isinstance(change, SetChange) and planned.slot is not None:
                if planned.slot in slots:
                    raise _refuse(
                        "cruxible.write.slot_set_twice",
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
    payload: ChangeSetAuthoringPayload
    claim_ids: tuple[str, ...]
    identity_by_change: dict[int, str]
    # Every accepted Claim the write revises, retires or dispositions, pinned to
    # the version planning read: admission refuses when one has moved since.
    expectations: tuple[AuthoringReferenceExpectation | AuthoringSlotExpectation, ...] = ()


def _with_dispositions(
    member: ClaimAuthoringPayloadV1, claim_ids: Sequence[str]
) -> ClaimAuthoringPayloadV1:
    return member.model_copy(
        update={
            "existing_claim_dispositions": tuple(
                AuthoringExistingClaimDisposition(claim_id=claim_id, disposition="not_tested")
                for claim_id in sorted(set(claim_ids), key=lambda item: item.encode("ascii"))
            )
        }
    )


def _lower(plan: _Plan, *, because: str, planned_at: AcceptedProjectionCoordinate) -> _Lowered:
    """Fold the plan into one change set, filling every disposition the slot law demands.

    A new Claim must disposition every live Claim of its slot when it is staged.
    Lowering stages Claim members in their canonical (identity) order, so a
    sibling added earlier in the same set is live by then: two ``add`` changes
    on one many-valued field need the second to disposition the first. The
    sibling has no ID until the coordinator mints it, so the IDs are minted
    here, in the coordinator's own order, and handed to it.
    """

    drafts: list[tuple[int | None, AuthoringChangeSetMember]] = [
        (None, SubjectAuthoringPayload(subject=shell)) for shell in plan.subjects.values()
    ]
    for item in plan.changes:
        if item.member is not None:
            drafts.append((item.index, item.member))
    by_identity: dict[str, tuple[int | None, AuthoringChangeSetMember]] = {}
    for index, member in drafts:
        identity = authoring_member_identity(member)
        if identity in by_identity:
            raise _refuse(
                "cruxible.write.duplicate_change",
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
    members: list[AuthoringChangeSetMember] = []
    identity_by_change: dict[int, str] = {}
    pins: dict[int, _SlotPin] = {}
    for identity in ordered:
        index, member = by_identity[identity]
        if index is not None:
            identity_by_change[index] = identity
            pin = planned_by_index[index].pin
            if pin is not None:
                pins[len(members)] = pin
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
        payload=ChangeSetAuthoringPayload(members=tuple(members), rationale=rationale),
        claim_ids=tuple(claim_ids),
        identity_by_change=identity_by_change,
        expectations=_pinned_claims(
            members, minted=set(claim_ids), planned_at=planned_at, slots=pins
        ),
    )


def _pinned_claims(
    members: Sequence[AuthoringChangeSetMember],
    *,
    minted: set[str],
    planned_at: AcceptedProjectionCoordinate,
    slots: Mapping[int, _SlotPin],
) -> tuple[AuthoringReferenceExpectation | AuthoringSlotExpectation, ...]:
    """Pin what the plan read: each slot's live membership, and each Claim's version.

    The plan chose what to revise, retire and disposition from each slot as it
    stood at ``planned_at``. Preflight checks every slot's exact live
    membership at the head the candidate is evaluated at, and settlement
    refuses any other head, so a Claim that joined or left a slot refuses the
    write before it becomes a proposal anyone could activate. The Claim pins
    add that each named Claim is still the version the plan read.
    """

    coordinate = AcceptedCoordinate.from_internal(planned_at)
    pins: list[tuple[str, str]] = []
    for position, member in enumerate(members):
        prefix = f"members[{position}]"
        if isinstance(member, ClaimRetirementMember):
            pins.append((f"{prefix}.retires", member.retires))
        elif isinstance(member, ClaimAuthoringPayloadV1):
            if member.revises is not None:
                pins.append((f"{prefix}.revises", member.revises))
            for ordinal, item in enumerate(member.existing_claim_dispositions):
                if item.claim_id not in minted:
                    pins.append(
                        (f"{prefix}.existing_claim_dispositions[{ordinal}].claim_id", item.claim_id)
                    )
    expectations: list[AuthoringReferenceExpectation | AuthoringSlotExpectation] = [
        AuthoringReferenceExpectation(
            payload_path=path,
            artifact_kind="Claim",
            address=claim_id,
            minted_coordinate=coordinate,
        )
        for path, claim_id in pins
    ]
    expectations.extend(
        AuthoringSlotExpectation(
            payload_path=f"members[{position}]",
            subject_path=pin.subject_path,
            predicate=pin.predicate,
            qualifier=pin.qualifier,
            live_claims=pin.live,
            minted_coordinate=coordinate,
        )
        for position, pin in slots.items()
    )
    return tuple(
        sorted(
            expectations,
            key=lambda item: (
                item.payload_path.encode("utf-8"),
                item.artifact_kind.encode("ascii"),
                item.address.encode("utf-8"),
            ),
        )
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
        if item.op != "retire" and item.member is not None:
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

    from cruxible_client.contracts.claim_reads import ClaimValuesRequest
    from cruxible_core.service.claims.claim_reads import service_read_claim_values

    slots = [item.slot for item in plan.changes if item.slot is not None and item.op != "retire"]
    if not slots:
        return {}
    result = service_read_claim_values(
        instance,
        request=ClaimValuesRequest(
            at=_full(coordinate),
            subject_paths=tuple(sorted({slot[0] for slot in slots})),
            predicates=tuple(sorted({slot[1] for slot in slots})),
        ),
    )
    return {_bare(row.claim_id): row.verdict for row in result.values}


def _pending_verdicts(
    instance: PlaybillInstance,
    head: AcceptedProjectionCoordinate,
    plan: _Plan,
    candidate: Mapping[str, str],
) -> dict[str, str]:
    """Verdicts for a write not yet accepted: the candidate's, and the head's for no-ops.

    An already-live add submits nothing, so the candidate evaluation never sees
    its Claim; its verdict is the one it holds at the head.
    """

    present = [item for item in plan.changes if item.member is None and item.op != "retire"]
    found = _accepted_verdicts(instance, head, _Plan(changes=present)) if present else {}
    return {**found, **candidate}


def _used_contract(
    instance: PlaybillInstance, planned: _Planned, names: CaptureContractNames
) -> str | None:
    if planned.used_contract is not None:
        return planned.used_contract
    if planned.capture is None:
        return None
    try:
        envelope = parse_capture_envelope(
            instance.body_store().read(planned.capture, access=_VALUE_READ_ACCESS)
        )
    except (CruxibleError, ValueError):
        return None
    return names.name(envelope.capture_contract_digest)


def _render_evidence_repair(
    surface: ReadSurface,
    change: SetChange | AddChange,
    *,
    because: str,
    contracts: Sequence[str],
) -> str:
    subject = _named(change.subject)
    placeholder = (
        f"<CAP- handle of a Capture under {' or '.join(contracts) or 'an admitted contract'}>"
    )
    value = change.value
    if surface == "cli":
        verb = "set" if isinstance(change, SetChange) else "add"
        role = "" if change.role is None else f" --role {change.role}"
        contend = " --contend" if isinstance(change, SetChange) and change.contend else ""
        return (
            f"cruxible {verb} {shlex.quote(subject)} "
            f"{shlex.quote(change.field)} {shlex.quote(str(value))} "
            f"--because {shlex.quote(because)} --capture {placeholder}{role}{contend}"
        )
    if surface == "sdk":
        # Rendered against the builder signatures: ``cx.set`` takes ``because``;
        # a batch ``add`` does not, so it goes on ``cx.changes`` instead.
        arguments = [json.dumps(subject), json.dumps(change.field), json.dumps(value)]
        options = [f"evidence=CaptureEvidence(capture={json.dumps(placeholder)})"]
        if change.role is not None:
            options.append(f"role={json.dumps(change.role)}")
        if isinstance(change, SetChange):
            if change.contend:
                options.append("contend=True")
            return (
                f"cx.set({', '.join(arguments)}, because={json.dumps(because)}, "
                f"{', '.join(options)})"
            )
        return (
            f"cx.changes(because={json.dumps(because)})"
            f".add({', '.join([*arguments, *options])}).write()"
        )
    evidence = json.dumps({"kind": "capture", "capture": placeholder})
    if isinstance(change, SetChange):
        return (
            f"cruxible_set(subject={json.dumps(subject)}, "
            f"field={json.dumps(change.field)}, value={json.dumps(value)}, "
            f"because={json.dumps(because)}, evidence={evidence})"
        )
    item = {
        "op": "add",
        "subject": subject,
        "field": change.field,
        "value": value,
        "evidence": {"kind": "capture", "capture": placeholder},
    }
    return f"cruxible_write(changes=[{json.dumps(item)}], because={json.dumps(because)})"


def _with_verdicts(
    instance: PlaybillInstance,
    *,
    head: AcceptedProjectionCoordinate,
    plan: _Plan,
    changes: tuple[ChangeOutcome, ...],
    verdicts: Mapping[str, str],
    surface: ReadSurface,
    because: str,
) -> tuple[tuple[ChangeOutcome, ...], tuple[WriteWarning, ...]]:
    """Attach each written Claim's verdict, and warn plainly when it is not supported."""

    planned_by_index = {item.index: item for item in plan.changes}
    updated: list[ChangeOutcome] = []
    warnings: list[WriteWarning] = list(plan.notes)
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
            VerdictNotSupportedWarning(
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
    return next(
        (
            item.repair
            for item in warnings
            if isinstance(item, VerdictNotSupportedWarning) and item.repair is not None
        ),
        None,
    )


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


def _preflight_refusal(result: PreflightResult, lowered: _Lowered | None = None) -> WriteRefusal:
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
    surface: ReadSurface,
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
    request: WriteRequest,
    caller: WriteCaller,
) -> WriteOutcome:
    """Resolve, check and lower one write, then preview it or carry it to acceptance.

    Every outcome is pinned to one accepted coordinate: the new generation once
    accepted, otherwise the head the write was checked against. A caller that
    asks for ``full_coordinate`` (the SDK, to pin its reads) gets it whole.
    """

    instance.require_writable()
    # The caller must be able to author here before anything is resolved or
    # planned, dry run included: a dry run never reports a write that the real
    # one would refuse, and an already-live value never answers "accepted" to a
    # caller who could not have written it.
    require_authoring_principal(instance, caller.actor.actor_id)
    # A dry run writes nothing, derived indexes included: it runs behind the
    # shared preview guards (R12), so every history read it makes, from planning
    # to the verdicts, is served without touching the index, and no write door
    # opens.
    with preview_guards(request.dry_run):
        outcome = _service_write(instance, request=request, caller=caller)
        if request.full_coordinate and outcome.accepted_coordinate is None:
            pinned = resolve_read_coordinate(instance, outcome.coordinate.git_oid)
            outcome = outcome.model_copy(update={"accepted_coordinate": _full(pinned)})
    return outcome


def _service_write(
    instance: PlaybillInstance,
    *,
    request: WriteRequest,
    caller: WriteCaller,
) -> WriteOutcome:
    head = instance.accepted_coordinate()
    refused: WriteStatus = "would_refuse" if request.dry_run else "refused"

    def refuse(refusal: WriteRefusal) -> WriteOutcome:
        return WriteOutcome(
            status=refused,
            coordinate=compact_coordinate(instance, head),
            refusal=refusal,
        )

    try:
        request = _with_default_subject(request)
        read_at = resolve_read_coordinate(instance, request.at) if request.at is not None else head
        plan = _Planner(instance, head=head, read_at=read_at, request=request).build()
        if all(item.member is None for item in plan.changes) and not plan.subjects:
            return _already_done(instance, head=head, plan=plan, request=request)
        lowered = _lower(plan, because=request.because, planned_at=head)
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
        reference_expectations=lowered.expectations or None,
    )
    submitted = coordinator.submit(view.intent.intent_id, actor=caller.actor)
    intent = submitted.intent
    changes = _change_outcomes(plan, lowered, intent)
    status = submitted.status
    if status.state in {"preflight_refused", "conflicted_after_rebase"} or (
        status.proposal_id is None and status.state != "accepted"
    ):
        refusal = _slot_moved(instance, planned_at=head, read_at=read_at, request=request) or (
            _preflight_refusal(intent.last_preflight, lowered)
            if status.state == "preflight_refused" and intent.last_preflight is not None
            else WriteRefusal(
                code="cruxible.write.head_moved",
                message="Accepted state moved while this write was being submitted.",
                repair="Run the same write again; it is checked against the new head",
            )
        )
        return WriteOutcome(
            status="refused",
            changes=changes,
            subjects_added=subjects_added,
            coordinate=compact_coordinate(instance, head),
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
    reason = _approval_reason(
        requires_approval=status.state == "awaiting_external_approval",
        caller=caller,
        accept=request.accept,
    )
    if reason is None and status.state == "ready_to_activate":
        from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

        try:
            receipt = service_activate_playbill_proposal(
                instance, proposal_id=proposal_id, activated_by=caller.actor.actor_id
            )
        except SettlementIntegrityError:
            # Settlement refuses a candidate evaluated at any head but the
            # current one; a write the head moved past is refused, not settled.
            if instance.accepted_coordinate().git_oid == evaluated_at.git_oid:
                raise
            receipt = None
        if (
            receipt is not None
            and receipt.status == "accepted"
            and receipt.accepted_coordinate is not None
        ):
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
            coordinate=compact_coordinate(instance, instance.accepted_coordinate()),
            refusal=_slot_moved(instance, planned_at=head, read_at=read_at, request=request)
            or WriteRefusal(
                code="cruxible.write.head_moved",
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
        verdicts=_pending_verdicts(
            instance,
            evaluated_at,
            plan,
            {}
            if status.candidate_digest is None
            else _candidate_verdicts(
                instance.proposal_evidence().read_candidate(status.candidate_digest)
            ),
        ),
        surface=request.surface,
        because=request.because,
    )
    return WriteOutcome(
        status="awaiting_approval",
        changes=changes,
        subjects_added=subjects_added,
        proposal=WriteProposalRef(proposal_id=proposal_id, state=status.state),
        coordinate=compact_coordinate(instance, evaluated_at),
        approval=approval,
        warnings=warnings,
        next=approval.approve or approval.activate,
    )


def _already_done(
    instance: PlaybillInstance,
    *,
    head: AcceptedProjectionCoordinate,
    plan: _Plan,
    request: WriteRequest,
) -> WriteOutcome:
    """Every change is already live: accepted, with no change set submitted."""

    changes = tuple(ChangeOutcome(op=item.op, **item.outcome) for item in plan.changes)
    changes, warnings = _with_verdicts(
        instance,
        head=head,
        plan=plan,
        changes=changes,
        verdicts=_accepted_verdicts(instance, head, plan),
        surface=request.surface,
        because=request.because,
    )
    first = next((item.subject for item in changes if item.subject is not None), None)
    return WriteOutcome(
        status="would_accept" if request.dry_run else "accepted",
        changes=changes,
        coordinate=compact_coordinate(instance, head),
        base=None if request.dry_run else compact_coordinate(instance, head),
        warnings=warnings,
        next=None if first is None else _render_get(request.surface, first),
    )


def _slot_moved(
    instance: PlaybillInstance,
    *,
    planned_at: AcceptedProjectionCoordinate,
    read_at: AcceptedProjectionCoordinate,
    request: WriteRequest,
    at: AcceptedProjectionCoordinate | None = None,
) -> WriteRefusal | None:
    """Why the plan no longer holds at ``at`` (the head by default), if it moved.

    Only for reporting: admission and settlement are what refuse the write.

    The write was planned at ``planned_at`` against the read coordinate
    ``read_at`` (the planned head itself when the caller named none). Planning
    again at the later head against that same read coordinate names the slot
    that changed, in the terms of the write rather than of the change set.
    """

    later = instance.accepted_coordinate() if at is None else at
    if later.git_oid == planned_at.git_oid:
        return None
    try:
        _Planner(instance, head=later, read_at=read_at, request=request).build()
    except (WriteRefusalError, ReadRefusalError) as error:
        return _refusal(error)
    return None


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
    request: WriteRequest,
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
        coordinate=compact_coordinate(instance, coordinate),
        base=compact_coordinate(instance, head),
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
    request: WriteRequest,
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
    compact = compact_coordinate(instance, head)
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
        verdicts=_pending_verdicts(
            instance,
            head,
            plan,
            {}
            if computed.evaluation is None or computed.evaluation.candidate is None
            else _candidate_verdicts(computed.evaluation.candidate),
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


__all__ = ["WriteCaller", "requires_captured_evidence", "service_playbill_write"]
