"""PB-E CLI approval keeps private signing material entirely client-side."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.documents import DocumentAuthority, DocumentLifecycle, DocumentShell
from cruxible_core.cli.main import cli
from cruxible_core.service.authoring.documents import (
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.service.proposals.review import service_prepare_playbill_approval
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance


@pytest.mark.parametrize("hold", (False, True))
@pytest.mark.parametrize(
    "recompiled_status", (None, "pending_review", "ready_to_activate", "already_live")
)
@pytest.mark.parametrize("output_json", (False, True))
def test_cli_approval_signs_exact_challenge_without_transmitting_key(
    tmp_path: Path,
    monkeypatch,
    hold: bool,
    recompiled_status: str | None,
    output_json: bool,
) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Review me\n")
    shell = DocumentShell(
        identity="document:signing-test",
        document_kind="design",
        title="Signing test",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposal = service_propose_playbill_document(
        instance,
        shell=shell,
        actor_id="owner",
        proposal_name="cli-signing",
        timestamp=TIMESTAMP,
    ).proposal
    assert proposal.candidate is not None
    proposal_id = proposal.admission.proposal_id
    submitted: list[dict[str, Any]] = []
    holds: list[bool] = []
    recompiled = (
        None
        if recompiled_status is None
        else contracts.ProposalRecompiled(
            status=recompiled_status,
            source_proposal_id=proposal_id,
            **(
                {}
                if recompiled_status == "already_live"
                else {
                    "proposal_id": "sha256:" + "a" * 64,
                    "candidate_digest": "sha256:" + "b" * 64,
                }
            ),
            # Identical members after a pending recompilation are a landing, which
            # the CLI signs next (``test_cli_approval_signs_the_landed_candidate``).
            members_identical=recompiled_status != "pending_review",
            accepted_coordinate=contracts.AcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        )
    )

    class StubClient:
        def resolve_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.ProposalSelectorResult:
            return contracts.ProposalSelectorResult(
                selector=selector,
                proposal_id=selector,
            )

        def prepare_approval(
            self,
            instance_id: str,
            selected_proposal_id: str,
            *,
            signer_id: str,
            include_body: bool,
            reviewed_candidate_digest: str | None = None,
        ) -> contracts.ApprovalChallenge:
            assert instance_id == "inst_cli"
            challenge = service_prepare_playbill_approval(
                instance,
                proposal_id=selected_proposal_id,
                signer_id=signer_id,
                access=BodyAccessContext(
                    principal_id=signer_id,
                    can_read_body=include_body,
                ),
            )
            return contracts.ApprovalChallenge.model_validate(challenge.model_dump(mode="json"))

        def submit_approval(
            self,
            instance_id: str,
            selected_proposal_id: str,
            *,
            attestation: dict[str, Any],
            hold: bool = False,
            reviewed_candidate_digest: str | None = None,
        ) -> contracts.ApprovalReceipt | contracts.ProposalRecompiled:
            assert instance_id == "inst_cli"
            submitted.append(attestation)
            holds.append(hold)
            if recompiled is not None:
                return recompiled
            receipt = service_submit_playbill_approval(
                instance,
                proposal_id=selected_proposal_id,
                attestation=ApprovalAttestation.model_validate(attestation),
                authenticated_submitter="reviewer",
                activate_when_complete=not hold,
                may_activate=True,
            )
            return contracts.ApprovalReceipt.model_validate(receipt.model_dump(mode="json"))

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            proposal_id,
            "--signer-id",
            "reviewer",
            "--key",
            str(reviewer.private_key_path),
            "--yes",
            *(("--hold",) if hold else ()),
            *(("--json",) if output_json else ()),
        ],
    )

    assert result.exit_code == 0, result.output
    assert holds == [hold]
    if recompiled is not None:
        if output_json:
            assert json.loads(result.stdout) == recompiled.model_dump(mode="json")
        elif recompiled_status == "already_live":
            # Nothing was proposed, so there is no successor to name.
            assert result.stdout.splitlines()[-1] == (
                f"{proposal_id} is already live at the head: every change it carried is "
                "accepted; closed as answered, nothing to review."
            )
            assert "None" not in result.stdout
        else:
            operation = "review" if recompiled_status == "pending_review" else "activate"
            assert result.stdout.splitlines()[-1] == (
                f"Next: cruxible proposal {operation} {recompiled.proposal_id}"
            )
        return
    if not output_json:
        # --yes prints one counts line before signing, not the approval summary.
        counts = result.stdout.splitlines()[0]
        assert counts.startswith(f"Signing {proposal.candidate.candidate_digest} ({proposal_id}): ")
        assert " change" in counts
        assert "Required approvals" not in result.stdout
        # The proposal passed is named beside the candidate signed (coldcli-10).
        assert (
            f"Approved {proposal.candidate.candidate_digest} ({proposal_id}) as reviewer"
            in result.stdout
        )
        return
    receipt = json.loads(result.stdout)
    # The reviewer's signature completes the candidate: it activates unless held.
    if hold:
        assert receipt["activation"] is None and receipt["not_activated"] == "held"
    else:
        assert receipt["not_activated"] is None
        assert receipt["activation"]["status"] == "accepted"
        assert receipt["activation"]["activated_by"] == "reviewer"
    assert len(submitted) == 1
    assert submitted[0]["payload_digest"] == proposal.candidate.candidate_digest
    assert submitted[0]["signing_semantic_root"] == (
        proposal.candidate.candidate.parent_semantic_root
    )
    serialized = json.dumps(submitted[0])
    assert str(reviewer.private_key_path) not in serialized
    assert "private_key" not in serialized


def test_cli_approval_signs_nothing_when_the_challenge_refuses_changed_members(
    tmp_path: Path, monkeypatch
) -> None:
    """Review F-001: landing a proposal for a late signer changed its members, so
    the daemon refuses the challenge. With no digest passed the CLI still signs
    and submits nothing, and prints the successor to review."""

    from cruxible_client.contracts.errors import ReviewedCandidateChanged
    from cruxible_core.governance.keys import generate_client_principal_key

    key = generate_client_principal_key(
        tmp_path / "keys", principal_id="late", kind="ordinary", forbidden_roots=()
    )
    submitted: list[object] = []

    class StubClient:
        def resolve_proposal_selector(self, instance_id: str, selector: str):  # type: ignore[no-untyped-def]
            return contracts.ProposalSelectorResult(selector=selector, proposal_id=selector)

        def prepare_approval(self, instance_id, selected, **options):  # type: ignore[no-untyped-def]
            assert options["reviewed_candidate_digest"] is None
            raise ReviewedCandidateChanged(
                selected,
                reviewed_candidate_digest="sha256:" + "3" * 64,
                current_proposal_id="sha256:" + "8" * 64,
                current_candidate_digest="sha256:" + "4" * 64,
                members_identical=False,
            )

        def submit_approval(self, *args: Any, **kwargs: Any) -> Any:
            submitted.append(kwargs)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            "sha256:" + "7" * 64,
            "--signer-id",
            "late",
            "--key",
            str(key.private_key_path),
            "--yes",
        ],
    )

    assert result.exit_code != 0 and submitted == []
    assert "members identical: false; nothing was signed" in result.output
    assert f"cruxible proposal review sha256:{'8' * 64}" in result.output
    assert "Approved" not in result.output


@pytest.mark.parametrize("pinned", [False, True])
def test_cli_approval_signs_the_landed_candidate(tmp_path: Path, monkeypatch, pinned: bool) -> None:
    """Nothing it touches changed since its base: one approve lands it and activates it.

    The daemon lands the same change set at the head as a new candidate; an
    approval signs exactly one candidate, so the CLI signs the landed one next.
    With ``--candidate-digest`` (the reviewed candidate) it refuses instead,
    naming the landed proposal and that its members are identical (coldmcp-10).
    Either way the source closes as superseded under the reviewer, whose
    approval landed it, not as withdrawn by its author (psw F5).
    """

    from cruxible_core.service.proposals.proposals import _proposal_result
    from tests.core_support._support import client_material
    from tests.core_support._write_support import KIND, seed_write_surface
    from tests.test_ledger.test_activation import _sign
    from tests.test_service.test_write_verbs import _set, _write

    instance, _owner = seed_write_surface(tmp_path, self_approval=False)
    reviewer = client_material(tmp_path, instance)
    pending = _write(instance, _set(f"{KIND}/wi-1", "title", "behind"))
    moving = _write(instance, _set(f"{KIND}/wi-2", "title", "unrelated"))
    assert pending.proposal is not None and moving.proposal is not None
    source = _proposal_result(instance, pending.proposal.proposal_id)
    moved = _proposal_result(instance, moving.proposal.proposal_id)
    assert source.candidate is not None and moved.candidate is not None
    service_submit_playbill_approval(
        instance,
        proposal_id=moved.admission.proposal_id,
        attestation=_sign(
            reviewer,
            moved.candidate.candidate_digest,
            moved.candidate.candidate.parent_semantic_root,
        ).attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=True,
        may_activate=True,
    )
    submitted: list[tuple[str, dict[str, Any]]] = []

    class StubClient:
        def resolve_proposal_selector(self, instance_id: str, selector: str):  # type: ignore[no-untyped-def]
            return contracts.ProposalSelectorResult(selector=selector, proposal_id=selector)

        def prepare_approval(  # type: ignore[no-untyped-def]
            self, instance_id, selected, *, signer_id, include_body=False, **bound
        ):
            challenge = service_prepare_playbill_approval(
                instance,
                proposal_id=selected,
                signer_id=signer_id,
                access=BodyAccessContext(principal_id=signer_id, can_read_body=include_body),
                **bound,
            )
            return contracts.ApprovalChallenge.model_validate(challenge.model_dump(mode="json"))

        def submit_approval(self, instance_id, selected, *, attestation, hold=False, **bound):  # type: ignore[no-untyped-def]
            submitted.append((selected, attestation))
            result = service_submit_playbill_approval(
                instance,
                proposal_id=selected,
                attestation=ApprovalAttestation.model_validate(attestation),
                authenticated_submitter="reviewer",
                activate_when_complete=not hold,
                may_activate=True,
                **bound,
            )
            if isinstance(result, (contracts.ProposalRecompiled, contracts.ActivationReceipt)):
                return result
            return contracts.ApprovalReceipt.model_validate(result.model_dump(mode="json"))

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    head_root = instance.accepted_coordinate().semantic_root
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            source.admission.proposal_id,
            "--signer-id",
            "reviewer",
            "--key",
            str(reviewer.private_key_path),
            "--yes",
            *(("--candidate-digest", source.candidate.candidate_digest) if pinned else ()),
        ],
    )

    from cruxible_core.service.proposals.proposals import service_playbill_proposal_status

    source_id = source.admission.proposal_id
    closed = service_playbill_proposal_status(instance, proposal_id=source_id)
    record = instance.proposal_evidence().read_withdrawal(source_id)
    assert record is not None
    assert (record.kind, record.actor_id, record.tag) == (
        "superseded",
        "reviewer",
        "playbill-proposal-withdrawal-v2",
    )
    assert (closed.status, closed.terminal_reason, closed.closed_by) == (
        "settled",
        "superseded",
        "reviewer",
    )
    landed_proposal = _proposal_result(instance, str(closed.superseded_by))
    assert landed_proposal.candidate is not None
    if pinned:
        assert result.exit_code != 0
        # Only the reviewed candidate was signed for; the landed one was not.
        ((first_id, first),) = submitted
        assert first["payload_digest"] == source.candidate.candidate_digest
        assert "cruxible.approval.reviewed_candidate_changed" in result.output
        assert (
            f"proposal {source_id} was proposed again at the head as "
            f"{closed.superseded_by} (candidate {landed_proposal.candidate.candidate_digest}), "
            f"not the reviewed {source.candidate.candidate_digest}; members identical: true; "
            "nothing was signed"
        ) in result.output
        assert f"cruxible proposal review {closed.superseded_by}" in result.output
        assert not instance.proposal_evidence().read_approvals(
            landed_proposal.candidate.candidate_digest
        )
        return
    assert result.exit_code == 0, result.output
    ((first_id, first), (landed_id, landed)) = submitted
    assert first_id == source_id and landed_id == closed.superseded_by
    assert first["payload_digest"] == source.candidate.candidate_digest
    assert landed_id != first_id and landed["payload_digest"] != first["payload_digest"]
    # The landed candidate sits on the head the approval met.
    assert landed["signing_semantic_root"] == head_root
    # Said once: the landed proposal and the one passed (gapa-03, coldcli-10).
    assert (
        f"Landed at the head as {landed_id}: the same change set (proposed as {first_id})"
        in result.stdout
    )
    assert f"Approved {landed['payload_digest']} ({landed_id}) as reviewer" in result.stdout
    assert "replaces" not in result.stdout
    assert "Activated at" in result.stdout


def test_cli_missing_signer_never_falls_back_to_daemon(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instance, _owner, _reviewer = _instance(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Missing key\n")
    proposal = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:missing-key",
            document_kind="design",
            title="Missing key",
            media_type="text/markdown",
            body_digest=body.digest,
            authority=DocumentAuthority(
                required_tier="graph_write",
            ),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="missing-key",
        timestamp=TIMESTAMP,
    ).proposal
    submitted = False

    class StubClient:
        def resolve_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.ProposalSelectorResult:
            return contracts.ProposalSelectorResult(
                selector=selector,
                proposal_id=selector,
            )

        def prepare_approval(
            self,
            _instance_id: str,
            proposal_id: str,
            *,
            signer_id: str,
            include_body: bool,
            reviewed_candidate_digest: str | None = None,
        ) -> contracts.ApprovalChallenge:
            challenge = service_prepare_playbill_approval(
                instance,
                proposal_id=proposal_id,
                signer_id=signer_id,
                access=BodyAccessContext(
                    principal_id=signer_id,
                    can_read_body=include_body,
                ),
            )
            return contracts.ApprovalChallenge.model_validate(challenge.model_dump(mode="json"))

        def submit_approval(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal submitted
            submitted = True

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    missing = tmp_path / "missing-private-key"
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            proposal.admission.proposal_id,
            "--signer-id",
            "owner",
            "--key",
            str(missing),
            "--yes",
        ],
    )

    # The review challenge may be fetched, but a missing client key cannot
    # silently turn into a daemon/bearer signature or an attestation submit.
    assert result.exit_code != 0
    assert submitted is False
    # Still creation's base kit generation: nothing was accepted.
    assert instance.accepted_history()[-1].sequence == 1


def test_cli_approval_without_a_terminal_refuses_before_contacting_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("no prompt, no daemon call")),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            "PRP-0123456789ab",
            "--signer-id",
            "reviewer",
        ],
    )

    assert result.exit_code == 2
    assert "Aborted" not in result.output
    assert (
        "review it with `cruxible proposal review PRP-0123456789ab`, then rerun with --yes"
        in result.stderr
    )


def test_cli_approval_prompt_shows_the_reviewers_view_before_asking(
    tmp_path: Path, monkeypatch
) -> None:
    """The moment someone signs shows what `proposal review` shows, then the question."""

    from cruxible_core.service.proposals.review import service_review_playbill_proposal

    instance, _owner, reviewer = _instance(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Review me\n")
    shell = DocumentShell(
        identity="document:signing-test",
        document_kind="design",
        title="Signing test",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposal = service_propose_playbill_document(
        instance, shell=shell, actor_id="owner", proposal_name="cli-signing", timestamp=TIMESTAMP
    ).proposal
    proposal_id = proposal.admission.proposal_id
    access = BodyAccessContext(principal_id="reviewer", can_read_body=True)
    asked: list[dict[str, Any]] = []

    class StubClient:
        def resolve_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.ProposalSelectorResult:
            return contracts.ProposalSelectorResult(selector=selector, proposal_id=selector)

        def review_proposal(
            self, instance_id: str, selected: str, **options: Any
        ) -> contracts.ProposalReviewSummary:
            asked.append(options)
            return contracts.ProposalReviewSummary.model_validate(
                service_review_playbill_proposal(
                    instance, proposal_id=selected, access=access, **options
                ).model_dump(mode="json")
            )

        def prepare_approval(
            self,
            instance_id: str,
            selected: str,
            *,
            signer_id: str,
            include_body: bool,
            reviewed_candidate_digest: str | None = None,
        ) -> contracts.ApprovalChallenge:
            challenge = service_prepare_playbill_approval(
                instance, proposal_id=selected, signer_id=signer_id, access=access
            )
            return contracts.ApprovalChallenge.model_validate(challenge.model_dump(mode="json"))

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr("cruxible_core.cli.commands.playbill._can_confirm", lambda: True)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            proposal_id,
            "--signer-id",
            "reviewer",
            "--key",
            str(reviewer.private_key_path),
        ],
        input="n\n",
    )

    assert result.exit_code != 0  # declined: nothing is signed
    assert asked == [{"groups": False}]
    lines = result.output.splitlines()
    assert f"Proposal: {proposal_id}" in lines
    assert f"Candidate: {proposal.candidate.candidate_digest}" in lines
    assert "Definitions (1):" in lines
    assert "  Document signing-test: created" in lines
    assert (
        f"Stored fields grouped by kind: cruxible proposal review {proposal_id} --json. "
        f"Every member's artifact and delta: cruxible proposal review {proposal_id} --detail"
    ) in lines
    assert any(line.startswith("Required approvals: ") for line in lines)
    assert lines.index("Sign this exact candidate? [y/N]: n") > lines.index("Definitions (1):")
    # Declining prints a plain line, without the unnamed-error framing.
    assert lines[-1] == "Not approved."
    assert result.exit_code == 1
    assert "Error" not in result.output and "Abort" not in result.output


def test_an_approval_awaiting_more_names_whose_and_the_approve_next() -> None:
    """gapa-08 (the parts that are this computation): "Awaiting 1 more approval"
    says whose, and the next line approves rather than activates (which refuses)."""

    from types import SimpleNamespace

    from cruxible_core.cli.commands.playbill import (
        _approval_outcome_lines,
        _principal_add_next_steps,
    )

    receipt = contracts.ApprovalReceipt(
        proposal_id="sha256:" + "a" * 64,
        candidate_digest="sha256:" + "b" * 64,
        signer_id="reviewer",
        submitted_by="reviewer",
        signing_semantic_root="sha256:" + "c" * 64,
        attestation_digest="sha256:" + "d" * 64,
        key_history_ref="principals/reviewer.json@sha256:" + "c" * 64,
        not_activated="approvals_remaining",
        approvals_remaining=2,
        required_signers=("owner",),
        eligible_signers=("ops2",),
    )
    proposal = receipt.proposal_id

    lines = _approval_outcome_lines(receipt)
    steps = _principal_add_next_steps(
        SimpleNamespace(  # type: ignore[arg-type]
            activated=False, proposal_id=proposal, signer_id="reviewer", approval=receipt
        ),
        "newcomer",
        Path("/keys"),
        "governed_write",
    )

    assert lines[0] == (
        "Awaiting 2 more approvals (owner must sign; 1 from: ops2); the approval that "
        "completes it activates it"
    )
    assert lines[1:] == [
        f"Next: cruxible proposal approve {proposal} --signer-id owner",
        f"Next: cruxible proposal approve {proposal} --signer-id ops2",
    ]
    assert steps[:2] == [
        f"cruxible proposal approve {proposal} --signer-id owner",
        f"cruxible proposal approve {proposal} --signer-id ops2",
    ]
    assert not any("proposal activate" in step for step in steps)


def test_a_principal_change_proposal_names_its_proposers_approval_next(capsys) -> None:  # type: ignore[no-untyped-def]
    """gapa-08: rotate, revoke and recover printed `next: cruxible get`; a principal
    change needs its proposer's own signature, so the next line is that approval."""

    from cruxible_core.cli.commands.playbill import _echo_proposal

    proposal_id = "sha256:" + "e" * 64
    inspection = contracts.ProposalInspection(
        proposal={
            "admission": {"proposal_id": proposal_id, "actor_id": "ops2"},
            "evaluation": {"verdict": "candidate"},
        },
        accepted_coordinate=contracts.AcceptedCoordinate(
            git_oid="f" * 40,
            semantic_root="sha256:" + "1" * 64,
            generation_root="sha256:" + "2" * 64,
            compiler_digest="sha256:" + "3" * 64,
        ),
    )

    _echo_proposal(inspection, None, principal_change=True)
    principal = capsys.readouterr().out
    _echo_proposal(inspection, None)
    ordinary = capsys.readouterr().out

    assert (
        f"next: cruxible proposal approve {proposal_id} --signer-id ops2 "
        "(a principal change needs its proposer's own signature)"
    ) in principal
    assert f"next: cruxible get {proposal_id}" in ordinary


