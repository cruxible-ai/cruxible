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


# --- a Line proposal recompiles at the head (no replay of the old decision) ----


def _two_line_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """One fan-out Procedure behind two accepted Lines: they propose independently."""

    from tests.test_procedures.test_proposal_fan_out import FAN_OUT

    instance, owner, root, line, _procedure = proposal_world(
        tmp_path,
        templates=FAN_OUT,  # type: ignore[arg-type]
    )
    other = line.model_copy(update={"identity": ArtifactIdentity(kind="Line", name="other-import")})
    fixtures._accept_more(
        instance,
        owner,
        {line_spec_path(other.identity.name): render_line_spec(other)},
        name="other-import-line",
    )
    return instance, owner, root, line, other


def _delivered(state):  # type: ignore[no-untyped-def]
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered" and egress.proposal_id is not None, egress
    return egress.proposal_id


def _readmit(instance, proposal_id: str, *, dry_run: bool = False):  # type: ignore[no-untyped-def]
    from cruxible_core.service.proposals.proposals import service_readmit_playbill_proposal

    return service_readmit_playbill_proposal(
        instance, proposal_id=proposal_id, actor_id="owner", dry_run=dry_run
    )


def test_a_line_proposal_partly_live_at_the_head_readmits_what_is_left(tmp_path: Path) -> None:
    """Readmit re-decides the Line's open proposal at the head instead of replaying it.

    The proposal restates a value another Line's proposal landed first. Its
    journaled Line decision was made about the whole change, so replaying it
    refused (``effectful_operation_payload_mismatch``) and readmit named itself
    as the repair. Now the successor carries what is left, and the Line's open
    proposal moves to it: the Line's next run finds it still current.
    """

    from cruxible_core.exhaust.line_dispatch import LineDispatchStore
    from cruxible_core.service.proposals.proposals import service_playbill_proposal_status
    from tests.test_procedures.test_proposal_fan_out import _element, _write_feed

    instance, owner, root, line, other = _two_line_world(tmp_path)
    _write_feed(root, [_element(None)])
    first = _delivered(run_line(instance, root, other))
    _write_feed(root, [_element(None), _element("vendor")])
    later = fixtures.NOW + timedelta(minutes=1)
    stale = _delivered(run_line(instance, root, line, at=later))
    accept_proposal(instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first))
    assert service_playbill_proposal_status(instance, proposal_id=stale).status == (
        "awaiting_readmit"
    )

    readmitted = _readmit(instance, stale)

    assert readmitted.status == "readmitted" and readmitted.proposal is not None
    successor = readmitted.proposal.proposal
    assert successor.candidate is not None

    def claim_paths(proposal_id: str) -> set[str]:
        candidate = _proposal_result(instance, proposal_id).candidate
        assert candidate is not None
        return {member.path for member in candidate.members if member.path.startswith("claims/")}

    # At the head the restated value is the landed Claim's lineage (its new
    # content refreshes that Claim's evidence), not the new Claim the stale
    # proposal created at its old base; the vendor value is still new.
    (landed,) = claim_paths(first)
    source = claim_paths(stale)
    assert landed not in source and len(source) == 2
    after = claim_paths(successor.admission.proposal_id)
    assert landed in after and len(after) == 2 and len(after & source) == 1
    from cruxible_client.contracts.procedures.line_specs import line_identity_digest
    from cruxible_core.service.procedures.procedure_runs import (
        service_get_playbill_procedure_run,
    )

    run = service_get_playbill_procedure_run(instance, run_id=_run_of(instance, stale))
    (node_id,) = {egress.node_id for egress in run.terminal_egress}
    record = LineDispatchStore(instance).open_proposal(line_identity_digest(line.identity), node_id)
    assert record is not None and record.proposal_id == successor.admission.proposal_id
    assert record.superseded == stale

    again = run_line(instance, root, line, at=later + timedelta(minutes=1))

    assert again.status == "succeeded", again.terminal
    (egress,) = again.terminal_egress
    assert egress.proposal_id is None, egress  # no duplicate of the successor
    assert egress.line_open_proposal is not None
    assert egress.line_open_proposal.outcome == "still_current"
    assert egress.line_open_proposal.proposal_id == successor.admission.proposal_id


