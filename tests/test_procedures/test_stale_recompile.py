"""Retained Procedure requests recompile without re-running their providers."""

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicy,
    render_approval_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.recompilation import ProposalRecompiled
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_inspect_playbill_proposal,
)
from cruxible_core.service.proposals.proposals import _proposal_result
from cruxible_core.service.proposals.recompile import recompile_proposal
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import proposal_world, run_line
from tests.test_procedures.test_procedure_settlement_delivery import (
    _level_item,
    _level_kind_members,
)


@pytest.mark.parametrize("required", [False, True])
def test_eight_distinct_product_proposals_recompile_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    # A Subject item plus its key Claim is the SBOM product-creation shape.
    instance, owner, root, line, procedure = proposal_world(tmp_path, templates=(_level_item(),))
    original_mandate = fixtures._line_mandate(procedure)
    mandate = original_mandate.model_copy(
        update={
            "namespace": ("claims", "subjects"),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(original_mandate).tagged
            ),
        }
    )
    lines = [
        line.model_copy(
            update={"identity": ArtifactIdentity(kind="Line", name=f"product-import-{i}")}
        )
        for i in range(8)
    ]
    fixtures._accept_more(
        instance,
        owner,
        {
            **_level_kind_members(rules=False),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            **{line_spec_path(item.identity.name): render_line_spec(item) for item in lines},
        },
        name="parallel-import-lines",
    )
    if required:
        fixtures._accept_more(
            instance,
            owner,
            {
                APPROVAL_POLICY_PATH: render_approval_policy(
                    ApprovalPolicy(mode="independent_approval_required")
                )
            },
            name="require-review",
        )
    proposals = []
    for index, item in enumerate(lines):
        (root / fixtures.RELATIVE_PATH).write_bytes(
            canonical_bytes({"advisory_id": f"product-{index}", "severity": f"product-{index}"})
        )
        state = run_line(instance, root, item, at=fixtures.NOW + timedelta(seconds=index))
        assert state.status == "succeeded", state.terminal
        (egress,) = state.terminal_egress
        assert egress.proposal_id is not None
        proposals.append(egress.proposal_id)
    # First acceptance advances the common head. Later activations must lower
    # from their own retained requests, with the original ids and times.
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=proposals[0])
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("recompilation ran a provider")

    monkeypatch.setattr(fixtures._WorkspaceInvoker, "invoke_provider", forbidden)
    for proposal_id in proposals[1:]:
        source = _proposal_result(instance, proposal_id)
        oracle = recompile_proposal(instance, source, preview=True)
        assert oracle is not None and oracle.proposal.candidate is not None
        result = service_activate_playbill_proposal(
            instance, proposal_id=proposal_id, activated_by="owner"
        )
        assert result.proposal_id != proposal_id
        if required:
            assert isinstance(result, ProposalRecompiled) and result.status == "pending_review"
            assert instance.proposal_evidence().read_approvals(result.candidate_digest) == ()
        else:
            assert result.status == "accepted"
        successor = _proposal_result(instance, result.proposal_id)
        assert successor.candidate is not None
        assert successor.candidate.members == oracle.proposal.candidate.members
        assert successor.admission.admitted_at == source.admission.admitted_at


def test_missing_prepared_journal_leaves_the_proposal_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.errors import ProposalStale
    from cruxible_core.exhaust.backends import LocalJournalBackend

    instance, _owner, root, line, _ = proposal_world(tmp_path)
    state = run_line(instance, root, line)
    (egress,) = state.terminal_egress
    assert egress.proposal_id is not None
    source = _proposal_result(instance, egress.proposal_id)
    select = LocalJournalBackend.select_records

    def records(self, stream, **filters):  # type: ignore[no-untyped-def]
        if filters.get("run_id") == state.run_id and filters.get("event_kind") == "terminal_egress":
            return ()
        return select(self, stream, **filters)

    monkeypatch.setattr(LocalJournalBackend, "select_records", records)
    before = instance.proposal_evidence().list_admissions()
    with pytest.raises(ProposalStale) as refusal:
        recompile_proposal(instance, source)
    assert refusal.value.reason == "input_unavailable"
    assert instance.proposal_evidence().list_admissions() == before
    assert instance.proposal_evidence().read_withdrawal(egress.proposal_id) is None


def test_procedure_recompile_matches_fresh_prepare_at_head_and_never_lowers_at_old_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.procedures.egress import PreparedTerminalEgressV1, TerminalEgressRequestV2
    from cruxible_core.procedures.proposal_delivery import (
        ProposalTerminalEgressSink,
        proposal_lowering_digest,
    )
    from cruxible_core.service.change_preview import preview_guards
    from cruxible_core.service.procedures.procedure_runs import (
        _accepted_line_mandates,
        _accepted_procedure,
    )
    from tests.test_procedures.test_procedure_settlement_delivery import _egress_records

    instance, owner, root, line, procedure = proposal_world(tmp_path)
    state = run_line(instance, root, line)
    (egress,) = state.terminal_egress
    assert egress.proposal_id is not None
    (payload,) = [
        item for item in _egress_records(instance, state) if item.get("verdict") == "prepared"
    ]
    request = TerminalEgressRequestV2.model_validate(payload["request"])
    admission = fixtures._admission(instance, state)
    fixtures._accept_more(instance, owner, _level_kind_members(rules=False), name="oracle-head")
    head = instance.accepted_coordinate()
    accepted = _accepted_procedure(instance, name=procedure.identity.name, coordinate=head)
    mandates = _accepted_line_mandates(
        instance, accepted, coordinate=head, evaluation_time=request.evaluation_time
    )
    sink = ProposalTerminalEgressSink(instance=instance, accepted_mandates=dict(mandates))
    original = instance.immutable_tree_at

    def tree_at(oid: str):  # type: ignore[no-untyped-def]
        assert oid != request.accepted_coordinate.git_oid
        return original(oid)

    monkeypatch.setattr(instance, "immutable_tree_at", tree_at)
    with preview_guards(True):
        fresh = sink.prepare_terminal_egress(
            request=request.model_copy(
                update={"accepted_coordinate": AcceptedCoordinate.from_internal(head)}
            ),
            admission=admission,
            evidence=payload["evidence"],
            journaled=PreparedTerminalEgressV1.model_validate(payload["prepared"]),
        )
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=egress.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    result = _proposal_result(instance, receipt.proposal_id)
    tree = instance._ledger.blobs_at(result.admission.candidate_tree_oid, fresh.target_paths)
    assert (
        proposal_lowering_digest(tuple((path, tree[path]) for path in fresh.target_paths))
        == fresh.lowering_digest
    )
