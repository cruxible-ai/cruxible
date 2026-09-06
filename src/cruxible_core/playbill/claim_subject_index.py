"""Rebuildable lookup of Claim statement Subjects, independent of policy/time."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from cruxible_client.contracts.claims import parse_claim

CLAIM_PATH_RE = re.compile(r"^claims/[0-9a-f]{2}/CLM-[0-9a-f]{32}\.json$")


@dataclass(frozen=True)
class ClaimSubjectIndex:
    """Exact statement membership, including retired and future-effective Claims.

    Subject pins are not the statement: their correspondence is checked by law
    later. Index the parsed statement itself, then apply lifecycle/effective-time
    filtering afresh when policy values are requested. No Claim model is retained.
    """

    subject_by_claim: Mapping[str, str]
    claims_by_subject: Mapping[str, frozenset[str]]


def build_claim_subject_index(tree: Mapping[str, bytes]) -> ClaimSubjectIndex:
    return update_claim_subject_index(ClaimSubjectIndex({}, {}), tree=tree, changed=tree)


def update_claim_subject_index(
    index: ClaimSubjectIndex, *, tree: Mapping[str, bytes], changed: Iterable[str]
) -> ClaimSubjectIndex:
    by_claim = dict(index.subject_by_claim)
    by_subject = dict(index.claims_by_subject)
    touched: dict[str, set[str]] = {}

    def bucket(subject: str) -> set[str]:
        if subject not in touched:
            touched[subject] = set(by_subject.get(subject, ()))
        return touched[subject]

    for path in sorted(set(changed), key=lambda item: item.encode("utf-8")):
        if not CLAIM_PATH_RE.fullmatch(path):
            continue
        previous = by_claim.pop(path, None)
        if previous is not None:
            bucket(previous).discard(path)
        content = tree.get(path)
        if content is None:
            continue
        subject = parse_claim(content, path=path).statement.subject.artifact_path
        by_claim[path] = subject
        bucket(subject).add(path)
    for subject, paths in touched.items():
        if paths:
            by_subject[subject] = frozenset(paths)
        else:
            by_subject.pop(subject, None)
    return ClaimSubjectIndex(by_claim, by_subject)
