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
import functools
import json
import re
import shlex
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from cruxible_client.contracts import PlaybillAcceptedCoordinate as ClientCoordinate
from cruxible_client.contracts.captures import (
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
    GET_HISTORY_DEFAULT_LIMIT,
    PlaybillByteRangeV1,
    PlaybillGetAttestationEvidenceV1,
    PlaybillGetBodyV1,
    PlaybillGetCaptureContractCardV1,
    PlaybillGetCaptureEvidenceV1,
    PlaybillGetCardV1,
    PlaybillGetClaimCardV1,
    PlaybillGetClaimTypeCardV1,
    PlaybillGetContenderV1,
    PlaybillGetCoordinateV1,
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
    summary_value,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.exact_content import ExactContentReader
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.read_flags import (
    answer_flags,
    ordered_flags,
    unsure_holds,
    verdict_flags,
)
from cruxible_core.service.list_pages import (
    ListContinuation,
    PlaybillListCursorMismatch,
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)
from cruxible_core.service.read_refusals import (
    ReadRefusalError,
    nearest,
    resolve_read_coordinate,
)
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


def _live_predicates(projection: Any) -> frozenset[str]:
    """Every live accepted predicate: the vocabulary field names are shortened against."""

    return frozenset(
        str(identity).removeprefix("ClaimType:")
        for (identity,) in projection.typed.connection.execute(
            "SELECT identity FROM claim_types WHERE lifecycle='live'"
        )
    )


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


def _shown(
    obj: object, value: Callable[[], object], content: ExactContentReader
) -> tuple[object, str | None]:
    """A value as a card shows it, with an exact-content value's digest beside its text."""

    if isinstance(obj, ExactContentClaimObject):
        return content.of(obj), obj.content_digest
    return value(), None


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
    content: ExactContentReader,
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
        accepted_predicates = _live_predicates(projection)
        contenders = tuple(
            cast(ClaimArtifactAny, projection.typed.source(f"Claim:{item.claim_id}"))
            for item in slot
            if item.claim_id != claim.identity.name
        )
    held = unsure_holds(
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
    value, content_digest = _shown(statement.object, lambda: _artifact_value(claim), content)
    contender_values = {
        item.claim_id: _shown(item.object, functools.partial(_claim_value, item), content)
        for item in slot
        if item.claim_id != name
    }
    return PlaybillGetClaimCardV1(
        claim=name,
        subject=_subject_ref(subject_path),
        predicate=short_field_name(statement.predicate, subject_kind, accepted_predicates),
        predicate_full=statement.predicate,
        qualifier=statement.qualifier,
        value=summary_value(value),
        content_digest=content_digest,
        verdict=verdict,
        status=status,
        revision=int(row.revision) if row is not None else 1,
        accepted=accepted,
        contenders=tuple(
            PlaybillGetContenderV1(
                claim=item.claim_id,
                value=summary_value(contender_values[item.claim_id][0]),
                content_digest=contender_values[item.claim_id][1],
                verdict=item.verdict,
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
    content: ExactContentReader,
) -> PlaybillGetSubjectCardV1:
    subject = _name(resolved.identity)
    kind = subject.split("/", 1)[0]
    path = resolved.path or f"subjects/{subject}.json"
    rows = _slot_values(instance, coordinate, subject_path=path, evaluation_time=evaluation_time)
    with instance.bind_accepted_projection(coordinate) as projection:
        shell = cast(SubjectShell, projection.typed.source(resolved.identity))
        accepted_predicates = _live_predicates(projection)
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
    held = unsure_holds(
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
        # Distinctness is judged on the accepted values (an exact-content value's
        # digest), before any is shown as text.
        marks: set[PlaybillReadFlag] = set(
            answer_flags(
                "many" if many else "one", len({repr(_claim_value(item)) for item in shown})
            )
        )
        pairs = [
            _shown(item.object, functools.partial(_claim_value, item), content) for item in shown
        ]
        values = [value for value, _digest in pairs]
        digests = tuple(digest for _value, digest in pairs if digest is not None)
        for item in shown:
            marks.update(
                verdict_flags(item.verdict, item.status, held=f"Claim:{item.claim_id}" in held)
            )
        listed = many or len(values) > 1
        claims_shown = tuple(item.claim_id for item in shown)
        entries.append(
            PlaybillGetSubjectClaimV1(
                predicate=short_field_name(predicate, kind, accepted_predicates),
                qualifier=qualifier,
                claim=claims_shown if listed else claims_shown[0],
                value=summary_value(values if listed else values[0]),
                content_digest=(digests if listed else digests[0]) if digests else None,
                flags=tuple(ordered_flags(marks)),
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
    evidence = CaptureContractNames(instance, coordinate).admitted(claim_type, qualified=True)
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
            if item.lifecycle.state == "live"
            and resolved.identity in names.admitted(item, qualified=True)
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
    content: ExactContentReader,
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
    with instance.bind_accepted_projection(coordinate) as projection:
        claim = cast(ClaimArtifactAny, projection.typed.source(resolved.identity))
    value, content_digest = _shown(claim.statement.object, lambda: _artifact_value(claim), content)
    return PlaybillGetEvidenceV1(
        value=value,
        content_digest=content_digest,
        captures=tuple(captures),
        attestations=attestations,
        rationale=_claim_rationale(instance, coordinate, _name(resolved.identity)),
    )


# -- history -------------------------------------------------------------------------

_HISTORY_LIST = "get history"


@dataclass(frozen=True)
class _RevisionEntry:
    """One accepted revision, located but not yet read."""

    revision: int
    sequence: int
    digest: str
    lifecycle: str | None
    # The revision's value and, for exact content, its digest.
    value: Callable[[], tuple[object, str | None]] | None = None


def _revision(
    instance: PlaybillInstance,
    history: Any,
    entry: _RevisionEntry,
) -> PlaybillGetRevisionV1:
    generation = history.generation(entry.sequence)
    record = history.read_generation_record(entry.sequence, instance.blob_at)
    value, content_digest = (None, None) if entry.value is None else entry.value()
    return PlaybillGetRevisionV1(
        revision=entry.revision,
        sequence=entry.sequence,
        accepted=str(record.candidate.timestamp),
        actor=generation.actor_id or record.actor_binding.actor_id,
        approved_by=tuple(dict.fromkeys(item.attestation.signer_id for item in record.approvals)),
        lifecycle=entry.lifecycle,
        value=value,
        content_digest=content_digest,
        digest=_short_digest(entry.digest),
    )


def _history_entries(
    instance: PlaybillInstance,
    resolved: ResolvedRef,
    history: Any,
    content: ExactContentReader,
) -> list[_RevisionEntry]:
    """Every accepted revision up to the read coordinate, oldest first."""

    from cruxible_core.service.authoring.documents import service_playbill_document_history
    from cruxible_core.service.claims.claims import service_playbill_claim_history
    from cruxible_core.service.claims.subjects import service_playbill_subject_history

    cutoff = history.sequence
    entries: list[_RevisionEntry] = []
    if resolved.kind == "claim":
        path = claim_path(_name(resolved.identity))

        def claim_value(git_oid: str) -> Callable[[], tuple[object, str | None]]:
            def read() -> tuple[object, str | None]:
                member = instance.blob_at(git_oid, path)
                if member is None:
                    return None, None
                claim = parse_claim(member, path=path)
                return _shown(claim.statement.object, lambda: _artifact_value(claim), content)

            return read

        for claim_entry in service_playbill_claim_history(
            instance, identity=resolved.identity
        ).entries:
            if claim_entry.sequence <= cutoff:
                entries.append(
                    _RevisionEntry(
                        revision=len(entries) + 1,
                        sequence=claim_entry.sequence,
                        digest=claim_entry.artifact_digest,
                        lifecycle=claim_entry.lifecycle_state,
                        value=claim_value(claim_entry.coordinate.git_oid),
                    )
                )
    elif resolved.kind == "subject":
        for subject_entry in service_playbill_subject_history(
            instance, identity=resolved.identity
        ).entries:
            if subject_entry.sequence <= cutoff:
                entries.append(
                    _RevisionEntry(
                        revision=len(entries) + 1,
                        sequence=subject_entry.sequence,
                        digest=subject_entry.artifact_digest,
                        lifecycle=subject_entry.lifecycle_state,
                    )
                )
    elif resolved.kind == "document":
        for document_entry in service_playbill_document_history(
            instance, identity=resolved.identity
        ).entries:
            if document_entry.sequence <= cutoff:
                body: object = {"body": _short_digest(document_entry.body_digest)}
                entries.append(
                    _RevisionEntry(
                        revision=len(entries) + 1,
                        sequence=document_entry.sequence,
                        digest=document_entry.envelope_digest,
                        lifecycle=None,
                        value=functools.partial(lambda value: (value, None), body),
                    )
                )
    else:
        # Definitions keep one version per accepted digest of their identity.
        seen: set[str] = set()
        for location in history.occurrences(resolved.identity):
            if location.artifact_digest in seen:
                continue
            seen.add(location.artifact_digest)
            entries.append(
                _RevisionEntry(
                    revision=len(entries) + 1,
                    sequence=location.occurrence_sequence,
                    digest=location.artifact_digest,
                    lifecycle=None,
                )
            )
    return entries


def _history(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    ref: str,
    limit: int,
    continuation: ListContinuation | None,
    content: ExactContentReader,
) -> tuple[PlaybillGetHistoryV1, bool, str | None]:
    """One page of revisions, newest first, and the cursor that continues it."""

    at = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=at) as history:
        newest_first = list(reversed(_history_entries(instance, resolved, history, content)))
        keys = [(str(entry.sequence), entry.digest) for entry in newest_first]
        snapshot = list_snapshot(keys)
        page, truncated = page_after_boundary(
            newest_first,
            keys=keys,
            snapshot=snapshot,
            continuation=continuation,
            limit=limit,
            list_name=_HISTORY_LIST,
        )
        revisions = tuple(_revision(instance, history, entry) for entry in page)
    next_cursor = (
        encode_list_cursor(
            list_name=_HISTORY_LIST,
            coordinate=at.model_dump(mode="json"),
            selection={"ref": ref},
            snapshot=snapshot,
            last_key=(str(page[-1].sequence), page[-1].digest),
        )
        if truncated and page
        else None
    )
    return PlaybillGetHistoryV1(revisions=revisions), truncated, next_cursor


def _history_continuation(
    request: PlaybillGetRequestV1,
) -> tuple[ListContinuation | None, ClientCoordinate | str | None]:
    """The page a history cursor continues, pinned to the coordinate it was cut at."""

    if request.cursor is None:
        return None, request.at
    continuation = decode_list_cursor(
        request.cursor, list_name=_HISTORY_LIST, selection={"ref": request.ref}
    )
    pinned = ClientCoordinate.model_validate(continuation.coordinate)
    at = request.at
    if at is not None and (
        at != pinned.git_oid
        if isinstance(at, str)
        else at.model_dump(mode="json") != pinned.model_dump(mode="json")
    ):
        raise PlaybillListCursorMismatch(
            f"{PlaybillListCursorMismatch.error_code}: the cursor continues a different "
            "coordinate; omit at to continue it, or read history again without a cursor"
        )
    return continuation, pinned


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
    content_access: BodyAccessContext | None = None,
) -> PlaybillGetResultV1:
    """Resolve one reference and answer it at one ``detail`` level.

    ``content_access`` reads exact-content Claim values as text (``access`` when
    not given); without body access they show as a ``withheld`` marker.
    """

    continuation, at = _history_continuation(request)
    coordinate = resolve_read_coordinate(instance, at)
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
    content = ExactContentReader(instance, access if content_access is None else content_access)
    if request.detail == "summary":
        if resolved.kind == "claim":
            card = _claim_card(
                instance,
                coordinate,
                resolved,
                evaluation_time=evaluation_time,
                surface=surface,
                content=content,
            )
        elif resolved.kind == "subject":
            card = _subject_card(
                instance,
                coordinate,
                resolved,
                evaluation_time=evaluation_time,
                surface=surface,
                content=content,
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
            instance, coordinate, resolved, evaluation_time=evaluation_time, content=content
        )
    elif request.detail == "why":
        fields["why"] = _why(
            instance, coordinate, resolved, evaluation_time=evaluation_time, access=access
        )
    elif request.detail == "history":
        fields["history"], fields["truncated"], fields["next_cursor"] = _history(
            instance,
            coordinate,
            resolved,
            ref=request.ref,
            limit=request.limit or GET_HISTORY_DEFAULT_LIMIT,
            continuation=continuation,
            content=content,
        )
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
    served = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=served) as history:
        generation = int(history.sequence)
    if request.detail == "proof" or request.full_coordinate:
        fields["accepted_coordinate"] = ClientCoordinate.model_validate(
            served.model_dump(mode="json")
        )
    return PlaybillGetResultV1(
        ref=resolved.display,
        kind=resolved.kind,
        detail=request.detail,
        coordinate=PlaybillGetCoordinateV1(git_oid=served.git_oid[:12], generation=generation),
        evaluation_time=evaluation_time,
        **fields,
    )


__all__ = [
    "ResolvedRef",
    "resolve_get_ref",
    "service_playbill_get",
]