def _run_of(instance, proposal_id: str) -> str:  # type: ignore[no-untyped-def]
    """The run whose terminal proposed ``proposal_id``, from its item reasons."""

    source = _proposal_result(instance, proposal_id)
    operation = source.admission.procedure_operation
    assert operation is not None and source.candidate is not None
    for member in source.candidate.members:
        reason = instance.proposal_evidence().read_item_reason(operation, member.path)
        if reason is not None:
            return reason.run_id
    raise AssertionError("no item reason")


def test_a_line_proposal_wholly_live_at_the_head_closes_as_answered(tmp_path: Path) -> None:
    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore
    from cruxible_core.service.proposals.proposals import (
        service_playbill_proposal_status,
        stale_unreadmitted_proposals,
    )
    from cruxible_core.service.proposals.recompile import ALREADY_LIVE_REASON
    from tests.test_procedures.test_proposal_fan_out import _element, _write_feed

    instance, owner, root, line, other = _two_line_world(tmp_path)
    _write_feed(root, [_element(None)])
    first = _delivered(run_line(instance, root, other))
    stale = _delivered(run_line(instance, root, line, at=fixtures.NOW + timedelta(minutes=1)))
    accept_proposal(instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first))
    (live,) = [
        member.path.rsplit("/", 1)[-1].removesuffix(".json")
        for member in _proposal_result(instance, first).candidate.members  # type: ignore[union-attr]
        if member.path.startswith("claims/")
    ]
    admissions = len(instance.proposal_evidence().list_admissions())
    # Index the head before the read-only preview fence, as a status read does.
    assert service_playbill_proposal_status(instance, proposal_id=stale).status == (
        "awaiting_readmit"
    )

    preview = _readmit(instance, stale, dry_run=True)
    assert (preview.status, preview.proposal) == ("already_live", None)
    assert instance.proposal_evidence().read_withdrawal(stale) is None  # a preview closes nothing

    answered = _readmit(instance, stale)

    assert (answered.status, answered.proposal) == ("already_live", None)
    assert answered.accepted_coordinate is not None
    closed = instance.proposal_evidence().read_withdrawal(stale)
    assert closed is not None and closed.reason == ALREADY_LIVE_REASON
    assert closed.superseded_by is None and closed.actor_id == "owner"
    entry = service_playbill_proposal_status(instance, proposal_id=stale)
    assert (entry.status, entry.terminal_reason) == ("settled", "withdrawn")
    assert len(instance.proposal_evidence().list_admissions()) == admissions  # nothing proposed
    head = instance.accepted_coordinate()
    from cruxible_client.contracts.projection import AcceptedCoordinate

    assert stale_unreadmitted_proposals(instance, AcceptedCoordinate.from_internal(head)) == ()
    # The unchanged item's re-observation is recorded, as delivery records it.
    assert len(ClaimReobservationStore(instance).history(live)) == 1


def test_a_refused_line_recompile_names_the_lines_next_run_not_readmit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.errors import ProposalReadmitRefused
    from cruxible_core.procedures.proposal_delivery import ProposalTerminalEgressSink
    from cruxible_core.procedures.terminal_services import ProposalDeliveryRefused
    from cruxible_core.service.proposals.proposals import service_playbill_proposal_status

    instance, owner, root, line, _procedure = proposal_world(tmp_path)
    stale = _delivered(run_line(instance, root, line))
    fixtures._accept_more(instance, owner, _level_kind_members(rules=False), name="move-head")

    def refuse(self, **kwargs):  # type: ignore[no-untyped-def]
        raise ProposalDeliveryRefused("proposal_lowering_refused", "The terminal no longer lowers.")

    monkeypatch.setattr(ProposalTerminalEgressSink, "prepare_recompilation", refuse)
    with pytest.raises(ProposalReadmitRefused) as refused:
        _readmit(instance, stale)

    error = refused.value
    assert error.error_code == "cruxible.proposal.readmit_refused"
    assert (error.reason, error.origin, error.origin_name) == (
        "proposal_lowering_refused",
        "line",
        line.identity.name,
    )
    assert error.repair is not None
    assert (error.repair.operation, error.repair.arguments) == (
        "cruxible.line.run",
        {"line": line.identity.name},
    )
    message = str(error)
    assert f"cruxible line run {line.identity.name}" in message
    assert f"cruxible proposal withdraw {stale}" in message
    assert "proposal readmit" not in message
    # Nothing is dropped: the proposal still awaits its author.
    assert service_playbill_proposal_status(instance, proposal_id=stale).status == (
        "awaiting_readmit"
    )
