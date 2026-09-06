"""A Line's `propose_change_set` terminal produces a durable, reviewable proposal.

These tests fail on the old missing bridge: before it, every terminal ended in
`terminal_not_available`, no proposal ref was created, and run state carried
no proposal identity at all. They drive the served Line lane end to end --
workspace Source, produced Capture, computed interpretation, rung-2 mandate,
shared authoring lowering, proposal receive -- and then act as the manager,
retrieving the exact candidate, activating it through the existing door, and
reading the accepted Claim back to its Capture and run.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import capture_contract_digest, parse_capture_envelope
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactV2,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecV2,
    line_identity_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import ProposeChangeSetNodeV3
from cruxible_client.contracts.procedures.results import ProcedureNodeRefusalV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.terminal_services import proposal_terminal_ref
from cruxible_core.playbill.service.documents import service_inspect_playbill_proposal
from cruxible_core.service.playbill_procedure_runs import (
    LineRunRequestV1,
    service_get_playbill_procedure_run,
    service_run_playbill_line,
)
from tests.test_playbill import test_procedure_source_runs as fixtures
from tests.test_playbill._knowledge_loop_support import accept_proposal
from tests.test_playbill._pc_c_support import capture_contract

SUBJECT_KIND = "security.advisory"
SUBJECT_ID = "osv-2026-0001"
PREDICATE = "security.advisory.severity"
NOW = fixtures.NOW


def _claim_type(contract_digest: str, *, roles: tuple[str, ...] = ("observation",)) -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
        predicate=PREDICATE,
        allowed_subject_kinds=(SUBJECT_KIND,),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="workspace-record",
                    claim_roles=roles,
                    capture_contract_digests=(contract_digest,),
                    evidence_kinds=("database_record",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _subject() -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/{SUBJECT_ID}"),
        subject_kind=SUBJECT_KIND,
        subject_id=SUBJECT_ID,
    )


def item_template(**overrides: Any) -> dict[str, object]:
    """One `propose_change_set` item: a statement over the shaped Source output."""

    template: dict[str, object] = {
        "tag": "playbill-procedure-claim-proposal-item-v1",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": SemanticAddress.whole_artifact(
                subject_path(SUBJECT_KIND, SUBJECT_ID)
            ).model_dump(mode="json"),
            "predicate": PREDICATE,
            "qualifier": None,
            "object": {"kind": "literal", "value": "$steps.result.severity"},
            "role": "observation",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": "Severity as read from the accepted advisory document.",
        "revises": None,
    }
    template.update(overrides)
    return template


def terminal_procedure(
    procedure: ProcedureArtifactV2,
    *,
    templates: tuple[object, ...] | None = None,
) -> ProcedureArtifactV2:
    """The Source Procedure with its shaped row flowing into a rung-2 terminal."""

    source, shape = procedure.definition.nodes
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                source,
                shape.model_copy(update={"next": "propose"}),
                ProposeChangeSetNodeV3(
                    node_id="propose",
                    candidate_templates=templates or (item_template(),),
                ),
            ),
        }
    )
    return procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
        }
    )


def _line(procedure: ProcedureArtifactV2, policy: Any) -> LineSpecV2:
    return fixtures._served_line(procedure, policy).model_copy(
        update={"requested_terminal_rung": 2}
    )


def proposal_world(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    templates: tuple[object, ...] | None = None,
    mandate: ProcedureMandateV1 | None | bool = True,
    claim_type: ClaimType | None = None,
    requested_terminal_rung: int = 2,
):
    """Accept everything the loop needs; return the instance, owner, root, Line."""

    instance, owner, procedure, root, policy = fixtures._world(tmp_path, accept_procedure=False)
    with_terminal = terminal_procedure(procedure, templates=templates)
    line = _line(with_terminal, policy).model_copy(
        update={"requested_terminal_rung": requested_terminal_rung}
    )
    contract = capture_contract()
    accepted_claim_type = claim_type or _claim_type(capture_contract_digest(contract).tagged)
    members = {
        procedure_path(fixtures.PROCEDURE_NAME): render_procedure(with_terminal),
        line_spec_path(line.identity.name): render_line_spec(line),
        claim_type_path(accepted_claim_type.predicate): render_claim_type(accepted_claim_type),
        subject_path(SUBJECT_KIND, SUBJECT_ID): render_subject(_subject()),
    }
    accepted_mandate: ProcedureMandateV1 | None
    if mandate is True:
        accepted_mandate = fixtures._line_mandate(with_terminal)
    elif mandate is False or mandate is None:
        accepted_mandate = None
    else:
        accepted_mandate = mandate
    if accepted_mandate is not None:
        members[procedure_mandate_path(accepted_mandate.identity.name)] = render_procedure_mandate(
            accepted_mandate
        )
    fixtures._accept_more(instance, owner, members, name="proposal-world")
    return instance, owner, root, line, with_terminal


def run_line(instance, root, line, *, at: datetime = NOW, invoker=None):  # type: ignore[no-untyped-def]
    spawned = fixtures._WorkspaceInvoker() if invoker is None else invoker
    identity_digest = line_identity_digest(line.identity)
    return service_run_playbill_line(
        instance,
        path_identity_digest=identity_digest,
        request=LineRunRequestV1(
            line_identity_digest=identity_digest,
            occurrence_id=None,
            evaluation_time=None,
        ),
        actor_context=fixtures._actor(instance).model_copy(update={"timestamp": at}),
        caller_rung=2,
        provider_runtime_operator=fixtures._Operator(spawned),  # type: ignore[arg-type]
        workspace_file_reader=fixtures._reader(instance, root),
        daemon_clock=fixtures._TestClock(at),
    )


def _proposal_refs(instance: PlaybillInstance) -> list[str]:
    """Every proposal ref a Procedure terminal created; setup proposals are not these."""

    return sorted(
        record.target_ref
        for record in instance.proposal_evidence().list_admissions()
        if "/procedure-" in record.target_ref
    )


# --- the public loop ---------------------------------------------------------


def test_a_line_terminal_produces_a_proposal_the_manager_accepts_and_reads_back(
    tmp_path: Path,
) -> None:
    instance, owner, root, line, procedure = proposal_world(tmp_path)
    base = instance.accepted_coordinate()

    state = run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    assert state.run_id is not None
    # The run observed the workspace file and retained the Capture it became.
    assert len(state.source_observations) == 1
    observation = state.source_observations[0]
    assert observation.capture_digest is not None
    # The terminal delivered exactly one proposal, and names it.
    assert len(state.terminal_egress) == 1
    egress = state.terminal_egress[0]
    assert egress.node_id == "propose"
    assert egress.kind == "propose_change_set"
    assert egress.verdict == "delivered"
    assert egress.effective_rung == 2
    assert egress.operation_key is not None
    assert egress.procedure_mandate_digest is not None
    assert egress.proposal_id is not None and egress.candidate_digest is not None
    assert len(egress.children) == 1
    child = egress.children[0]
    assert child.path is not None and child.path.startswith("claims/")
    assert child.path in egress.target_paths
    # Producing the proposal activated nothing.
    assert instance.accepted_coordinate() == base
    actor_id = fixtures._actor(instance).actor_id
    assert _proposal_refs(instance) == [proposal_terminal_ref(actor_id, egress.operation_key)]

    # The manager retrieves the exact candidate through the existing door.
    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    candidate = inspection.proposal.candidate
    assert candidate is not None, inspection.proposal.evaluation.diagnostics
    assert candidate.candidate_digest == egress.candidate_digest
    member = next(item for item in candidate.members if item.path == child.path)
    assert member.candidate_artifact_digest == child.egress_digest
    # The Claim's evidence grade is what its ClaimType's policy says about a
    # daemon-fetched observed Capture: eligible, directly admitted evidence.
    evidence = next(item for item in candidate.law_evidence if item.path == child.path).result
    assert evidence["verdict"] == "accepted"
    law_evidence = evidence["claim_evidence"]
    assert law_evidence["initial_verdict"] == "supported"
    assert law_evidence["evidence_basis"] == ["direct"]
    (verdict_capture,) = law_evidence["verdict_captures"]
    assert verdict_capture["capture_digest"] == observation.capture_digest
    assert verdict_capture["admission"] == "direct"
    assert verdict_capture["epistemic_grade"] == "observed"
    assert verdict_capture["provenance_grade"] == "daemon-fetched"
    assert verdict_capture["producer"]["kind"] == "Provider"

    # Activation is the manager's, through the existing authority.
    accept_proposal(instance, owner, inspection)
    accepted = instance.accepted_coordinate()
    assert accepted != base

    # The accepted Claim reads back to the Capture and the run that produced it.
    tree = instance.tree_at(accepted.git_oid)
    claim = parse_claim(tree[child.path], path=child.path)
    assert claim.statement.predicate == PREDICATE
    assert claim.statement.object.model_dump(mode="json")["value"] == "high"
    assert observation.capture_digest in claim.backing.capture_digests
    envelope = parse_capture_envelope(
        instance.body_store().read(
            observation.capture_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert envelope.run_coordinate.run_id == state.run_id
    assert envelope.producer_binding_digest is not None
    # And the run reads back the same receipt after the fact.
    again = service_get_playbill_procedure_run(instance, run_id=state.run_id)
    assert again.terminal_egress == state.terminal_egress


# --- authority, evidence, and shape refusals: no unauthorized proposal mutation


def _refusal(state) -> ProcedureNodeRefusalV1:  # type: ignore[no-untyped-def]
    assert state.status == "node_refused", state.terminal
    assert isinstance(state.terminal, ProcedureNodeRefusalV1), state.terminal
    return state.terminal


def test_a_mandate_over_another_namespace_refuses_typed_before_any_ref(tmp_path: Path) -> None:
    instance, _owner, root, line, procedure = proposal_world(tmp_path, mandate=None)
    # Accept a live mandate whose namespace is `subjects`, not `claims`.
    mandate = fixtures._line_mandate(procedure).model_copy(update={"namespace": ("subjects",)})
    fixtures._accept_more(
        instance,
        _owner,
        {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
        name="subjects-mandate",
    )
    base = instance.accepted_coordinate()

    state = run_line(instance, root, line)

    refusal = _refusal(state)
    assert refusal.code == "procedure_mandate_namespace_mismatch", refusal
    assert refusal.node_id == "propose"
    assert refusal.details["codes"] == ["procedure_mandate_namespace_mismatch"]
    assert refusal.details["repair_kind"] == "author_successor"
    assert refusal.repair.model_dump(mode="json")["hand_edit"]["required_change"] == (
        "accept_a_procedure_mandate_whose_namespace_covers_the_target_paths"
    )
    assert _proposal_refs(instance) == []
    assert instance.accepted_coordinate() == base
    (egress,) = state.terminal_egress
    assert egress.verdict == "refused"
    assert egress.refusal_code == "procedure_mandate_namespace_mismatch"
    assert egress.proposal_id is None
    # The prepared intent still names what WOULD have been proposed.
    assert egress.target_paths and all(path.startswith("claims/") for path in egress.target_paths)


def test_a_line_requesting_rung_one_is_capped_before_the_proposal_door(tmp_path: Path) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path, requested_terminal_rung=1)

    state = run_line(instance, root, line)

    refusal = _refusal(state)
    assert refusal.code == "terminal_rung_capped_by_line_requested_rung", refusal
    assert _proposal_refs(instance) == []
    (egress,) = state.terminal_egress
    assert egress.verdict == "refused_effective_rung"
    assert egress.effective_rung == 1
    assert egress.limiting_term == "line_requested_rung"


def test_a_source_failure_stops_the_run_before_the_terminal(tmp_path: Path) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path)

    state = run_line(instance, root, line, invoker=fixtures._DecliningInvoker())

    refusal = _refusal(state)
    assert refusal.node_id == "read", refusal
    assert state.terminal_egress == ()
    assert _proposal_refs(instance) == []


def test_a_template_that_is_not_a_claim_item_refuses_typed_to_the_item(tmp_path: Path) -> None:
    instance, _owner, root, line, _procedure = proposal_world(
        tmp_path,
        templates=({"severity": "$steps.result.severity", "note": "not an item"},),
    )

    state = run_line(instance, root, line)

    refusal = _refusal(state)
    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["child_index"] == 0
    assert refusal.details["errors"], refusal.details
    assert _proposal_refs(instance) == []
    (egress,) = state.terminal_egress
    assert egress.verdict == "refused"
    assert egress.refusal_code == "proposal_item_invalid"


def test_an_item_whose_closure_reached_no_capture_refuses_evidence_missing(
    tmp_path: Path,
) -> None:
    """A literal item reads nothing the Source produced, so it has no evidence to cite."""

    instance, _owner, root, line, _procedure = proposal_world(
        tmp_path,
        templates=(
            item_template(
                statement={
                    **item_template()["statement"],  # type: ignore[dict-item]
                    "object": {"kind": "literal", "value": "high"},
                }
            ),
        ),
    )

    state = run_line(instance, root, line)

    refusal = _refusal(state)
    assert refusal.code == "proposal_item_evidence_missing", refusal
    assert _proposal_refs(instance) == []


def test_a_claim_type_that_does_not_admit_the_capture_refuses_through_shared_lowering(
    tmp_path: Path,
) -> None:
    """The evidence law is the ClaimType's, applied by the same lowering every author gets."""

    instance, _owner, root, line, _procedure = proposal_world(
        tmp_path,
        claim_type=_claim_type("sha256:" + "e" * 64),
    )

    state = run_line(instance, root, line)

    refusal = _refusal(state)
    assert refusal.code == "proposal_lowering_refused", refusal
    assert refusal.details["code"] == "playbill.authoring.existing_capture_not_admitted"
    assert refusal.details["offending_element"] == "members[0].source.capture_digest"
    assert _proposal_refs(instance) == []


