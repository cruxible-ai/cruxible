"""A keyed Subject's key Claims follow its lifecycle (maintainer ruling, identity D4).

A key Claim is a mechanical identity part of its Subject, not a belief about
it, so a change never has to disposition one by hand:

- retiring a keyed Subject retires its live key Claims in the same change
  (``derive_key_claim_retirements``, applied by the candidate boundary before
  anything is scoped, like derived cards), freeing the key;
- succeeding a keyed Subject (a live successor) carries its key Claims to the
  successor unchanged (``carried_key_claim``: the closure law does not ask for
  a disposition of them), unless the change itself revises or retires them.

Both read only the parent and candidate trees, so admission, settlement and
replay derive the same thing, and a tree that already holds the retirement
derives nothing more.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimRetirementAttribution,
    claim_artifact_digest,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import SubjectFormatError
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import SubjectKindFormatError, live_subject_kind
from cruxible_client.contracts.subjects import parse_subject
from cruxible_core.derived.derived_state import SnapshotTree, fork_tree

_SUBJECTS = "subjects/"


@dataclass(frozen=True)
class _Slot:
    subject: SemanticAddress
    predicate: str


def _lifecycle(tree: Mapping[str, bytes], path: str) -> str | None:
    content = tree.get(path)
    if content is None:
        return None
    try:
        return parse_subject(content, path=path).lifecycle.state
    except SubjectFormatError:
        return None


def _key_predicates(
    current_tree: Mapping[str, bytes], candidate_tree: Mapping[str, bytes], kind: str
) -> tuple[str, ...]:
    for tree in (current_tree, candidate_tree):
        try:
            declared = live_subject_kind(tree, kind)
        except SubjectKindFormatError:
            declared = None
        if declared is not None and not declared.minted:
            return tuple(declared.keys)
    return ()


def _live_claims(
    tree: Mapping[str, bytes], subject: str, predicate: str
) -> Iterator[tuple[str, bytes]]:
    slot = _Slot(SemanticAddress.whole_artifact(subject), predicate)
    if isinstance(tree, SnapshotTree):
        yield from tree.claim_items(slot)
        return
    needle = f'"{predicate}"'.encode()
    for path, content in tree.items():
        if not path.startswith("claims/") or needle not in content:
            continue
        claim = parse_claim(content, path=path)
        if (
            claim.lifecycle.state == "live"
            and claim.statement.subject == slot.subject
            and claim.statement.predicate == predicate
        ):
            yield path, content


def _is_key_claim(claim: ClaimArtifactAny, subject: str, keys: tuple[str, ...]) -> bool:
    statement = claim.statement
    return (
        claim.lifecycle.state == "live"
        and statement.predicate in keys
        and statement.qualifier is None
        and statement.subject == SemanticAddress.whole_artifact(subject)
    )


def _changed_subjects(
    current_tree: Mapping[str, bytes], candidate_tree: Mapping[str, bytes]
) -> tuple[str, ...]:
    edits = (
        candidate_tree.edits_from(current_tree)
        if isinstance(candidate_tree, SnapshotTree)
        else None
    )
    paths = edits if edits is not None else candidate_tree
    return tuple(
        sorted(
            path
            for path in paths
            if path.startswith(_SUBJECTS)
            and path.endswith(".json")
            and path in current_tree
            and candidate_tree.get(path) != current_tree.get(path)
        )
    )


def retired_key_claim(claim: ClaimArtifactAny) -> ClaimArtifact:
    """The attributed retirement of one live key Claim, retired with its Subject.

    Its statement, backing and pins stay as accepted: they name the Subject
    version the Claim was accepted about, which the Claim law admits for a Claim
    retired in the change that retires its Subject.
    """

    return ClaimArtifact(
        identity=claim.identity,
        statement=claim.statement,
        backing=claim.backing,
        pins=claim.pins,
        lifecycle=ArtifactLifecycle(
            state="retired", predecessor_digest=claim_artifact_digest(claim).tagged
        ),
        retirement=ClaimRetirementAttribution(reason="superseded"),
    )


def derive_key_claim_retirements(
    *, current_tree: Mapping[str, bytes], candidate_tree: Mapping[str, bytes]
) -> Mapping[str, bytes]:
    """The candidate with each retired keyed Subject's untouched live key Claims retired.

    Only a Subject live in the parent and retired in the candidate counts; a key
    Claim the change itself already revised or retired is left as the change
    wrote it. Returns ``candidate_tree`` itself when nothing is derived.
    """

    derived: dict[str, bytes] = {}
    for path in _changed_subjects(current_tree, candidate_tree):
        if _lifecycle(current_tree, path) != "live" or _lifecycle(candidate_tree, path) != (
            "retired"
        ):
            continue
        kind = path.removeprefix(_SUBJECTS).partition("/")[0]
        keys = _key_predicates(current_tree, candidate_tree, kind)
        for predicate in keys:
            for claim_file, content in _live_claims(current_tree, path, predicate):
                if candidate_tree.get(claim_file) != content:
                    continue  # the change dispositioned it itself
                claim = parse_claim(content, path=claim_file)
                if _is_key_claim(claim, path, keys):
                    derived[claim_file] = render_claim(retired_key_claim(claim))
    if not derived:
        return candidate_tree
    tree = fork_tree(candidate_tree)
    for claim_file, content in derived.items():
        tree[claim_file] = content
    return tree.snapshot()


def carried_key_claim(
    *,
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    dependent_path: str,
    subject_path: str,
) -> bool:
    """Whether ``dependent_path`` is a key Claim its succeeded Subject carries unchanged.

    The Subject stays live in the candidate (a successor, not a retirement), the
    dependent is a live, unqualified key Claim about exactly that Subject, and
    the change left its bytes as they were.
    """

    if not subject_path.startswith(_SUBJECTS) or _lifecycle(candidate_tree, subject_path) != (
        "live"
    ):
        return False
    content = current_tree.get(dependent_path)
    if content is None or candidate_tree.get(dependent_path) != content:
        return False
    kind = subject_path.removeprefix(_SUBJECTS).partition("/")[0]
    keys = _key_predicates(current_tree, candidate_tree, kind)
    if not keys:
        return False
    return _is_key_claim(parse_claim(content, path=dependent_path), subject_path, keys)


def retired_with_subject(
    *,
    candidate_tree: Mapping[str, bytes],
    source_path: str,
    target_name: str,
    pin_role: str,
    expected_digest: str,
) -> bool:
    """Whether a pin names the Subject version a Claim retired with that Subject held.

    The Claim (``source_path``) is retired in the candidate, and the Subject it
    pins (role ``subject``) is retired there too, as the successor of exactly
    the version the pin names: the Claim was retired in the change that retired
    its Subject, and keeps naming what it was accepted about.
    """

    if pin_role != "subject":
        return False
    claim_content = candidate_tree.get(source_path)
    subject_file = f"{_SUBJECTS}{target_name}.json"
    subject_content = candidate_tree.get(subject_file)
    if claim_content is None or subject_content is None:
        return False
    if parse_claim(claim_content, path=source_path).lifecycle.state != "retired":
        return False
    try:
        shell = parse_subject(subject_content, path=subject_file)
    except SubjectFormatError:
        return False
    return (
        shell.lifecycle.state == "retired" and shell.lifecycle.predecessor_digest == expected_digest
    )


__all__ = [
    "carried_key_claim",
    "derive_key_claim_retirements",
    "retired_key_claim",
    "retired_with_subject",
]
