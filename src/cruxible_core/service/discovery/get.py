"""``get``: one governed thing by reference, as values first.

A reference arrives in whatever form an agent saw it -- ``CLM-…``, ``kind/id``,
a predicate, ``Document:<name>``, an artifact path, a proposal id or prefix --
and resolves to exactly one accepted artifact at one coordinate, or refuses
with the candidates it could have meant. ``detail`` then chooses what comes
back: a compact card (``summary``), what backs a Claim (``evidence``), today's
explain output (``why``), revisions (``history``), today's full envelope
(``proof``), or a byte range of a Document body (``body``). Every detail level
reuses the service that already answers it; this module only resolves, shapes,
and names things by identity.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from cruxible_client.contracts import PlaybillAcceptedCoordinate as ClientCoordinate
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    CaptureContractV1,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_reads import ClaimValuesRequestV1, ClaimValueV1
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ExactContentClaimObject,
    SubjectClaimObject,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.get_reads import (
    GET_BODY_DEFAULT_MAX_BYTES,
    GET_DETAILS_BY_KIND,
    PlaybillByteRangeV1,
    PlaybillGetAttestationEvidenceV1,
    PlaybillGetBodyV1,
    PlaybillGetCaptureContractCardV1,
    PlaybillGetCaptureEvidenceV1,
    PlaybillGetCardV1,
    PlaybillGetClaimCardV1,
    PlaybillGetClaimTypeCardV1,
    PlaybillGetContenderV1,
    PlaybillGetDocumentCardV1,
    PlaybillGetEvidenceV1,
    PlaybillGetHistoryV1,
    PlaybillGetProcedureCardV1,
    PlaybillGetProposalCardV1,
    PlaybillGetProposalChangeV1,
    PlaybillGetQueryCardV1,
    PlaybillGetQueryParameterV1,
    PlaybillGetRefKind,
    PlaybillGetRequestV1,
    PlaybillGetResultV1,
    PlaybillGetRevisionV1,
    PlaybillGetSubjectCardV1,
    PlaybillGetSubjectClaimV1,
    PlaybillReadFlag,
    PlaybillReadSurface,
)
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionRuleV3
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.read_refusals import ReadRefusalError, nearest
from cruxible_core.storage.cas import BodyAccessContext

_CLAIM_ID = re.compile(r"^CLM-[0-9a-f]{32}$")
_CLAIM_PREFIX = re.compile(r"^CLM-[0-9a-f]{4,31}$")
_SUBJECT = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*)/(?P<id>[a-z][a-z0-9_.-]{0,255})$"
)
_SERVICE_ACCESS = BodyAccessContext(principal_id="playbill-service", can_read_body=True)
_MAX_CANDIDATES = 10
_MAX_CHANGES = 25

# Typed reference prefixes an agent sees, and the kind each names.
_TYPED_PREFIXES: Mapping[str, PlaybillGetRefKind] = {
    "Claim": "claim",
    "Subject": "subject",
    "ClaimType": "claim_type",
    "Document": "document",
    "document": "document",
    "Procedure": "procedure",
    "QueryDefinition": "query",
    "query": "query",
    "CaptureContract": "capture_contract",
    "Proposal": "proposal",
}
# The projection's artifact kind for each reference kind, and back.
_PROJECTION_KIND: Mapping[PlaybillGetRefKind, str] = {
    "claim": "claim",
    "subject": "subject",
    "claim_type": "claim-type",
    "document": "document",
    "procedure": "procedure",
    "query": "query-definition",
    "capture_contract": "capture-contract",
}
_REF_KIND = {value: key for key, value in _PROJECTION_KIND.items()}
_QUALIFIER: Mapping[PlaybillGetRefKind, str] = {
    "claim": "Claim",
    "subject": "Subject",
    "claim_type": "ClaimType",
    "document": "document",
    "procedure": "Procedure",
    "query": "QueryDefinition",
    "capture_contract": "CaptureContract",
}
_NAMED_KINDS: tuple[PlaybillGetRefKind, ...] = (
    "document",
    "procedure",
    "query",
    "capture_contract",
)
_DISPLAY_PREFIX: Mapping[PlaybillGetRefKind, str] = {
    "claim_type": "ClaimType",
    "document": "Document",
    "procedure": "Procedure",
    "query": "query",
    "capture_contract": "CaptureContract",
    "proposal": "Proposal",
}


@dataclass(frozen=True)
class ResolvedRef:
    """One reference resolved to exactly one accepted (or proposed) thing."""

    kind: PlaybillGetRefKind
    # The projection identity (``Claim:CLM-…``, ``document:<name>``), or the
    # full proposal id.
    identity: str
    # The canonical reference shown back to the caller.
    display: str
    path: str | None = None


def _name(identity: str) -> str:
    return identity.split(":", 1)[1]


def _display(kind: PlaybillGetRefKind, identity: str) -> str:
    name = _name(identity) if kind != "proposal" else identity
    if kind in {"claim", "subject"}:
        return name
    return f"{_DISPLAY_PREFIX[kind]}:{name}"


def _subject_ref(path: str) -> str:
    """``subjects/<kind>/<id>.json`` as the ``kind/id`` shorthand an agent types."""

    if path.startswith("subjects/") and path.endswith(".json"):
        return path[len("subjects/") : -len(".json")]
    return path


def _short_digest(digest: str, *, length: int = 12) -> str:
    algorithm, _, value = digest.partition(":")
    return f"{algorithm}:{value[:length]}" if value else digest[:length]


def _short_predicate(predicate: str, subject_kind: str | None) -> str:
    if subject_kind is not None and predicate.startswith(subject_kind + "."):
        return predicate[len(subject_kind) + 1 :]
    return predicate.rpartition(".")[2]


# -- coordinate ---------------------------------------------------------------


def resolve_read_coordinate(
    instance: PlaybillInstance,
    at: ClientCoordinate | AcceptedCoordinate | str | None,
) -> AcceptedProjectionCoordinate:
    """The accepted coordinate a read names: head, an exact coordinate, or a git oid."""

    from cruxible_client.contracts.errors import PlaybillError

    if at is None:
        return instance.accepted_coordinate()
    try:
        if isinstance(at, str):
            return instance.coordinate_for_oid(at)
        return instance.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )
    except PlaybillError as exc:
        raise ReadRefusalError(
            "playbill.read.coordinate_not_accepted",
            f"at does not name an accepted generation of this instance ({exc})",
            http_status=404,
            repair_line="Omit at to read the current head, or pass a git oid from history",
        ) from exc


# -- reference resolution ------------------------------------------------------


def _not_found(
    what: str,
    ref: str,
    candidates: Sequence[str],
    *,
    surface: PlaybillReadSurface,
) -> ReadRefusalError:
    if candidates:
        repair = RepairOperationV1(operation="playbill.get", arguments={"ref": candidates[0]})
        line = f"Run {_render_get(surface, candidates[0])}"
    else:
        repair = RepairOperationV1(operation="playbill.orient")
        line = "Run orient to see what exists, then get one of its names"
    return ReadRefusalError(
        "playbill.get.ref_not_found",
        f"no accepted {what} {ref!r}",
        http_status=404,
        candidates=candidates,
        repair=repair,
        repair_line=line,
        context={"ref": ref},
    )


def _ambiguous(ref: str, candidates: Sequence[str]) -> ReadRefusalError:
    return ReadRefusalError(
        "playbill.get.ref_ambiguous",
        f"{ref!r} names {len(candidates)} artifacts",
        http_status=409,
        candidates=candidates[:_MAX_CANDIDATES],
        repair=RepairOperationV1(operation="playbill.get", arguments={"ref": candidates[0]}),
        repair_line="Name one of them exactly",
        context={"ref": ref},
    )


def resolve_get_ref(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    ref: str,
    *,
    surface: PlaybillReadSurface = "mcp",
) -> ResolvedRef:
    """Resolve every reference form an agent sees to exactly one thing, or refuse."""

    value = ref.strip()
    if not value:
        raise _not_found("artifact", ref, (), surface=surface)
    if value.startswith("sha256:") or value.startswith("refs/"):
        return _resolve_proposal(instance, value)
    head, separator, rest = value.partition(":")
    if separator and head in _TYPED_PREFIXES:
        kind = _TYPED_PREFIXES[head]
        if kind == "proposal":
            return _resolve_proposal(instance, rest)
        with instance.bind_accepted_projection(coordinate) as projection:
            return _resolve_typed(projection, kind, rest, ref=value, surface=surface)
    with instance.bind_accepted_projection(coordinate) as projection:
        if value.endswith(".json") and "/" in value:
            rows = projection.typed.envelopes(paths=(value,))
            if len(rows) == 1 and rows[0].kind in _REF_KIND:
                kind = _REF_KIND[rows[0].kind]
                return ResolvedRef(kind, rows[0].identity, _display(kind, rows[0].identity), value)
            raise _not_found("artifact at path", value, (), surface=surface)
        if value.startswith("CLM-"):
            return _resolve_typed(projection, "claim", value, ref=value, surface=surface)
        if "/" in value:
            return _resolve_typed(projection, "subject", value, ref=value, surface=surface)
        return _resolve_bare(projection, value, surface=surface)


def _resolve_proposal(instance: PlaybillInstance, selector: str) -> ResolvedRef:
    """A proposal id or unique prefix, or a target ref, without requiring every record.

    Retained partial evidence is still a proposal: the by-ID status read answers
    it as ``incomplete``, so resolution must not demand the admission first.
    """

    from cruxible_client.contracts.errors import ProposalSelectorAmbiguousError
    from cruxible_core.authoring.id_prefixes import AmbiguousIdPrefix
    from cruxible_core.service.proposals.proposals import (
        service_playbill_proposal_status,
        service_resolve_playbill_proposal_selector,
    )

    if selector.startswith("refs/"):
        resolved = service_resolve_playbill_proposal_selector(instance, selector=selector)
        return ResolvedRef("proposal", resolved.proposal_id, f"Proposal:{resolved.proposal_id}")
    try:
        entry = service_playbill_proposal_status(instance, proposal_id=selector)
    except AmbiguousIdPrefix as exc:
        evidence = instance.proposal_evidence()
        candidates = (
            ()
            if evidence.index is None
            else tuple(
                row["proposal_id"]
                for row in evidence.index.rows(
                    evidence, "proposal_id>=? AND proposal_id<?", (selector, selector + "\uffff")
                )
            )
        )
        raise ProposalSelectorAmbiguousError(selector, candidates) from exc
    return ResolvedRef("proposal", entry.proposal_id, f"Proposal:{entry.proposal_id}")


def _envelope(projection: Any, identity: str) -> Any | None:
    return projection.typed.envelope(identity)


def _resolve_typed(
    projection: Any,
    kind: PlaybillGetRefKind,
    name: str,
    *,
    ref: str,
    surface: PlaybillReadSurface,
) -> ResolvedRef:
    if kind == "claim":
        return _resolve_claim(projection, name.removeprefix("Claim:"), ref=ref, surface=surface)
    if kind == "claim_type":
        return _resolve_claim_type(projection, name, ref=ref, surface=surface)
    if kind == "subject":
        name = name.removeprefix("Subject:")
    identity = f"{_QUALIFIER[kind]}:{name}"
    row = _envelope(projection, identity)
    if row is not None:
        return ResolvedRef(kind, row.identity, _display(kind, row.identity), row.path)
    names = _names_of(projection, kind)
    if kind == "subject":
        match = _SUBJECT.fullmatch(name)
        if match is not None and not any(item.startswith(match["kind"] + "/") for item in names):
            kinds = nearest(match["kind"], {item.split("/", 1)[0] for item in names})
            raise ReadRefusalError(
                "playbill.get.ref_not_found",
                f"no accepted Subject has kind {match['kind']!r} ({ref!r})",
                http_status=404,
                candidates=kinds,
                repair=RepairOperationV1(operation="playbill.orient"),
                repair_line="Use one of these Subject kinds; orient lists every kind",
                context={"ref": ref},
            )
    what = {
        "subject": "Subject",
        "document": "Document",
        "procedure": "Procedure",
        "query": "QueryDefinition",
        "capture_contract": "CaptureContract",
    }[kind]
    candidates = tuple(
        item if kind == "subject" else f"{_DISPLAY_PREFIX[kind]}:{item}"
        for item in nearest(name, names)
    )
    raise _not_found(what, ref, candidates, surface=surface)


def _names_of(projection: Any, kind: PlaybillGetRefKind) -> tuple[str, ...]:
    return tuple(
        _name(row.identity) for row in projection.typed.envelopes(kind=_PROJECTION_KIND[kind])
    )


def _resolve_claim(
    projection: Any, name: str, *, ref: str, surface: PlaybillReadSurface
) -> ResolvedRef:
    if _CLAIM_ID.fullmatch(name):
        identity = f"Claim:{name}"
        row = _envelope(projection, identity)
        if row is None:
            raise _not_found("Claim", ref, (), surface=surface)
        return ResolvedRef("claim", identity, name, row.path)
    if not _CLAIM_PREFIX.fullmatch(name):
        raise ReadRefusalError(
            "playbill.get.ref_malformed",
            f"{ref!r} is not a Claim id or a unique CLM- prefix",
            candidates=(),
            repair=RepairOperationV1(operation="playbill.orient"),
            repair_line="Pass CLM- plus 32 lowercase hex, or a unique prefix of at least 4 hex",
            context={"ref": ref},
        )
    lower = f"Claim:{name}"
    rows = projection.typed.connection.execute(
        "SELECT identity FROM claims WHERE identity >= ? AND identity < ? "
        "ORDER BY identity LIMIT ?",
        (lower, lower + "￿", _MAX_CANDIDATES + 1),
    ).fetchall()
    matches = [str(row[0]) for row in rows]
    if not matches:
        raise _not_found("Claim with prefix", ref, (), surface=surface)
    if len(matches) > 1:
        raise _ambiguous(ref, [_name(item) for item in matches])
    row = _envelope(projection, matches[0])
    return ResolvedRef("claim", matches[0], _name(matches[0]), row.path if row else None)


def _resolve_claim_type(
    projection: Any, name: str, *, ref: str, surface: PlaybillReadSurface
) -> ResolvedRef:
    row = _envelope(projection, f"ClaimType:{name}")
    if row is not None:
        return ResolvedRef(
            "claim_type", row.identity, _display("claim_type", row.identity), row.path
        )
    predicates = _names_of(projection, "claim_type")
    leaf = [item for item in predicates if item.endswith(f".{name}")]
    if len(leaf) == 1:
        identity = f"ClaimType:{leaf[0]}"
        found = _envelope(projection, identity)
        return ResolvedRef(
            "claim_type", identity, _display("claim_type", identity), found.path if found else None
        )
    if len(leaf) > 1:
        raise _ambiguous(ref, [f"ClaimType:{item}" for item in sorted(leaf)])
    raise _not_found(
        "ClaimType",
        ref,
        tuple(f"ClaimType:{item}" for item in nearest(name, predicates)),
        surface=surface,
    )


def _resolve_bare(projection: Any, value: str, *, surface: PlaybillReadSurface) -> ResolvedRef:
    """A bare name: a predicate (full or unique leaf), or another artifact's name."""

    matches: list[ResolvedRef] = []
    predicates = _names_of(projection, "claim_type")
    if value in predicates:
        identity = f"ClaimType:{value}"
        matches.append(ResolvedRef("claim_type", identity, _display("claim_type", identity)))
    else:
        for predicate in predicates:
            if predicate.endswith(f".{value}"):
                identity = f"ClaimType:{predicate}"
                matches.append(
                    ResolvedRef("claim_type", identity, _display("claim_type", identity))
                )
    for typed_kind in _NAMED_KINDS:
        identity = f"{_QUALIFIER[typed_kind]}:{value}"
        if _envelope(projection, identity) is not None:
            matches.append(ResolvedRef(typed_kind, identity, _display(typed_kind, identity)))
    if len(matches) == 1:
        found = matches[0]
        row = _envelope(projection, found.identity)
        return ResolvedRef(found.kind, found.identity, found.display, row.path if row else None)
    if matches:
        raise _ambiguous(value, [item.display for item in matches])
    names = [f"ClaimType:{item}" for item in predicates]
    for typed_kind in _NAMED_KINDS:
        names.extend(
            f"{_DISPLAY_PREFIX[typed_kind]}:{item}" for item in _names_of(projection, typed_kind)
        )
    by_name = {item.split(":", 1)[1]: item for item in names}
    candidates = tuple(by_name[item] for item in nearest(value, by_name))
    raise _not_found("artifact named", value, candidates, surface=surface)


