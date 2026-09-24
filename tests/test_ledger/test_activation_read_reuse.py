"""Activation reuses what this process already proved about immutable ledger objects."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.ledger import git as ledger_git
from cruxible_core.ledger.git import GitLedger
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import TIMESTAMP, seed_claims, work_item_query
from tests.core_support._support import client_material, initialize_local
from tests.test_ledger.test_activation import _sign


def _verifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    command = ledger_git._command

    def counted(args, *rest, **kwargs):
        if "verify-commit" in args:
            calls.append(args[-1])
        return command(args, *rest, **kwargs)

    monkeypatch.setattr(ledger_git, "_command", counted)
    return calls


def test_a_verified_commit_is_not_reverified_and_a_failure_is_never_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = initialize_local(tmp_path)
    ledger = instance._ledger
    signed = ledger.read_main()
    unsigned = ledger._git(["commit-tree", ledger.tree_oid(signed), "-m", "unsigned"])
    unsigned_oid = unsigned.decode().strip()
    ledger_git._VERIFIED_COMMITS.clear()
    calls = _verifications(monkeypatch)

    assert ledger.verify_commit(signed) and ledger.verify_commit(signed)
    assert calls == [signed]
    assert not ledger.verify_commit(unsigned_oid)
    assert not ledger.verify_commit(unsigned_oid)
    assert calls == [signed, unsigned_oid, unsigned_oid]
    # Another handle on the same repository reuses the proof; another signer
    # entry is a different question.
    other = GitLedger(
        ledger.path,
        signing_key_path=ledger._signing_key_path,
        allowed_signers_path=ledger._allowed_signers_path,
    )
    assert other.verify_commit(signed)
    assert calls == [signed, unsigned_oid, unsigned_oid]


def test_batched_proposal_notes_match_single_reads(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    ledger = instance._ledger
    annotated = ledger.read_main()
    bare = ledger._git(["commit-tree", ledger.tree_oid(annotated), "-m", "bare"]).decode().strip()
    ledger.write_proposal_note("evaluation", annotated, b"evaluated\n")
    ledger.write_proposal_note("approval", annotated, b"[]\n")
    pairs = (
        ("evaluation", annotated),
        ("approval", annotated),
        ("evaluation", bare),
        ("approval", bare),
    )

    batched = ledger.read_proposal_notes(pairs)

    assert batched == {pair: ledger.read_proposal_note(*pair) for pair in pairs}
    assert batched[("evaluation", annotated)] == b"evaluated\n"
    assert batched[("approval", bare)] is None


def test_activation_extends_the_proposal_tree_instead_of_rewriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    submitted = submit_query_definition_candidate(
        instance,
        query=work_item_query("reuse.query"),
        actor_id="owner",
        proposal_name="reuse-query",
        timestamp=TIMESTAMP,
    )
    candidate = submitted.proposal.candidate
    assert candidate is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="reviewer",
    )
    extended: list[str] = []
    extend = GitLedger._extend_tree

    def counted(self, base_tree, tree, **kwargs):
        extended.append(base_tree)
        return extend(self, base_tree, tree, **kwargs)

    def full_write(*args, **kwargs):
        pytest.fail("activation rewrote the whole generation tree")

    monkeypatch.setattr(GitLedger, "_extend_tree", counted)
    monkeypatch.setattr(GitLedger, "_write_tree", full_write)

    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.proposal.admission.proposal_id,
        activated_by="owner",
    )

    assert receipt.status == "accepted"
    assert extended == [submitted.proposal.evaluation.evaluated_tree_oid]
    # The stored generation is its proposal tree plus exactly one change-set record.
    ledger = instance._ledger
    proposal = {entry.path: entry.oid for entry in ledger._list_tree(extended[0], with_sizes=False)}
    generation = {
        entry.path: entry.oid for entry in ledger._list_tree(ledger.read_main(), with_sizes=False)
    }
    added = set(generation) - set(proposal)
    assert len(added) == 1 and next(iter(added)).startswith("changesets/")
    assert {path: generation[path] for path in proposal} == proposal
