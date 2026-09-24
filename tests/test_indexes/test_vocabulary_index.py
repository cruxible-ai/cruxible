"""The reuse vocabulary index returns exactly the whole-tree oracle's matches."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_type_structure import claim_type_structural_signature
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.discovery import (
    DiscoveryHintsV1,
    ProposedSemanticInterfaceV1,
    ReuseDispositionV1,
    VocabularyReuseRequestV1,
    evaluate_vocabulary_reuse,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import (
    SubjectShell,
    parse_subject,
    render_subject,
    subject_path,
)
from cruxible_core.indexes.evaluated_state import EvaluationRows
from cruxible_core.indexes.typed_state import schema_sql, vocabulary_terms
from cruxible_core.proposals.proposals import _indexed_reuse_interfaces, _reuse_interfaces
from tests.test_claims.test_claims import _claim, _claim_type

DIGEST = "sha256:" + "ab" * 32
COORDINATE = AcceptedCoordinate(
    git_oid="a" * 40,
    semantic_root="sha256:" + "01" * 32,
    generation_root="sha256:" + "02" * 32,
    compiler_digest="sha256:" + "03" * 32,
)


def _type(predicate: str, *, retired: bool = False) -> ClaimType:
    base = _claim_type()
    return base.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "lifecycle": ArtifactLifecycle(state="retired") if retired else base.lifecycle,
        }
    )


def _subject(subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"project.work_item/{subject_id}"),
        subject_kind="project.work_item",
        subject_id=subject_id,
    )


def _descriptor(claim_id: str, predicate: str, subject: str, value: object, *, live=True):
    base = _claim(claim_id=claim_id, capture_digest=DIGEST, source_digest=DIGEST, source_length=4)
    obj = (
        SubjectClaimObject(address=SemanticAddress.whole_artifact(value))
        if predicate in {"semantic.related_to", "semantic.distinct_from"}
        else LiteralClaimObject(value=value)
    )
    claim = base.model_copy(
        update={
            "statement": base.statement.model_copy(
                update={
                    "subject": SemanticAddress.whole_artifact(subject),
                    "claim_type": ArtifactIdentity(kind="ClaimType", name=predicate),
                    "predicate": predicate,
                    "object": obj,
                }
            ),
            "lifecycle": base.lifecycle if live else ArtifactLifecycle(state="retired"),
        }
    )
    return claim_path(claim_id), render_claim(claim)


def _tree() -> dict[str, bytes]:
    tree: dict[str, bytes] = {}
    for predicate, retired in (
        ("project.work_item.status", False),
        ("project.work_item.owner", False),
        ("ops.ticket.state", False),
        ("ops.ticket.phase", True),
    ):
        tree[claim_type_path(predicate)] = render_claim_type(_type(predicate, retired=retired))
    for subject_id in ("alpha", "beta", "status"):
        shell = _subject(subject_id)
        tree[subject_path(shell.subject_kind, shell.subject_id)] = render_subject(shell)
    owner_path = claim_type_path("project.work_item.owner")
    state_path = claim_type_path("ops.ticket.state")
    alpha = subject_path("project.work_item", "alpha")
    for index, (predicate, subject, value, live) in enumerate(
        (
            ("semantic.alias", owner_path, "Assignee", True),
            ("semantic.alias", owner_path, "Lifecycle", True),
            ("semantic.tag", state_path, "workflow", True),
            ("semantic.tag", state_path, "Condition", False),
            ("semantic.related_to", alpha, owner_path, True),
            ("semantic.distinct_from", state_path, alpha, True),
        )
    ):
        path, content = _descriptor(f"CLM-{index:032x}", predicate, subject, value, live=live)
        tree[path] = content
    return tree


class _Rows:
    """EvaluationRows' index reads over an in-memory index of one tree."""

    def __init__(self, tree: Mapping[str, bytes]) -> None:
        self.tree = tree
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(schema_sql())
        for path, content in tree.items():
            if path.startswith("claim-types/"):
                source: object = parse_claim_type(content, path=path)
            elif path.startswith("subjects/"):
                source = parse_subject(content, path=path)
            else:
                source = parse_claim(content, path=path)
            self.connection.executemany(
                "INSERT INTO vocabulary_terms VALUES (?,?,?,?,?)",
                vocabulary_terms(source, path=path),
            )

    def table(self, name: str) -> str:
        return "main." + name

    def source_bytes(self, path: str) -> bytes:
        return self.tree[path]

    vocabulary_matches = EvaluationRows.vocabulary_matches
    vocabulary_descriptors = EvaluationRows.vocabulary_descriptors


@pytest.mark.parametrize(
    "predicate",
    [
        "project.work_item.status",  # exact identity
        "ops.board.status",  # canonical token shared with two types and a Subject id
        "ops.board.assignee",  # an accepted alias
        "ops.board.workflow",  # an accepted tag
        "ops.board.condition",  # a retired descriptor's tag must not match
        "ops.board.phase",  # a retired ClaimType must not match
        "ops.board.untouched",  # only the shared structural signature
    ],
)
def test_indexed_candidates_and_result_digest_equal_the_whole_tree_oracle(predicate) -> None:
    tree = _tree()
    proposal_type = _type(predicate)
    path = claim_type_path(predicate)
    candidate_tree = {**tree, path: render_claim_type(proposal_type)}
    proposal = ProposedSemanticInterfaceV1(
        address=SemanticAddress.whole_artifact(path),
        identity=proposal_type.identity,
        kind="claim-type",
        label=predicate,
        canonical_tokens=tuple(
            sorted({predicate, predicate.rpartition(".")[2]}, key=lambda item: item.encode())
        ),
        structural_signature_digest=claim_type_structural_signature(proposal_type.structure),
    )

    def evidence(interfaces):
        return evaluate_vocabulary_reuse(
            VocabularyReuseRequestV1(
                proposal=proposal,
                hints=DiscoveryHintsV1(),
                disposition=ReuseDispositionV1(kind="new_distinct"),
            ),
            accepted_interfaces=interfaces,
            coordinate=COORDINATE,
            implementation_digest=COORDINATE.compiler_digest,
        )

    oracle = evidence(
        tuple(
            item for item in _reuse_interfaces(candidate_tree) if item.address.artifact_path != path
        )
    )
    indexed_interfaces = _indexed_reuse_interfaces(
        _Rows(candidate_tree), proposal, exclude_path=path
    )
    indexed = evidence(indexed_interfaces)
    assert indexed == oracle
    # Every indexed key is a match condition: the index returns exactly the
    # matching interfaces, never the whole vocabulary.
    assert len(indexed_interfaces) == len(oracle.candidates)