# -- rendering next steps for the caller's surface -----------------------------


def _render_get(
    surface: PlaybillReadSurface,
    ref: str,
    detail: str | None = None,
    *,
    window: str | None = None,
) -> str:
    """One ``get`` call spelled for the caller's surface (R07)."""

    if surface == "cli":
        rendered = f"cruxible playbill get {shlex.quote(ref)}"
        if detail:
            rendered += f" --detail {detail}"
        return rendered + (f" --range {window}" if window else "")
    arguments = [json.dumps(ref)]
    if detail:
        arguments.append(f"detail={json.dumps(detail)}")
    if window:
        start, _, end = window.partition(":")
        arguments.append(
            f"range=({start}, {end})"
            if surface == "sdk"
            else f'range={{"start": {start}, "end": {end}}}'
        )
    if surface == "sdk":
        return f"pb.get({', '.join(arguments)})"
    arguments[0] = f"ref={arguments[0]}"
    return f"cruxible_playbill_get({', '.join(arguments)})"


def _render_proposal_step(surface: PlaybillReadSurface, step: str, proposal_id: str) -> str:
    if surface == "cli":
        return f"cruxible playbill proposal {step} {proposal_id}"
    if surface == "sdk":
        return f"pb.proposal({json.dumps(proposal_id)}).{step}()"
    tool = {
        "review": "cruxible_playbill_review",
        "refusal": "cruxible_playbill_inspect_refusal",
        "readmit": "cruxible_playbill_proposal_readmit",
    }[step]
    return f"{tool}(proposal_id={json.dumps(proposal_id)})"


