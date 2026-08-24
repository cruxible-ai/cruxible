"""Every registered member kind's refusals survive the one-evaluator pipeline.

The proposal evaluator used to be two evaluators: one for a single-member
Document, Subject or principal change, and one for everything else. They are now
one pipeline over a dispatch table keyed by member kind, and the refusals that
lived inside the fork moved onto the kinds that own them. A refusal that no test
reaches is a refusal nobody notices losing, so each one is reached here.

Nothing about a verdict changed: every case below was refused before and is
refused now, with the same code and the same message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import render_claim_type
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.subjects import render_subject
from cruxible_core.playbill.proposals import evaluate_proposal_tree
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_change_set_closure import claim_type, subject

TIMESTAMP = "2026-08-16T14:00:00.000000Z"
DOCUMENT_PATH = "documents/dispatch.yaml"
SUBJECT_PATH = "subjects/project.work_item/wi-closure.yaml"
CLAIM_TYPE_PATH = "claim-types/project.work_item/status.yaml"
PRINCIPAL_PATH = "principals/owner.yaml"


def _document(instance: object) -> bytes:
    body = instance.store_document_body(b"# Dispatch\n")  # type: ignore[attr-defined]
    return render_document(
        DocumentShell(
            identity="document:dispatch",
            document_kind="design",
            title="Dispatch",
            media_type="text/markdown",
            body_digest=body.digest,
            authority=DocumentAuthority(
                required_tier="graph_write",
                approval_roles=("owner",),
            ),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        )
    )


def _codes(instance: object, base: dict[str, bytes], proposed: dict[str, bytes]) -> list[str]:
    evaluation = evaluate_proposal_tree(
        base_tree=base,
        current_tree=base,
        proposed_tree=proposed,
        current=instance.accepted_coordinate(),  # type: ignore[attr-defined]
        bodies=instance.body_store(),  # type: ignore[attr-defined]
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )
    assert evaluation.candidate is None
    return [item.code for item in evaluation.diagnostics]


def test_an_empty_change_set_is_refused_as_it_always_was(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert _codes(instance, base, dict(base)) == ["playbill.proposal.non_singleton_scope"]


def test_a_path_no_member_kind_claims_is_refused_as_unregistered(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert _codes(instance, base, {**base, "notes/loose.yaml": b"{}\n"}) == [
        "playbill.proposal.unregistered_semantic_kind"
    ]


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (DOCUMENT_PATH, "playbill.document.removal_unsupported"),
        (SUBJECT_PATH, "playbill.subject.removal_unsupported"),
        (CLAIM_TYPE_PATH, "playbill.change_set.delete_unsupported"),
    ],
)
def test_a_removal_is_refused_by_the_kind_that_owns_the_path(
    tmp_path: Path,
    path: str,
    code: str,
) -> None:
    """The member law states what its own artifacts may never do.

    Document, Subject and principal each keep the bespoke refusal they had when
    they were single-member special cases; a kind that never had one keeps the
    generic change-set refusal.
    """

    instance, _owner = initialize_local(tmp_path)
    accepted = {
        DOCUMENT_PATH: _document(instance),
        SUBJECT_PATH: render_subject(subject()),
        CLAIM_TYPE_PATH: render_claim_type(claim_type("project.work_item.status")),
    }
    base = {**instance.tree_at(instance.accepted_coordinate().git_oid), **accepted}
    proposed = {key: value for key, value in base.items() if key != path}
    assert _codes(instance, base, proposed) == [code]


def test_a_principal_removal_is_refused_by_the_principal_law(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert PRINCIPAL_PATH in base
    proposed = {key: value for key, value in base.items() if key != PRINCIPAL_PATH}
    assert _codes(instance, base, proposed) == ["playbill.principal.removal_unsupported"]


def test_a_principal_change_bundled_with_an_artifact_is_not_recognized(tmp_path: Path) -> None:
    """A key transition may not ride along with artifact edits.

    Whether a change set is a principal-lifecycle transition decides which
    approval purpose it is verified under and whether its actor must
    cryptographically approve it. That question is answered by "is every member a
    principal record", so a change set that is only partly one must never be
    acceptable -- it was refused when principals had their own single-member
    evaluator, and it is refused now that they are a member kind like any other.
    """

    instance, _owner = initialize_local(tmp_path)
    base = instance.tree_at(instance.accepted_coordinate().git_oid)
    record = json.loads(base[PRINCIPAL_PATH])
    rotated = dict(record, public_key="ab" * 32)
    proposed = {
        **base,
        DOCUMENT_PATH: _document(instance),
        PRINCIPAL_PATH: canonical_bytes(rotated) + b"\n",
    }
    codes = _codes(instance, base, proposed)
    assert "playbill.proposal.unregistered_semantic_kind" in codes


def test_a_malformed_member_is_a_format_refusal_not_an_escaping_parse_error(
    tmp_path: Path,
) -> None:
    """Parseability is decided before anything asks what a member is.

    The dependency index is only advanced once every scoped member has been
    proven parseable, so a malformed member is refused by the evaluator rather
    than escaping as a parse failure from a cache update nobody asked for. The
    refusal is the one the member's own kind declares: Document, Subject and
    ClaimType each had a format refusal of their own before the evaluators
    merged, and a kind that never had one takes the change set's.
    """

    instance, _owner = initialize_local(tmp_path)
    base = instance.tree_at(instance.accepted_coordinate().git_oid)
    expected = {
        DOCUMENT_PATH: "playbill.document.format_invalid",
        SUBJECT_PATH: "playbill.subject.format_invalid",
        CLAIM_TYPE_PATH: "playbill.claim_type.format_invalid",
        "providers/dispatch.yaml": "playbill.proposal.member_format_invalid",
    }
    for path, code in expected.items():
        assert _codes(instance, base, {**base, path: b"not-json\n"}) == [code]
