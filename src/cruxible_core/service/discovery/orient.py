"""The orient read: a bounded map of accepted state for a caller about to act.

``orient()`` names every Subject kind with its live Subject count and compact
predicate descriptors, counts each artifact family, lists the named queries,
says who the caller is and whether it can author, summarizes what the ``next``
queue holds, and suggests runnable follow-up calls rendered for the caller's
surface. ``orient(kind=K)`` widens one kind to every predicate in full plus a
few sample Subject IDs; ``orient(section=S)`` pages one artifact family.

Everything here is read from the accepted index at one coordinate. The
attention summary is the existing ``next`` service's answer, called rather than
re-derived, and evidence is named by CaptureContract identity: v6 rules name
contracts by reference, and a v5 rule's digests are resolved through accepted
state, so a digest is shown only when no accepted contract carries it.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from cruxible_client import contracts
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.orient import (
    PLAYBILL_ORIENT_ATTENTION_TOP,
    PLAYBILL_ORIENT_DEFAULT_LIMIT,
    PLAYBILL_ORIENT_DEFAULT_QUERIES,
    PLAYBILL_ORIENT_SAMPLE_SUBJECTS,
    PlaybillOrientArtifactCountsV1,
    PlaybillOrientAttentionV1,
    PlaybillOrientDocumentV1,
    PlaybillOrientKindDetailV1,
    PlaybillOrientKindV1,
    PlaybillOrientPredicateV1,
    PlaybillOrientProcedureV1,
    PlaybillOrientQueryV1,
    PlaybillOrientResultV1,
    PlaybillOrientSection,
    PlaybillOrientSurface,
    PlaybillOrientYouV1,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_core.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.next import (
    PlaybillNextItemV1,
    PlaybillNextRequestV2,
    service_playbill_next,
)
from cruxible_core.service.list_pages import (
    ListContinuation,
    PlaybillListCursorMismatch,
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from cruxible_core.service.read_refusals import (
    ReadRefusalError,
    nearest,
    resolve_read_coordinate,
)

_LIST = "orient"
_NEXT_PROFILE = CoverageAccessProfileV1(
    profile_id="orient", permitted_access_classes=("instance", "public")
)
_UPGRADE_MARKERS = ("evidence_rules_upgrade", "upgrade-evidence-rules", "evidence-rules")


def _request_invalid(message: str) -> ReadRefusalError:
    return ReadRefusalError(
        "playbill.orient.request_invalid",
        message,
        repair=RepairOperationV1(operation="playbill.orient"),
    )


def _kind_not_found(kind: str, known: Iterable[str]) -> ReadRefusalError:
    return ReadRefusalError(
        "playbill.orient.kind_not_found",
        f"no accepted Subject or ClaimType has kind {kind!r}",
        http_status=404,
        candidates=nearest(kind, known),
        repair=RepairOperationV1(operation="playbill.orient"),
        repair_line="Use one of these kinds; orient without a kind lists every kind",
        context={"kind": kind},
    )


@dataclass(frozen=True)
class OrientCaller:
    """Who is asking, as the transport authenticated it (whoami's answer)."""

    actor_id: str | None
    principal_registration_status: Literal["active", "revoked", "absent"] | None
    credential_permission_mode: str | None


# -- reading accepted state -------------------------------------------------


@dataclass(frozen=True)
class _State:
    """Everything one orient answer reads from the accepted index."""

    claim_types: tuple[ClaimType, ...]
    subjects_by_kind: dict[str, int]
    evidence: dict[str, tuple[str, ...]]
    digest_named: int
    procedures: tuple[PlaybillOrientProcedureV1, ...]
    documents: tuple[PlaybillOrientDocumentV1, ...]
    queries: tuple[PlaybillOrientQueryV1, ...]


def _query_row(query: QueryDefinitionV1) -> PlaybillOrientQueryV1:
    return PlaybillOrientQueryV1(
        name=query.identity.name,
        description=query.description,
        params=tuple(
            f"{param.name}{'' if param.required else '?'}: {param.value_type}"
            for param in query.parameters
        ),
    )


def _read_state(instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate) -> _State:
    with instance.bind_accepted_projection(coordinate) as projection:
        typed = projection.typed
        connection = typed.connection
        names = CaptureContractNames(instance, coordinate, connection=connection)
        claim_types: list[ClaimType] = []
        evidence: dict[str, tuple[str, ...]] = {}
        digest_named = 0
        for (identity,) in connection.execute(
            "SELECT identity FROM claim_types WHERE lifecycle='live' ORDER BY identity"
        ).fetchall():
            claim_type = typed.source(str(identity))
            if not isinstance(claim_type, ClaimType):
                continue
            claim_types.append(claim_type)
            evidence[claim_type.predicate] = names.admitted(claim_type)
            digest_named += names.names_by_digest(claim_type)
        subjects_by_kind = {
            str(kind): int(count)
            for kind, count in connection.execute(
                "SELECT subject_kind, count(*) FROM subjects WHERE lifecycle='live' "
                "GROUP BY subject_kind"
            )
        }
        procedures = tuple(
            PlaybillOrientProcedureV1(
                name=item.identity.removeprefix("Procedure:"),
                lifecycle="retired" if item.lifecycle == "retired" else "live",
                runnable="directly_runnable" if item.directly_runnable else "binding_required",
            )
            for item in sorted(typed.procedure_inventory(), key=lambda item: item.identity)
        )
        documents = tuple(
            PlaybillOrientDocumentV1(
                name=str(identity).removeprefix("document:"),
                title=str(title),
                document_kind=str(document_kind),
                media_type=str(media_type),
            )
            for identity, title, document_kind, media_type in connection.execute(
                "SELECT identity, title, document_kind, media_type FROM documents ORDER BY identity"
            )
        )
        queries: list[PlaybillOrientQueryV1] = []
        for row in typed.envelopes(kind="query-definition"):
            query = typed.source(row.identity)
            if isinstance(query, QueryDefinitionV1) and query.lifecycle.state == "live":
                queries.append(_query_row(query))
    return _State(
        claim_types=tuple(claim_types),
        subjects_by_kind=subjects_by_kind,
        evidence=evidence,
        digest_named=digest_named,
        procedures=procedures,
        documents=documents,
        queries=tuple(sorted(queries, key=lambda item: item.name)),
    )


# -- predicate descriptors ----------------------------------------------------


def _value_type(
    claim_type: ClaimType,
) -> tuple[str, tuple[str | int | float | bool | None, ...] | None]:
    if claim_type.object_kind == "subject":
        kinds = claim_type.allowed_object_subject_kinds
        return ("subject:" + ",".join(kinds) if kinds else "subject"), None
    if claim_type.object_kind == "exact_content":
        return "exact_content", None
    schema: Mapping[str, Any] = claim_type.literal_schema or {}
    if "enum" in schema or "const" in schema:
        raw = schema["enum"] if "enum" in schema else [schema["const"]]
        members = tuple(
            item if item is None or isinstance(item, (str, int, float, bool)) else json.dumps(item)
            for item in (raw if isinstance(raw, list) else [raw])
        )
        return "enum", members
    declared = schema.get("type")
    fmt = schema.get("format")
    if declared == "string" and fmt == "date":
        return "date", None
    if declared == "string" and fmt == "date-time":
        return "datetime", None
    if isinstance(declared, list):
        return "|".join(str(item) for item in declared), None
    return (str(declared) if declared is not None else "any"), None


def _duration(microseconds: int) -> str:
    for unit, size in (("d", 86_400_000_000), ("h", 3_600_000_000), ("m", 60_000_000)):
        if microseconds and microseconds % size == 0:
            return f"{microseconds // size}{unit}"
    if microseconds % 1_000_000 == 0:
        return f"{microseconds // 1_000_000}s"
    return f"{microseconds}us"


def _short_names(predicates: Iterable[str], kind: str, state: _State) -> dict[str, str]:
    """Each predicate's advertised field name for ``kind``, by the shared rule."""

    accepted = {item.predicate for item in state.claim_types}
    return {predicate: short_field_name(predicate, kind, accepted) for predicate in predicates}


def _descriptor(
    claim_type: ClaimType,
    *,
    name: str,
    evidence: tuple[str, ...],
    full: bool,
    live_claims: int | None = None,
) -> PlaybillOrientPredicateV1:
    value_type, members = _value_type(claim_type)
    freshness = claim_type.evidence_freshness
    return PlaybillOrientPredicateV1(
        name=name,
        predicate=claim_type.predicate,
        cardinality=claim_type.cardinality,
        type=value_type,
        members=members,
        evidence=evidence,
        subject_kinds=claim_type.allowed_subject_kinds if full else None,
        roles=tuple(claim_type.permitted_roles) if full else None,
        stale_after=(
            _duration(freshness.stale_after.microseconds)
            if full and freshness is not None
            else None
        ),
        live_claims=live_claims if full else None,
    )


def _kind_names(state: _State) -> tuple[str, ...]:
    kinds = set(state.subjects_by_kind)
    for claim_type in state.claim_types:
        kinds.update(claim_type.allowed_subject_kinds)
    return tuple(sorted(kinds, key=lambda item: item.encode("utf-8")))


def _kind_row(state: _State, kind: str) -> PlaybillOrientKindV1:
    of_kind = [item for item in state.claim_types if kind in item.allowed_subject_kinds]
    names = _short_names((item.predicate for item in of_kind), kind, state)
    predicates = sorted(
        (
            _descriptor(
                item,
                name=names[item.predicate],
                evidence=state.evidence.get(item.predicate, ()),
                full=False,
            )
            for item in of_kind
        ),
        key=lambda item: item.name,
    )
    shared = {item.evidence for item in predicates}
    evidence: tuple[str, ...] = ()
    if len(shared) == 1 and len(predicates) > 1:
        # Every predicate admits the same contracts: name them once, on the kind.
        (evidence,) = shared
        predicates = [item.model_copy(update={"evidence": ()}) for item in predicates]
    return PlaybillOrientKindV1(
        kind=kind,
        subjects=state.subjects_by_kind.get(kind, 0),
        evidence=evidence,
        predicates=tuple(predicates),
    )


# -- next suggestions, rendered per surface ---------------------------------


@dataclass(frozen=True)
class _Call:
    verb: Literal["orient", "query", "get", "next", "evidence_rules_upgrade"]
    args: tuple[tuple[str, object], ...] = ()


def _py(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _python(value: object) -> str:
    """A Python literal for an SDK suggestion: ``True``/``None``, never JSON's ``true``/``null``.

    Strings keep their double-quoted JSON spelling, which is also a valid Python
    string literal, so SDK and MCP suggestions read alike.
    """

    if isinstance(value, str):
        return _py(value)
    if isinstance(value, Mapping):
        return (
            "{" + ", ".join(f"{_python(key)}: {_python(item)}" for key, item in value.items()) + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_python(item) for item in value) + "]"
    return repr(value)


def _cli_value(value: object) -> str:
    return shlex.quote(value if isinstance(value, str) else _py(value))


def _cli_where(item: Mapping[str, object]) -> str:
    field = str(item["field"])
    for op, symbol in (("eq", "="), ("ne", "!="), ("lt", "<"), ("gt", ">")):
        if op in item:
            return f"{field}{symbol}{item[op]}"
    return field


def render_orient_call(call: _Call, surface: PlaybillOrientSurface) -> str:
    """One runnable call in the caller's own syntax."""

    args = dict(call.args)
    if surface == "mcp":
        tool = f"cruxible_playbill_{call.verb}"
        return f"{tool}({', '.join(f'{key}={_py(value)}' for key, value in call.args)})"
    if surface == "sdk":
        if call.verb == "evidence_rules_upgrade":
            return "client.upgrade_playbill_evidence_rules(instance_id)"
        if call.verb == "next":
            return "pb.next(expiring_within=Duration.days(count=7))"
        if call.verb == "get":
            return f"pb.get({_python(args['ref'])})"
        return (
            f"pb.{call.verb}("
            + ", ".join(f"{key}={_python(value)}" for key, value in call.args)
            + ")"
        )
    # cli
    if call.verb == "evidence_rules_upgrade":
        return "cruxible playbill claim-type upgrade-evidence-rules"
    if call.verb == "next":
        return "cruxible playbill next"
    if call.verb == "get":
        return f"cruxible playbill get {_cli_value(args['ref'])}"
    parts = ["cruxible", "playbill", call.verb]
    if call.verb == "query" and "kind" in args:
        parts.append(_cli_value(args.pop("kind")))
    for key, value in args.items():
        if key == "where" and isinstance(value, list):
            parts.extend(f"--where {_cli_value(_cli_where(item))}" for item in value)
        elif key == "select" and isinstance(value, list):
            parts.append(f"--select {_cli_value(','.join(str(item) for item in value))}")
        else:
            parts.append(f"--{key.replace('_', '-')} {_cli_value(value)}")
    return " ".join(parts)


def _select_names(kind: PlaybillOrientKindV1) -> list[str]:
    return [item.name for item in kind.predicates[:3]]


def _enum_filter(kind: PlaybillOrientKindV1) -> dict[str, object] | None:
    for item in kind.predicates:
        if item.type == "enum" and item.members and item.cardinality == "one":
            return {"field": item.name, "eq": item.members[0]}
    return None


# -- attention ----------------------------------------------------------------


def _upgrade_hint(items: Sequence[PlaybillNextItemV1]) -> PlaybillNextItemV1 | None:
    for item in items:
        text = " ".join(
            str(part)
            for part in (item.reason, item.repair.command, item.repair.required_change)
            if part is not None
        )
        if any(marker in text for marker in _UPGRADE_MARKERS):
            return item
    return None


_FULL_DIGEST = re.compile(r"sha256:([0-9a-f]{12})[0-9a-f]{52}")


def _line(item: PlaybillNextItemV1) -> str:
    # A full digest is a unique prefix at 12 hex characters, which every
    # proposal selector (and get) accepts; the line stays one short line.
    subject = _FULL_DIGEST.sub(r"sha256:\1", item.subject_identity)
    return f"{item.severity} {item.reason}: {subject}"


def _attention(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    state: _State,
    caller: OrientCaller | None,
    provider_lane: contracts.ProviderLaneStatusV1 | None,
    consumers_running: bool,
) -> tuple[PlaybillOrientAttentionV1, bool]:
    notes: list[str] = []
    terminal = instance.descriptor.decommissioned
    if terminal is not None:
        notes.append(
            f"instance decommissioned at {terminal.decommissioned_at} ({terminal.reason}); "
            "reads serve, every write is refused"
        )
    items: tuple[PlaybillNextItemV1, ...] = ()
    total = 0
    try:
        queue = service_playbill_next(
            instance,
            request=PlaybillNextRequestV2(
                at=AcceptedCoordinate.from_internal(coordinate),
                evaluation_time=evaluation_time,
                access_profile=_NEXT_PROFILE,
                limit=contracts.PLAYBILL_NEXT_MAX_LIMIT,
            ),
            provider_lane=provider_lane,
            consumers_running=consumers_running,
            caller_principal_id=None if caller is None else caller.actor_id,
        )
        items, total = queue.items, queue.total_items
    except PlaybillError as exc:
        code = getattr(exc, "error_code", None) or getattr(exc, "code", None)
        notes.append(f"the next queue could not be read: {code or type(exc).__name__}")
    upgrade = False
    reused = _upgrade_hint(items)
    if reused is not None:
        notes.append(_line(reused))
        upgrade = True
    elif state.digest_named:
        noun = "ClaimType still names" if state.digest_named == 1 else "ClaimTypes still name"
        notes.append(
            f"{state.digest_named} {noun} CaptureContracts by digest; run evidence_rules_upgrade"
        )
        upgrade = True
    open_proposals = len(service_list_playbill_proposals(instance, status="open").entries)
    return (
        PlaybillOrientAttentionV1(
            next_items=total,
            open_proposals=open_proposals,
            top=tuple(_line(item) for item in items[:PLAYBILL_ORIENT_ATTENTION_TOP]),
            notes=tuple(notes),
        ),
        upgrade,
    )


def _you(caller: OrientCaller | None, *, instance: PlaybillInstance) -> PlaybillOrientYouV1:
    terminal = instance.descriptor.decommissioned
    if terminal is not None:
        # Every write door refuses a decommissioned instance, whoever asks.
        active = caller is not None and caller.principal_registration_status == "active"
        return PlaybillOrientYouV1(
            actor=None if caller is None else caller.actor_id,
            principal=caller.actor_id if caller is not None and active else None,
            can_author=False,
            reason=(
                f"the instance was decommissioned at {terminal.decommissioned_at} "
                f"({terminal.reason}); every write is refused"
            ),
        )
    if caller is None or caller.actor_id is None:
        return PlaybillOrientYouV1(
            actor=None,
            can_author=False,
            reason="no authenticated actor; connect with a credential to author",
        )
    registration = caller.principal_registration_status
    if registration != "active":
        return PlaybillOrientYouV1(
            actor=caller.actor_id,
            can_author=False,
            reason=(
                f"actor {caller.actor_id!r} has no active principal "
                f"(registration: {registration or 'unknown'}); an admin registers one "
                "with a principal change"
            ),
        )
    if caller.credential_permission_mode == "read_only":
        return PlaybillOrientYouV1(
            actor=caller.actor_id,
            principal=caller.actor_id,
            can_author=False,
            reason="this credential is read_only; authoring needs governed_write",
        )
    return PlaybillOrientYouV1(actor=caller.actor_id, principal=caller.actor_id, can_author=True)


# -- paging -------------------------------------------------------------------


def _continuation(
    cursor: str | None,
    *,
    view: str,
    at: AcceptedCoordinate | str | None,
) -> tuple[ListContinuation | None, AcceptedCoordinate | str | None]:
    if cursor is None:
        return None, at
    continuation = decode_list_cursor(cursor, list_name=_LIST, selection={"view": view})
    pinned = AcceptedCoordinate.model_validate(continuation.coordinate)
    if at is not None and (at != pinned.git_oid if isinstance(at, str) else at != pinned):
        raise PlaybillListCursorMismatch(
            f"{PlaybillListCursorMismatch.error_code}: the cursor continues a different "
            "coordinate; orient again without a cursor"
        )
    return continuation, pinned


def _page(
    rows: Sequence[Any],
    keys: Sequence[str],
    *,
    view: str,
    served: AcceptedCoordinate,
    continuation: ListContinuation | None,
    limit: int,
) -> tuple[tuple[Any, ...], str | None]:
    """One page of ``rows`` (keyed by ``keys``) and the cursor that continues it."""

    snapshot = list_snapshot(list(keys))
    indices, truncated = page_after_boundary(
        tuple(range(len(rows))),
        keys=tuple((key,) for key in keys),
        snapshot=snapshot,
        continuation=continuation,
        limit=limit,
        list_name=_LIST,
    )
    next_cursor = (
        encode_list_cursor(
            list_name=_LIST,
            coordinate=served.model_dump(mode="json"),
            selection={"view": view},
            snapshot=snapshot,
            last_key=(keys[indices[-1]],),
        )
        if truncated and indices
        else None
    )
    return tuple(rows[index] for index in indices), next_cursor


# -- the verb -------------------------------------------------------------------


def service_playbill_orient(
    instance: PlaybillInstance,
    *,
    kind: str | None = None,
    section: PlaybillOrientSection | None = None,
    limit: int = PLAYBILL_ORIENT_DEFAULT_LIMIT,
    cursor: str | None = None,
    at: AcceptedCoordinate | str | None = None,
    evaluation_time: datetime | None = None,
    surface: PlaybillOrientSurface = "cli",
    caller: OrientCaller | None = None,
    provider_lane: contracts.ProviderLaneStatusV1 | None = None,
    consumers_running: bool = False,
) -> PlaybillOrientResultV1:
    """Answer one orient read at one accepted coordinate."""

    if kind is not None and section is not None:
        raise _request_invalid(
            "pass kind or section, not both; orient(kind=K) reads one kind, "
            "orient(section=S) pages one artifact family"
        )
    if kind is not None and cursor is not None:
        raise _request_invalid("orient(kind=K) is one page and takes no cursor; drop the cursor")
    view = section or ("kind" if kind is not None else "kinds")
    continuation, at = _continuation(cursor, view=view, at=at)
    coordinate = resolve_read_coordinate(instance, at)
    served = AcceptedCoordinate.from_internal(coordinate)
    moment = (evaluation_time or datetime.now(UTC)).astimezone(UTC)
    state = _read_state(instance, coordinate)
    base: dict[str, Any] = {
        "instance": instance.descriptor.instance_id,
        "coordinate": served,
        "generation": next(
            item.sequence for item in instance.accepted_history() if item.oid == coordinate.git_oid
        ),
        "accepted_at": instance.accepted_evaluation_time(coordinate.git_oid),
        "evaluation_time": moment,
        "mirror_url": instance.ledger_mirror_url(),
    }
    calls: list[_Call] = []

    if kind is not None:
        detail = _kind_detail(instance, coordinate, state, kind)
        select = _select_names(detail)
        calls.append(
            _Call("query", (("kind", kind), ("select", select), ("limit", 10)))
            if select
            else _Call("query", (("kind", kind), ("limit", 10)))
        )
        enum_filter = _enum_filter(detail)
        if enum_filter is not None:
            calls.append(_Call("query", (("kind", kind), ("where", [enum_filter]))))
        if detail.sample_subject_ids:
            calls.append(_Call("get", (("ref", f"{kind}/{detail.sample_subject_ids[0]}"),)))
        return PlaybillOrientResultV1(
            **base,
            kind_detail=detail,
            next=tuple(render_orient_call(call, surface) for call in calls),
        )

    if section is not None:
        rows, keys, first_ref = _section_rows(state, section)
        page, next_cursor = _page(
            rows, keys, view=view, served=served, continuation=continuation, limit=limit
        )
        if page:
            calls.append(_Call("get", (("ref", first_ref(page[0])),)))
        if next_cursor is not None:
            calls.append(_Call("orient", (("section", section), ("cursor", next_cursor))))
        base[section] = page
        return PlaybillOrientResultV1(
            **base,
            section=section,
            truncated=next_cursor is not None,
            next_cursor=next_cursor,
            next=tuple(render_orient_call(call, surface) for call in calls),
        )

    kind_rows = tuple(_kind_row(state, name) for name in _kind_names(state))
    kinds_page, next_cursor = _page(
        kind_rows,
        [row.kind for row in kind_rows],
        view=view,
        served=served,
        continuation=continuation,
        limit=limit,
    )
    if continuation is not None:
        # A continuation page carries only the next kinds; the rest was on page one.
        if next_cursor is not None:
            calls.append(_Call("orient", (("cursor", next_cursor),)))
        return PlaybillOrientResultV1(
            **base,
            kinds=kinds_page,
            truncated=next_cursor is not None,
            next_cursor=next_cursor,
            next=tuple(render_orient_call(call, surface) for call in calls),
        )

    attention, upgrade = _attention(
        instance,
        coordinate=coordinate,
        evaluation_time=moment,
        state=state,
        caller=caller,
        provider_lane=provider_lane,
        consumers_running=consumers_running,
    )
    focus = max(kinds_page, key=lambda row: (row.subjects, len(row.predicates)), default=None)
    if focus is not None:
        calls.append(_Call("orient", (("kind", focus.kind),)))
        select = _select_names(focus)
        if select:
            calls.append(_Call("query", (("kind", focus.kind), ("select", select), ("limit", 10))))
    if attention.next_items:
        calls.append(_Call("next"))
    if upgrade:
        calls.append(_Call("evidence_rules_upgrade"))
    if len(state.queries) > PLAYBILL_ORIENT_DEFAULT_QUERIES:
        calls.append(_Call("orient", (("section", "queries"),)))
    if next_cursor is not None:
        calls.append(_Call("orient", (("cursor", next_cursor),)))
    live_procedures = sum(item.lifecycle == "live" for item in state.procedures)
    return PlaybillOrientResultV1(
        **base,
        you=_you(caller, instance=instance),
        kinds=kinds_page,
        artifacts=PlaybillOrientArtifactCountsV1(
            claim_types=len(state.claim_types),
            procedures=live_procedures,
            documents=len(state.documents),
            queries=len(state.queries),
        ),
        queries=state.queries[:PLAYBILL_ORIENT_DEFAULT_QUERIES],
        attention=attention,
        truncated=next_cursor is not None,
        next_cursor=next_cursor,
        next=tuple(render_orient_call(call, surface) for call in calls),
    )


def _kind_detail(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    state: _State,
    kind: str,
) -> PlaybillOrientKindDetailV1:
    known = _kind_names(state)
    if kind not in known:
        raise _kind_not_found(kind, known)
    of_kind = [item for item in state.claim_types if kind in item.allowed_subject_kinds]
    names = _short_names((item.predicate for item in of_kind), kind, state)
    prefix = f"subjects/{kind}/"
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        counts = {
            str(predicate): int(count)
            for predicate, count in connection.execute(
                "SELECT predicate, count(*) FROM claims WHERE lifecycle='live' "
                "AND subject_path >= ? AND subject_path < ? GROUP BY predicate",
                (prefix, prefix[:-1] + "0"),
            )
        }
        samples = tuple(
            str(subject_id)
            for (subject_id,) in connection.execute(
                "SELECT subject_id FROM subjects WHERE subject_kind=? AND lifecycle='live' "
                "ORDER BY subject_id LIMIT ?",
                (kind, PLAYBILL_ORIENT_SAMPLE_SUBJECTS),
            )
        )
    predicates = sorted(
        (
            _descriptor(
                item,
                name=names[item.predicate],
                evidence=state.evidence.get(item.predicate, ()),
                full=True,
                live_claims=counts.get(item.predicate, 0),
            )
            for item in of_kind
        ),
        key=lambda item: item.name,
    )
    return PlaybillOrientKindDetailV1(
        kind=kind,
        subjects=state.subjects_by_kind.get(kind, 0),
        predicates=tuple(predicates),
        sample_subject_ids=samples,
    )


def _section_rows(
    state: _State, section: PlaybillOrientSection
) -> tuple[tuple[Any, ...], list[str], Any]:
    if section == "documents":
        return (
            state.documents,
            [item.name for item in state.documents],
            lambda row: f"Document:{row.name}",
        )
    if section == "procedures":
        return (
            state.procedures,
            [item.name for item in state.procedures],
            lambda row: f"Procedure:{row.name}",
        )
    if section == "queries":
        return (
            state.queries,
            [item.name for item in state.queries],
            lambda row: f"query:{row.name}",
        )
    accepted = {item.predicate for item in state.claim_types}
    rows = tuple(
        _descriptor(
            item,
            # One subject kind: its advertised field name; otherwise the predicate.
            name=(
                short_field_name(item.predicate, item.allowed_subject_kinds[0], accepted)
                if len(item.allowed_subject_kinds) == 1
                else item.predicate
            ),
            evidence=state.evidence.get(item.predicate, ()),
            full=True,
        )
        for item in sorted(state.claim_types, key=lambda item: item.predicate)
    )
    return rows, [row.predicate for row in rows], lambda row: f"ClaimType:{row.predicate}"


__all__ = [
    "OrientCaller",
    "render_orient_call",
    "service_playbill_orient",
]