# -- flags ---------------------------------------------------------------------


def verdict_flags(verdict: str, status: str, *, held: bool) -> tuple[PlaybillReadFlag, ...]:
    """The verdict problems one Claim row shows, from the existing verdict and status."""

    flags: list[PlaybillReadFlag] = []
    if verdict in {"stale", "stale_evidence"}:
        flags.append("stale")
    if status == "conflicted":
        flags.append("contested")
    if verdict == "contradicted":
        flags.append("contradicted")
    if held:
        flags.append("unsure_hold")
    return tuple(flags)


def unsure_held_claims(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    claims: Iterable[ClaimArtifactAny],
    *,
    statuses: Mapping[str, str],
    evaluation_time: datetime,
) -> frozenset[str]:
    """Claims an ``unsure`` examined attestation holds right now, as ``next`` decides.

    ``statuses`` are the slot resolution statuses by bare Claim id. The decision
    is ``next``'s own: its Claim rows for these Claims and its hold coverage.
    """

    from cruxible_core.service.discovery.next import claim_unsure_holds

    return claim_unsure_holds(
        instance,
        coordinate=coordinate,
        claims=tuple(claims),
        evaluation_time=evaluation_time,
        resolution_statuses=statuses,
    )


# -- CaptureContract identities --------------------------------------------------


