"""Retained Procedure requests recompile without re-running their providers."""

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicy,
    render_approval_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedure_mandates import (
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
    mandate = fixtures._line_mandate(procedure).model_copy(
        update={"namespace": ("claims", "subjects")}
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
