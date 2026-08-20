"""End-to-end proof that a Playbill instance is drivable from the CLI alone.

This is the TauBench-runnable surface, written as the harness recipe it has to
support: allocate a host, bootstrap it, seed a ClaimType, a Subject, two Claims,
and a named entrypoint through the governed propose/approve/activate loop, then
read the resulting accepted state back through every read the loop publishes --
query execution with its receipt, semantic discovery, bounded expansion, and the
deterministic floor.

Every step goes through ``cruxible ...`` argv. Nothing here reaches into a
service, and no fixture writes accepted state on the test's behalf.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.playbill.claim_types import claim_type_digest
from cruxible_core.playbill.claims import ClaimStatement, LiteralClaimObject
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from cruxible_core.service.playbill_claims import DirectClaimAuthoringV1
from tests.test_playbill._knowledge_loop_support import (
    PREDICATE,
    QUERY_NAME,
    SUBJECT_KIND,
    subject_address,
    subject_shell,
    work_item_query,
)
from tests.test_playbill.test_claims import _claim_type

SIGNER_ID = "operator"


class _Cli:
    """Invoke the real CLI against one served daemon, as an operator would."""

    def __init__(self, runner: CliRunner, key_dir: Path) -> None:
        self._runner = runner
        self.private_key = key_dir / f"{SIGNER_ID}.ed25519"

    def run(self, *args: str) -> Result:
        result = self._runner.invoke(cli, list(args))
        assert result.exit_code == 0, f"cruxible {' '.join(args)}\n{result.output}"
        return result

    def json(self, *args: str) -> Any:
        return json.loads(self.run(*args, "--json").stdout)

    def accept(self, proposal_id: str) -> dict[str, Any]:
        """Approve with the client-held key and settle, exactly as an operator does."""

        self.run(
            "playbill",
            "proposal",
            "approve",
            proposal_id,
            "--signer-id",
            SIGNER_ID,
            "--key",
            str(self.private_key),
            "--yes",
            "--json",
        )
        activated = self.json("playbill", "proposal", "activate", proposal_id)
        assert activated["status"] == "accepted", activated
        return activated


def _write(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _proposal_id(inspection: dict[str, Any]) -> str:
    return str(inspection["proposal"]["admission"]["proposal_id"])


def _claim_authoring(subject_id: str, value: str, *, seed_subject: bool) -> DirectClaimAuthoringV1:
    """One direct-Claim authoring request against the already-accepted ClaimType."""

    claim_type = _claim_type()
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=subject_address(subject_id),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale=f"The reviewed status of {subject_id} is {value}.",
        subject_shell=subject_shell(subject_id) if seed_subject else None,
    )


@pytest.fixture
def served_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Cli]:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()

    with TestClient(create_app()) as transport:
        client = CruxibleClient(base_url="http://cruxible")
        client._client = transport  # type: ignore[assignment]
        monkeypatch.setattr(
            "cruxible_core.cli.commands._common._get_client",
            lambda: client,
        )
        yield _Cli(CliRunner(), tmp_path / "custody")

    get_playbill_manager().clear()
    reset_registry()
    reset_permissions()


def test_cli_drives_the_whole_knowledge_loop_on_a_served_instance(
    served_cli: _Cli,
    tmp_path: Path,
) -> None:
    cruxible = served_cli

    # 1. Allocate a host and bootstrap it. The host is remembered, so every
    #    later command names neither the daemon nor the instance.
    host = cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    assert host["status"] == "created"
    initialized = cruxible.json(
        "playbill",
        "init",
        "--key-dir",
        str(tmp_path / "custody"),
        "--principal-id",
        SIGNER_ID,
    )
    assert initialized["instance_id"] == host["instance_id"]
    assert cruxible.private_key.is_file()

    # 2. Seed the predicate vocabulary.
    claim_type = _claim_type()
    proposed = cruxible.json(
        "playbill",
        "claim-type",
        "propose",
        "--envelope",
        _write(tmp_path / "claim-type.json", claim_type.model_dump(mode="json")),
        "--name",
        "seed-claim-type",
    )
    cruxible.accept(_proposal_id(proposed))

    # 3. Seed one Subject on its own surface.
    proposed = cruxible.json(
        "playbill",
        "subject",
        "propose",
        "--envelope",
        _write(tmp_path / "subject.json", subject_shell("wi-42").model_dump(mode="json")),
        "--name",
        "seed-subject",
    )
    cruxible.accept(_proposal_id(proposed))

    # 4. Two Claims: the first against accepted dependencies, the second
    #    carrying its own Subject so dependency-closed authoring is exercised.
    claim_identities: list[str] = []
    for subject_id, value, seed_subject in (
        ("wi-42", "ready", False),
        ("wi-43", "blocked", True),
    ):
        authoring = _claim_authoring(subject_id, value, seed_subject=seed_subject)
        proposal = cruxible.json(
            "playbill",
            "claim",
            "propose",
            "--authoring",
            _write(tmp_path / f"claim-{subject_id}.json", authoring.model_dump(mode="json")),
            "--name",
            f"seed-claim-{subject_id}",
        )
        claim_identities.append(str(proposal["claim_identity"]))
        cruxible.accept(_proposal_id(proposal["proposal"]))

    # 5. Publish the named entrypoint that reads them.
    proposed = cruxible.json(
        "playbill",
        "query",
        "propose",
        "--envelope",
        _write(tmp_path / "query.json", work_item_query().model_dump(mode="json")),
        "--name",
        "seed-query",
    )
    accepted = cruxible.accept(_proposal_id(proposed))
    coordinate = accepted["accepted_coordinate"]

    # -- reads ------------------------------------------------------------

    subjects = cruxible.json("playbill", "subject", "list")
    assert {item["envelope"]["identity"] for item in subjects["subjects"]} == {
        f"Subject:{SUBJECT_KIND}/wi-42",
        f"Subject:{SUBJECT_KIND}/wi-43",
    }
    assert subjects["coordinate"] == coordinate
    assert (
        cruxible.json("playbill", "subject", "get", SUBJECT_KIND, "wi-42")["envelope"]["identity"]
        == f"Subject:{SUBJECT_KIND}/wi-42"
    )
    assert cruxible.json("playbill", "subject", "history", SUBJECT_KIND, "wi-42")["entries"]

    claim_types = cruxible.json("playbill", "claim-type", "list")
    assert [item["predicate"] for item in claim_types["claim_types"]] == [PREDICATE]
    assert cruxible.json("playbill", "claim-type", "get", PREDICATE)["predicate"] == PREDICATE

    claims = cruxible.json("playbill", "claim", "list", "--predicate", PREDICATE)
    assert {item["envelope"]["identity"] for item in claims["claims"]} == set(claim_identities)
    assert (
        cruxible.json("playbill", "claim", "get", claim_identities[0])["envelope"]["identity"]
        == claim_identities[0]
    )
    assert cruxible.json("playbill", "claim", "history", claim_identities[0])["entries"]
    explained = cruxible.json("playbill", "claim", "explain", claim_identities[0])
    assert explained["verdict"]["verdict"] == "supported"
    assert explained["law_evidence"]

    definitions = cruxible.json("playbill", "query", "list")
    assert [item["name"] for item in definitions["query_definitions"]] == [QUERY_NAME]
    assert cruxible.json("playbill", "query", "get", QUERY_NAME)["name"] == QUERY_NAME

    # 6. Execute the entrypoint. The receipt is the replay coordinate of the
    #    read, and it is surfaced in both output modes.
    run = cruxible.json("playbill", "query", "run", QUERY_NAME)
    assert run["result"]["verdict"] == "completed"
    projected = {
        next(field["value"] for field in row["fields"] if field["name"] == "item_id"): next(
            field["value"] for field in row["fields"] if field["name"] == "status"
        )
        for row in run["result"]["rows"]
    }
    assert projected == {"wi-42": "ready", "wi-43": "blocked"}
    receipt = run["receipt"]
    assert receipt["tag"] == "playbill-query-execution-receipt-v1"
    assert receipt["verdict"] == "completed"
    assert receipt["result_digest"].startswith("sha256:")
    assert receipt["definition_digest"] == run["definition_digest"]
    # The daemon opens no journal for this read; PC-G owns that seam.
    assert run["journal_record_digest"] is None

    # Replaying at the receipt's own evaluation time reproduces it exactly, and
    # the human rendering names the same receipt the JSON does.
    human = cruxible.run(
        "playbill",
        "query",
        "run",
        QUERY_NAME,
        "--evaluation-time",
        receipt["evaluation_time"],
    ).stdout
    assert f"Receipt result digest: {receipt['result_digest']}" in human
    assert f"Receipt definition: {receipt['definition_digest']}" in human
    assert f"Receipt parameters: {receipt['parameter_digest']}" in human
    assert f"{QUERY_NAME}: completed with 2 row(s)" in human

    # 7. Discovery and bounded expansion over the same accepted coordinate.
    page = cruxible.json("playbill", "discover", "--query", "wi-42", "--profile", "all")
    assert page["vocabulary_entry_count"] > 0
    assert any(
        hit["address"]["artifact_path"] == f"subjects/{SUBJECT_KIND}/wi-42.yaml"
        for hit in page["page"]["hits"]
    )

    capsule = cruxible.json("playbill", "expand", f"subjects/{SUBJECT_KIND}/wi-42.yaml")
    assert capsule["tag"] == "playbill-context-capsule-v1"
    assert capsule["at"] == coordinate
    assert capsule["canonical_summary"]["identity"] == f"Subject:{SUBJECT_KIND}/wi-42"

    # 8. Materialize the floor and prove it carries the accepted facts bound to
    #    the coordinate they were projected from.
    floor = tmp_path / "floor"
    exported = cruxible.json("playbill", "floor", "export", "--output", str(floor))
    manifest = json.loads((floor / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == exported
    assert manifest["coordinate"] == coordinate
    assert manifest["floor_digest"].startswith("sha256:")

    written = {str(path.relative_to(floor)) for path in floor.rglob("*") if path.is_file()} - {
        "manifest.json"
    }
    assert written == {item["path"] for item in manifest["files"]}
    assert f"subjects/{SUBJECT_KIND}/wi-42.profile.json" in written
    assert f"subjects/{SUBJECT_KIND}/wi-43.profile.json" in written
    assert "claim-types/project.work_item/status.card.json" in written

    profile = json.loads(
        (floor / f"subjects/{SUBJECT_KIND}/wi-42.profile.json").read_text(encoding="utf-8")
    )
    assert PREDICATE in json.dumps(profile)
    assert "ready" in json.dumps(profile)


def test_cli_floor_export_refuses_to_overwrite_a_non_empty_directory(
    served_cli: _Cli,
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    cruxible.json(
        "playbill",
        "init",
        "--key-dir",
        str(tmp_path / "custody"),
        "--principal-id",
        SIGNER_ID,
    )

    floor = tmp_path / "floor"
    floor.mkdir()
    (floor / "occupied.txt").write_text("not the floor\n", encoding="utf-8")

    refused = CliRunner().invoke(
        cli, ["playbill", "floor", "export", "--output", str(floor), "--json"]
    )

    assert refused.exit_code != 0
    assert "Refusing to write the floor into a non-empty directory" in refused.output
    assert (floor / "occupied.txt").read_text(encoding="utf-8") == "not the floor\n"


def test_cli_batch_propose_settles_every_claim_in_one_generation(
    served_cli: _Cli,
    tmp_path: Path,
) -> None:
    """The plural command drives CLI -> HTTP -> facade -> service in one call.

    The load-bearing observation is the generation count: two Claims arrive
    through one proposal id, one approval, and one activation, so the accepted
    ledger gains exactly one generation carrying both.
    """

    cruxible = served_cli
    cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    cruxible.json(
        "playbill",
        "init",
        "--key-dir",
        str(tmp_path / "custody"),
        "--principal-id",
        SIGNER_ID,
    )
    proposed = cruxible.json(
        "playbill",
        "claim-type",
        "propose",
        "--envelope",
        _write(tmp_path / "claim-type.json", _claim_type().model_dump(mode="json")),
        "--name",
        "seed-claim-type",
    )
    seeded = cruxible.accept(_proposal_id(proposed))

    batch = cruxible.json(
        "playbill",
        "claim",
        "propose-batch",
        "--authoring",
        _write(
            tmp_path / "claim-wi-50.json",
            _claim_authoring("wi-50", "ready", seed_subject=True).model_dump(mode="json"),
        ),
        "--authoring",
        _write(
            tmp_path / "claim-wi-51.json",
            _claim_authoring("wi-51", "blocked", seed_subject=True).model_dump(mode="json"),
        ),
        "--name",
        "seed-both-claims",
    )

    assert batch["tag"] == "playbill-direct-claim-batch-proposal-v1"
    assert len(batch["claims"]) == 2
    accepted = cruxible.accept(_proposal_id(batch["proposal"]))
    assert accepted["accepted_coordinate"] != seeded["accepted_coordinate"]

    claims = cruxible.json("playbill", "claim", "list", "--predicate", PREDICATE)
    assert {item["envelope"]["identity"] for item in claims["claims"]} == {
        str(item["claim_identity"]) for item in batch["claims"]
    }
    for authored in batch["claims"]:
        history = cruxible.json("playbill", "claim", "history", str(authored["claim_identity"]))
        assert [entry["sequence"] for entry in history["entries"]] == [2]