def test_two_items_from_one_source_lower_into_two_claims_of_one_proposal(tmp_path: Path) -> None:
    second = item_template(
        statement={
            **item_template()["statement"],  # type: ignore[dict-item]
            "qualifier": "reported",
        },
        rationale="The reported severity, qualified.",
    )
    instance, owner, root, line, _procedure = proposal_world(
        tmp_path, templates=(item_template(), second)
    )

    state = run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered"
    assert len(egress.children) == 2
    paths = {child.path for child in egress.children}
    assert len(paths) == 2 and paths <= set(egress.target_paths)
    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    assert inspection.proposal.candidate is not None
    accept_proposal(instance, owner, inspection)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claims = [parse_claim(tree[path], path=path) for path in sorted(paths)]
    assert {claim.statement.qualifier for claim in claims} == {None, "reported"}
    assert len({claim.backing.capture_digests for claim in claims}) == 1


# --- durability: one operation, one proposal ---------------------------------


from cruxible_core.playbill.procedures import proposal_delivery as delivery_module  # noqa: E402
from cruxible_core.playbill.procedures import terminal_services  # noqa: E402
from cruxible_core.playbill.procedures.execution import ProcedureExecutor  # noqa: E402
from cruxible_core.service.playbill_proposal_egress import (  # noqa: E402
    service_recover_proposal_egress,
)


