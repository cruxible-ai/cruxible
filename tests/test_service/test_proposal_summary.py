"""Factored review pages stay bounded and preserve exact template membership."""

from __future__ import annotations

import json
from collections import Counter
from types import SimpleNamespace

import pytest

from cruxible_client.contracts import ProposalReviewSummary
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.proposal_models import OriginatingLine
from cruxible_client.contracts.refs import Ref
from cruxible_core.service.proposals.review import ProposalReview, service_review_playbill_proposal
from cruxible_core.service.proposals.summary import BATCH_SIZE
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures import test_claims_terminal as claims
from tests.test_procedures import test_procedure_proposal_delivery as delivery

ACCESS = BodyAccessContext(principal_id="test", can_read_body=True)


def _review(instance, proposal_id, **options):
    return service_review_playbill_proposal(
        instance, proposal_id=proposal_id, access=ACCESS, **options
    )


def test_thousand_item_summary_is_small_exact_and_warm_reads_are_bounded(tmp_path, monkeypatch):
    original_contract = claims.record_contract

    def roomy():
        contract = original_contract()
        return contract.model_copy(
            update={
                "selection_budget": contract.selection_budget.model_copy(
                    update={"max_bytes": 1 << 20}
                )
            }
        )

    monkeypatch.setattr(claims, "record_contract", roomy)
    instance, root = claims.claims_world(
        tmp_path, {"entries": [{"id": "watched", "status": "open"}]}
    )
    from cruxible_client.contracts.authoring.inputs import ProcedureRuntimePolicyInput
    from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
    from tests.support.operation_world import author

    author(
        instance,
        ProcedureRuntimePolicyInput(
            kind="procedure_runtime_policy",
            procedure_runtime_policy=ProcedureRuntimePolicy(
                provider_output_bytes_cap=16 << 20, result_bytes_cap=16 << 20
            ),
        ),
        name="large-review-budget",
    )
    spec = claims.claims_spec()
    spec = spec.model_copy(
        update={
            "hard_caps": spec.hard_caps.model_copy(
                update={"result_bytes": 16 << 20, "capture_bytes": 16 << 20}
            )
        }
    )
    terminal = spec.steps[-1]
    terminal = terminal.model_copy(
        update={
            "subject": terminal.subject.model_copy(update={"key": {"cve_id": "watched"}}),
            "claims": (
                terminal.claims[0].model_copy(
                    update={"qualifier": Ref(root="item", path=("key", "cve_id"))}
                ),
            ),
        }
    )
    spec = spec.model_copy(update={"steps": (*spec.steps[:-1], terminal)})
    claims.install(instance, spec)
    (root / "data/doc.json").write_text(
        json.dumps({"entries": [{"id": f"row-{i:04}", "status": "open"} for i in range(1000)]})
    )
    state = claims.run(instance, root)
    assert state.status == "succeeded", state.terminal.model_dump_json(indent=2)
    proposal_id = state.terminal_egress[0].proposal_id
    full = _review(instance, proposal_id, detail=True)
    assert isinstance(full, ProposalReview)
    assert len(full.members) == 1000
    before_size = len(full.model_dump_json().encode())
    # Summary construction cannot call the full-review builder or read all blobs at once.
    from cruxible_core.service.proposals import review as review_module

    monkeypatch.setattr(
        review_module, "_review_members", lambda *_a, **_k: pytest.fail("full review materialized")
    )
    original = instance.proposal_blobs
    reads = []

    def bounded(tree, paths, **kwargs):
        reads.append(len(paths))
        assert len(paths) <= BATCH_SIZE
        return original(tree, paths, **kwargs)

    monkeypatch.setattr(instance, "proposal_blobs", bounded)
    summary = _review(instance, proposal_id)
    assert isinstance(summary, ProposalReviewSummary)
    assert summary.counts.created == 1000
    assert summary.group_count == 1
    (group,) = summary.groups
    assert group.counts.created == len(full.members)
    assert summary.subject_kinds[claims.KIND].created == 1000
    assert group.template == 0 and group.node == "write"
    assert group.shared_fields["after.predicate"].value == claims.PREDICATE
    assert "after.qualifier" in group.varying_columns
    assert all("predicate" not in row.after for row in group.rows)
    summary_size = len(summary.model_dump_json().encode())
    assert summary_size < 32_000
    assert summary_size * 100 < before_size
    assert reads and max(reads) <= BATCH_SIZE
    # Warm paging must not decode the candidate or read any proposal member.
    monkeypatch.setattr(
        type(instance.proposal_evidence()),
        "read_candidate",
        lambda *_a, **_k: pytest.fail("warm candidate read"),
    )
    monkeypatch.setattr(instance, "proposal_blobs", lambda *_a, **_k: pytest.fail("warm blob read"))
    next_page = _review(
        instance, proposal_id, summary_group=group.group, rows_after=group.next_cursor
    )
    assert {r.path for r in next_page.groups[0].rows}.isdisjoint(r.path for r in group.rows)
    expected = sorted(m.path for m in full.members)
    assert [r.path for r in next_page.groups[0].rows] == expected[5:10]
    assert next_page.groups[0].counts == group.counts
    (tmp_path / "summary-sizes.json").write_text(
        json.dumps(
            {"full_bytes": before_size, "summary_bytes": summary_size, "members": len(full.members)}
        )
    )


