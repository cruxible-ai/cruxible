"""0.6.0 proposal ergonomics: bare-hex selectors, 12-character CLI handles, ordered approve.

Every proposal command takes a proposal id written without ``sha256:`` (8 to 64
hex characters), human CLI text names proposals by their first 12 hex
characters, and ``proposal approve P1 P2 ...`` approves and activates several in
order after one prompt, landing each later one at the head the earlier ones
moved. ``--json`` keeps every full id.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, contracts
from cruxible_client.contracts.errors import ProposalNotFoundError, ProposalSelectorAmbiguousError
from cruxible_core.cli.commands import _common, playbill
from cruxible_core.cli.commands import context as context_commands
from cruxible_core.cli.commands.playbill import _short_proposal_id, _shorten_proposal_ids
from cruxible_core.cli.context import load_cli_context
from cruxible_core.cli.main import cli
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.config import get_runtime_bearer_token
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from cruxible_core.service.proposals.proposals import service_resolve_playbill_proposal_selector

URL = "http://cruxible-daemon"
FULL_ID = re.compile(r"sha256:[0-9a-f]{64}")


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in (
        "CRUXIBLE_SERVER_AUTH",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
        "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(playbill.getpass, "getuser", lambda: "owner")
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    app = create_app()

    def get_client() -> CruxibleClient:
        obj = _common._root_ctx_obj()
        cached = obj.get("_test_client")
        if isinstance(cached, CruxibleClient):
            return cached
        real = CruxibleClient(
            base_url=URL, token=get_runtime_bearer_token(), principal_id=obj.get("principal_id")
        )
        headers = dict(real._client._client.headers)
        real._client = TestClient(app, base_url=URL, headers=headers)  # type: ignore[assignment]
        obj["_test_client"] = real
        return real

    monkeypatch.setattr(_common, "_get_client", get_client)
    monkeypatch.setattr(context_commands, "_get_client", get_client)
    yield tmp_path
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _run(*args: str, principal: str | None = None, input: str | None = None) -> Result:
    base = ["--server-url", URL]
    if principal is not None:
        base += ["--principal-id", principal]
    return CliRunner().invoke(cli, [*base, *args], input=input)


def _ok(*args: str, principal: str | None = None) -> Result:
    result = _run(*args, principal=principal)
    assert result.exit_code == 0, f"cruxible {' '.join(args)}\n{result.output}"
    return result


def _json(result: Result) -> Any:
    return json.loads(result.stdout[result.stdout.index("{") :])


def _init(daemon: Path) -> str:
    """An instance whose base kit awaits the reviewer init created; its proposal id."""

    made = _run(
        "init",
        "--principal-id",
        "owner",
        "--reviewer-key-dir",
        str(daemon / "rev"),
        "--require-independent-approval",
    )
    assert made.exit_code == 0, made.output
    (line,) = [item for item in made.stdout.splitlines() if item.startswith("Base kit: ")]
    return line.split("proposal ", 1)[1].split(" ", 1)[0]


def _propose_subject_kind(daemon: Path, name: str, *, description: str | None = None) -> str:
    """The owner proposes a Subject kind (and its key ClaimType) the reviewer must approve.

    ``description`` makes a different proposal defining the same artifacts.
    """

    example = _ok("authoring", "example", "subject-kind", principal="owner")
    payload = example.stdout[example.stdout.index("{") :].replace("project.work_item", name)
    if description is not None:
        old = "The work item's readable name; its key."
        assert old in payload
        payload = payload.replace(old, description)
    path = daemon / f"{name}-{description is not None}.json"
    path.write_text(payload)
    submitted = _ok("authoring", "submit", str(path), "--json", principal="owner")
    record = _json(submitted)
    proposal_id = record["outcome"]["proposal_id"]
    assert FULL_ID.fullmatch(proposal_id), record
    return str(proposal_id)


def _instance() -> Any:
    instance_id = load_cli_context().instance_id
    assert instance_id is not None
    return get_playbill_manager().get(instance_id)


# -- display helpers ---------------------------------------------------------


def test_short_proposal_id_is_the_first_twelve_hex_characters() -> None:
    full = "sha256:" + "0123456789abcdef" * 4
    assert _short_proposal_id(full) == "0123456789ab"
    # Anything else (a ref, an already-short handle) is shown as given.
    assert _short_proposal_id("refs/proposals/owner/x") == "refs/proposals/owner/x"
    assert _short_proposal_id("0123456789ab") == "0123456789ab"
    text = f"Next: cruxible proposal approve {full} --signer-id reviewer"
    assert _shorten_proposal_ids(text, (full,)) == (
        "Next: cruxible proposal approve 0123456789ab --signer-id reviewer"
    )
    # Only the named ids are shortened; any other digest stays whole.
    other = "sha256:" + "f" * 64
    assert _shorten_proposal_ids(f"{full} {other}", (full,)) == f"0123456789ab {other}"


# -- bare-hex selectors (service) -----------------------------------------------


def test_bare_hex_selectors_resolve_like_their_sha256_prefix(daemon: Path) -> None:
    base_kit = _init(daemon)
    proposal_id = _proposal_id_from_list(base_kit)
    instance = _instance()
    bare = proposal_id.removeprefix("sha256:")

    for selector in (bare[:8], bare[:12], bare, f"sha256:{bare[:8]}", proposal_id):
        resolved = service_resolve_playbill_proposal_selector(instance, selector=selector)
        # The selector as given is kept; the id is the full one.
        assert resolved.selector == selector
        assert resolved.proposal_id == proposal_id

    with pytest.raises(ProposalNotFoundError):
        service_resolve_playbill_proposal_selector(instance, selector="0" * 12)
    with pytest.raises(ProposalNotFoundError):
        # Fewer than 8 hex characters is no prefix.
        service_resolve_playbill_proposal_selector(instance, selector=bare[:7])
    with pytest.raises(ProposalNotFoundError):
        # Upper case is not the id's spelling.
        service_resolve_playbill_proposal_selector(instance, selector=bare[:12].upper())


def test_a_target_ref_selector_still_resolves(daemon: Path) -> None:
    base_kit = _init(daemon)
    proposal_id = _proposal_id_from_list(base_kit)
    entry = _json(_ok("proposal", "list", "--json"))["entries"]
    (row,) = [item for item in entry if item["proposal_id"] == proposal_id]
    ref = row["target_ref"]
    assert ref.startswith("refs/")
    resolved = service_resolve_playbill_proposal_selector(_instance(), selector=ref)
    assert resolved.proposal_id == proposal_id


def test_an_ambiguous_bare_hex_prefix_refuses_naming_the_candidates(
    daemon: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(daemon)
    instance = _instance()
    evidence = instance.proposal_evidence()
    assert evidence.index is not None
    first = "sha256:" + "abcdef01" + "1" * 56
    second = "sha256:" + "abcdef01" + "2" * 56
    real_rows = evidence.index.rows

    def rows(store: Any, where: str, params: tuple[Any, ...]) -> Any:
        if where.startswith("proposal_id>=?") and params[0] == "sha256:abcdef01":
            return [{"proposal_id": first}, {"proposal_id": second}]
        return real_rows(store, where, params)

    monkeypatch.setattr(type(evidence.index), "rows", lambda self, *args: rows(*args))
    monkeypatch.setattr(instance, "proposal_evidence", lambda: evidence)

    with pytest.raises(ProposalSelectorAmbiguousError) as excinfo:
        service_resolve_playbill_proposal_selector(instance, selector="abcdef01")
    assert first in str(excinfo.value) and second in str(excinfo.value)
    assert "abcdef01" in str(excinfo.value)


# -- CLI handles ----------------------------------------------------------------


def _proposal_id_from_list(short_or_full: str) -> str:
    """The full id `proposal list --json` names for a printed handle or id."""

    entries = _json(_ok("proposal", "list", "--json"))["entries"]
    handle = short_or_full.removeprefix("sha256:")
    (match,) = [
        item["proposal_id"]
        for item in entries
        if item["proposal_id"].removeprefix("sha256:").startswith(handle)
    ]
    return str(match)


def test_cli_text_names_proposals_by_twelve_characters_and_json_keeps_full_ids(
    daemon: Path,
) -> None:
    _init(daemon)
    proposal_id = _propose_subject_kind(daemon, "alpha.item")
    short = proposal_id[7:19]

    listed = _ok("proposal", "list")
    assert short in listed.stdout
    assert proposal_id not in listed.stdout
    full = _ok("proposal", "list", "--full-ids")
    assert proposal_id in full.stdout
    as_json = _json(_ok("proposal", "list", "--json"))
    assert proposal_id in {entry["proposal_id"] for entry in as_json["entries"]}

    # `proposal review` keeps the full id; a bare handle selects it.
    reviewed = _ok("proposal", "review", short)
    assert proposal_id in reviewed.stdout
    assert _json(_ok("proposal", "review", short, "--json"))["proposal_id"] == proposal_id

    # `cruxible get` takes the handle with and without its kind.
    for selector in (f"Proposal:{short}", short, f"Proposal:{proposal_id[7:15]}"):
        got = _ok("get", selector, "--json")
        assert proposal_id in got.stdout

    # `next` names the waiting proposal and its repair by the handle; --json keeps it whole.
    queue = _ok("next", principal="reviewer")
    assert short in queue.stdout
    assert proposal_id not in queue.stdout
    next_json = _json(_ok("next", "--json", principal="reviewer"))
    assert proposal_id in {item["subject_identity"] for item in next_json["items"]}


def test_authoring_submit_brief_names_the_handle(daemon: Path) -> None:
    _init(daemon)
    example = _ok("authoring", "example", "subject-kind", principal="owner")
    path = daemon / "beta.json"
    path.write_text(
        example.stdout[example.stdout.index("{") :].replace("project.work_item", "beta.item")
    )

    brief = _ok("authoring", "submit", str(path), "--brief", principal="owner")

    (line,) = [item for item in brief.stdout.splitlines() if item.startswith("proposal: ")]
    handle = line.removeprefix("proposal: ")
    assert re.fullmatch(r"[0-9a-f]{12}", handle), brief.stdout
    assert not FULL_ID.search(brief.stdout)
    assert _proposal_id_from_list(handle).startswith(f"sha256:{handle}")


# -- ordered multi-proposal approve -----------------------------------------------


def test_approve_several_lands_each_at_the_head_the_earlier_ones_moved(
    daemon: Path,
) -> None:
    """P1 (the base kit) activates and moves the head; P2, proposed at the old head,
    touches nothing P1 changed, so it lands at the new head and is signed there."""

    base_kit = _proposal_id_from_list(_init(daemon))
    alpha = _propose_subject_kind(daemon, "alpha.item")
    head_before = _instance().accepted_coordinate().git_oid

    approved = _run(
        "proposal",
        "approve",
        base_kit[7:19],
        alpha[7:19],
        alpha,  # the same proposal again is approved once
        "--yes",
        principal="reviewer",
    )

    assert approved.exit_code == 0, approved.output
    assert "[1/2]" in approved.stdout and "[2/2]" in approved.stdout
    assert "[3/" not in approved.stdout
    assert f"Approved 2 of 2: {base_kit[7:19]} {alpha[7:19]}" in approved.stdout
    assert "Landed at the head as" in approved.stdout
    assert _instance().accepted_coordinate().git_oid != head_before
    entries = _json(_ok("proposal", "list", "--json"))["entries"]
    by_id = {entry["proposal_id"]: entry for entry in entries}
    assert by_id[base_kit]["terminal_reason"] == "accepted"
    # The proposal passed was superseded by the one landed at the new head, which settled.
    landed = by_id[alpha]["superseded_by"]
    assert landed is not None and by_id[landed]["terminal_reason"] == "accepted"


def test_approve_several_json_reports_each_receipt(daemon: Path) -> None:
    base_kit = _proposal_id_from_list(_init(daemon))
    alpha = _propose_subject_kind(daemon, "alpha.item")

    approved = _run("proposal", "approve", base_kit, alpha, "--yes", "--json", principal="reviewer")

    assert approved.exit_code == 0, approved.output
    payload = json.loads(approved.stdout)
    assert [item["proposal_id"] for item in payload["approved"]] == [base_kit, alpha]
    assert payload["failed"] is None
    assert payload["not_attempted"] == []
    for item in payload["approved"]:
        receipt = item["receipt"]
        assert receipt["activation"]["status"] == "accepted", receipt


def test_approve_several_stops_at_the_first_failure_and_keeps_the_earlier(
    daemon: Path,
) -> None:
    """Two proposals defining the same Subject kind: the second cannot land once the
    first activates, so it refuses; the earlier one stays accepted and the later
    ones are never attempted."""

    base_kit = _proposal_id_from_list(_init(daemon))
    _ok("proposal", "approve", base_kit, "--yes", principal="reviewer")
    first = _propose_subject_kind(daemon, "gamma.item")
    clash = _propose_subject_kind(daemon, "gamma.item", description="Another key.")
    later = _propose_subject_kind(daemon, "delta.item")
    assert first != clash

    approved = _run(
        "proposal", "approve", first, clash, later, "--yes", "--json", principal="reviewer"
    )

    assert approved.exit_code == 1, approved.output
    payload = json.loads(approved.stdout)
    assert [item["proposal_id"] for item in payload["approved"]] == [first]
    assert payload["failed"]["proposal_id"] == clash
    assert payload["failed"]["message"]
    assert payload["not_attempted"] == [later]
    by_id = {
        entry["proposal_id"]: entry for entry in _json(_ok("proposal", "list", "--json"))["entries"]
    }
    assert by_id[first]["terminal_reason"] == "accepted"
    assert by_id[later]["status"] == "open"

    text = _run("proposal", "approve", later, clash, "--yes", principal="reviewer")
    assert text.exit_code == 1, text.output
    assert f"Approved 1 of 2: {later[7:19]}" in text.stdout
    assert f"Failed: {clash[7:19]}" in text.stdout


def test_approve_several_refuses_a_candidate_digest(daemon: Path) -> None:
    base_kit = _proposal_id_from_list(_init(daemon))
    alpha = _propose_subject_kind(daemon, "alpha.item")
    digest = _json(_ok("proposal", "review", base_kit, "--json"))["candidate_digest"]

    refused = _run(
        "proposal", "approve", base_kit, alpha, "--candidate-digest", digest, "--yes",
        principal="reviewer",
    )  # fmt: skip

    assert refused.exit_code == 2, refused.output
    assert "--candidate-digest pins the one candidate you reviewed" in refused.output
    statuses = {
        entry["proposal_id"]: entry["status"]
        for entry in _json(_ok("proposal", "list", "--json"))["entries"]
    }
    assert statuses[base_kit] == statuses[alpha] == "open"


def test_approve_several_prompts_once_over_a_numbered_summary(
    daemon: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_kit = _proposal_id_from_list(_init(daemon))
    alpha = _propose_subject_kind(daemon, "alpha.item")
    monkeypatch.setattr(playbill, "_can_confirm", lambda: True)

    declined = _run("proposal", "approve", base_kit, alpha, principal="reviewer", input="n\n")

    assert declined.exit_code == 1, declined.output
    assert "Approving 2 proposals in order as reviewer:" in declined.stdout
    assert f"  1. {base_kit[7:19]}  by owner" in declined.stdout
    assert f"  2. {alpha[7:19]}  by owner" in declined.stdout
    assert declined.output.count("Approve and activate 2 proposals in order?") == 1
    assert "Sign this exact candidate?" not in declined.output
    assert "Not approved." in declined.output
    statuses = {
        entry["proposal_id"]: entry["status"]
        for entry in _json(_ok("proposal", "list", "--json"))["entries"]
    }
    assert statuses[base_kit] == statuses[alpha] == "open"

    held = _run("proposal", "approve", base_kit, alpha, "--hold", principal="reviewer", input="n\n")
    assert held.output.count("Approve 2 proposals in order?") == 1

    accepted = _run("proposal", "approve", base_kit, alpha, principal="reviewer", input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert accepted.output.count("Approve and activate 2 proposals in order?") == 1
    assert "Approved 2 of 2" in accepted.stdout


def test_approve_several_without_a_terminal_refuses_before_signing(daemon: Path) -> None:
    base_kit = _proposal_id_from_list(_init(daemon))
    alpha = _propose_subject_kind(daemon, "alpha.item")

    refused = _run("proposal", "approve", base_kit, alpha, principal="reviewer")

    assert refused.exit_code == 2, refused.output
    assert "review each with `cruxible proposal review ID`" in refused.output


def test_approve_several_refuses_an_unknown_selector_before_signing_any(daemon: Path) -> None:
    base_kit = _proposal_id_from_list(_init(daemon))

    refused = _run("proposal", "approve", base_kit, "0" * 12, "--yes", principal="reviewer")

    assert refused.exit_code == 1, refused.output
    statuses = {
        entry["proposal_id"]: entry["status"]
        for entry in _json(_ok("proposal", "list", "--json"))["entries"]
    }
    assert statuses[base_kit] == "open"


def test_single_approve_json_shape_is_unchanged(daemon: Path) -> None:
    base_kit = _proposal_id_from_list(_init(daemon))

    approved = _run("proposal", "approve", base_kit[7:19], "--yes", "--json", principal="reviewer")

    assert approved.exit_code == 0, approved.output
    receipt = json.loads(approved.stdout)
    # One proposal answers with its receipt, never the batch document.
    assert "approved" not in receipt and "not_attempted" not in receipt
    assert receipt["proposal_id"] == base_kit
    assert receipt["signer_id"] == "reviewer"
    assert receipt["activation"]["status"] == "accepted"
    assert contracts.ApprovalReceipt.model_validate(receipt).proposal_id == base_kit


def test_approve_several_full_renders_each_review_before_the_one_prompt(
    daemon: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_kit = _proposal_id_from_list(_init(daemon))
    alpha = _propose_subject_kind(daemon, "alpha.item")
    monkeypatch.setattr(playbill, "_can_confirm", lambda: True)

    declined = _run(
        "proposal", "approve", base_kit, alpha, "--full", principal="reviewer", input="n\n"
    )

    assert declined.exit_code == 1, declined.output
    before_prompt = declined.output.split("Approve and activate 2 proposals in order?")[0]
    # The full review keeps the full ids; the numbered summary shows the handles.
    assert f"Proposal: {base_kit}" in before_prompt
    assert f"Proposal: {alpha}" in before_prompt
    assert f"  2. {alpha[7:19]}  by owner" in before_prompt