class CaptureContractNames:
    """Name CaptureContracts by identity and version, never by digest.

    v6 ClaimTypes name contracts by identity already. Older ClaimTypes name
    exact digests; each digest resolves through accepted history to the
    identity it is a version of, or shows as ``unresolved:<digest prefix>``.
    """

    def __init__(self, instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate):
        self._instance = instance
        self._at = AcceptedCoordinate.from_internal(coordinate)
        self._versions: dict[str, AcceptedCaptureContract | None] = {}
        self._lineages: dict[str, tuple[str, ...]] = {}

    def version_of(self, digest: str) -> AcceptedCaptureContract | None:
        if digest not in self._versions:
            self._versions[digest] = self._instance.accepted_capture_contract_version(
                self._at, digest
            )
        return self._versions[digest]

    def lineage(self, identity: str) -> tuple[str, ...]:
        """Every accepted version digest of one contract identity, oldest first."""

        if identity not in self._lineages:
            with self._instance.accepted_history_reader(at=self._at) as history:
                occurrences = history.occurrences(identity)
            ordered: list[str] = []
            for location in occurrences:
                if location.artifact_digest not in ordered:
                    ordered.append(location.artifact_digest)
            self._lineages[identity] = tuple(ordered)
        return self._lineages[identity]

    def version_number(self, identity: str, digest: str) -> int:
        lineage = self.lineage(identity)
        return lineage.index(digest) + 1 if digest in lineage else len(lineage)

    def name(self, digest: str) -> str:
        found = self.version_of(digest)
        if found is None:
            return f"unresolved:{_short_digest(digest).partition(':')[2]}"
        return found.contract.identity.qualified

    def accepted_evidence(self, claim_type: ClaimType) -> tuple[str, ...]:
        """The contract names a ClaimType's evidence rules admit."""

        names: set[str] = set()
        for rule in claim_type.evidence_admission_policy.rules:
            if isinstance(rule, ClaimEvidenceAdmissionRuleV3):
                names.update(item.target.qualified for item in rule.capture_contracts)
            else:
                names.update(self.name(digest) for digest in rule.capture_contract_digests)
        return tuple(sorted(names, key=lambda item: item.encode("utf-8")))


# -- per-kind builders ------------------------------------------------------------


def _claim_value(row: ClaimValueV1) -> object:
    if row.object_kind == "subject" and isinstance(row.value, str):
        return _subject_ref(row.value)
    return row.value


def _artifact_value(claim: ClaimArtifactAny) -> object:
    obj = claim.statement.object
    if isinstance(obj, SubjectClaimObject):
        return _subject_ref(obj.address.artifact_path)
    if isinstance(obj, ExactContentClaimObject):
        return obj.content_digest
    return obj.value


def _slot_values(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    subject_path: str,
    predicates: tuple[str, ...] = (),
    evaluation_time: datetime,
) -> tuple[ClaimValueV1, ...]:
    from cruxible_core.service.claims.claim_reads import service_read_claim_values

    public = AcceptedCoordinate.from_internal(coordinate)
    result = service_read_claim_values(
        instance,
        request=ClaimValuesRequestV1(
            at=ClientCoordinate.model_validate(public.model_dump(mode="json")),
            subject_paths=(subject_path,),
            predicates=predicates,
            evaluation_time=evaluation_time,
        ),
    )
    return result.values


def _generation_timestamp(instance: PlaybillInstance, history: Any, sequence: int) -> str:
    record = history.read_generation_record(sequence, instance.blob_at)
    return str(record.candidate.timestamp)


def _claim_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    surface: PlaybillReadSurface,
) -> PlaybillGetClaimCardV1:
    with instance.bind_accepted_projection(coordinate) as projection:
        claim = cast(ClaimArtifactAny, projection.typed.source(resolved.identity))
        row = projection.typed.envelope(resolved.identity)
    statement = claim.statement
    subject_path = statement.subject.artifact_path
    subject_kind = _subject_ref(subject_path).split("/", 1)[0]
    slot = tuple(
        item
        for item in _slot_values(
            instance,
            coordinate,
            subject_path=subject_path,
            predicates=(statement.predicate,),
            evaluation_time=evaluation_time,
        )
        if item.qualifier == statement.qualifier
    )
    own = next((item for item in slot if item.claim_id == claim.identity.name), None)
    verdict = own.verdict if own is not None else "retired"
    status = own.status if own is not None else "retired"
    with instance.bind_accepted_projection(coordinate) as projection:
        contenders = tuple(
            cast(ClaimArtifactAny, projection.typed.source(f"Claim:{item.claim_id}"))
            for item in slot
            if item.claim_id != claim.identity.name
        )
    held = unsure_held_claims(
        instance,
        coordinate,
        (claim, *contenders),
        statuses={item.claim_id: item.status for item in slot},
        evaluation_time=evaluation_time,
    )
    with instance.accepted_history_reader(at=AcceptedCoordinate.from_internal(coordinate)) as h:
        latest = h.latest_member(claim_path(claim.identity.name))
        accepted = None if latest is None else _generation_timestamp(instance, h, latest.sequence)
    name = claim.identity.name
    return PlaybillGetClaimCardV1(
        claim=name,
        subject=_subject_ref(subject_path),
        predicate=_short_predicate(statement.predicate, subject_kind),
        predicate_full=statement.predicate,
        qualifier=statement.qualifier,
        value=_artifact_value(claim),
        verdict=verdict,
        status=status,
        revision=int(row.revision) if row is not None else 1,
        accepted=accepted,
        contenders=tuple(
            PlaybillGetContenderV1(
                claim=item.claim_id, value=_claim_value(item), verdict=item.verdict
            )
            for item in slot
            if item.claim_id != name
        ),
        flags=verdict_flags(verdict, status, held=claim.identity.qualified in held),
        next=(
            _render_get(surface, name, "evidence"),
            _render_get(surface, name, "why"),
            _render_get(surface, _subject_ref(subject_path)),
        ),
    )


