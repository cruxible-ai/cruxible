"""Operational proposal inventory and writer identity reads."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.errors import (
    ProposalAdmissionError,
    ProposalNotFoundError,
    ProposalSelectorAmbiguousError,
    ProposalWithdrawnError,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_submit_playbill_approval,
)
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.auth import ResolvedAuthContext
from cruxible_core.service.playbill_proposals import (
    service_list_playbill_proposals,
    service_playbill_whoami,
    service_readmit_playbill_proposal,
    service_resolve_playbill_proposal_selector,
    service_withdraw_playbill_proposal,
)
from tests.test_playbill._candidate_support import submit_query_definition_candidate
from tests.test_playbill._claim_authoring_support import service_propose_playbill_claim
from tests.test_playbill._knowledge_loop_support import (
    TIMESTAMP,
    activate,
    authoring,
    seed_claims,
    work_item_query,
)
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation import _sign


def test_proposal_inventory_reduces_open_accepted_refused_and_stale(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    stale = submit_query_definition_candidate(
        instance,
        query=work_item_query("stale-query"),
        actor_id="owner",
        proposal_name="stale-query",
        timestamp=TIMESTAMP,
    )
    accepted = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-44", "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name="advance-head",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, accepted)
    refused = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:missing-body",
            document_kind="design",
            title="Missing body",
            media_type="text/markdown",
            body_digest="sha256:" + "f" * 64,
            authority=DocumentAuthority(
                required_tier="graph_write",
            ),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="refused",
        timestamp=TIMESTAMP,
    )
    assert refused.proposal.evaluation.verdict == "refused"
    opened = submit_query_definition_candidate(
        instance,
        query=work_item_query("open-query"),
        actor_id="owner",
        proposal_name="open-query",
        timestamp=TIMESTAMP,
    )

    result = service_list_playbill_proposals(instance)
    by_id = {item.proposal_id: item for item in result.entries}
    assert by_id[stale.proposal.admission.proposal_id].terminal_reason == "stale"
    assert by_id[accepted.proposal.proposal.admission.proposal_id].terminal_reason == "accepted"
    assert by_id[refused.proposal.admission.proposal_id].terminal_reason == "refused"
    assert by_id[opened.proposal.admission.proposal_id].status == "open"
    assert by_id[opened.proposal.admission.proposal_id].terminal_reason is None
    assert result.entries == tuple(
        sorted(
            result.entries,
            key=lambda item: (item.admitted_at.encode("utf-8"), item.proposal_id.encode()),
        )
    )
    assert service_list_playbill_proposals(instance, status="open").entries == (
        by_id[opened.proposal.admission.proposal_id],
    )


def test_whoami_binds_transport_identity_to_current_principal_registry(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)

    active = service_playbill_whoami(
        instance,
        actor_id="owner",
        credential_label="owner",
        actor_id_source="runtime_credential_label",
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )
    absent = service_playbill_whoami(
        instance,
        actor_id="local-operator",
        credential_label="local-operator",
        actor_id_source="local_operator",
        permission_mode=PermissionMode.ADMIN,
    )

    assert active.actor_id == active.credential_label == "owner"
    assert active.principal_registration_status == "active"
    assert active.credential_permission_mode == "governed_write"
    assert active.active_principal_ids == tuple(
        sorted(active.active_principal_ids, key=lambda item: item.encode("utf-8"))
    )
    assert absent.principal_registration_status == "absent"


def test_proposal_selector_resolves_full_prefix_and_current_target_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    proposed = submit_query_definition_candidate(
        instance,
        query=work_item_query("selector-query"),
        actor_id="owner",
        proposal_name="selector-query",
        timestamp=TIMESTAMP,
    )
    admission = proposed.proposal.admission

    assert (
        service_resolve_playbill_proposal_selector(
            instance, selector=admission.proposal_id
        ).proposal_id
        == admission.proposal_id
    )
    assert (
        service_resolve_playbill_proposal_selector(
            instance, selector=admission.proposal_id[:20]
        ).proposal_id
        == admission.proposal_id
    )
    assert (
        service_resolve_playbill_proposal_selector(
            instance, selector=admission.target_ref
        ).proposal_id
        == admission.proposal_id
    )
    with pytest.raises(ProposalNotFoundError) as refused:
        service_resolve_playbill_proposal_selector(
            instance, selector="refs/proposals/owner/missing"
        )
    assert refused.value.error_code == "playbill.proposal_not_found"
    assert refused.value.repair_commands == ("cruxible playbill proposal list",)

    forced = (
        admission.model_copy(update={"proposal_id": "sha256:" + "a" * 8 + "1" * 56}),
        admission.model_copy(update={"proposal_id": "sha256:" + "a" * 8 + "2" * 56}),
    )

    class AmbiguousEvidence:
        def list_admissions(self):  # type: ignore[no-untyped-def]
            return forced

    with monkeypatch.context() as scoped:
        scoped.setattr(instance, "proposal_evidence", lambda: AmbiguousEvidence())
        with pytest.raises(ProposalSelectorAmbiguousError) as ambiguous_prefix:
            service_resolve_playbill_proposal_selector(
                instance,
                selector="sha256:" + "a" * 8,
            )
    assert ambiguous_prefix.value.candidates == tuple(item.proposal_id for item in forced)
    assert ambiguous_prefix.value.repair_commands == ("cruxible playbill proposal list",)

    subprocess.run(
        [
            "git",
            f"--git-dir={instance._ledger.path}",
            "update-ref",
            "-d",
            admission.target_ref,
        ],
        check=True,
        capture_output=True,
    )
    assert instance.proposal_ref_target(admission.target_ref) is None
    with pytest.raises(ProposalSelectorAmbiguousError) as historical:
        service_resolve_playbill_proposal_selector(
            instance,
            selector=admission.target_ref,
        )
    assert historical.value.candidates == (admission.proposal_id,)
    assert "no longer names a current admission" in str(historical.value)
    assert "cruxible playbill proposal list" in str(historical.value)


def test_runtime_whoami_uses_the_runtime_credential_label_as_actor_id(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner = seed_claims(tmp_path)

    class Manager:
        def get(self, instance_id: str):  # type: ignore[no-untyped-def]
            assert instance_id == instance.descriptor.instance_id
            return instance

    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: Manager())
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(playbill_api, "get_current_mode", lambda: PermissionMode.READ_ONLY)
    monkeypatch.setattr(
        playbill_api,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            principal_id="cred_opaque",
            principal_label="owner",
            credential_type="runtime_credential",
            instance_scope=instance.descriptor.instance_id,
            role="read_only",
            effective_permission_mode=PermissionMode.READ_ONLY,
        ),
    )

    result = playbill_api.playbill_whoami(instance.descriptor.instance_id)

    assert result.actor_id == result.credential_label == "owner"
    assert result.actor_id_source == "runtime_credential_label"
    assert result.credential_permission_mode == "read_only"


WITHDRAWN_AT = "2026-09-08T09:00:00.000000Z"


def test_withdrawing_an_open_proposal_moves_it_out_of_the_open_inventory(
    tmp_path: Path,
) -> None:
    """Card 110(d): a proposal that can never activate stops being open work.

    The case this exists for is a proposal the ledger will refuse at activation
    forever. Nothing about accepted state changes and every byte of the
    candidate stays readable; what changes is that the actor's open list stops
    carrying a tombstone.
    """

    instance, _owner = seed_claims(tmp_path)
    opened = submit_query_definition_candidate(
        instance,
        query=work_item_query("doomed-query"),
        actor_id="owner",
        proposal_name="doomed-query",
        timestamp=TIMESTAMP,
    )
    proposal_id = opened.proposal.admission.proposal_id
    assert service_list_playbill_proposals(instance, status="open").entries != ()

    result = service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner",
        reason="its change-set record exceeds the ledger blob ceiling",
        withdrawn_at=WITHDRAWN_AT,
    )

    assert result.proposal_id == proposal_id
    assert result.already_withdrawn is False
    assert result.reason == "its change-set record exceeds the ledger blob ceiling"
    entry = next(
        item
        for item in service_list_playbill_proposals(instance).entries
        if item.proposal_id == proposal_id
    )
    assert entry.status == "settled"
    assert entry.terminal_reason == "withdrawn"
    assert service_list_playbill_proposals(instance, status="open").entries == ()
    # The candidate itself is untouched: withdrawal is inventory, not deletion.
    assert entry.candidate_digest == opened.proposal.evaluation.candidate_digest
    assert instance.proposal_evidence().read_candidate(entry.candidate_digest) is not None


def test_a_second_withdrawal_repeats_the_first_answer(tmp_path: Path) -> None:
    """Withdrawal is terminal, so its recorded reason cannot be rewritten."""

    instance, _owner = seed_claims(tmp_path)
    opened = submit_query_definition_candidate(
        instance,
        query=work_item_query("doomed-twice"),
        actor_id="owner",
        proposal_name="doomed-twice",
        timestamp=TIMESTAMP,
    )
    proposal_id = opened.proposal.admission.proposal_id
    first = service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner",
        reason="the first reason",
        withdrawn_at=WITHDRAWN_AT,
    )

    second = service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner",
        reason="a different reason",
        withdrawn_at="2026-09-09T09:00:00.000000Z",
    )

    assert second.already_withdrawn is True
    assert second.reason == first.reason == "the first reason"
    assert second.withdrawn_at == WITHDRAWN_AT


def test_only_the_submitting_actor_may_withdraw(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    opened = submit_query_definition_candidate(
        instance,
        query=work_item_query("not-yours"),
        actor_id="owner",
        proposal_name="not-yours",
        timestamp=TIMESTAMP,
    )

    with pytest.raises(ProposalAdmissionError, match="only the source proposal actor"):
        service_withdraw_playbill_proposal(
            instance,
            proposal_id=opened.proposal.admission.proposal_id,
            actor_id="someone-else",
            reason="not mine to withdraw",
            withdrawn_at=WITHDRAWN_AT,
        )

    entry = next(
        item
        for item in service_list_playbill_proposals(instance).entries
        if item.proposal_id == opened.proposal.admission.proposal_id
    )
    assert entry.status == "open"


def test_an_accepted_proposal_cannot_be_withdrawn(tmp_path: Path) -> None:
    """A settled outcome is not an intention, so withdrawal must not overwrite it."""

    instance, owner = seed_claims(tmp_path)
    accepted = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-44", "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name="advance-head",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, accepted)
    proposal_id = accepted.proposal.proposal.admission.proposal_id

    with pytest.raises(ProposalAdmissionError, match="only an open or stale proposal"):
        service_withdraw_playbill_proposal(
            instance,
            proposal_id=proposal_id,
            actor_id="owner",
            reason="second thoughts",
            withdrawn_at=WITHDRAWN_AT,
        )

    entry = next(
        item
        for item in service_list_playbill_proposals(instance).entries
        if item.proposal_id == proposal_id
    )
    assert entry.terminal_reason == "accepted"


def test_a_stale_proposal_may_be_withdrawn_instead_of_readmitted(tmp_path: Path) -> None:
    """Staleness is not an ending, so it is the other state withdrawal answers."""

    instance, owner = seed_claims(tmp_path)
    stale = submit_query_definition_candidate(
        instance,
        query=work_item_query("stale-then-withdrawn"),
        actor_id="owner",
        proposal_name="stale-then-withdrawn",
        timestamp=TIMESTAMP,
    )
    accepted = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-44", "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name="advance-head",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, accepted)
    proposal_id = stale.proposal.admission.proposal_id
    assert (
        next(
            item
            for item in service_list_playbill_proposals(instance).entries
            if item.proposal_id == proposal_id
        ).terminal_reason
        == "stale"
    )

    service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner",
        reason="superseded by the split generations",
        withdrawn_at=WITHDRAWN_AT,
    )

    entry = next(
        item
        for item in service_list_playbill_proposals(instance).entries
        if item.proposal_id == proposal_id
    )
    assert entry.terminal_reason == "withdrawn"


def test_a_withdrawn_proposal_cannot_be_activated(tmp_path: Path) -> None:
    """Withdrawal is terminal in fact, not only in the inventory.

    Nothing read the withdrawal store on the way to settlement, so a withdrawn
    proposal could be approved and activated and would then report `accepted` --
    the one outcome its own record says will never happen. Every settlement door
    refuses it now, naming the record: who withdrew it, when, and why.
    """

    instance, _owner = seed_claims(tmp_path)
    opened = submit_query_definition_candidate(
        instance,
        query=work_item_query("withdrawn-then-activated"),
        actor_id="owner",
        proposal_name="withdrawn-then-activated",
        timestamp=TIMESTAMP,
    )
    proposal_id = opened.proposal.admission.proposal_id
    service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner",
        reason="its change-set record exceeds the ledger blob ceiling",
        withdrawn_at=WITHDRAWN_AT,
    )

    with pytest.raises(ProposalWithdrawnError) as excinfo:
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposal_id,
            activated_by="owner",
        )

    assert excinfo.value.error_code == "playbill.proposal_withdrawn"
    assert excinfo.value.actor_id == "owner"
    assert excinfo.value.withdrawn_at == WITHDRAWN_AT
    assert "its change-set record exceeds the ledger blob ceiling" in str(excinfo.value)
    entry = next(
        item
        for item in service_list_playbill_proposals(instance).entries
        if item.proposal_id == proposal_id
    )
    assert entry.terminal_reason == "withdrawn"


def test_a_withdrawn_proposal_cannot_be_approved(tmp_path: Path) -> None:
    """The approval door reads the same record the activation door does."""

    instance, _owner = seed_claims(tmp_path)
    opened = submit_query_definition_candidate(
        instance,
        query=work_item_query("withdrawn-then-approved"),
        actor_id="owner",
        proposal_name="withdrawn-then-approved",
        timestamp=TIMESTAMP,
    )
    proposal_id = opened.proposal.admission.proposal_id
    candidate = opened.proposal.candidate
    assert candidate is not None
    service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner",
        reason="superseded",
        withdrawn_at=WITHDRAWN_AT,
    )

    with pytest.raises(ProposalWithdrawnError):
        service_submit_playbill_approval(
            instance,
            proposal_id=proposal_id,
            attestation=_sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
                instance.accepted_coordinate().semantic_root,
            ).attestation,
            authenticated_submitter="owner",
        )


def test_a_withdrawn_stale_proposal_cannot_be_readmitted(tmp_path: Path) -> None:
    """Readmission settles the withdrawn tree under a new id, so it refuses too."""

    instance, owner = seed_claims(tmp_path)
    stale = submit_query_definition_candidate(
        instance,
        query=work_item_query("stale-then-withdrawn-then-readmitted"),
        actor_id="owner",
        proposal_name="stale-then-withdrawn-then-readmitted",
        timestamp=TIMESTAMP,
    )
    accepted = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-44", "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name="advance-head",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, accepted)
    proposal_id = stale.proposal.admission.proposal_id
    service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner",
        reason="superseded by the split generations",
        withdrawn_at=WITHDRAWN_AT,
    )

    with pytest.raises(ProposalWithdrawnError):
        service_readmit_playbill_proposal(
            instance,
            proposal_id=proposal_id,
            actor_id="owner",
        )


def test_a_daemon_wide_operator_may_withdraw_a_foreign_proposal(tmp_path: Path) -> None:
    """A proposal whose author's credential label is gone is not withdrawable by nobody.

    The actor check is on the credential LABEL, because that is the only
    identity a proposal carries. A rotated, revoked or re-minted credential
    therefore leaves its open proposals with no author who can present that
    label, and the inventory keeps them forever -- card 110's graveyard, back
    through the door withdrawal exists to close. The daemon-wide operator, the
    authority that already allocates and stops hosts, is the way out; an
    instance-scoped credential of another label is still refused.
    """

    instance, _owner = seed_claims(tmp_path)
    opened = submit_query_definition_candidate(
        instance,
        query=work_item_query("orphaned-by-rotation"),
        actor_id="owner",
        proposal_name="orphaned-by-rotation",
        timestamp=TIMESTAMP,
    )
    proposal_id = opened.proposal.admission.proposal_id

    with pytest.raises(ProposalAdmissionError, match="or a daemon-wide operator"):
        service_withdraw_playbill_proposal(
            instance,
            proposal_id=proposal_id,
            actor_id="owner-rotated",
            reason="the author's credential was rotated",
            withdrawn_at=WITHDRAWN_AT,
        )

    result = service_withdraw_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        actor_id="owner-rotated",
        reason="the author's credential was rotated",
        withdrawn_at=WITHDRAWN_AT,
        unscoped_operator=True,
    )

    assert result.actor_id == "owner-rotated"
    entry = next(
        item
        for item in service_list_playbill_proposals(instance).entries
        if item.proposal_id == proposal_id
    )
    assert entry.terminal_reason == "withdrawn"