class _Crash(BaseException):
    """A process death, not an exception a run may catch and journal."""


def _crash_after(monkeypatch: pytest.MonkeyPatch, *, after_submit: bool) -> None:
    """Kill the process at the proposal door: after the ref is created, or before it."""

    original_deliver = terminal_services.ProposalTerminalAdapter.deliver
    original_append = ProcedureExecutor._append_event
    crashed = {"value": False}

    def crashing_deliver(self, **kwargs):  # type: ignore[no-untyped-def]
        if not after_submit:
            crashed["value"] = True
            raise _Crash()
        original_deliver(self, **kwargs)
        crashed["value"] = True
        raise _Crash()

    def dead_append(self, admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if crashed["value"]:
            raise _Crash()
        return original_append(self, admission, records, event_kind, payload)

    monkeypatch.setattr(terminal_services.ProposalTerminalAdapter, "deliver", crashing_deliver)
    monkeypatch.setattr(ProcedureExecutor, "_append_event", dead_append)


@pytest.mark.parametrize("after_submit", [True, False], ids=["after-proposal", "before-proposal"])
def test_a_crash_at_the_proposal_door_recovers_one_proposal_and_publishes_its_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_submit: bool,
) -> None:
    instance, owner, root, line, _procedure = proposal_world(tmp_path)
    _crash_after(monkeypatch, after_submit=after_submit)
    with pytest.raises(_Crash):
        run_line(instance, root, line)
    monkeypatch.undo()
    refs_after_crash = _proposal_refs(instance)
    assert len(refs_after_crash) == (1 if after_submit else 0)

    # The run is recoverable and nothing has read it as complete.
    recovered = service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(minutes=1))
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    # Exactly one proposal exists under the operation's ref, whichever side crashed.
    assert len(_proposal_refs(instance)) == 1
    state = service_get_playbill_procedure_run(instance, run_id=run_id)
    assert state.status == "operational_failed"
    assert state.terminal is not None and state.terminal.code == "terminal_egress_recovered"
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered"
    assert egress.proposal_id is not None and egress.candidate_digest is not None
    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    assert inspection.proposal.candidate is not None
    assert inspection.proposal.candidate.candidate_digest == egress.candidate_digest
    # Recovery is idempotent: a second sweep finds nothing to do.
    assert service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(minutes=2)) == {}
    # And the manager can still accept the recovered proposal.
    accept_proposal(instance, owner, inspection)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert egress.children[0].path in tree


