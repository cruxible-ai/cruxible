"""The ledger's publication to a private remote, and how it reports lagging."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_client.contracts.ledger_mirror import (
    PlaybillLedgerMirrorUrlInvalid,
    validate_mirror_url,
)
from cruxible_core.playbill.git import NOTE_REFS
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.ledger_mirror import MIRROR_STATE_FILE
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_proposals import service_withdraw_playbill_proposal
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_proposal_notes import _submit

WITHDRAWN_AT = "2026-08-11T13:00:00.000000Z"


def _bare_remote(tmp_path: Path, *, object_format: str, name: str = "mirror.git") -> Path:
    """Create the remote in the ledger's own object format.

    Git refuses a push between repositories with different hash algorithms, so
    an operator preparing a mirror creates it with `git init --bare
    --object-format=<the ledger's>`. The refusal is Git's and it is loud; the
    daemon reports it as `ledger_mirror_behind` with that exact text.
    """

    path = tmp_path / name
    subprocess.run(
        ["git", "init", "--bare", "-q", f"--object-format={object_format}", str(path)],
        check=True,
    )
    return path


def _remote_refs(remote: Path) -> tuple[str, ...]:
    output = subprocess.run(
        ["git", f"--git-dir={remote}", "for-each-ref", "--format=%(refname)"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    return tuple(line for line in output.splitlines() if line)


def _mirrored(tmp_path: Path) -> tuple[PlaybillInstance, Path]:
    instance, owner = initialize_local(tmp_path)
    remote = _bare_remote(tmp_path, object_format=instance.descriptor.git_object_format)
    state = instance.set_ledger_mirror(str(remote))
    assert state is not None and state.status == "current", state
    instance._owner_material = owner  # type: ignore[attr-defined]
    return instance, remote


def test_a_mirror_url_must_be_a_plain_credential_free_remote() -> None:
    for accepted in (
        "https://forge.invalid/team/ledger.git",
        "ssh://git@forge.invalid/team/ledger.git",
        "git@forge.invalid:team/ledger.git",
        "file:///srv/ledger.git",
        "/srv/ledger.git",
    ):
        assert validate_mirror_url(accepted) == accepted
    for refused in (
        "ext::sh -c 'curl evil'",
        "--upload-pack=/bin/sh",
        "http://forge.invalid/ledger.git",
        "https://token@forge.invalid/ledger.git",
        "ssh://user:secret@forge.invalid/ledger.git",
        "/srv/../etc/ledger.git",
        "",
    ):
        with pytest.raises(PlaybillLedgerMirrorUrlInvalid):
            validate_mirror_url(refused)


def test_setting_a_mirror_publishes_main_and_the_notes_at_once(tmp_path: Path) -> None:
    instance, remote = _mirrored(tmp_path)

    refs = _remote_refs(remote)

    assert "refs/heads/main" in refs
    assert instance.ledger_mirror_url() == str(remote)
    assert (instance.root / MIRROR_STATE_FILE).is_file()


def test_a_submitted_proposal_reaches_the_mirror_as_a_branch_with_its_eval_note(
    tmp_path: Path,
) -> None:
    instance, remote = _mirrored(tmp_path)

    result = _submit(instance)

    digest = result.admission.proposal_id.removeprefix("sha256:")
    refs = _remote_refs(remote)
    assert f"refs/heads/proposals/{digest}" in refs
    assert NOTE_REFS["evaluation"] in refs


def test_an_approval_reaches_the_mirror_as_its_own_note_ref(tmp_path: Path) -> None:
    instance, remote = _mirrored(tmp_path)
    result = _submit(instance)
    assert result.candidate is not None
    submission = _sign(
        instance._owner_material,  # type: ignore[attr-defined]
        result.candidate.candidate_digest,
        result.candidate.candidate.parent_semantic_root,
    )

    service_submit_playbill_approval(
        instance,
        proposal_id=result.admission.proposal_id,
        attestation=submission.attestation,
        authenticated_submitter="approval-relay",
    )

    assert NOTE_REFS["approval"] in _remote_refs(remote)


def test_activation_moves_main_and_replaces_the_branch_with_a_settled_ref(
    tmp_path: Path,
) -> None:
    instance, remote = _mirrored(tmp_path)
    result = _submit(instance)
    digest = result.admission.proposal_id.removeprefix("sha256:")
    before = (
        subprocess.run(
            ["git", f"--git-dir={remote}", "rev-parse", "refs/heads/main"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )

    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        activated_by="owner",
    )

    assert receipt.status == "accepted"
    refs = _remote_refs(remote)
    after = (
        subprocess.run(
            ["git", f"--git-dir={remote}", "rev-parse", "refs/heads/main"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    assert after != before
    assert f"refs/heads/proposals/{digest}" not in refs
    assert f"refs/settled/{digest}" in refs
    assert f"refs/settled/{digest}" in instance._ledger.settled_proposal_refs()


def test_withdrawal_retires_the_branch_on_both_sides(tmp_path: Path) -> None:
    instance, remote = _mirrored(tmp_path)
    result = _submit(instance)
    digest = result.admission.proposal_id.removeprefix("sha256:")
    assert f"refs/heads/proposals/{digest}" in _remote_refs(remote)

    service_withdraw_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        actor_id="owner",
        reason="superseded by a later change set",
        withdrawn_at=WITHDRAWN_AT,
    )

    refs = _remote_refs(remote)
    assert f"refs/heads/proposals/{digest}" not in refs
    assert f"refs/settled/{digest}" in refs


def test_a_failed_push_lands_the_write_and_records_the_lag(tmp_path: Path) -> None:
    """The remote is a copy; losing it may never refuse a governed write."""

    instance, remote = _mirrored(tmp_path)
    subprocess.run(["rm", "-rf", str(remote)], check=True)

    result = _submit(instance)

    assert result.evaluation.verdict == "candidate"
    state = instance.ledger_mirror_state()
    assert state is not None
    assert state.status == "behind"
    assert state.detail is not None
    assert instance.proposal_evidence().read_admission(result.admission.proposal_id) is not None


def test_a_repaired_remote_publishes_again_without_a_new_write(tmp_path: Path) -> None:
    instance, remote = _mirrored(tmp_path)
    subprocess.run(["rm", "-rf", str(remote)], check=True)
    _submit(instance)
    assert (state := instance.ledger_mirror_state()) is not None and state.status == "behind"

    subprocess.run(
        [
            "git",
            "init",
            "--bare",
            "-q",
            f"--object-format={instance.descriptor.git_object_format}",
            str(remote),
        ],
        check=True,
    )
    republished = instance.publish_ledger_mirror()

    assert republished is not None
    assert republished.status == "current"
    assert republished.published_main_oid == instance._ledger.read_main()
    assert "refs/heads/main" in _remote_refs(remote)