def _subject_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    surface: PlaybillReadSurface,
) -> PlaybillGetSubjectCardV1:
    subject = _name(resolved.identity)
    kind = subject.split("/", 1)[0]
    path = resolved.path or f"subjects/{subject}.json"
    rows = _slot_values(instance, coordinate, subject_path=path, evaluation_time=evaluation_time)
    with instance.bind_accepted_projection(coordinate) as projection:
        shell = cast(SubjectShell, projection.typed.source(resolved.identity))
        claim_types: dict[str, ClaimType] = {}
        claims: list[ClaimArtifactAny] = []
        for item in rows:
            if item.predicate not in claim_types:
                claim_types[item.predicate] = cast(
                    ClaimType, projection.typed.source(f"ClaimType:{item.predicate}")
                )
            claims.append(cast(ClaimArtifactAny, projection.typed.source(f"Claim:{item.claim_id}")))
        incoming = int(
            projection.typed.connection.execute(
                "SELECT count(*) FROM claims WHERE object_kind='subject' AND object_path=? "
                "AND lifecycle='live'",
                (path,),
            ).fetchone()[0]
        )
    held = unsure_held_claims(
        instance,
        coordinate,
        claims,
        statuses={item.claim_id: item.status for item in rows},
        evaluation_time=evaluation_time,
    )
    slots: dict[tuple[str, str | None], list[ClaimValueV1]] = defaultdict(list)
    for item in rows:
        slots[(item.predicate, item.qualifier)].append(item)
    entries: list[PlaybillGetSubjectClaimV1] = []
    for (predicate, qualifier), members in sorted(
        slots.items(), key=lambda pair: (pair[0][0], pair[0][1] or "")
    ):
        declared = claim_types.get(predicate)
        many = declared is not None and declared.cardinality == "many"
        # The slot's answer: what resolution selected, or every live contender
        # while it is contested. Overturned and refused contenders are not values.
        shown = [item for item in members if item.status in {"accepted", "conflicted"}] or members
        values = [_claim_value(item) for item in shown]
        flags: list[PlaybillReadFlag] = []
        for item in shown:
            for flag in verdict_flags(
                item.verdict, item.status, held=f"Claim:{item.claim_id}" in held
            ):
                if flag not in flags:
                    flags.append(flag)
        entries.append(
            PlaybillGetSubjectClaimV1(
                predicate=_short_predicate(predicate, kind),
                qualifier=qualifier,
                value=values if many or len(values) > 1 else values[0],
                flags=tuple(flags),
            )
        )
    return PlaybillGetSubjectCardV1(
        subject=subject,
        kind=kind,
        lifecycle=shell.lifecycle.state,
        claims=tuple(entries),
        incoming_count=incoming,
        next=(_render_get(surface, subject, "why"), _render_get(surface, subject, "history")),
    )


def _object_description(claim_type: ClaimType) -> tuple[str, tuple[Any, ...] | None]:
    if claim_type.object_kind == "subject":
        kinds = ",".join(claim_type.allowed_object_subject_kinds)
        return (f"subject:{kinds}" if kinds else "subject"), None
    if claim_type.object_kind == "exact_content":
        return "exact_content", None
    schema = claim_type.literal_schema or {}
    members = schema.get("enum")
    kind = schema.get("type")
    return (
        str(kind) if isinstance(kind, str) else "literal",
        tuple(members) if isinstance(members, list) else None,
    )


def _claim_type_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: PlaybillReadSurface,
) -> PlaybillGetClaimTypeCardV1:
    with instance.bind_accepted_projection(coordinate) as projection:
        claim_type = cast(ClaimType, projection.typed.source(resolved.identity))
        live = int(
            projection.typed.connection.execute(
                "SELECT count(*) FROM claims WHERE predicate=? AND lifecycle='live'",
                (claim_type.predicate,),
            ).fetchone()[0]
        )
    evidence = CaptureContractNames(instance, coordinate).accepted_evidence(claim_type)
    object_type, members = _object_description(claim_type)
    next_steps = [_render_get(surface, resolved.display, "proof")]
    next_steps.extend(
        _render_get(surface, name) for name in evidence[:1] if not name.startswith("unresolved:")
    )
    return PlaybillGetClaimTypeCardV1(
        predicate=claim_type.predicate,
        subject_kinds=claim_type.allowed_subject_kinds,
        object=object_type,
        cardinality=claim_type.cardinality,
        members=members,
        description=cast(str | None, getattr(claim_type, "description", None)),
        evidence=evidence,
        live_claims=live,
        next=tuple(next_steps),
    )


def _document_size(instance: PlaybillInstance, body_digest: str) -> int | None:
    metadata = instance.body_store().metadata(body_digest, access=_SERVICE_ACCESS)
    return metadata.byte_length


def _document_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: PlaybillReadSurface,
) -> PlaybillGetDocumentCardV1:
    with instance.bind_accepted_projection(coordinate) as projection:
        shell = cast(DocumentShell, projection.typed.source(resolved.identity))
    return PlaybillGetDocumentCardV1(
        document=_name(resolved.identity),
        title=shell.title,
        document_kind=shell.document_kind,
        media_type=shell.media_type,
        size=_document_size(instance, shell.body_digest),
        revision=shell.lifecycle.revision,
        next=(
            _render_get(surface, resolved.display, "body"),
            _render_get(surface, resolved.display, "history"),
        ),
    )


def _pin_name(binding: Any) -> str:
    target = getattr(binding, "target", None)
    if target is not None:
        return str(target.qualified)
    return f"slot:{getattr(binding, 'slot_name', '?')}"


def _procedure_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    surface: PlaybillReadSurface,
) -> PlaybillGetProcedureCardV1:
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureReadinessRequestV1,
        service_playbill_procedure_readiness,
    )

    readiness = service_playbill_procedure_readiness(
        instance,
        name=_name(resolved.identity),
        request=ProcedureReadinessRequestV1(
            at=AcceptedCoordinate.from_internal(coordinate), evaluation_time=evaluation_time
        ),
    )
    definition = readiness.artifact.definition
    inputs: dict[str, Any] = {"input": _pin_name(definition.contract_in)}
    if definition.parameter_contract is not None:
        inputs["parameters"] = _pin_name(definition.parameter_contract)
    if definition.pin_slots:
        inputs["slots"] = [slot.slot_name for slot in definition.pin_slots]
    return PlaybillGetProcedureCardV1(
        procedure=_name(resolved.identity),
        description=definition.description,
        inputs=inputs,
        readiness=readiness.state,
        required_slots=readiness.required_slots,
        unsupported_nodes=len(readiness.unsupported_nodes),
        next=(_render_get(surface, resolved.display, "proof"),),
    )


def _query_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: PlaybillReadSurface,
) -> PlaybillGetQueryCardV1:
    with instance.bind_accepted_projection(coordinate) as projection:
        query = cast(QueryDefinitionV1, projection.typed.source(resolved.identity))
    return PlaybillGetQueryCardV1(
        query=query.identity.name,
        description=query.description,
        params=tuple(
            PlaybillGetQueryParameterV1(
                name=item.name, type=str(item.value_type), required=item.required
            )
            for item in query.parameters
        ),
        next=(_render_get(surface, resolved.display, "proof"),),
    )


