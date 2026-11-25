"""Revision 73 upgrades to revision 74 along one explicit edge; every other coordinate refuses."""

import json
from dataclasses import dataclass

import pytest

from cruxible_client.contracts.candidates import LawEvaluationCoordinate
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.compiler_upgrade import (
    COMPILER_UPGRADE_PATH,
    CompilerUpgrade,
    render_compiler_upgrade,
)
from cruxible_client.contracts.errors import (
    InstanceDecommissioned,
    ReseedRequired,
    SettlementIntegrityError,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import (
    COMPILER_REVISION_73,
    CURRENT_COMPILER,
    require_installed_compiler,
)
from cruxible_core.compiler.upgrades import (
    compiler_after_record,
    supported_upgrade_targets,
    upgrade_law,
)
from cruxible_core.errors import RequestRefusedError
from cruxible_core.proposals.proposals import evaluate_proposal_tree
from cruxible_core.runtime.instance import DESCRIPTOR_FILE, PlaybillInstance
from cruxible_core.service.authoring.documents import (
    AcceptedCoordinate,
    service_activate_playbill_proposal,
    service_propose_compiler_upgrade,
    service_submit_playbill_approval,
)
from tests.test_ledger.test_activation import TIMESTAMP, _candidate, _instance, _sign

#: The coordinate an earlier, unshipped build recorded (revision 35, claim-rebind-v1).
EARLIER_COMPILER = CompilerCoordinate(
    rule_digest="sha256:56542b566100db8fadfd4bfcea8f0fe58e37de444885906f3914ec1cc225d085"
)
UNKNOWN_COMPILER = CompilerCoordinate(rule_digest="sha256:" + "ff" * 32)


def _propose(instance, target=None):
    return service_propose_compiler_upgrade(
        instance,
        target=target,
        actor_id="owner",
        proposal_name="upgrade",
        timestamp=TIMESTAMP,
        base=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


def test_one_edge_leads_from_revision_73_to_the_current_compiler() -> None:
    assert COMPILER_REVISION_73.rule_digest == (
        "sha256:5b1e93a005c680b1709f9c58c6fba03ef8ff87f9ec4b38b1b7fab6fb04891be8"
    )
    assert supported_upgrade_targets(COMPILER_REVISION_73) == (CURRENT_COMPILER.rule_digest,)
    assert supported_upgrade_targets(CURRENT_COMPILER) == ()
    assert upgrade_law(COMPILER_REVISION_73, CURRENT_COMPILER).artifact_kind == "compiler-upgrade"
    for source, target in (
        (CURRENT_COMPILER, UNKNOWN_COMPILER),
        (EARLIER_COMPILER, CURRENT_COMPILER),
        (CURRENT_COMPILER, CURRENT_COMPILER),
        (CURRENT_COMPILER, COMPILER_REVISION_73),
        (COMPILER_REVISION_73, COMPILER_REVISION_73),
    ):
        with pytest.raises(ValueError, match="unsupported compiler transition"):
            upgrade_law(source, target)


def revision_73_instance(tmp_path, monkeypatch):
    """An instance created as a 0.5.x daemon creates it: genesis on revision 73."""

    with monkeypatch.context() as patch:
        patch.setattr(
            "cruxible_core.runtime.instance.current_compiler_coordinate",
            lambda: COMPILER_REVISION_73,
        )
        return _instance(tmp_path)


def _approve_and_activate(instance, inspection, reviewer):
    proposal = inspection.proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    candidate = proposal.candidate
    signature = _sign(
        reviewer, candidate.candidate_digest, candidate.candidate.parent_semantic_root
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=signature.attestation,
        authenticated_submitter="reviewer",
    )
    return service_activate_playbill_proposal(
        instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
    )


def test_a_revision_73_instance_opens_upgrades_and_replays(tmp_path, monkeypatch):
    instance, _, reviewer = revision_73_instance(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    assert before.compiler == COMPILER_REVISION_73
    assert instance.descriptor.compiler == COMPILER_REVISION_73
    inspection = _propose(instance)
    assert inspection.compiler_upgrade is not None
    assert inspection.compiler_upgrade.source_revision == "retiring-query-traversal"
    assert inspection.compiler_upgrade.target_revision == "based-on-observation-carry"
    receipt = _approve_and_activate(instance, inspection, reviewer)
    assert receipt.status == "accepted"
    assert instance.accepted_coordinate().compiler == CURRENT_COMPILER
    assert instance.coordinate_for_oid(before.git_oid) == before
    # The descriptor keeps its genesis compiler; the head names the new one.
    assert instance.descriptor.compiler == COMPILER_REVISION_73
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    assert reopened.coordinate_for_oid(before.git_oid) == before
    assert reopened.inspect().compiler == CURRENT_COMPILER
    # Ordinary authoring lands under the new compiler and replays on reopen.
    base, tree, candidate = _candidate(reopened)
    assert base.compiler == CURRENT_COMPILER
    with pytest.raises(RequestRefusedError, match="already_current"):
        _propose(reopened)


@pytest.mark.parametrize(
    ("target", "code"),
    [
        (None, "cruxible.compiler_upgrade.already_current"),
        (UNKNOWN_COMPILER, "cruxible.compiler_upgrade.unsupported_transition"),
        (EARLIER_COMPILER, "cruxible.compiler_upgrade.unsupported_transition"),
    ],
)
def test_an_upgrade_proposal_refuses_typed_and_changes_nothing(tmp_path, target, code):
    instance, _, _ = _instance(tmp_path)
    before = instance.accepted_coordinate()
    assert before.compiler == CURRENT_COMPILER
    with pytest.raises(RequestRefusedError, match=code) as refused:
        _propose(instance, target)
    assert refused.value.error_code == code
    if code.endswith("unsupported_transition"):
        assert "supported targets: none" in str(refused.value)
    assert instance.accepted_coordinate() == before


def test_a_hand_built_transition_is_refused_alone_or_mixed(tmp_path):
    instance, _, _ = _instance(tmp_path)
    current = instance.accepted_coordinate()
    transition = render_compiler_upgrade(
        CompilerUpgrade(
            instance_id=current.instance_id,
            base=LawEvaluationCoordinate(
                git_oid=current.git_oid,
                semantic_root=current.semantic_root,
                generation_root=current.generation_root,
                compiler_digest=current.compiler.rule_digest,
            ),
            target=UNKNOWN_COMPILER,
        )
    )
    tree = instance.tree_at(current.git_oid)
    _, mixed_tree, _ = _candidate(instance)
    for candidate_tree in (
        {**tree, COMPILER_UPGRADE_PATH: transition},
        {**mixed_tree, COMPILER_UPGRADE_PATH: transition},
    ):
        result = evaluate_proposal_tree(
            base_tree=tree,
            current_tree=tree,
            proposed_tree=candidate_tree,
            current=current,
            bodies=instance.body_store(),
            timestamp=TIMESTAMP,
            rebased=False,
            actor_id="owner",
        )
        assert result.candidate is None
        assert any(d.code == "cruxible.compiler_upgrade.invalid" for d in result.diagnostics)
    assert instance.accepted_coordinate() == current


def test_a_decommissioned_instance_cannot_propose_an_upgrade(tmp_path):
    instance, _, _ = _instance(tmp_path)
    instance.decommission(reason="retired", decommissioned_by="owner")
    with pytest.raises(InstanceDecommissioned):
        _propose(instance)


@dataclass(frozen=True)
class _Member:
    artifact_kind: str
    path: str


@dataclass(frozen=True)
class _Record:
    compiler_digest: str
    members: tuple[_Member, ...]
    law_evidence: tuple[object, ...] = ()


def test_replay_refuses_a_recorded_compiler_transition() -> None:
    plain = _Record(CURRENT_COMPILER.rule_digest, (_Member("document", "documents/d.json"),))
    assert compiler_after_record(plain) == CURRENT_COMPILER  # type: ignore[arg-type]
    transition = _Record(
        CURRENT_COMPILER.rule_digest, (_Member("compiler-upgrade", COMPILER_UPGRADE_PATH),)
    )
    with pytest.raises(SettlementIntegrityError):
        compiler_after_record(transition)  # type: ignore[arg-type]


def test_an_instance_recorded_under_an_earlier_compiler_refuses_to_open(tmp_path):
    instance, _, _ = _instance(tmp_path)
    descriptor_path = instance.root / DESCRIPTOR_FILE
    payload = json.loads(descriptor_path.read_bytes())
    payload["compiler"] = EARLIER_COMPILER.model_dump(mode="json")
    descriptor_path.write_bytes(canonical_bytes(payload) + b"\n")

    with pytest.raises(ReseedRequired, match="cruxible.instance.reseed_required") as refused:
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert refused.value.compiler_digest == EARLIER_COMPILER.rule_digest
    with pytest.raises(ReseedRequired):
        require_installed_compiler(EARLIER_COMPILER)
    require_installed_compiler(CURRENT_COMPILER)
