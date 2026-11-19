"""Cold full-tree Claim scans: reference oracles for the indexed contender lookups.

Each walks every ``claims/`` path of a tree, so tests compare the indexed
``claims_for`` and slot paths against them; production never scans this way.
"""

from __future__ import annotations

from collections.abc import Mapping

from cruxible_client.contracts.claims import ClaimArtifact, ClaimStatement, parse_claim


def same_predicate_claims(
    tree: Mapping[str, bytes], statement: ClaimStatement
) -> tuple[ClaimArtifact, ...]:
    """Return every live Claim on this statement's (subject, predicate).

    Wider than the contended slot: these are the Claims an author may
    disposition voluntarily, not the ones the law demands.
    """
    claims: list[ClaimArtifact] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if (
            claim.lifecycle.state == "live"
            and claim.statement.subject == statement.subject
            and claim.statement.predicate == statement.predicate
        ):
            claims.append(claim)
    return tuple(claims)


def same_slot_claims(
    tree: Mapping[str, bytes], statement: ClaimStatement
) -> tuple[ClaimArtifact, ...]:
    """Return the live Claims contending for this statement's own slot.

    The slot is ``(subject, predicate, qualifier)`` — the same partition the
    resolution policy and the curation detectors already use. Matching on
    subject+predicate alone made every distinct qualifier and every
    cardinality=many sibling demand a disposition it does not contend with.
    """
    claims: list[ClaimArtifact] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if (
            claim.lifecycle.state == "live"
            and claim.statement.subject == statement.subject
            and claim.statement.predicate == statement.predicate
            and claim.statement.qualifier == statement.qualifier
        ):
            claims.append(claim)
    return tuple(claims)


__all__ = ["same_predicate_claims", "same_slot_claims"]