def test_line_attribution_is_exact_and_direct_submissions_have_none(tmp_path):
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest
    from cruxible_core.service.discovery.get import service_playbill_get

    instance, _owner, root, line, _procedure = delivery.proposal_world(tmp_path)
    state = delivery.run_line(instance, root, line)
    assert state.status == "succeeded", state.terminal
    proposal_id = state.terminal_egress[0].proposal_id
    expected = OriginatingLine(
        identity=line.identity, artifact_digest=line_spec_digest(line).tagged
    )
    assert instance.proposal_evidence().read_admission(proposal_id).originating_line == expected
    from cruxible_client.contracts.proposal_models import ProposalAdmissionRecord
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
    from cruxible_core.proposals.proposal_notes import admission_bytes

    admitted = instance.proposal_evidence().read_admission(proposal_id)
    changed = admitted.model_copy(
        update={
            "originating_line": expected.model_copy(
                update={"artifact_digest": "sha256:" + "a" * 64}
            )
        }
    )
    with pytest.raises(ProposalIntegrityError, match="identity"):
        ProposalEvidenceStore.parse_model_bytes(
            admission_bytes(changed),
            ProposalAdmissionRecord,
            label="proposal admission",
            render=admission_bytes,
        )
    assert _review(instance, proposal_id).originating_line == expected
    assert _review(instance, proposal_id, detail=True).originating_line == expected
    get = service_playbill_get(instance, request=GetRequest(ref=proposal_id), access=ACCESS)
    assert get.card.originating_line == expected

    from tests.test_proposals.test_proposal_notes import _submit

    manual = _submit(instance)
    assert manual.admission.originating_line is None
    assert _review(instance, manual.admission.proposal_id).originating_line is None


def test_projection_orders_problems_then_revisions_retirements_and_pages_groups(tmp_path):
    """Mixed actions exercise the same cold builder used for admitted candidates."""
    import sqlite3

    from cruxible_core.service.proposals.summary import _SCHEMA, _build

    candidate_id = "sha256:" + "1" * 64
    members, evidence, base, proposed = [], [], {}, {}
    for i, disposition in enumerate(("create", "replace", "retire", "create")):
        path = f"subjects/test.kind/{i}.json"
        old = {"subject_kind": "test.kind", "subject_id": str(i), "status": "old"}
        new = {**old, "status": "new"}
        if disposition != "create":
            base[path] = json.dumps(old).encode()
        proposed[path] = json.dumps(new).encode()
        members.append(SimpleNamespace(path=path, artifact_kind="subject", disposition=disposition))
        diagnostic = SimpleNamespace(model_dump=lambda **_: {"problem": "attention"})
        evidence.append(SimpleNamespace(path=path, diagnostics=(diagnostic,) if i == 3 else ()))
    instance = SimpleNamespace(
        blobs_at=lambda _, paths: {p: base[p] for p in paths if p in base},
        proposal_blobs=lambda _, paths, **__: {p: proposed[p] for p in paths},
    )
    candidate = SimpleNamespace(
        candidate_digest=candidate_id, members=members, law_evidence=evidence
    )
    with sqlite3.connect(tmp_path / "projection.sqlite3") as connection:
        connection.executescript(_SCHEMA)
        _build(connection, instance, candidate, "base", "tree", "proposal")
        groups = [
            json.loads(row[0])
            for row in connection.execute("SELECT data FROM review_groups ORDER BY priority,id")
        ]
        assert [g["disposition"] for g in groups][1:] == ["replace", "retire"] or [
            g["disposition"] for g in groups
        ][1:] == ["retire", "replace"]
        assert groups[0]["disposition"] == "create"  # problem beats disposition
        assert Counter(
            {
                key: sum(g["counts"][key] for g in groups)
                for key in ("created", "revised", "retired")
            }
        ) == {"created": 2, "revised": 1, "retired": 1}
        rows = connection.execute(
            "SELECT path FROM review_rows "
            "WHERE group_id=(SELECT id FROM review_groups WHERE priority=-1) ORDER BY problem,path"
        ).fetchall()
        assert rows[0][0].endswith("/3.json")


