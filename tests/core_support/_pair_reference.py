"""The reference answer for an end's pairs: a full fold of every Claim, judged by the shared gate.

Independent of the pair index: an end's pairs are the Subjects whose
Subject-valued key Claims (any lifecycle) name it; each is listed when one of
its live, positive, non-key Claims is visible under the listing policy at the
read's instant (``claim_row_outcome`` over every accepted fact, the served and
reference backends' one verdict path), and hidden when it holds live facts but
none is visible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from cruxible_core.query.backends import claim_row_outcome
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.pair_reads import PAIR_LISTING_POLICY
from cruxible_core.service.discovery.query import build_accepted_query_facts


@dataclass(frozen=True)
class ReferencePairs:
    #: Visible pairs by (pair kind, short part), each list in id order.
    listed: dict[tuple[str, str], list[str]]
    #: Pairs holding live facts, none of them visible.
    hidden: frozenset[str]


def _ref(path: str) -> str:
    return path.removeprefix("subjects/").removesuffix(".json")


def reference_pairs(instance: PlaybillInstance, end: str, *, at: datetime) -> ReferencePairs:
    path = f"subjects/{end}.json"
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        keys = {
            str(identity).removeprefix("SubjectKind:"): set(json.loads(str(predicates)))
            for identity, predicates in connection.execute(
                "SELECT identity, key_predicates FROM subject_kinds"
            )
        }
        naming = {
            (str(pair), str(predicate))
            for pair, predicate, obj in connection.execute(
                "SELECT subject_path, predicate, object_path FROM claims "
                "WHERE object_kind='subject'"
            )
            if obj == path and str(predicate) in keys.get(str(pair).split("/")[1], ())
        }
    facts = build_accepted_query_facts(instance, coordinate=coordinate)
    subjects = {subject.path: subject for subject in facts.subjects}
    providers = {provider.identity.qualified: provider for provider in facts.providers}
    holding: set[str] = set()
    visible: set[str] = set()
    for row in facts.claims:
        statement = row.accepted.claim.statement
        if statement.predicate in keys.get(row.subject_path.split("/")[1], ()):
            continue
        holding.add(row.subject_path)
        if statement.object.kind == "none":
            continue
        outcome = claim_row_outcome(
            row,
            subject=subjects.get(row.subject_path),
            providers=providers,
            policy=PAIR_LISTING_POLICY,
            evaluation_time=at,
        )
        if outcome.visible is not None:
            visible.add(row.subject_path)
    listed: dict[tuple[str, str], list[str]] = {}
    hidden: set[str] = set()
    for pair, predicate in naming:
        if pair in visible:
            listed.setdefault((pair.split("/")[1], predicate.rsplit(".", 1)[1]), []).append(
                _ref(pair)
            )
        elif pair in holding:
            hidden.add(_ref(pair))
    return ReferencePairs({key: sorted(value) for key, value in listed.items()}, frozenset(hidden))


__all__ = ["ReferencePairs", "reference_pairs"]
