"""Bounded Claim selection over a single verified accepted coordinate."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cruxible_client.contracts import PlaybillAcceptedCoordinate as ClientAcceptedCoordinate
from cruxible_client.contracts import PlaybillClaimViewV2
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_reads import (
    ClaimBackingsRequestV1,
    ClaimBackingsResultV1,
    ClaimReadBatchRequestV1,
    ClaimReadBatchResultV1,
    ClaimValuesRequestV1,
    ClaimValuesResultV1,
    ClaimValueV1,
)
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.claims import (
    ClaimFormatError,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import ProjectionClaimBackingV1
from cruxible_client.contracts.errors import (
    ClaimNotFoundError,
    PlaybillFormatError,
)
from cruxible_core.authoring.id_prefixes import resolve_id_prefix
from cruxible_core.compiler.compiler import artifact_codec_for_compiler
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.claims.claims import (
    _accepted_generation_time,
    _public_claim,
    _resolve_coordinate,
    materialize_playbill_claim_view,
)
from cruxible_core.service.evidence.evidence import (
    _claim_read_history_index,
    _IndexedClaimLawEvidence,
)


def _cursor_selection(request: ClaimReadBatchRequestV1) -> str:
    body = request.model_dump(mode="json", exclude={"cursor", "limit"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def service_read_claim_batch(
    instance: PlaybillInstance,
    *,
    request: ClaimReadBatchRequestV1,
) -> ClaimReadBatchResultV1:
    if request.cursor is not None and request.at is None:
        raise PlaybillFormatError("Claim batch cursor requires an explicit accepted coordinate")
    coordinate = _resolve_coordinate(
        instance,
        PlaybillAcceptedCoordinate.model_validate(request.at.model_dump())
        if request.at is not None
        else None,
    )
    resolved_at = ClientAcceptedCoordinate.model_validate(
        PlaybillAcceptedCoordinate.from_internal(coordinate).model_dump()
    )
    # A latest-head request becomes one immutable selection before any reads or
    # cursor binding. Subsequent pages must supply this returned coordinate.
    request = request.model_copy(update={"at": resolved_at})
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        generation_sequence = history.sequence
    after = ""
    if request.cursor:
        try:
            binding, after = json.loads(base64.urlsafe_b64decode(request.cursor))
            if binding != _cursor_selection(request) or not isinstance(after, str):
                raise ValueError("cursor selection differs")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise PlaybillFormatError("Claim batch cursor does not match this selection") from exc
    if generation_sequence == 0:
        if request.claim_ids:
            raise ClaimNotFoundError("the accepted generation contains no Claims")
        return ClaimReadBatchResultV1(coordinate=resolved_at, claims=())
    truncated = False
    with instance.bind_accepted_projection(coordinate) as projection:
        if request.claim_ids:
            accepted_ids: tuple[str, ...] | None = None

            def resolve(identity: str) -> str:
                nonlocal accepted_ids
                bare = identity.removeprefix("Claim:")
                try:
                    claim_path(bare)
                except ClaimFormatError:
                    if accepted_ids is None:
                        accepted_ids = tuple(
                            row.identity.removeprefix("Claim:")
                            for row in projection.typed.envelopes(kind="claim")
                        )
                    bare = resolve_id_prefix(bare, accepted_ids, marker="CLM-", label="Claim")
                return "Claim:" + bare

            identities = tuple(resolve(identity) for identity in request.claim_ids)
        else:
            identities = projection.select_claim_identities(
                subject_paths=request.subject_paths,
                predicates=request.predicates,
                include_retired=request.include_retired,
                after=after,
                limit=request.limit + 1,
            )
            truncated = len(identities) > request.limit
            identities = identities[: request.limit]
        views: list[PlaybillClaimViewV2] = []
        public_views = []
        projected_views = {view.envelope.identity: view for view in projection.claims(identities)}
        for identity in identities:
            projected = projected_views.get(identity)
            if projected is None:
                raise ClaimNotFoundError(f"Claim not found: {identity}")
            public_views.append(_public_claim(projected))
        # One shared exact accepted blob selection; single-view admission logic
        # still evaluates each Claim independently against this same material.
        wanted: set[str] = set()
        for identity in identities:
            wanted.update(
                row[0]
                for row in projection.typed.connection.execute(
                    "SELECT DISTINCT t.path FROM citation_uses u "
                    "JOIN captures c ON c.capture_digest=u.capture_digest "
                    "JOIN capture_contracts t ON t.artifact_digest=c.contract_digest "
                    "WHERE u.owner_kind='Claim' AND u.owner_key=? ORDER BY t.path",
                    (identity,),
                )
            )
        for public in public_views:
            statement = next(
                fact["value"]
                for fact in public.facts
                if fact["schema_id"] == "playbill.claim.statement"
            )
            if isinstance(statement, dict):
                wanted.add(claim_type_path(str(statement["predicate"])))
        admission_tree = (
            instance.blobs_at(coordinate.git_oid, tuple(sorted(wanted))) if public_views else {}
        )
        claim_history = _claim_read_history_index(
            instance, coordinate=coordinate, records=projection.typed.records
        )
        if isinstance(claim_history.law_evidence, _IndexedClaimLawEvidence):
            # Every selected view's law evidence under one history snapshot.
            claim_history.law_evidence.prefetch(
                tuple(str(public.envelope["path"]) for public in public_views)
            )
        evaluation_time = request.evaluation_time or (
            _accepted_generation_time(instance, coordinate) if public_views else None
        )
        for public in public_views:
            assert evaluation_time is not None
            view = materialize_playbill_claim_view(
                instance,
                public=public,
                coordinate=coordinate,
                evaluation_time=evaluation_time,
                admission_tree=admission_tree,
                law=claim_history.law_evidence.get(str(public.envelope["path"])),
            )
            views.append(PlaybillClaimViewV2.model_validate(view.model_dump(mode="json")))
    cursor = None
    if truncated:
        cursor = base64.urlsafe_b64encode(
            json.dumps([_cursor_selection(request), identities[-1]]).encode()
        ).decode()
    return ClaimReadBatchResultV1(
        coordinate=resolved_at,
        claims=tuple(views),
        truncated=truncated,
        cursor=cursor,
    )


def service_read_claim_backings(
    instance: PlaybillInstance,
    *,
    request: ClaimBackingsRequestV1,
) -> ClaimBackingsResultV1:
    coordinate = _resolve_coordinate(
        instance, PlaybillAcceptedCoordinate.model_validate(request.at.model_dump())
    )
    names = tuple(name.removeprefix("Claim:") for name in request.claim_ids)
    try:
        paths = tuple(claim_path(name) for name in names)
    except ValueError as exc:
        raise PlaybillFormatError("Claim backings require exact full Claim identities") from exc
    bodies = instance.blobs_at(coordinate.git_oid, paths)
    backings: list[ProjectionClaimBackingV1] = []
    for name, path in zip(names, paths, strict=True):
        if path not in bodies:
            raise ClaimNotFoundError(f"Claim backing not found: {name}")
        claim = parse_claim(
            bodies[path], path=path, codec=artifact_codec_for_compiler(coordinate.compiler)
        )
        if claim.identity != ArtifactIdentity(kind="Claim", name=name):
            raise PlaybillFormatError("Claim backing identity differs from its accepted path")
        if claim.lifecycle.state != "live":
            raise PlaybillFormatError(f"Claim backing must identify a live Claim: {name}")
        backings.append(
            ProjectionClaimBackingV1(
                identity=claim.identity,
                statement_digest=claim_statement_digest(claim.statement).tagged,
            )
        )
    return ClaimBackingsResultV1(coordinate=request.at, backings=tuple(backings))


def service_read_claim_values(
    instance: PlaybillInstance,
    *,
    request: ClaimValuesRequestV1,
) -> ClaimValuesResultV1:
    """Live Claim values and verdicts for explicit Subjects, without full Claim views.

    Selecting by Subject path (and predicate) returns every live contender of
    each slot it touches, so each verdict is the slot's full resolution. The
    verdicts come from the same per-slot derivation orient and block checks use,
    and are reused across coordinates wherever its reads still hold.
    """

    from cruxible_client.contracts.claim_reads import MAX_CLAIM_VALUE_ROWS
    from cruxible_client.contracts.errors import PlaybillFormatError as ValuesFormatError
    from cruxible_core.service.discovery.search import claim_resolution_statuses
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    coordinate = _resolve_coordinate(
        instance,
        PlaybillAcceptedCoordinate.model_validate(request.at.model_dump())
        if request.at is not None
        else None,
    )
    at = PlaybillAcceptedCoordinate.from_internal(coordinate)
    clauses = [
        "lifecycle='live'",
        "subject_path IN (" + ",".join("?" * len(request.subject_paths)) + ")",
    ]
    values: list[object] = list(request.subject_paths)
    if request.predicates:
        clauses.append("predicate IN (" + ",".join("?" * len(request.predicates)) + ")")
        values.extend(request.predicates)
    with instance.bind_accepted_projection(coordinate) as projection:
        identities = tuple(
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE " + " AND ".join(clauses) + " ORDER BY identity",
                values,
            )
        )
    if len(identities) > MAX_CLAIM_VALUE_ROWS:
        raise ValuesFormatError("Claim value selection exceeds its row bound; narrow it")
    evaluation_time = request.evaluation_time or _accepted_generation_time(instance, coordinate)
    context = ClaimVerdictReadContext(instance, coordinate)
    context.prefetch(tuple(claim_path(identity.removeprefix("Claim:")) for identity in identities))
    claims = tuple(context.claim(identity) for identity in identities)
    verdicts: dict[str, object] = {}
    statuses = claim_resolution_statuses(
        instance,
        claims=claims,
        at=at,
        evaluation_time=evaluation_time,
        verdicts_by_identity=verdicts,  # type: ignore[arg-type]
        read_context=context,
    )
    rows = tuple(
        _claim_value_row(
            claim,
            verdict=verdicts.get(claim.identity.qualified),
            status=statuses[claim.identity.name],
        )
        for claim in claims
    )
    return ClaimValuesResultV1(
        coordinate=ClientAcceptedCoordinate.model_validate(at.model_dump()),
        evaluation_time=evaluation_time,
        values=rows,
    )


def _claim_value_row(claim: Any, *, verdict: object, status: str) -> ClaimValueV1:
    """One Claim's value row, for every statement object variant."""

    from cruxible_client.contracts.claims import ExactContentClaimObject, SubjectClaimObject

    statement = claim.statement
    claim_object = statement.object
    if isinstance(claim_object, SubjectClaimObject):
        value: object = claim_object.address.artifact_path
    elif isinstance(claim_object, ExactContentClaimObject):
        value = claim_object.content_digest
    else:
        value = claim_object.value
    return ClaimValueV1(
        claim_id=claim.identity.name,
        subject_path=statement.subject.artifact_path,
        predicate=statement.predicate,
        qualifier=statement.qualifier,
        role=statement.role,
        object_kind=claim_object.kind,
        object=claim_object,
        value=value,
        verdict=str(getattr(verdict, "verdict", "unevaluated")),
        status=status,
    )