def test_a_duplicate_delivery_of_one_operation_returns_the_same_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two deliveries of one prepared operation are one proposal and one receipt."""

    receipts = []
    original = delivery_module.ProposalTerminalEgressSink.deliver_terminal_egress

    def twice(self, **kwargs):  # type: ignore[no-untyped-def]
        first = original(self, **kwargs)
        second = original(self, **kwargs)
        receipts.append((first, second))
        return second

    monkeypatch.setattr(
        delivery_module.ProposalTerminalEgressSink, "deliver_terminal_egress", twice
    )
    instance, _owner, root, line, _procedure = proposal_world(tmp_path)

    state = run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    ((first, second),) = receipts
    assert first == second
    assert len(_proposal_refs(instance)) == 1
    (egress,) = state.terminal_egress
    assert egress.proposal_id == first.proposal_id  # type: ignore[attr-defined]


def test_the_same_operation_key_with_other_member_bytes_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that lowers other bytes under one key must not replace or relabel the proposal."""

    original_prepare = delivery_module.ProposalTerminalEgressSink.prepare_terminal_egress
    calls = {"n": 0}

    def drifting_prepare(self, **kwargs):  # type: ignore[no-untyped-def]
        prepared = original_prepare(self, **kwargs)
        calls["n"] += 1
        key = (kwargs["request"].admission_binding_digest, kwargs["request"].node_id)
        entry = self._prepared[key]
        if calls["n"] == 2:
            # Same targets, same key, other member bytes.
            path, content = entry.changed_members[0]
            drifted = content.replace(b"high", b"low!")
            assert drifted != content
            entry.candidate_tree[path] = drifted
            object.__setattr__(entry, "changed_members", ((path, drifted),))
        return prepared

    original_deliver = delivery_module.ProposalTerminalEgressSink.deliver_terminal_egress

    def deliver_then_drift(self, **kwargs):  # type: ignore[no-untyped-def]
        first = original_deliver(self, **kwargs)
        # A second attempt of the same admitted operation prepares again and drifts.
        drifting_prepare(
            self,
            request=kwargs["request"],
            admission=kwargs["admission"],
            evidence=self._evidence_for_test,  # type: ignore[attr-defined]
        )
        with pytest.raises(terminal_services.ProposalDeliveryRefused) as caught:
            original_deliver(self, **kwargs)
        assert caught.value.code == "effectful_operation_payload_mismatch"
        assert caught.value.details["proposal_id"] == first.proposal_id  # type: ignore[attr-defined]
        return first

    def remember_evidence(self, **kwargs):  # type: ignore[no-untyped-def]
        evidence = kwargs.get("evidence")
        if evidence is None:
            evidence = delivery_module.evidence_by_item(
                kwargs["request"], kwargs.get("manifests") or {}
            )
        self._evidence_for_test = evidence
        return drifting_prepare(self, **kwargs)

    monkeypatch.setattr(
        delivery_module.ProposalTerminalEgressSink, "prepare_terminal_egress", remember_evidence
    )
    monkeypatch.setattr(
        delivery_module.ProposalTerminalEgressSink, "deliver_terminal_egress", deliver_then_drift
    )
    instance, _owner, root, line, _procedure = proposal_world(tmp_path)

    state = run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    assert len(_proposal_refs(instance)) == 1
    assert calls["n"] == 2