def test_originating_line_rejects_non_line_and_invalid_digest():
    from pydantic import ValidationError

    from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest

    with pytest.raises(ValidationError):
        ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/forged",
            proposed_base_oid="a" * 40,
            originating_line={
                "identity": {"kind": "Line", "name": "forged"},
                "artifact_digest": "sha256:" + "a" * 64,
            },
        )
    with pytest.raises(ValidationError):
        OriginatingLine(
            identity=ArtifactIdentity(kind="Procedure", name="wrong"),
            artifact_digest="sha256:" + "a" * 64,
        )
    with pytest.raises(ValidationError):
        OriginatingLine(identity=ArtifactIdentity(kind="Line", name="test"), artifact_digest="bad")


def test_group_paging_template_identity_and_cursor_binding(tmp_path):
    second = delivery.item_template(
        statement={**delivery.item_template()["statement"], "qualifier": "reported"}
    )
    instance, _owner, root, line, _procedure = delivery.proposal_world(
        tmp_path, templates=(delivery.item_template(), second)
    )
    state = delivery.run_line(instance, root, line)
    assert state.status == "succeeded", state.terminal
    proposal_id = state.terminal_egress[0].proposal_id
    first = _review(instance, proposal_id, groups_limit=1)
    assert first.group_count == 2 and first.next_cursor
    second_page = _review(instance, proposal_id, groups_limit=1, summary_after=first.next_cursor)
    assert second_page.next_cursor is None
    assert {g.template for g in (*first.groups, *second_page.groups)} == {0, 1}
    assert first.groups[0].group != second_page.groups[0].group
    assert sum(g.counts.created for g in (*first.groups, *second_page.groups)) == 2
    with pytest.raises(ProposalIntegrityError, match="cursor"):
        _review(
            instance, proposal_id, summary_group=first.groups[0].group, rows_after=first.next_cursor
        )
    with pytest.raises(ProposalIntegrityError, match="cursor"):
        _review(instance, proposal_id, summary_after="bad")


def test_admitted_mixed_proposal_preserves_before_after_and_action_counts(tmp_path):
    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        ClaimRetirementInput,
        lower_authoring_input,
    )
    from cruxible_client.contracts.claims import parse_claim
    from tests.core_support._identity_support import KIND, seed_identity_surface
    from tests.support.operation_world import author
    from tests.test_service.test_subject_identity import _alias_member, submit

    instance = seed_identity_surface(tmp_path)
    created = author(
        instance,
        ChangeSetInput(
            kind="change_set",
            members=(
                _alias_member(KIND, "alpha", "Alpha before"),
                _alias_member(KIND, "beta", "Beta before"),
            ),
        ),
        name="seed-aliases",
    )
    previous = [
        parse_claim(raw, path=path) for path, raw in created.items() if path.startswith("claims/")
    ]
    previous.sort(key=lambda claim: claim.statement.subject.artifact_path)
    revised, retired = previous
    submitted = submit(
        instance,
        lower_authoring_input(
            ChangeSetInput(
                kind="change_set",
                members=(
                    _alias_member(KIND, "alpha", "New alias").model_copy(
                        update={"qualifier": "additional"}
                    ),
                    _alias_member(KIND, "alpha", "Alpha after").model_copy(
                        update={"revises": revised.identity.name}
                    ),
                    ClaimRetirementInput(
                        kind="claim_retirement", retires=retired.identity.name, reason="was-wrong"
                    ),
                ),
            )
        ),
    )
    proposal_id = submitted.status.proposal_id
    assert proposal_id, submitted.intent.last_preflight.model_dump_json(indent=2)
    full = _review(instance, proposal_id, detail=True)
    summary = _review(instance, proposal_id)
    assert summary.counts.model_dump() == {"created": 1, "revised": 1, "retired": 1}
    assert Counter(member.disposition for member in full.members) == {
        "create": 1,
        "replace": 1,
        "retire": 1,
    }
    assert [group.disposition for group in summary.groups][-1] == "create"
    revision = next(group for group in summary.groups if group.disposition == "replace")
    assert revision.shared_fields["before.object"].value == revised.statement.object.model_dump(
        mode="json"
    )
    changed = next(member for member in full.members if member.disposition == "replace")
    assert (
        revision.shared_fields["after.object"].value
        == changed.candidate_semantic_artifact["statement"]["object"]
    )