def _capture_contract_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: PlaybillReadSurface,
) -> PlaybillGetCaptureContractCardV1:
    names = CaptureContractNames(instance, coordinate)
    with instance.bind_accepted_projection(coordinate) as projection:
        contract = cast(CaptureContractV1, projection.typed.source(resolved.identity))
        row = projection.typed.envelope(resolved.identity)
        claim_types = [
            cast(ClaimType, projection.typed.source(item.identity))
            for item in projection.typed.envelopes(kind="claim-type")
        ]
    admitted_by = tuple(
        sorted(
            item.predicate
            for item in claim_types
            if item.lifecycle.state == "live" and resolved.identity in names.accepted_evidence(item)
        )
    )
    return PlaybillGetCaptureContractCardV1(
        contract=resolved.identity,
        version=names.version_number(resolved.identity, row.artifact_digest) if row else 1,
        lifecycle=contract.lifecycle.state,
        captures={
            "sources": list(contract.logical_source_identities),
            "evidence_kinds": list(contract.evidence_kinds),
            "grade": contract.epistemic_grade,
            "source_kinds": list(contract.allowed_source_kinds),
        },
        admitted_by=admitted_by,
        next=tuple(
            [_render_get(surface, resolved.display, "history")]
            + [_render_get(surface, f"ClaimType:{item}") for item in admitted_by[:1]]
        ),
    )


def _proposal_records(instance: PlaybillInstance, proposal_id: str) -> dict[str, Any]:
    """The proposal's list entry and whichever of its records are retained.

    The entry is the by-ID status read's, which authenticates every record that
    is present and answers ``incomplete`` with its reasons for any that is not.
    """

    from cruxible_core.service.proposals.proposals import service_playbill_proposal_status

    entry = service_playbill_proposal_status(instance, proposal_id=proposal_id)
    missing = set(entry.incomplete_reasons)
    evidence = instance.proposal_evidence()
    admission = None if "missing_admission" in missing else evidence.read_admission(proposal_id)
    evaluation = None if "missing_evaluation" in missing else evidence.read_evaluation(proposal_id)
    candidate = (
        None
        if evaluation is None
        or evaluation.candidate_digest is None
        or "missing_candidate" in missing
        else evidence.read_candidate(evaluation.candidate_digest)
    )
    return {
        "status": entry,
        "admission": admission,
        "evaluation": evaluation,
        "candidate": candidate,
    }


def _proposal_card(
    instance: PlaybillInstance,
    resolved: ResolvedRef,
    *,
    surface: PlaybillReadSurface,
) -> PlaybillGetProposalCardV1:
    records = _proposal_records(instance, resolved.identity)
    entry = records["status"]
    admission = records["admission"]
    candidate = records["candidate"]
    changes: list[PlaybillGetProposalChangeV1] = []
    members = () if candidate is None else candidate.members
    for member in members[:_MAX_CHANGES]:
        changes.append(PlaybillGetProposalChangeV1(path=member.path, change=member.disposition))
    if len(members) > _MAX_CHANGES:
        changes.append(
            PlaybillGetProposalChangeV1(
                path="…", change=f"{len(members) - _MAX_CHANGES} more; see detail=proof"
            )
        )
    step: str | None = None
    if entry.status == "open" and entry.verdict == "candidate":
        step = "review"
    elif entry.terminal_reason == "refused":
        step = "refusal"
    elif entry.terminal_reason == "stale":
        step = "readmit"
    return PlaybillGetProposalCardV1(
        proposal=resolved.identity,
        status=entry.status,
        incomplete=entry.incomplete_reasons,
        verdict=entry.verdict,
        reason=entry.terminal_reason,
        actor=entry.actor_id,
        admitted_at=entry.admitted_at,
        rationale=None if admission is None else admission.rationale,
        changes=tuple(changes),
        next=() if step is None else (_render_proposal_step(surface, step, resolved.identity),),
    )


# -- evidence ------------------------------------------------------------------------


def _claim_rationale(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate, claim_id: str
) -> str | None:
    """The change-set rationale of the proposal that accepted this Claim revision."""

    with instance.accepted_history_reader(at=AcceptedCoordinate.from_internal(coordinate)) as h:
        latest = h.latest_member(claim_path(claim_id))
        if latest is None:
            return None
        candidate_digest = h.generation(latest.sequence).candidate_digest
    if candidate_digest is None:
        return None
    evidence = instance.proposal_evidence()
    if evidence.index is None:
        return None
    for row in evidence.index.rows(evidence, "candidate_digest=?", (candidate_digest,)):
        admission = evidence.read_admission(row["proposal_id"])
        if admission.rationale:
            return admission.rationale
    return None


def _claim_evidence(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
) -> PlaybillGetEvidenceV1:
    from cruxible_core.service.claims.claims import service_explain_playbill_claim

    explanation = service_explain_playbill_claim(
        instance,
        identity=resolved.identity,
        at=AcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluation_time,
    )
    names = CaptureContractNames(instance, coordinate)
    bodies = instance.body_store()
    captures: list[PlaybillGetCaptureEvidenceV1] = []
    for account in explanation.admission_accounts:
        envelope = parse_capture_envelope(
            bodies.read(account.capture_digest, access=_SERVICE_ACCESS)
        )
        version = names.version_of(account.capture_contract_digest)
        source = getattr(envelope.source, "source_identity", None)
        if source is None and version is not None:
            source = version.contract.logical_source_identities[0]
        captures.append(
            PlaybillGetCaptureEvidenceV1(
                capture=_short_digest(account.capture_digest),
                contract=account.capture_contract_identity,
                version=names.version_number(
                    account.capture_contract_identity, account.capture_contract_digest
                ),
                source=str(source or "unknown"),
                observed_at=envelope.observed_at,
                role=account.citation_role,
                admitted=account.status == "admitted",
            )
        )
    attestations = tuple(
        PlaybillGetAttestationEvidenceV1(
            stance=item.statement.stance,
            principal=item.statement.provider_or_principal.name,
            at=item.statement.observed_at,
            current=item.current,
        )
        for item in explanation.exact_attestations
    )
    return PlaybillGetEvidenceV1(
        captures=tuple(captures),
        attestations=attestations,
        rationale=_claim_rationale(instance, coordinate, _name(resolved.identity)),
    )


# -- history -------------------------------------------------------------------------


