"""Bounded Claim selection over a single verified accepted coordinate."""

from __future__ import annotations

import base64
import hashlib
import json

from cruxible_client.contracts import PlaybillClaimViewV2
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_reads import (
    ClaimBackingsRequestV1,
    ClaimBackingsResultV1,
    ClaimReadBatchRequestV1,
    ClaimReadBatchResultV1,
)
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.claims import claim_path, claim_statement_digest, parse_claim
from cruxible_client.contracts.declared_blocks import ProjectionClaimBackingV1
from cruxible_client.contracts.errors import ClaimNotFoundError, PlaybillFormatError
from cruxible_core.playbill.compiler import artifact_codec_for_compiler
from cruxible_core.playbill.id_prefixes import resolve_id_prefix
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    _accepted_claim_ids,
    _accepted_generation_time,
    _public_claim,
    _resolve_coordinate,
    materialize_playbill_claim_view,
)


def _cursor_selection(request: ClaimReadBatchRequestV1) -> str:
    body = request.model_dump(mode="json", exclude={"cursor", "limit"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def service_read_claim_batch(
    instance: PlaybillInstance,
    *,
    request: ClaimReadBatchRequestV1,
) -> ClaimReadBatchResultV1:
    coordinate = _resolve_coordinate(
        instance, PlaybillAcceptedCoordinate.model_validate(request.at.model_dump())
    )
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        if request.claim_ids:
            raise ClaimNotFoundError("the accepted generation contains no Claims")
        return ClaimReadBatchResultV1(coordinate=request.at, claims=())
    after = ""
    if request.cursor:
        try:
            binding, after = json.loads(base64.urlsafe_b64decode(request.cursor))
            if binding != _cursor_selection(request) or not isinstance(after, str):
                raise ValueError("cursor selection differs")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise PlaybillFormatError("Claim batch cursor does not match this selection") from exc
    truncated = False
    with instance.bind_accepted_projection(coordinate) as projection:
        if request.claim_ids:
            accepted = _accepted_claim_ids(instance, coordinate=coordinate)
            identities = tuple(
                "Claim:"
                + resolve_id_prefix(
                    identity.removeprefix("Claim:"), accepted, marker="CLM-", label="Claim"
                )
                for identity in request.claim_ids
            )
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
        for identity in identities:
            projected = projection.claim(identity)
            if projected is None:
                raise ClaimNotFoundError(f"Claim not found: {identity}")
            public_views.append(_public_claim(projected))
        # One shared exact accepted blob selection; single-view admission logic
        # still evaluates each Claim independently against this same material.
        wanted = {
            path
            for path in instance.paths_at(coordinate.git_oid)
            if path.startswith("capture-contracts/")
        }
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
        for public in public_views:
            view = materialize_playbill_claim_view(
                instance,
                public=public,
                coordinate=coordinate,
                evaluation_time=request.evaluation_time
                or _accepted_generation_time(instance, coordinate),
                admission_tree=admission_tree,
            )
            views.append(PlaybillClaimViewV2.model_validate(view.model_dump(mode="json")))
    cursor = None
    if truncated:
        cursor = base64.urlsafe_b64encode(
            json.dumps([_cursor_selection(request), identities[-1]]).encode()
        ).decode()
    return ClaimReadBatchResultV1(
        coordinate=request.at,
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
