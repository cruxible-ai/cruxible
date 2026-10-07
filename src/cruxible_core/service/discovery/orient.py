"""The orient read: a bounded map of accepted state for a caller about to act.

``orient()`` names every Subject kind with its live Subject count and compact
predicate descriptors, counts each artifact family, lists the named queries,
says who the caller is and whether it can author, summarizes what the ``next``
queue holds, and suggests runnable follow-up calls rendered for the caller's
surface. ``orient(kind=K)`` widens one kind to every predicate in full plus a
few sample Subject IDs; ``orient(section=S)`` pages one artifact family.

Everything here is read from the accepted index at one coordinate. The
attention summary uses the existing ``next`` service's queue fold, without
building health facets, a result digest or a continuation page. Evidence is
named by CaptureContract identity: v6 rules name
contracts by reference, and a v5 rule's digests are resolved through accepted
state, so a digest is shown only when no accepted contract carries it.
"""

from __future__ import annotations

import json
import re
import shlex
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from cruxible_client import contracts
from cruxible_client.contracts.claim_types import (
    ClaimType,
    effective_evidence_requirement,
    effective_revision_evidence,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.orient import (
    ORIENT_ATTENTION_TOP,
    ORIENT_DEFAULT_LIMIT,
    ORIENT_DEFAULT_QUERIES,
    ORIENT_SAMPLE_SUBJECTS,
    Head,
    OrientArms,
    OrientArtifactCounts,
    OrientAttention,
    OrientClaimCounts,
    OrientDocument,
    OrientInterface,
    OrientInterfaceProvider,
    OrientKind,
    OrientKindDetail,
    OrientPredicate,
    OrientProcedure,
    OrientQuery,
    OrientResult,
    OrientSection,
    OrientSurface,
    OrientYou,
)
from cruxible_client.contracts.policy_rows import PolicyInForce
from cruxible_client.contracts.provider_contracts import ProviderOperationContract
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.exhaust.journal_index import RunPageInvalidated
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.discovery import (
    AcceptedProviderInterface,
    accepted_provider_interfaces,
)
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.next import (
    NextRequest,
    PlaybillNextItemV1,
    summarize_playbill_next,
)
from cruxible_core.service.discovery.operational import (
    capture_contract_rows,
    capture_rows,
    line_rows,
    live_view,
    mandate_rows,
    prediction_rows,
)
from cruxible_core.service.discovery.runs import run_counts, run_rows
from cruxible_core.service.identity import authoring_refusal, principal_standing
from cruxible_core.service.list_pages import (
    ListContinuation,
    ListCursorMismatch,
    ListCursorStale,
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
_NEXT_PROFILE = CoverageAccessProfile(
    profile_id="orient", permitted_access_classes=("instance", "public")
)
_UPGRADE_MARKERS = ("claim_type_upgrade", "claim-type upgrade", "upgrade_claim_types")


def _request_invalid(message: str) -> ReadRefusalError:
    return ReadRefusalError(
        "cruxible.orient.request_invalid",
        message,
        repair=RepairOperation(operation="cruxible.orient"),
    )


def _kind_not_found(kind: str, known: Iterable[str]) -> ReadRefusalError:
    return ReadRefusalError(
        "cruxible.orient.kind_not_found",
        f"no accepted Subject or ClaimType has kind {kind!r}",
        http_status=404,
        candidates=nearest(kind, known),
        repair=RepairOperation(operation="cruxible.orient"),
        repair_line="Use one of these kinds; orient without a kind lists every kind",
        context={"kind": kind},
    )


@dataclass(frozen=True)
class OrientCaller:
    """Who is asking, as the transport authenticated it (whoami's answer)."""

    actor_id: str | None
    credential_permission_mode: str | None
    # False for the implicit local operator, which names no principal.
    configured: bool = True
    credential_label: str | None = None


# -- reading accepted state -------------------------------------------------


@dataclass(frozen=True)
class _State:
    """Everything one orient answer reads from the accepted index."""

    claim_types: tuple[ClaimType, ...]
    subjects_by_kind: dict[str, int]
    evidence: dict[str, tuple[str, ...]]
    digest_named: int
    procedures: tuple[OrientProcedure, ...]
    documents: tuple[OrientDocument, ...]
    queries: tuple[OrientQuery, ...]
    interfaces: tuple[OrientInterface, ...] = ()


def _query_row(query: QueryDefinition) -> OrientQuery:
    return OrientQuery(
        name=query.identity.name,
        description=query.description,
        params=tuple(
            f"{param.name}{'' if param.required else '?'}: {param.value_type}"
            for param in query.parameters
        ),
    )


def _contract_fields(schema: object, *, stub: bool) -> tuple[str, ...]:
    """An operation contract side as ``name: type`` rows, ``?`` marking optional fields.

    A contract names its fields under ``fields``; a stub interface's definition
    is the field map itself; an acquisition output is a named contract.
    """

    if isinstance(schema, str):
        return (schema,)
    if not isinstance(schema, Mapping):
        return ()
    fields = schema if stub else schema.get("fields")
    if not isinstance(fields, Mapping):
        return ()
    rows: list[str] = []
    for name, spec in fields.items():
        if not isinstance(spec, Mapping):
            continue
        optional = spec.get("optional") is True or spec.get("required") is False
        rows.append(f"{name}{'?' if optional else ''}: {spec.get('type', 'any')}")
    return tuple(rows)


def _first_sentence(text: str) -> str | None:
    text = " ".join(text.split())
    if not text:
        return None
    head, separator, _rest = text.partition(". ")
    return head + "." if separator else text


def interface_row(item: AcceptedProviderInterface) -> OrientInterface:
    registration = item.registration
    definition = json.loads(bytes.fromhex(registration.interface_bytes_hex))
    vocabulary = json.loads(bytes.fromhex(registration.vocabulary_bytes_hex))
    contracts_block = definition.get("contracts") if isinstance(definition, Mapping) else None
    stub = not isinstance(contracts_block, Mapping)
    sides: Mapping[str, Any] = (
        definition if stub and isinstance(definition, Mapping) else contracts_block or {}
    )
    description = vocabulary.get("description") if isinstance(vocabulary, Mapping) else None
    return OrientInterface(
        name=registration.interface_id,
        description=_first_sentence(description) if isinstance(description, str) else None,
        input=_contract_fields(sides.get("input"), stub=stub),
        output=_contract_fields(sides.get("output"), stub=stub),
        effect=registration.effect_class,
        providers=tuple(
            OrientInterfaceProvider(
                provider=provider.provider_identity.removeprefix("Provider:"),
                implementation_digest=provider.implementation_digest,
            )
            for provider in item.entry.providers
        ),
        interface_digest=item.entry.interface_digest,
        operation_contract=(
            None
            if item.entry.operation_contract is None
            else ProviderOperationContract.model_validate(
                item.entry.operation_contract.model_dump(mode="json")
            )
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
            OrientProcedure(
                name=item.identity.removeprefix("Procedure:"),
                lifecycle="retired" if item.lifecycle == "retired" else "live",
                runnable="directly_runnable" if item.directly_runnable else "binding_required",
            )
            for item in sorted(typed.procedure_inventory(), key=lambda item: item.identity)
        )
        documents = tuple(
            OrientDocument(
                name=str(identity).removeprefix("document:"),
                title=str(title),
                document_kind=str(document_kind),
                media_type=str(media_type),
            )
            for identity, title, document_kind, media_type in connection.execute(
                "SELECT identity, title, document_kind, media_type FROM documents ORDER BY identity"
            )
        )
        queries: list[OrientQuery] = []
        for row in typed.envelopes(kind="query-definition"):
            query = typed.source(row.identity)
            if isinstance(query, QueryDefinition) and query.lifecycle.state == "live":
                queries.append(_query_row(query))
    return _State(
        claim_types=tuple(claim_types),
        subjects_by_kind=subjects_by_kind,
        evidence=evidence,
        digest_named=digest_named,
        procedures=procedures,
        documents=documents,
        queries=tuple(sorted(queries, key=lambda item: item.name)),
        interfaces=tuple(
            interface_row(item) for item in accepted_provider_interfaces(instance, coordinate)
        ),
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


_COMPACT_DESCRIPTION_CHARS = 160


def _compact_description(text: str) -> str:
    """A description's first sentence, at most 160 characters, for compact descriptors."""

    first = _first_sentence(text) or text
    if len(first) <= _COMPACT_DESCRIPTION_CHARS:
        return first
    return first[: _COMPACT_DESCRIPTION_CHARS - 1].rstrip() + "\u2026"


def _descriptor(
    claim_type: ClaimType,
    *,
    name: str,
    evidence: tuple[str, ...],
    full: bool,
    live_claims: int | None = None,
) -> OrientPredicate:
    value_type, members = _value_type(claim_type)
    freshness = claim_type.evidence_freshness
    requirement = effective_evidence_requirement(claim_type)
    return OrientPredicate(
        name=name,
        predicate=claim_type.predicate,
        cardinality=claim_type.cardinality,
        type=value_type,
        members=members,
        description=(
            claim_type.description
            if full or claim_type.description is None
            else _compact_description(claim_type.description)
        ),
        evidence=evidence,
        evidence_requirement=None if requirement == "self" else requirement,
        subject_kinds=claim_type.allowed_subject_kinds if full else None,
        roles=tuple(claim_type.permitted_roles) if full else None,
        default_role=claim_type.default_role if full else None,
        member_descriptions=claim_type.member_descriptions if full else (),
        revision_evidence=effective_revision_evidence(claim_type) if full else None,
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


def _kind_row(state: _State, kind: str) -> OrientKind:
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
    evidence, hoisted = _hoist_evidence(predicates)
    return OrientKind(
        kind=kind,
        subjects=state.subjects_by_kind.get(kind, 0),
        evidence=evidence,
        predicates=hoisted,
    )


def _hoist_evidence(
    predicates: Sequence[OrientPredicate],
) -> tuple[tuple[str, ...], tuple[OrientPredicate, ...]]:
    """Name the modal set once; ties use byte order, independent of input order."""

    counts = Counter(item.evidence for item in predicates if item.evidence is not None)
    evidence = min(
        counts,
        key=lambda value: (-counts[value], tuple(name.encode("utf-8") for name in value)),
        default=(),
    )
    return evidence, tuple(
        item.model_copy(update={"evidence": None}) if item.evidence == evidence else item
        for item in predicates
    )


# -- next suggestions, rendered per surface ---------------------------------


@dataclass(frozen=True)
class _Call:
    verb: Literal["orient", "query", "get", "next", "claim_type_upgrade"]
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


def render_orient_call(call: _Call, surface: OrientSurface) -> str:
    """One runnable call in the caller's own syntax."""

    args = dict(call.args)
    if surface == "mcp":
        tool = f"cruxible_{call.verb}"
        return f"{tool}({', '.join(f'{key}={_py(value)}' for key, value in call.args)})"
    if surface == "sdk":
        if call.verb == "claim_type_upgrade":
            return "cx.upgrade_claim_types()"
        if call.verb == "next":
            return "cx.next(expiring_within=Duration.days(count=7))"
        if call.verb == "get":
            return f"cx.get({_python(args['ref'])})"
        return (
            f"cx.{call.verb}("
            + ", ".join(f"{key}={_python(value)}" for key, value in call.args)
            + ")"
        )
    # cli
    if call.verb == "claim_type_upgrade":
        return "cruxible claim-type upgrade"
    if call.verb == "next":
        return "cruxible next"
    if call.verb == "get":
        return f"cruxible get {_cli_value(args['ref'])}"
    parts = ["cruxible", call.verb]
    if call.verb == "query" and "kind" in args:
        parts.append(_cli_value(args.pop("kind")))
    for key, value in args.items():
        if key == "where" and isinstance(value, list):
            parts.extend(f"--where {_cli_value(_cli_where(item))}" for item in value)
        elif key == "select" and isinstance(value, list):
            parts.append(f"--select {_cli_value(','.join(str(item) for item in value))}")
        elif key == "follow" and isinstance(value, list):
            parts.extend(
                f"--follow{'-in' if item.get('direction') == 'reverse' else ''} "
                + _cli_value(f"{item['field']}:{item['as']}")
                for item in value
            )
        else:
            parts.append(f"--{key.replace('_', '-')} {_cli_value(value)}")
    return " ".join(parts)


def _select_names(kind: OrientKind) -> list[str]:
    return [item.name for item in kind.predicates[:3]]


def _enum_filter(kind: OrientKind) -> dict[str, object] | None:
    for item in kind.predicates:
        if item.type == "enum" and item.members and item.cardinality == "one":
            return {"field": item.name, "eq": item.members[0]}
    return None


# -- attention ----------------------------------------------------------------


def _upgrade_hint(items: Sequence[PlaybillNextItemV1]) -> PlaybillNextItemV1 | None:
    for item in items:
        repair = item.repair
        text = " ".join(
            str(part)
            for part in (
                item.reason,
                None if repair is None else repair.command,
                None if repair is None else repair.required_change,
            )
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
    caller_rung: int | None,
    surface: OrientSurface,
    caller_tools: tuple[str, ...] | None,
    provider_lane: contracts.ProviderLaneStatus | None = None,
    consumers_running: bool = False,
) -> tuple[OrientAttention, bool]:
    notes: list[str] = []
    terminal = instance.descriptor.decommissioned
    if terminal is not None:
        notes.append(
            f"instance decommissioned at {terminal.decommissioned_at} ({terminal.reason}); "
            "reads serve, every write is refused"
        )
    items: tuple[PlaybillNextItemV1, ...] = ()
    reused: PlaybillNextItemV1 | None = None
    total = 0
    try:
        queue = summarize_playbill_next(
            instance,
            request=NextRequest(
                at=AcceptedCoordinate.from_internal(coordinate),
                evaluation_time=evaluation_time,
                access_profile=_NEXT_PROFILE,
                limit=contracts.NEXT_MAX_LIMIT,
                caller_surface=surface,
                caller_tools=caller_tools,
            ),
            caller_principal_id=None if caller is None else caller.actor_id,
            caller_rung=caller_rung,
            match=lambda item: _upgrade_hint((item,)) is not None,
        )
        items, total = queue.items, queue.total_items
        reused = queue.matching_item
    except CruxibleError as exc:
        code = getattr(exc, "error_code", None) or getattr(exc, "code", None)
        notes.append(f"the next queue could not be read: {code or type(exc).__name__}")
    upgrade = False
    if reused is not None:
        notes.append(_line(reused))
        upgrade = True
    elif state.digest_named:
        noun = "ClaimType still names" if state.digest_named == 1 else "ClaimTypes still name"
        notes.append(
            f"{state.digest_named} {noun} CaptureContracts by digest; run claim-type upgrade"
        )
        upgrade = True
    arms = _arms(instance, evaluation_time=evaluation_time)
    if provider_lane is not None and provider_lane.state == "unavailable":
        notes.append(
            f"the provider lane is unavailable ({provider_lane.code}): Procedures that call "
            "providers refuse until the daemon's provider runtime is repaired"
        )
    if arms is not None and arms.running + arms.stalled and not consumers_running:
        notes.append(
            "the daemon's consumer loop is not running here: armed Lines do not admit their "
            "own work and worker findings do not advance until a daemon runs it"
        )
    open_proposals = len(service_list_playbill_proposals(instance, status="open").entries)
    return (
        OrientAttention(
            next_items=total,
            open_proposals=open_proposals,
            top=tuple(_line(item) for item in items[:ORIENT_ATTENTION_TOP]),
            notes=tuple(notes),
            arms=arms,
        ),
        upgrade,
    )


def _arms(instance: PlaybillInstance, *, evaluation_time: datetime) -> OrientArms | None:
    """Every Line's latest arm as the Line consumer reports it, read from the instance alone."""

    from cruxible_core.consumers.lines import LINE_STALL_AFTER
    from cruxible_core.service.procedures.line_dispatch import line_arm_health

    health = line_arm_health(instance, now=evaluation_time, stall_after=LINE_STALL_AFTER)
    if not health:
        return None
    counts = Counter(state for state, _arm in health)
    flagged = [
        f"{arm.line} {state}" + (f" ({arm.stop_reason})" if arm.stop_reason else "")
        for state, arm in health
        if state != "running"
    ]
    return OrientArms(
        running=counts["running"],
        stalled=counts["stalled"],
        stopped=counts["stopped"],
        needs_attention=tuple(flagged[:ORIENT_ATTENTION_TOP]),
    )


def _you(caller: OrientCaller | None, *, instance: PlaybillInstance) -> OrientYou:
    """Whether the caller can author, with the same refusal whoami and authoring give."""

    actor_id = None if caller is None else caller.actor_id
    mode_name = None if caller is None else caller.credential_permission_mode
    refusal = authoring_refusal(
        instance,
        actor_id=actor_id,
        configured=True if caller is None else caller.configured,
        credential_id=None,
        credential_label=None if caller is None else caller.credential_label,
        permission_mode=(
            PermissionMode.READ_ONLY if mode_name is None else PermissionMode[mode_name.upper()]
        ),
    )
    active = actor_id is not None and principal_standing(instance, actor_id) == "active"
    return OrientYou(
        actor=actor_id,
        principal=actor_id if active else None,
        can_author=refusal is None,
        authoring_refusal=refusal,
    )


# -- paging -------------------------------------------------------------------


def _continuation(
    instance: PlaybillInstance,
    cursor: str | None,
    *,
    view: str,
    at: AcceptedCoordinate | str | None,
) -> tuple[ListContinuation | None, AcceptedCoordinate | str | None]:
    if cursor is None:
        return None, at
    continuation = decode_list_cursor(cursor, list_name=_LIST, selection={"view": view})
    pinned = AcceptedCoordinate.model_validate(continuation.coordinate)
    if (
        at is not None
        and AcceptedCoordinate.from_internal(resolve_read_coordinate(instance, at)) != pinned
    ):
        raise ListCursorMismatch(
            f"{ListCursorMismatch.error_code}: the cursor continues a different "
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


def _generation(instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate) -> int:
    return next(
        item.sequence for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )


def service_playbill_head(
    instance: PlaybillInstance,
    *,
    at: AcceptedCoordinate | str | None = None,
) -> Head:
    """The accepted head (or ``at``) as a coordinate and its generation; nothing else."""

    coordinate = resolve_read_coordinate(instance, at)
    return Head(
        instance=instance.descriptor.instance_id,
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        generation=_generation(instance, coordinate),
    )


def service_playbill_orient(
    instance: PlaybillInstance,
    *,
    kind: str | None = None,
    section: OrientSection | None = None,
    limit: int = ORIENT_DEFAULT_LIMIT,
    cursor: str | None = None,
    at: AcceptedCoordinate | str | None = None,
    evaluation_time: datetime | None = None,
    surface: OrientSurface = "cli",
    caller: OrientCaller | None = None,
    caller_rung: int | None = None,
    caller_tools: tuple[str, ...] | None = None,
    provider_lane: contracts.ProviderLaneStatus | None = None,
    consumers_running: bool = False,
) -> OrientResult:
    """Answer one orient read at one accepted coordinate.

    The runtime supplies its effective authenticated ``caller_rung``, just as
    for next; ``None`` is an unrestricted in-process read. ``surface`` and
    ``caller_tools`` select that same caller's repair view for attention.
    """

    if kind is not None and section is not None:
        raise _request_invalid(
            "pass kind or section, not both; orient(kind=K) reads one kind, "
            "orient(section=S) pages one artifact family"
        )
    if kind is not None and cursor is not None:
        raise _request_invalid("orient(kind=K) is one page and takes no cursor; drop the cursor")
    view = section or ("kind" if kind is not None else "kinds")
    continuation, at = _continuation(instance, cursor, view=view, at=at)
    coordinate = resolve_read_coordinate(instance, at)
    served = AcceptedCoordinate.from_internal(coordinate)
    moment = (evaluation_time or datetime.now(UTC)).astimezone(UTC)
    state = _read_state(instance, coordinate)
    base: dict[str, Any] = {
        "instance": instance.descriptor.instance_id,
        "coordinate": served,
        "generation": _generation(instance, coordinate),
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
        reverse = _reverse_follow(state, kind)
        if reverse is not None:
            calls.append(
                _Call(
                    "query",
                    (
                        ("kind", kind),
                        ("follow", [reverse]),
                        ("select", [reverse["as"]]),
                        ("limit", 10),
                    ),
                )
            )
        if detail.sample_subject_ids:
            calls.append(_Call("get", (("ref", f"{kind}/{detail.sample_subject_ids[0]}"),)))
        return OrientResult(
            **base,
            kind_detail=detail,
            next=tuple(render_orient_call(call, surface) for call in calls),
        )

    if section in {"runs", "running"}:
        return _runs_section(
            instance,
            base,
            section="running" if section == "running" else "runs",
            served=served,
            continuation=continuation,
            limit=limit,
            surface=surface,
        )
    if section == "captures":
        return _captures_section(
            instance,
            base,
            coordinate=coordinate,
            served=served,
            continuation=continuation,
            limit=limit,
            surface=surface,
        )
    if section is not None:
        rows, keys, first_ref = (
            _operational_rows(instance, coordinate, section, evaluation_time=moment)
            if section in _OPERATIONAL_SECTIONS
            else _governance_rows(instance, coordinate, section)
            if section in _GOVERNANCE_SECTIONS
            else _section_rows(state, section)
        )
        page, next_cursor = _page(
            rows, keys, view=view, served=served, continuation=continuation, limit=limit
        )
        first = None if not page or first_ref is None else first_ref(page[0])
        if first is not None:
            calls.append(_Call("get", (("ref", first),)))
        if next_cursor is not None:
            calls.append(_Call("orient", (("section", section), ("cursor", next_cursor))))
        base[section] = page
        if section in _LIVE_SECTIONS:
            base["live"] = live_view(instance, _LIVE_SECTIONS[section])
        return OrientResult(
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
        return OrientResult(
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
        caller_rung=caller_rung,
        surface=surface,
        caller_tools=caller_tools,
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
        calls.append(_Call("claim_type_upgrade"))
    if len(state.queries) > ORIENT_DEFAULT_QUERIES:
        calls.append(_Call("orient", (("section", "queries"),)))
    if state.interfaces:
        calls.append(_Call("orient", (("section", "interfaces"),)))
    counts = _operational_counts(instance, coordinate)
    # Operational families are listed by count, never inlined; point at the
    # two an agent most often needs: runs in flight and the Lines.
    if counts["running"]:
        calls.append(_Call("orient", (("section", "running"),)))
    if counts["lines"]:
        calls.append(_Call("orient", (("section", "lines"),)))
    if next_cursor is not None:
        calls.append(_Call("orient", (("cursor", next_cursor),)))
    live_procedures = sum(item.lifecycle == "live" for item in state.procedures)
    if attention.arms is not None or counts["runs"]:
        base["live"] = live_view(
            instance,
            tuple(
                name
                for name, present in (
                    ("attention.arms", attention.arms is not None),
                    ("artifacts.runs", bool(counts["runs"])),
                    ("artifacts.running", bool(counts["runs"])),
                )
                if present
            ),
        )
    return OrientResult(
        **base,
        you=_you(caller, instance=instance),
        kinds=kinds_page,
        artifacts=OrientArtifactCounts(
            claim_types=len(state.claim_types),
            procedures=live_procedures,
            documents=len(state.documents),
            queries=len(state.queries),
            interfaces=len(state.interfaces),
            **counts,
            claims=_claim_counts(instance, coordinate, evaluation_time=moment),
        ),
        queries=state.queries[:ORIENT_DEFAULT_QUERIES],
        attention=attention,
        truncated=next_cursor is not None,
        next_cursor=next_cursor,
        next=tuple(render_orient_call(call, surface) for call in calls),
    )


_KEYSET = "keyset"
#: The sections that carry live operational state, and which of their fields do.
_LIVE_SECTIONS: dict[str, tuple[str, ...]] = {
    "lines": ("lines.arm", "lines.due", "lines.waiting"),
    "predictions": ("predictions.open", "predictions.settleable", "predictions.resolved"),
}
_OPERATIONAL_SECTIONS: frozenset[str] = frozenset(
    {"lines", "capture_contracts", "predictions", "mandates"}
)


def _claim_counts(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    evaluation_time: datetime,
) -> OrientClaimCounts:
    """Every accepted Claim counted by status, from the remembered resolution when it holds.

    The ``next`` fold just derived the same statuses at this coordinate, so a
    warm memo answers from the index alone; a miss derives them once.
    """

    from cruxible_core.service.authoring.documents import AcceptedCoordinate
    from cruxible_core.service.discovery.claim_status import (
        claim_resolution_statuses,
        remembered_resolution_statuses,
    )
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    with instance.bind_accepted_projection(coordinate) as projection:
        identities = tuple(
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims ORDER BY identity"
            )
        )
    at = AcceptedCoordinate.from_internal(coordinate)
    statuses = remembered_resolution_statuses(
        instance, identities=identities, at=at, evaluation_time=evaluation_time
    )
    if statuses is None or len(statuses) != len(identities):
        context = ClaimVerdictReadContext(instance, coordinate)
        statuses = claim_resolution_statuses(
            instance,
            claims=context.claims(),
            at=at,
            evaluation_time=evaluation_time,
            read_context=context,
        )
    counts = Counter(statuses.values())
    return OrientClaimCounts(
        accepted=counts["accepted"],
        conflicted=counts["conflicted"],
        overturned=counts["overturned"],
        refused=counts["refused"],
        retired=counts["retired"],
    )


def _operational_counts(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> dict[str, int]:
    """Counts of each operational family: index counts only, never a row."""

    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection

        def count(sql: str) -> int:
            return int(connection.execute(sql).fetchone()[0])

        counts = {
            "lines": count("SELECT count(*) FROM lines WHERE lifecycle='live'"),
            "captures": count("SELECT count(*) FROM captures"),
            "capture_contracts": count(
                "SELECT count(*) FROM capture_contracts WHERE lifecycle='live'"
            ),
            "resolution_contracts": count(
                "SELECT count(*) FROM resolution_contracts WHERE lifecycle='live'"
            ),
            "mandates": count("SELECT count(*) FROM procedure_mandates WHERE lifecycle='live'"),
        }
    counts["runs"], counts["running"] = run_counts(instance)
    return counts


_GOVERNANCE_SECTIONS: frozenset[str] = frozenset({"principals", "policies"})
# Declaring artifacts ``get`` resolves by their identity, for a policy row's next call.
_GETTABLE_POLICY_DECLARERS = (
    "ApprovalPolicy:",
    "ClaimType:",
    "CaptureContract:",
    "QueryDefinition:",
    "document:",
    "Procedure:",
    "Line:",
)


def _governance_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    section: str,
) -> tuple[tuple[Any, ...], list[str], Any]:
    """The principal registry, or every live governed policy, at the coordinate."""

    if section == "principals":
        generation = next(
            item for item in instance.accepted_history() if item.oid == coordinate.git_oid
        )
        principals = tuple(
            PrincipalRecord.model_validate(item.model_dump(mode="json"))
            for item in generation.principals.principals
        )
        return (
            principals,
            [row.principal_id for row in principals],
            lambda row: f"Principal:{row.principal_id}",
        )
    from cruxible_core.service.claims.policies import service_playbill_policies_in_force

    policies = tuple(
        PolicyInForce.model_validate(row.model_dump(mode="json"))
        for row in service_playbill_policies_in_force(
            instance,
            at=contracts.AcceptedCoordinate.model_validate(
                AcceptedCoordinate.from_internal(coordinate).model_dump(mode="json")
            ),
        ).policies
    )
    return (
        policies,
        [f"{row.path}#{row.field_path}" for row in policies],
        lambda row: (
            row.declaring_artifact_identity
            if row.declaring_artifact_identity.startswith(_GETTABLE_POLICY_DECLARERS)
            else None
        ),
    )


def _operational_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    section: str,
    *,
    evaluation_time: datetime,
) -> tuple[tuple[Any, ...], list[str], Any]:
    """An accepted operational family's rows, their keys, and each row's get reference.

    Operational state (arm state, pending counts, bound windows) is read live at
    the current head; the section's answer carries ``live`` to say so.
    """

    if section == "lines":
        lines = line_rows(instance, coordinate, evaluation_time=evaluation_time)
        return lines, [row.line for row in lines], lambda row: row.line
    if section == "capture_contracts":
        contracts = capture_contract_rows(instance, coordinate)
        return contracts, [row.contract for row in contracts], lambda row: row.contract
    if section == "predictions":
        predictions = prediction_rows(instance, coordinate, evaluation_time=evaluation_time)
        return predictions, [row.contract for row in predictions], lambda row: row.contract
    mandates = mandate_rows(instance, coordinate, evaluation_time=evaluation_time)
    return (
        mandates,
        [row.mandate for row in mandates],
        lambda row: "Mandate:" + row.mandate.removeprefix("ProcedureMandate:"),
    )


def _captures_section(
    instance: PlaybillInstance,
    base: dict[str, Any],
    *,
    coordinate: AcceptedProjectionCoordinate,
    served: AcceptedCoordinate,
    continuation: ListContinuation | None,
    limit: int,
    surface: OrientSurface,
) -> OrientResult:
    after = _keyset_after(continuation)
    if after is not None and (len(after) != 2 or not after[0].isdigit()):
        raise ListCursorMismatch(
            f"{ListCursorMismatch.error_code}: the cursor is malformed; "
            "orient again without a cursor"
        )
    rows, stop = capture_rows(
        instance,
        coordinate,
        limit=limit,
        after=None if after is None else (after[0], after[1]),
    )
    next_cursor = (
        None
        if stop is None
        else _keyset_cursor(view="captures", served=served, last_key=[stop[0], stop[1]])
    )
    calls = [_Call("get", (("ref", rows[0].capture),))] if rows else []
    if next_cursor is not None:
        calls.append(_Call("orient", (("section", "captures"), ("cursor", next_cursor))))
    return OrientResult(
        **base,
        section="captures",
        captures=rows,
        truncated=next_cursor is not None,
        next_cursor=next_cursor,
        next=tuple(render_orient_call(call, surface) for call in calls),
    )


def _keyset_cursor(*, view: str, served: AcceptedCoordinate, last_key: Sequence[str]) -> str:
    """A cursor that continues a keyset listing after its last row.

    An operational listing (runs land continuously) is paged by key rather
    than by snapshot, so a run admitted after the first page never makes a
    later page stale or shifts it.
    """

    return encode_list_cursor(
        list_name=_LIST,
        coordinate=served.model_dump(mode="json"),
        selection={"view": view},
        snapshot=_KEYSET,
        last_key=last_key,
    )


def _keyset_after(continuation: ListContinuation | None) -> tuple[str, ...] | None:
    if continuation is None:
        return None
    if continuation.snapshot != _KEYSET:
        raise ListCursorMismatch(
            f"{ListCursorMismatch.error_code}: the cursor does not continue this "
            "listing; orient again without a cursor"
        )
    return continuation.last_key


def _runs_section(
    instance: PlaybillInstance,
    base: dict[str, Any],
    *,
    section: Literal["runs", "running"],
    served: AcceptedCoordinate,
    continuation: ListContinuation | None,
    limit: int,
    surface: OrientSurface,
) -> OrientResult:
    """Procedure runs newest admission first; ``running`` keeps only runs still running.

    The order never depends on a run's status, which changes as runs finish,
    so a page continues by an immutable key: no run is repeated or skipped.
    """

    after = _keyset_after(continuation)
    if after is not None and (len(after) != 2 or not after[1].isdigit()):
        raise ListCursorMismatch(
            f"{ListCursorMismatch.error_code}: the cursor is malformed; "
            "orient again without a cursor"
        )
    try:
        rows, stop = run_rows(
            instance,
            limit=limit,
            running_only=section == "running",
            after=None if after is None else (after[0], int(after[1])),
        )
    except RunPageInvalidated as exc:
        raise ListCursorStale(
            f"{ListCursorStale.error_code}: {exc}; orient again without a cursor"
        ) from exc
    next_cursor = (
        None
        if stop is None
        else _keyset_cursor(view=section, served=served, last_key=[stop[0], str(stop[1])])
    )
    calls = [_Call("get", (("ref", f"ProcedureRun:{rows[0].run}"),))] if rows else []
    if next_cursor is not None:
        calls.append(_Call("orient", (("section", section), ("cursor", next_cursor))))
    return OrientResult(
        **base,
        section=section,
        live=live_view(instance, ("runs",)),
        runs=rows,
        truncated=next_cursor is not None,
        next_cursor=next_cursor,
        next=tuple(render_orient_call(call, surface) for call in calls),
    )


def _kind_detail(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    state: _State,
    kind: str,
) -> OrientKindDetail:
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
                (kind, ORIENT_SAMPLE_SUBJECTS),
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
    evidence, hoisted = _hoist_evidence(predicates)
    return OrientKindDetail(
        kind=kind,
        subjects=state.subjects_by_kind.get(kind, 0),
        evidence=evidence,
        predicates=hoisted,
        incoming=tuple(item.predicate for item in _incoming(state, kind)),
        sample_subject_ids=samples,
    )


def _incoming(state: _State, kind: str) -> tuple[ClaimType, ...]:
    """The live Subject-valued ClaimTypes whose values may name ``kind`` Subjects.

    The same set ``query`` resolves a reverse follow against: a predicate points
    at a kind only when it admits that kind as its object.
    """

    return tuple(
        sorted(
            (
                item
                for item in state.claim_types
                if item.object_kind == "subject" and kind in item.allowed_object_subject_kinds
            ),
            key=lambda item: item.predicate.encode("utf-8"),
        )
    )


def _reverse_follow(state: _State, kind: str) -> dict[str, str] | None:
    """One runnable reverse follow into ``kind``, aliased by its source kind."""

    incoming = _incoming(state, kind)
    if not incoming:
        return None
    first = incoming[0]
    reserved = {"subject", "subject_id", "value"}
    reserved.update(item.predicate.split(".", 1)[0] for item in state.claim_types)
    reserved.update(name.split(".", 1)[0] for name in _kind_names(state))
    source = sorted(first.allowed_subject_kinds)[0] if first.allowed_subject_kinds else ""
    for alias in (source.rsplit(".", 1)[-1], "source"):
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", alias) and alias not in reserved:
            return {"field": first.predicate, "as": alias, "direction": "reverse"}
    return None


def _section_rows(state: _State, section: OrientSection) -> tuple[tuple[Any, ...], list[str], Any]:
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
    if section == "interfaces":
        return (
            state.interfaces,
            [item.name for item in state.interfaces],
            lambda row: f"ProviderInterface:{row.name}",
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
    "interface_row",
    "render_orient_call",
    "service_playbill_head",
    "service_playbill_orient",
]