def test_a_head_that_moves_before_delivery_rebases_the_candidate_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proposal is admitted at the run's base and evaluated at the moved head."""

    original = delivery_module.ProposalTerminalEgressSink.deliver_terminal_egress
    moved = {}

    def move_head_then_deliver(self, **kwargs):  # type: ignore[no-untyped-def]
        filler = SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/sibling"),
            subject_kind=SUBJECT_KIND,
            subject_id="sibling",
        )
        fixtures._accept_more(
            self.instance,
            moved["owner"],
            {subject_path(SUBJECT_KIND, "sibling"): render_subject(filler)},
            name="independent-sibling",
        )
        moved["head"] = self.instance.accepted_coordinate()
        return original(self, **kwargs)

    monkeypatch.setattr(
        delivery_module.ProposalTerminalEgressSink,
        "deliver_terminal_egress",
        move_head_then_deliver,
    )
    instance, owner, root, line, _procedure = proposal_world(tmp_path)
    moved["owner"] = owner
    base = instance.accepted_coordinate()

    state = run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered"
    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    admission = inspection.proposal.admission
    evaluation = inspection.proposal.evaluation
    assert admission.proposed_base_oid == base.git_oid
    assert evaluation.rebased is True
    assert evaluation.evaluated_base_oid == moved["head"].git_oid
    assert inspection.proposal.candidate is not None
    assert inspection.proposal.candidate.candidate_digest == egress.candidate_digest
    # The candidate reviewed at the moved head is the one accepted.
    accept_proposal(instance, owner, inspection)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert egress.children[0].path in tree
    assert subject_path(SUBJECT_KIND, "sibling") in tree


