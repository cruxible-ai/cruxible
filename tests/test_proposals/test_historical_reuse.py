"""The frozen reuse reproducer still yields the bytes the retired law recorded.

Historical ClaimType law revisions committed their reuse evidence into accepted
change-set records, and replay re-derives it. The pins below were computed by
the live reuse law immediately before its removal
(``dev.decision/reuse-removal-laws-0929``); they must never move.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.laws import (
    CLAIM_TYPE_ACCEPTANCE_LAW,
    CLAIM_TYPE_V3_ACCEPTANCE_LAW,
    CLAIM_TYPE_V4_ACCEPTANCE_LAW,
    CLAIM_TYPE_V5_ACCEPTANCE_LAW,
    CLAIM_TYPE_V6_ACCEPTANCE_LAW,
    PLAYBILL_ACCEPTANCE_LAWS,
)
from cruxible_client.contracts.projection import AcceptedProjectionCoordinate
from cruxible_core.compiler.compiler import AUTHORITY_VERBS_COMPILER
from tests.test_indexes.test_vocabulary_index import _descriptor, _tree, _type

COORDINATE = AcceptedProjectionCoordinate(
    instance_id="historical-reuse",
    repository_path="/historical/reuse",
    git_object_format="sha1",
    git_oid="3" * 40,
    semantic_root="sha256:" + "44" * 32,
    generation_root="sha256:" + "55" * 32,
    compiler=AUTHORITY_VERBS_COMPILER,
)
_DISTINCT_TARGETS = (
    claim_type_path("project.work_item.status"),
    claim_type_path("project.work_item.owner"),
    claim_type_path("ops.ticket.state"),
    "subjects/project.work_item/status.json",
)

Scenario = tuple[str, ClaimType, str, Mapping[str, bytes], tuple[str, ...]]


def _scenarios() -> Iterator[Scenario]:
    for name, predicate, distinct in (
        ("exact_collision", "project.work_item.status", ()),
        ("canonical_token_refused", "ops.board.status", ()),
        ("canonical_token_partly_distinct", "ops.board.status", _DISTINCT_TARGETS[:1]),
        ("canonical_token_distinct", "ops.board.status", _DISTINCT_TARGETS),
        ("accepted_alias", "ops.board.assignee", ()),
        ("accepted_alias_distinct", "ops.board.assignee", _DISTINCT_TARGETS),
        ("accepted_tag", "ops.board.workflow", ()),
        ("retired_descriptor", "ops.board.condition", ()),
        ("retired_claim_type", "ops.board.phase", ()),
    ):
        claim_type = _type(predicate)
        path = claim_type_path(predicate)
        tree = {**_tree(), path: render_claim_type(claim_type)}
        scope = [path]
        for index, target in enumerate(distinct):
            relation_path, content = _descriptor(
                f"CLM-{0xF00 + index:032x}", "semantic.distinct_from", path, target
            )
            tree[relation_path] = content
            scope.append(relation_path)
        yield name, claim_type, path, tree, tuple(scope)


def _digests(evidence: Callable[..., dict[str, object]]) -> dict[str, tuple[str, str]]:
    digests = {}
    for name, claim_type, path, tree, scope in _scenarios():
        recorded = evidence(
            claim_type=claim_type,
            path=path,
            lookup_tree=tree,
            candidate_scope=scope,
            current=COORDINATE,
        )
        digests[name] = (
            hashlib.sha256(canonical_bytes(recorded)).hexdigest(),
            str(recorded["verdict"]),
        )
    return digests


# Computed by ``_claim_type_reuse_evidence`` at 26fd6707f, before the removal.
RECORDED = {
    "accepted_alias": (
        "244d1b92e691929298446f7f993ef92fee47c7b7ce46dd5dd1c81087f27fed7d",
        "refused",
    ),
    "accepted_alias_distinct": (
        "8816d089dccf83d95d3cdf84fa3969c117e1e4d2b646b16d0823b2b1f162b7e1",
        "satisfied",
    ),
    "accepted_tag": (
        "26c0e8b36b3cde33b58fb3e0cf81c9ab0a14778c4c1221d996e33e2f7b7e2908",
        "satisfied",
    ),
    "canonical_token_distinct": (
        "08dcbf36976c74ba2ee1818d1f1399a63e3fdbb7b48fe91d7925bf923700e3cb",
        "satisfied",
    ),
    "canonical_token_partly_distinct": (
        "95c5da5f043e1ee2be0e67aa1ae3e908cd14b21bca306b04e6ef6edcf9a54fc6",
        "refused",
    ),
    "canonical_token_refused": (
        "d2c07d2e7bff69b3242d6d69817ca1913fbf035cba70e99c4a2bf0c0e904396d",
        "refused",
    ),
    "exact_collision": (
        "07c6e75080f015ce2734ff935387931aa3158f7193cb2bbe0822a1d8d9f93e50",
        "refused",
    ),
    "retired_claim_type": (
        "00f27459345745c64988e5c5fe7412816f406b56ac6879e8ed5f9f2ed40e3d1e",
        "satisfied",
    ),
    "retired_descriptor": (
        "c9f3349b9ca0fe038dbbe89599dc499649a6fb934e1b5b6db6a8afe4040808d7",
        "satisfied",
    ),
}


def test_historical_reproducer_matches_the_recorded_law_bytes() -> None:
    from cruxible_core.proposals.historical_reuse import historical_claim_type_reuse_evidence

    assert _digests(historical_claim_type_reuse_evidence) == RECORDED


def test_only_the_retired_claim_type_revisions_run_the_reproducer() -> None:
    from cruxible_core.proposals.historical_reuse import HISTORICAL_REUSE_CLAIM_TYPE_LAWS

    historical = {
        (law.coordinate.identifier, law.coordinate.digest)
        for law in PLAYBILL_ACCEPTANCE_LAWS._by_coordinate.values()
        if law.artifact_kind == "claim-type" and not law.current
    }
    assert historical == HISTORICAL_REUSE_CLAIM_TYPE_LAWS
    for current in (
        CLAIM_TYPE_ACCEPTANCE_LAW,
        CLAIM_TYPE_V3_ACCEPTANCE_LAW,
        CLAIM_TYPE_V4_ACCEPTANCE_LAW,
        CLAIM_TYPE_V5_ACCEPTANCE_LAW,
        CLAIM_TYPE_V6_ACCEPTANCE_LAW,
    ):
        coordinate = (current.coordinate.identifier, current.coordinate.digest)
        assert coordinate not in HISTORICAL_REUSE_CLAIM_TYPE_LAWS