def test_approve_with_no_principal_refuses_as_every_proposal_act_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no principal configured, approve answered a client-side usage error (exit
    2) while activate, reject, withdraw and readmit answer the daemon's
    `cruxible.identity.principal_unconfigured`; approve now refuses the same way,
    with the signer hint (`--principal-id ID --key FILE`) kept in its repair."""

    from cruxible_client.contracts.principals import AuthoringRefusal
    from cruxible_client.contracts.repairs import RepairOperation

    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_ID", raising=False)
    coordinate = contracts.AcceptedCoordinate(
        git_oid="f" * 40,
        semantic_root="sha256:" + "1" * 64,
        generation_root="sha256:" + "2" * 64,
        compiler_digest="sha256:" + "3" * 64,
    )
    detail = (
        "this process names no principal, and the local operator identity 'operator' is "
        "not a registered principal on inst_cli; repair: set CRUXIBLE_PRINCIPAL_ID"
    )

    class StubClient:
        def whoami(self, instance_id: str) -> contracts.WhoAmI:
            return contracts.WhoAmI(
                actor_id="operator",
                credential_label=None,
                actor_id_source="local_operator",
                authenticated=False,
                credential_permission_mode="admin",
                principal_registration_status="absent",
                active_principal_ids=["owner", "reviewer"],
                coordinate=coordinate,
                can_author=False,
                authoring_refusal=AuthoringRefusal(
                    code="cruxible.identity.principal_unconfigured",
                    detail=detail,
                    repair=RepairOperation(
                        operation="cruxible.orient",
                        arguments={"section": "principals", "configure": "CRUXIBLE_PRINCIPAL_ID"},
                    ),
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            "sha256:" + "a" * 64,
            "--yes",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "cruxible.identity.principal_unconfigured" in result.output
    assert "to approve, pass --principal-id ID --key FILE" in result.output
    assert '"or_pass":"--principal-id ID --key FILE"' in result.output