def _revision(
    instance: PlaybillInstance,
    history: Any,
    *,
    index: int,
    sequence: int,
    digest: str,
    lifecycle: str | None,
    value: object = None,
) -> PlaybillGetRevisionV1:
    generation = history.generation(sequence)
    record = history.read_generation_record(sequence, instance.blob_at)
    return PlaybillGetRevisionV1(
        revision=index,
        sequence=sequence,
        accepted=str(record.candidate.timestamp),
        actor=generation.actor_id or record.actor_binding.actor_id,
        approved_by=tuple(dict.fromkeys(item.attestation.signer_id for item in record.approvals)),
        lifecycle=lifecycle,
        value=value,
        digest=_short_digest(digest),
    )


def _history(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
) -> PlaybillGetHistoryV1:
    from cruxible_core.service.authoring.documents import service_playbill_document_history
    from cruxible_core.service.claims.claims import service_playbill_claim_history
    from cruxible_core.service.claims.subjects import service_playbill_subject_history

    at = AcceptedCoordinate.from_internal(coordinate)
    revisions: list[PlaybillGetRevisionV1] = []
    with instance.accepted_history_reader(at=at) as history:
        cutoff = history.sequence
        if resolved.kind == "claim":
            path = claim_path(_name(resolved.identity))
            claim_entries = service_playbill_claim_history(instance, identity=resolved.identity)
            for entry in (item for item in claim_entries.entries if item.sequence <= cutoff):
                content = instance.blob_at(entry.coordinate.git_oid, path)
                value = (
                    None if content is None else _artifact_value(parse_claim(content, path=path))
                )
                revisions.append(
                    _revision(
                        instance,
                        history,
                        index=len(revisions) + 1,
                        sequence=entry.sequence,
                        digest=entry.artifact_digest,
                        lifecycle=entry.lifecycle_state,
                        value=value,
                    )
                )
        elif resolved.kind == "subject":
            subject_entries = service_playbill_subject_history(instance, identity=resolved.identity)
            for subject_entry in (
                item for item in subject_entries.entries if item.sequence <= cutoff
            ):
                revisions.append(
                    _revision(
                        instance,
                        history,
                        index=len(revisions) + 1,
                        sequence=subject_entry.sequence,
                        digest=subject_entry.artifact_digest,
                        lifecycle=subject_entry.lifecycle_state,
                    )
                )
        elif resolved.kind == "document":
            document_entries = service_playbill_document_history(
                instance, identity=resolved.identity
            )
            for document_entry in (
                item for item in document_entries.entries if item.sequence <= cutoff
            ):
                revisions.append(
                    _revision(
                        instance,
                        history,
                        index=len(revisions) + 1,
                        sequence=document_entry.sequence,
                        digest=document_entry.envelope_digest,
                        lifecycle=None,
                        value={"body": _short_digest(document_entry.body_digest)},
                    )
                )
        else:
            # Definitions keep one version per accepted digest of their identity.
            for location in history.occurrences(resolved.identity):
                if any(
                    item.digest == _short_digest(location.artifact_digest) for item in revisions
                ):
                    continue
                revisions.append(
                    _revision(
                        instance,
                        history,
                        index=len(revisions) + 1,
                        sequence=location.occurrence_sequence,
                        digest=location.artifact_digest,
                        lifecycle=None,
                    )
                )
    return PlaybillGetHistoryV1(revisions=tuple(revisions))


# -- body ------------------------------------------------------------------------------


def _body(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    requested: PlaybillByteRangeV1 | None,
    access: BodyAccessContext,
    surface: PlaybillReadSurface,
) -> PlaybillGetBodyV1:
    from cruxible_core.service.authoring.documents import service_dereference_playbill_document

    read = service_dereference_playbill_document(
        instance,
        identity=resolved.identity,
        access=access,
        at=AcceptedCoordinate.from_internal(coordinate),
    )
    content = base64.b64decode(read.content_base64)
    size = len(content)
    if requested is None:
        if size > GET_BODY_DEFAULT_MAX_BYTES:
            window = f"0:{GET_BODY_DEFAULT_MAX_BYTES}"
            line = (
                f"Run {_render_get(surface, resolved.display, 'body', window=window)}, "
                "then read the following ranges"
            )
            raise ReadRefusalError(
                "playbill.get.body_too_large",
                f"{resolved.display} is {size} bytes, over the {GET_BODY_DEFAULT_MAX_BYTES}-byte "
                "whole-body cap",
                repair=RepairOperationV1(
                    operation="playbill.get",
                    arguments={"ref": resolved.display, "detail": "body", "range": window},
                ),
                repair_line=line,
                context={"size": size, "cap": GET_BODY_DEFAULT_MAX_BYTES},
            )
        window_range: PlaybillByteRangeV1 | None = (
            PlaybillByteRangeV1(start=0, end=size) if size else None
        )
    else:
        # An empty Document has no bytes to range over: a range from 0 reads
        # its empty body, and any later start is out of bounds.
        if requested.start > 0 and requested.start >= size:
            arguments: dict[str, object] = {"ref": resolved.display, "detail": "body"}
            if size:
                arguments["range"] = f"0:{size}"
            raise ReadRefusalError(
                "playbill.get.range_out_of_bounds",
                f"range starts at {requested.start} but {resolved.display} is {size} bytes",
                repair=RepairOperationV1(operation="playbill.get", arguments=arguments),
                repair_line=(
                    f"Pass a range inside 0:{size}" if size else "Omit range; the body is empty"
                ),
                context={"size": size},
            )
        window_range = (
            PlaybillByteRangeV1(start=requested.start, end=min(requested.end, size))
            if size
            else None
        )
    chunk = b"" if window_range is None else content[window_range.start : window_range.end]
    text: str | None = None
    encoded: str | None = None
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        encoded = base64.b64encode(chunk).decode("ascii")
    return PlaybillGetBodyV1(
        document=_name(resolved.identity),
        media_type=read.media_type,
        size=size,
        range=window_range,
        text=text,
        content_base64=encoded,
    )


# -- proof and why ----------------------------------------------------------------------