# --- optimization parity: no second base-tree read ----------------------------


def test_delivery_hands_the_door_lowering_paths_and_reads_no_second_base_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The targets the door checks are exactly the diff a fresh base-tree read would give."""

    instance, _owner, root, line, _procedure = proposal_world(tmp_path)
    base = instance.accepted_coordinate()
    base_tree = instance.tree_at(base.git_oid)
    reads: list[str] = []
    original_tree_at = type(instance).tree_at

    def counting_tree_at(self, oid):  # type: ignore[no-untyped-def]
        reads.append(oid)
        return original_tree_at(self, oid)

    monkeypatch.setattr(type(instance), "tree_at", counting_tree_at)
    original_lower = delivery_module.authoring_lowering.lower_authoring
    seen: dict[str, object] = {}

    def observing_lower(*args, **kwargs):  # type: ignore[no-untyped-def]
        lowered = original_lower(*args, **kwargs)
        seen["reads_after_lowering"] = list(reads)
        return lowered

    monkeypatch.setattr(delivery_module.authoring_lowering, "lower_authoring", observing_lower)
    original_deliver = terminal_services.ProposalTerminalAdapter.deliver

    def observing_deliver(self, **kwargs):  # type: ignore[no-untyped-def]
        seen["changed_paths"] = kwargs.get("changed_paths")
        seen["candidate_tree"] = kwargs["candidate_tree"]
        seen["reads_before_door"] = list(reads)
        return original_deliver(self, **kwargs)

    monkeypatch.setattr(terminal_services.ProposalTerminalAdapter, "deliver", observing_deliver)

    state = run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    # The door received lowering's own paths, and they equal an independent diff.
    assert seen["changed_paths"] == egress.target_paths
    assert terminal_services._changed_paths(base_tree, seen["candidate_tree"]) == (  # noqa: SLF001
        egress.target_paths
    )
    # Between lowering's own read and the door, preparation read no tree at all.
    assert seen["reads_before_door"] == seen["reads_after_lowering"]
