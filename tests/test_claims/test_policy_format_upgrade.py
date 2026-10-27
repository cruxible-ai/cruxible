"""Compiler revision 36 writes v4 identity evidence policies; v3 ones wait for an upgrade.

An instance whose ClaimTypes were accepted with v3 policies keeps working after
`compiler upgrade`: only a ClaimType a write creates or changes must state the
revision's format. `claim-type upgrade` is the governed move that restates the
accepted v3 policies as v4, in one reviewed change set carrying every Claim,
and `next` names it while any is pending.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1,
    capture_contract_path,
)
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import claim_path as claim_artifact_path
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV3,
    ClaimEvidenceAdmissionRuleV3,
)
from cruxible_client.contracts.subjects import render_subject
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.compiler.compiler import CLAIM_REBIND_COMPILER, LIGHT_ARTIFACTS_COMPILER
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_inspect_playbill_proposal,
    service_propose_compiler_upgrade,
    service_submit_playbill_approval,
)
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.claims.claim_type_upgrade import service_upgrade_claim_types
from cruxible_core.service.discovery.next import service_playbill_next
from tests.core_support._subject_kind_support import core_assigned
from tests.core_support._write_support import caller
from tests.core_support.legacy_capture_builders import initialize_legacy_local
from tests.support.store_snapshot import assert_writes_nothing
from tests.test_claims.test_claims import TIMESTAMP, _claim_type, _subject
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_integration.test_next_closed_loop import _request
from tests.test_ledger.test_activation import _sign

STATUS = "project.work_item.status"
SLUG = "project.work_item.slug"


def _v3_rule(*, rule_id: str = "self", kinds: tuple[str, ...] = ("self_asserted",)) -> Any:
    return ClaimEvidenceAdmissionRuleV3(
        rule_id=rule_id,
        claim_roles=("normative", "observation"),
        capture_contracts=(
            {
                "role": CAPTURE_CONTRACT_REF_ROLE,
                "target": COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1.identity,
            },
        ),
        evidence_kinds=kinds,
        admission="direct",
        subject_binding="exact_claim_subject",
    )


def _v3_type(predicate: str = STATUS, **rule: Any) -> ClaimType:
    """A v7 ClaimType stating a v3 identity evidence policy, as revision 35 wrote them."""

    return _claim_type().model_copy(
        update={
            "artifact_format": "playbill-claim-type-v7",
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "evidence_requirement": "self",
            "revision_evidence": "replace",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV3(rules=(_v3_rule(**rule),)),
        }
    )


def _tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.tree_at(instance.accepted_coordinate().git_oid))


def _claim_type_at(tree: dict[str, bytes], predicate: str) -> ClaimType:
    path = claim_type_path(predicate)
    return parse_claim_type(tree[path], path=path)


def _write(instance: PlaybillInstance, subject: str, value: str) -> Any:
    request = WriteRequest.model_validate(
        {
            "because": "The writer checked it.",
            "changes": [
                {
                    "op": "set",
                    "subject": subject,
                    "field": "status",
                    "value": value,
                    "role": "observation",
                },
            ],
        }
    )
    return service_playbill_write(instance, request=request, caller=caller())


def _approve_and_activate(
    instance: PlaybillInstance, owner: Any, proposal_id: str, *, refresh: bool = True
) -> None:
    candidate = service_inspect_playbill_proposal(
        instance, proposal_id=proposal_id
    ).proposal.candidate
    assert candidate is not None
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=_sign(
            owner, candidate.candidate_digest, instance.accepted_coordinate().semantic_root
        ).attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance, proposal_id=proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"
    if refresh:
        instance.refresh()


def _land(instance: PlaybillInstance, owner: Any, tree: dict[str, bytes], *, name: str) -> None:
    """Propose, approve and activate as a daemon does: no refresh afterwards."""

    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert result.candidate is not None, result.evaluation.diagnostics
    _approve_and_activate(instance, owner, result.admission.proposal_id, refresh=False)


def _upgrade_compiler(instance: PlaybillInstance, owner: Any) -> None:
    proposal = service_propose_compiler_upgrade(
        instance, target=LIGHT_ARTIFACTS_COMPILER, actor_id="owner", timestamp=TIMESTAMP
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    _approve_and_activate(instance, owner, proposal.admission.proposal_id)
    assert instance.accepted_coordinate().compiler == LIGHT_ARTIFACTS_COMPILER


def _upgrade_claim_types(instance: PlaybillInstance, **request: Any) -> ClaimTypeUpgradeResult:
    return service_upgrade_claim_types(
        instance,
        request=ClaimTypeUpgradeRequest.model_validate(request),
        actor_id="owner",
        timestamp=TIMESTAMP,
    )


@pytest.fixture
def revision_35(tmp_path: Path) -> tuple[PlaybillInstance, Any, str]:
    """A revision-35 instance with v3-policy ClaimTypes and one Claim citing its writer."""

    instance, owner = initialize_legacy_local(tmp_path, compiler=CLAIM_REBIND_COMPILER)
    subject = _subject()
    tree = core_assigned(
        instance,
        {
            **_tree(instance),
            "subjects/project.work_item/wi-42.json": render_subject(subject),
            claim_type_path(STATUS): render_claim_type(_v3_type()),
        },
    )
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="pre-light-types")
    written = _write(instance, "project.work_item/wi-42", "ready")
    assert written.status == "accepted", written
    claim_id = written.changes[0].claim.removeprefix("Claim:")
    return instance, owner, claim_id


def test_old_v3_claim_types_keep_working_after_the_compiler_upgrade(
    revision_35: tuple[PlaybillInstance, Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner, _claim_id = revision_35
    # Revision 35 authored both ClaimTypes (the slug key and the status) with v3.
    tree = _tree(instance)
    for predicate in (STATUS, SLUG):
        policy = _claim_type_at(tree, predicate).evidence_admission_policy
        assert isinstance(policy, ClaimEvidenceAdmissionPolicyV3)
    _upgrade_compiler(instance, owner)

    import cruxible_core.proposals.proposals as proposals

    gate = proposals._validate_identity_evidence_policy_format
    judged: list[str] = []

    def counted(claim_type: ClaimType, compiler: object) -> None:
        judged.append(claim_type.identity.qualified)
        gate(claim_type, compiler)

    monkeypatch.setattr(proposals, "_validate_identity_evidence_policy_format", counted)
    # The first revision-36 write installs the v3 coordinator self-source contract,
    # whose referrers are the v3 ClaimTypes: they are rechecked, not re-gated.
    written = _write(instance, "project.work_item/wi-43", "done")
    assert written.status == "accepted", written
    path = capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1.identity.name)
    assert _tree(instance)[path] != tree[path]
    # No ClaimType was written, so the gate judged none (the recheck adds no work).
    assert judged == []
    assert _write(instance, "project.work_item/wi-42", "blocked").status == "accepted"


def test_an_edit_of_a_v3_claim_type_on_revision_36_names_the_upgrade(
    revision_35: tuple[PlaybillInstance, Any, str],
) -> None:
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance, owner, _claim_id = revision_35
    note = "project.work_item.note"
    tree = _tree(instance)
    tree[claim_type_path(note)] = render_claim_type(_v3_type(note))
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="note")
    _upgrade_compiler(instance, owner)
    tree = _tree(instance)
    previous = _claim_type_at(tree, note)
    edited = previous.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(previous).tagged),
            "literal_schema": {"enum": ["blocked", "done", "ready", "review"], "type": "string"},
        }
    )
    tree[claim_type_path(note)] = render_claim_type(edited)
    base = instance.accepted_coordinate()
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/v3-edit", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert result.candidate is None
    (diagnostic,) = result.evaluation.diagnostics
    assert diagnostic.code == "cruxible.claim_type.format_invalid"
    assert "`cruxible claim-type upgrade`" in diagnostic.message
    assert "Broken referrer" not in diagnostic.message


def test_the_claim_type_upgrade_restates_v3_policies_and_next_names_it(
    revision_35: tuple[PlaybillInstance, Any, str],
) -> None:
    instance, owner, claim_id = revision_35

    # Below revision 36, v3 is the format a write states: nothing to restate.
    below = _upgrade_claim_types(instance)
    assert below.status == "unchanged", below
    assert below.restated == ()
    assert service_playbill_next(instance, request=_request(instance)).status.compiler.state == (
        "upgrade_available"
    )

    _upgrade_compiler(instance, owner)
    status = service_playbill_next(instance, request=_request(instance)).status
    assert status.compiler.state == "claim_type_upgrade_available"
    assert status.compiler.repair is not None
    assert status.compiler.repair.command == "cruxible claim-type upgrade"
    # The unedited genesis seeds are not pending work of this instance.
    assert status.compiler.detail["claim_types"] == [f"ClaimType:{SLUG}", f"ClaimType:{STATUS}"]
    assert status.compiler.detail["count"] == 2
    # A governed-write caller runs it; MCP and the SDK name their own door.
    mcp = _request(instance).model_copy(
        update={
            "caller_surface": "mcp",
            "caller_tools": ("cruxible_claim_type_upgrade", "cruxible_next"),
        }
    )
    shown = service_playbill_next(instance, request=mcp, caller_rung=1).status.compiler
    assert shown.repair is not None
    assert shown.repair.command == "cruxible_claim_type_upgrade(request={})"
    sdk = _request(instance).model_copy(update={"caller_surface": "sdk"})
    shown = service_playbill_next(instance, request=sdk, caller_rung=1).status.compiler
    assert shown.repair is not None and shown.repair.command == "cx.upgrade_claim_types()"
    hidden = service_playbill_next(instance, request=_request(instance), caller_rung=0)
    assert hidden.status.compiler.repair_requires is not None
    assert hidden.status.compiler.repair_requires.tier == "governed_write"

    claim_path = claim_artifact_path(claim_id)
    before = _tree(instance)
    claim_before = parse_claim(before[claim_path], path=claim_path)
    dry = assert_writes_nothing([instance.root.parent], lambda: _upgrade_claim_types(instance))
    assert dry.status == "would_propose", dry
    assert dry.upgraded == ()
    # The seeded builtins are restated as well, in the same change set.
    assert [item.claim_type for item in dry.restated] == [
        f"ClaimType:{SLUG}",
        f"ClaimType:{STATUS}",
        "ClaimType:semantic.alias",
        "ClaimType:semantic.distinct_from",
    ]
    assert dry.carried_claims >= 1
    assert "v3 evidence policy as v4" in (dry.detail or "")

    result = _upgrade_claim_types(instance, dry_run=False, at=dry.coordinate.git_oid)
    assert result.status == "proposed", result
    assert result.restated == dry.restated
    # Activated as a daemon does (no refresh): next carries its warm pending set.
    _approve_and_activate(instance, owner, result.proposal_id, refresh=False)

    after = _tree(instance)
    for item in dry.restated:
        predicate = item.claim_type.removeprefix("ClaimType:")
        old = _claim_type_at(before, predicate)
        new = _claim_type_at(after, predicate)
        assert isinstance(new.evidence_admission_policy, ClaimEvidenceAdmissionPolicy)
        assert new.lifecycle.predecessor_digest == claim_type_digest(old).tagged
        # Everything but the policy and the lifecycle is exactly what it was.
        assert new.model_dump(exclude={"evidence_admission_policy", "lifecycle"}) == (
            old.model_dump(exclude={"evidence_admission_policy", "lifecycle"})
        )
        (old_rule,) = old.evidence_admission_policy.rules
        (new_rule,) = new.evidence_admission_policy.rules
        assert new_rule.model_dump(exclude={"tag", "capture_contracts"}) == old_rule.model_dump(
            exclude={"tag", "capture_contracts", "evidence_kinds"}
        )
        assert [(ref.target, ref.logical_sources) for ref in new_rule.capture_contracts] == [
            (ref.target, ()) for ref in old_rule.capture_contracts
        ]
    claim_after = parse_claim(after[claim_path], path=claim_path)
    assert (
        claim_after.statement.claim_type_digest
        == claim_type_digest(_claim_type_at(after, STATUS)).tagged
    )
    assert claim_after.backing == claim_before.backing
    assert service_playbill_next(instance, request=_request(instance)).status.compiler.state == (
        "current"
    )
    again = _upgrade_claim_types(instance)
    assert again.status == "unchanged" and again.restated == ()
    # Restated, the ClaimType takes ordinary edits and new Claims again.
    assert _write(instance, "project.work_item/wi-42", "done").status == "accepted"


def test_a_v3_policy_v4_cannot_state_exactly_is_refused_with_a_typed_reason(
    revision_35: tuple[PlaybillInstance, Any, str],
) -> None:
    instance, owner, _claim_id = revision_35
    narrow = "project.work_item.narrow"
    # The self-source v1 contract captures self_asserted evidence; this rule admits
    # only another kind, a restriction no v4 rule can state.
    tree = _tree(instance)
    narrow_type = _v3_type(narrow, kinds=("observed",)).model_copy(
        update={"literal_schema": {"type": "string"}}
    )
    tree[claim_type_path(narrow)] = render_claim_type(narrow_type)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="narrow")
    _upgrade_compiler(instance, owner)

    result = _upgrade_claim_types(instance, claim_types=[narrow, STATUS])
    assert result.status == "would_propose", result
    assert [item.claim_type for item in result.restated] == [f"ClaimType:{STATUS}"]
    (refusal,) = result.refused
    assert refusal.claim_type == f"ClaimType:{narrow}"
    assert refusal.code == "cruxible.claim_type.policy_restatement_inexact"
    assert "self_asserted" in refusal.reason
    assert "cruxible claim-type propose" in refusal.reason


@pytest.mark.parametrize("extra", [5, 50])
def test_the_first_next_after_one_claim_type_edit_costs_the_edit(
    revision_35: tuple[PlaybillInstance, Any, str], monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    """The pending set is carried forward by the change, not re-read whole."""

    import cruxible_core.runtime.instance as instance_module
    from cruxible_client.contracts.policies import ClaimEvidenceAdmissionRule

    instance, owner, _claim_id = revision_35
    tree = _tree(instance)
    pending = [SLUG, STATUS] + [f"project.work_item.legacy_{index:02d}" for index in range(extra)]
    for predicate in pending[2:]:
        tree[claim_type_path(predicate)] = render_claim_type(_v3_type(predicate))
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="legacy-types")
    _upgrade_compiler(instance, owner)
    current = "project.work_item.current"
    v4 = _v3_type(current).model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="self",
                        claim_roles=("normative", "observation"),
                        capture_contracts=(
                            {"contract": COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1.identity.name},
                        ),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    tree = _tree(instance)
    tree[claim_type_path(current)] = render_claim_type(v4)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="current-type")
    expected = sorted(f"ClaimType:{item}" for item in pending)[:20]
    warm = service_playbill_next(instance, request=_request(instance)).status.compiler
    assert warm.detail["count"] == len(pending) and warm.detail["claim_types"] == expected

    tree = _tree(instance)
    tree[claim_type_path(current)] = render_claim_type(
        v4.model_copy(
            update={
                "description": "Edited once.",
                "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(v4).tagged),
            }
        )
    )
    _land(instance, owner, tree, name="describe")

    classified: list[str] = []
    classify = getattr(instance_module, "_pending_restatement", None)

    def counted(path: str, content: bytes) -> str | None:
        classified.append(path)
        assert classify is not None
        return classify(path, content)

    def no_search(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise AssertionError("a warm pending set is never searched for again")

    monkeypatch.setattr(instance_module, "_pending_restatement", counted, raising=False)
    monkeypatch.setattr(type(instance._ledger), "tree_paths_containing_literal", no_search)
    after = service_playbill_next(instance, request=_request(instance)).status.compiler
    assert after.detail["count"] == len(pending) and after.detail["claim_types"] == expected
    # Flat in the number of ClaimTypes: only the edited one is read again.
    assert classified == [claim_type_path(current)]