def _proof(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    access: BodyAccessContext,
) -> dict[str, Any]:
    at = AcceptedCoordinate.from_internal(coordinate)
    name = _name(resolved.identity) if resolved.kind != "proposal" else resolved.identity
    if resolved.kind == "claim":
        from cruxible_core.service.claims.claims import service_get_playbill_claim

        return service_get_playbill_claim(
            instance, identity=resolved.identity, at=at, evaluation_time=evaluation_time
        ).model_dump(mode="json")
    if resolved.kind == "subject":
        from cruxible_core.service.claims.subjects import service_get_playbill_subject

        return service_get_playbill_subject(instance, identity=resolved.identity, at=at).model_dump(
            mode="json"
        )
    if resolved.kind == "claim_type":
        from cruxible_core.service.claims.claim_types import service_get_playbill_claim_type

        return service_get_playbill_claim_type(instance, predicate=name, at=at).model_dump(
            mode="json"
        )
    if resolved.kind == "document":
        from cruxible_core.service.authoring.documents import service_get_playbill_document

        return service_get_playbill_document(
            instance, identity=resolved.identity, access=access, at=at
        ).model_dump(mode="json")
    if resolved.kind == "query":
        from cruxible_core.service.discovery.query_definitions import (
            service_get_playbill_query_definition,
        )

        return service_get_playbill_query_definition(instance, name=name, at=at).model_dump(
            mode="json"
        )
    if resolved.kind == "procedure":
        from cruxible_core.service.procedures.procedure_runs import (
            ProcedureReadinessRequestV1,
            service_playbill_procedure_readiness,
        )

        return service_playbill_procedure_readiness(
            instance,
            name=name,
            request=ProcedureReadinessRequestV1(at=at, evaluation_time=evaluation_time),
        ).model_dump(mode="json")
    if resolved.kind == "capture_contract":
        with instance.bind_accepted_projection(coordinate) as projection:
            contract = cast(CaptureContractV1, projection.typed.source(resolved.identity))
            row = projection.typed.envelope(resolved.identity)
        return {
            "coordinate": at.model_dump(mode="json"),
            "path": row.path if row else None,
            "artifact_digest": row.artifact_digest if row else None,
            "envelope": contract.model_dump(mode="json"),
        }
    # Proposals are operational: the status entry and the retained records.
    return {
        name: None if value is None else value.model_dump(mode="json")
        for name, value in _proposal_records(instance, resolved.identity).items()
    }


def _why(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    access: BodyAccessContext,
) -> dict[str, Any]:
    at = AcceptedCoordinate.from_internal(coordinate)
    if resolved.kind == "claim":
        from cruxible_core.service.claims.claims import service_explain_playbill_claim

        return service_explain_playbill_claim(
            instance, identity=resolved.identity, at=at, evaluation_time=evaluation_time
        ).model_dump(mode="json")
    from cruxible_core.service.discovery.explain import service_explain_playbill_subject

    return service_explain_playbill_subject(
        instance,
        subject=SemanticAddress.whole_artifact(
            resolved.path or f"subjects/{_name(resolved.identity)}.json"
        ),
        at=at,
        detail="summary",
        access=access,
    ).model_dump(mode="json")


# -- entry point ----------------------------------------------------------------------------


def service_playbill_get(
    instance: PlaybillInstance,
    *,
    request: PlaybillGetRequestV1,
    access: BodyAccessContext,
) -> PlaybillGetResultV1:
    """Resolve one reference and answer it at one ``detail`` level."""

    coordinate = resolve_read_coordinate(instance, request.at)
    evaluation_time = request.evaluation_time or utc_now()
    resolved = resolve_get_ref(instance, coordinate, request.ref, surface=request.surface)
    if resolved.kind == "proposal":
        # Proposals are operational state, read only as of the current head;
        # one response never mixes an older requested generation with it.
        head = instance.accepted_coordinate()
        if request.at is not None and coordinate.git_oid != head.git_oid:
            raise ReadRefusalError(
                "playbill.get.historical_read_unsupported",
                f"{resolved.display} is operational state, readable only at the current "
                "head, not at an earlier accepted generation",
                repair=RepairOperationV1(
                    operation="playbill.get", arguments={"ref": resolved.display}
                ),
                repair_line="Omit at to read the proposal as of the current head",
                context={"ref": resolved.display, "kind": resolved.kind},
            )
        coordinate = head
    allowed = GET_DETAILS_BY_KIND[resolved.kind]
    if request.detail not in allowed:
        raise ReadRefusalError(
            "playbill.get.detail_unsupported",
            f'detail="{request.detail}" does not apply to a {resolved.kind.replace("_", " ")}; '
            f"it takes {', '.join(allowed)}",
            repair=RepairOperationV1(
                operation="playbill.get", arguments={"ref": resolved.display, "detail": "summary"}
            ),
            repair_line=f"Run {_render_get(request.surface, resolved.display)}",
            context={"ref": resolved.display, "kind": resolved.kind, "allowed": list(allowed)},
        )
    card: PlaybillGetCardV1 | None = None
    fields: dict[str, Any] = {}
    surface = request.surface
    if request.detail == "summary":
        if resolved.kind == "claim":
            card = _claim_card(
                instance, coordinate, resolved, evaluation_time=evaluation_time, surface=surface
            )
        elif resolved.kind == "subject":
            card = _subject_card(
                instance, coordinate, resolved, evaluation_time=evaluation_time, surface=surface
            )
        elif resolved.kind == "claim_type":
            card = _claim_type_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "document":
            card = _document_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "procedure":
            card = _procedure_card(
                instance, coordinate, resolved, evaluation_time=evaluation_time, surface=surface
            )
        elif resolved.kind == "query":
            card = _query_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "capture_contract":
            card = _capture_contract_card(instance, coordinate, resolved, surface=surface)
        else:
            card = _proposal_card(instance, resolved, surface=surface)
        fields["card"] = card
    elif request.detail == "evidence":
        fields["evidence"] = _claim_evidence(
            instance, coordinate, resolved, evaluation_time=evaluation_time
        )
    elif request.detail == "why":
        fields["why"] = _why(
            instance, coordinate, resolved, evaluation_time=evaluation_time, access=access
        )
    elif request.detail == "history":
        fields["history"] = _history(instance, coordinate, resolved)
    elif request.detail == "proof":
        fields["proof"] = _proof(
            instance, coordinate, resolved, evaluation_time=evaluation_time, access=access
        )
    else:
        fields["body"] = _body(
            instance,
            coordinate,
            resolved,
            requested=request.range,
            access=access,
            surface=surface,
        )
    return PlaybillGetResultV1(
        ref=resolved.display,
        kind=resolved.kind,
        detail=request.detail,
        coordinate=ClientCoordinate.model_validate(
            AcceptedCoordinate.from_internal(coordinate).model_dump(mode="json")
        ),
        evaluation_time=evaluation_time,
        **fields,
    )


__all__ = [
    "CaptureContractNames",
    "ResolvedRef",
    "resolve_get_ref",
    "resolve_read_coordinate",
    "service_playbill_get",
    "unsure_held_claims",
    "verdict_flags",
]
