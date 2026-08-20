"""End-to-end proof that coverage is delivered through the reference surface.

§11.7 makes the CLI and the file-based context floor the reference surface, so
this drives the whole thing through ``cruxible ...`` argv against a served
instance: govern some exact bytes, export the floor with its coverage boundary,
put those bytes in a working file, resolve coverage over it, then edit the file
and resolve again.

What the reachable accepted state can and cannot show
-----------------------------------------------------
Every Capture the served authoring surface produces today is content-addressed:
`build_direct_claim_capture` and `build_direct_claim_selection_capture` both
cite CAS, and a CAS reference deliberately names no logical source. By §11.6.1
that makes a byte match at a *working* source a labeled `content_equivalent`
candidate rather than an `exact` match, and it makes `drifted` -- which requires
the accepted and observed logical source to be the same -- unreachable from this
surface. `build_ledger_capture` and the external acquisition path produce the
logical-source-bound Captures that unlock `exact`/`drifted`, and no served
operation invokes them yet; PC-G's watcher is their first caller.

So this test proves what the surface can prove end to end -- governed bytes are
recognized and named to their accepted Claim, an edit removes that answer, and
the absence is summarized once rather than annotated per line -- and the drift
card's own rendering is pinned against real drift in
`tests/test_playbill/test_coverage_render.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.playbill.captures import DirectByteSpanSelectionV1
from cruxible_core.playbill.claim_types import claim_type_digest
from cruxible_core.playbill.claims import ClaimStatement, LiteralClaimObject
from cruxible_core.playbill.coverage.render import BATCH_SUMMARY_PREFIX
from cruxible_core.playbill.semantic import ContentSpan
from cruxible_core.service.playbill_claims import DirectClaimAuthoringV1
from tests.test_cli.test_playbill_knowledge_loop_smoke import (  # noqa: F401
    SIGNER_ID,
    _Cli,
    _proposal_id,
    _write,
    served_cli,
)
from tests.test_playbill._knowledge_loop_support import (
    subject_address,
    subject_shell,
)
from tests.test_playbill.test_claims import _claim_type

GOVERNED_BYTES = (
    b"# Migration handbook\n\nThe reviewer accepted the migration plan on the second reading.\n"
)
WORKING_PATH = "docs/handbook.md"
WORKING_SOURCE = "external:workspace.handbook"


def _bootstrap(cruxible: _Cli, tmp_path: Path) -> None:
    cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    cruxible.json(
        "playbill",
        "init",
        "--key-dir",
        str(tmp_path / "custody"),
        "--principal-id",
        SIGNER_ID,
    )


def _govern_the_bytes(cruxible: _Cli, tmp_path: Path) -> str:
    """Store exact bytes and accept one Claim whose Capture cites exactly them."""

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

    body_path = tmp_path / "governed.md"
    body_path.write_bytes(GOVERNED_BYTES)
    stored = cruxible.json("playbill", "body", "store", str(body_path))
    assert stored["byte_length"] == len(GOVERNED_BYTES)

    authoring = DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=subject_address("wi-42"),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        rationale="The handbook records the reviewer's acceptance.",
        subject_shell=subject_shell("wi-42"),
        source_selection=DirectByteSpanSelectionV1(
            span=ContentSpan(
                content_digest=stored["digest"],
                start_byte=0,
                end_byte=len(GOVERNED_BYTES),
            ),
            media_type="text/markdown",
        ),
    )
    proposal = cruxible.json(
        "playbill",
        "claim",
        "propose",
        "--authoring",
        _write(tmp_path / "claim.json", authoring.model_dump(mode="json")),
        "--name",
        "seed-claim",
    )
    cruxible.accept(_proposal_id(proposal["proposal"]))
    return str(proposal["claim_identity"])


def _resolve(cruxible: _Cli, workspace: Path, *extra: str) -> tuple[str, dict[str, Any]]:
    """Run one `coverage resolve` in both output modes over the same working set."""

    argv = (
        "playbill",
        "coverage",
        "resolve",
        "--root",
        str(workspace),
        "--bind",
        f"{WORKING_PATH}={WORKING_SOURCE}",
        "--file",
        WORKING_PATH,
        *extra,
    )
    return cruxible.run(*argv).stdout, cruxible.json(*argv)


def test_cli_delivers_coverage_for_a_governed_working_file_and_drops_it_on_edit(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    claim_identity = _govern_the_bytes(cruxible, tmp_path)

    # 1. The floor exports its own coverage boundary beside the render manifest,
    #    enumerated like every other floor file.
    floor = tmp_path / "floor"
    exported = cruxible.json("playbill", "floor", "export", "--output", str(floor))
    boundary = json.loads((floor / "coverage-manifest.json").read_text(encoding="utf-8"))
    assert "coverage-manifest.json" in {item["path"] for item in exported["files"]}
    assert boundary["format"] == "playbill-coverage-manifest-v1"
    assert boundary["coordinate"] == exported["coordinate"]
    assert boundary["completeness"] == "complete"
    assert boundary["epoch"] is None
    assert boundary["exact_bytes_commitment_count"] > 0

    # 2. The governed bytes, sitting in a working file the agent is looking at.
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    working = workspace / WORKING_PATH
    working.write_bytes(GOVERNED_BYTES)

    human, payload = _resolve(cruxible, workspace)

    assert payload["tag"] == "playbill-coverage-result-v1"
    assert payload["summary"] == {
        "tag": "playbill-coverage-batch-summary-v1",
        "exact": 0,
        "drifted": 0,
        "candidate": 1,
        "none": 0,
        "returned_spans": 1,
        "omitted_card_count": 0,
    }
    card = payload["spans"][0]["cards"][0]
    # The card names the accepted Claim and the accepted coordinate it points
    # at, and it grants that working file nothing.
    assert claim_identity.endswith(Path(card["claim_addresses"][0]["artifact_path"]).stem)
    assert card["at"] == payload["at"]
    assert card["grants_mutation_authority"] is False
    assert card["resolves_equivalence"] is False
    # Content-addressed accepted evidence names no logical source, so identical
    # bytes at a working occurrence are labeled, never inherited.
    assert card["match_basis"] == "content_equivalent"
    assert list(card["reason_codes"]) == ["foreign_occurrence"]

    lines = human.strip().splitlines()
    assert lines[0].startswith("candidate  external:workspace.handbook  ")
    assert card["claim_addresses"][0]["artifact_path"] in lines[0]
    assert "basis content_equivalent" in lines[0]
    assert lines[-3] == "Playbill coverage: 0 exact, 0 drifted, 1 candidates, 0 none"
    assert lines[-2].startswith("coverage complete for 1 returned spans at generation ")
    assert lines[-1] == "omitted cards: 0, truncated spans: 0"

    # 3. Edit the governed span in the working copy. The answer goes away in the
    #    same turn, without compiling, proposing, or accepting anything.
    working.write_bytes(GOVERNED_BYTES.replace(b"accepted", b"rejected"))

    edited_human, edited = _resolve(cruxible, workspace)

    assert edited["summary"]["candidate"] == 0
    assert edited["summary"]["none"] == 1
    assert edited["spans"][0]["cards"] == []
    assert edited["spans"][0]["absence_is_factual"] is True
    assert edited["health"] == "complete"
    # The manifest epoch advanced because the observation moved, not because a
    # second call was made.
    assert edited["epoch"] == payload["epoch"] + 1

    # An ungoverned span is summarized once and never annotated per line.
    edited_lines = edited_human.strip().splitlines()
    assert len(edited_lines) == 3
    assert edited_lines[0] == "Playbill coverage: 0 exact, 0 drifted, 0 candidates, 1 none"
    assert not any("none" in line for line in edited_lines[1:])
    assert WORKING_SOURCE not in edited_human

    # 4. Nothing about accepted state moved: the coordinate is the one the
    #    Claim was accepted at, and coverage appended no receipt to reach it.
    assert edited["at"] == exported["coordinate"]


def test_cli_coverage_status_renders_the_manifest_over_the_declared_scope(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_the_bytes(cruxible, tmp_path)

    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / WORKING_PATH).write_bytes(GOVERNED_BYTES)
    (workspace / "notes.txt").write_bytes(b"ordinary working notes\n")

    status = cruxible.run(
        "playbill",
        "coverage",
        "status",
        "--root",
        str(workspace),
        "--bind",
        f"{WORKING_PATH}={WORKING_SOURCE}",
        "--bind",
        "notes.txt=external:workspace.notes",
    ).stdout

    lines = status.strip().splitlines()
    assert lines[0] == "Playbill coverage manifest: epoch 0, health complete, boundary complete"
    assert lines[1].startswith("instance ")
    assert "watcher absent, access profile playbill.coverage.read" in lines
    assert "scope 2 source(s):" in lines
    assert "  external:workspace.handbook" in lines
    assert "  external:workspace.notes" in lines
    # Status renders the boundary, never the cards.
    assert not any(line.startswith(BATCH_SUMMARY_PREFIX) for line in lines)


def test_cli_coverage_refuses_a_working_path_it_was_never_given_a_binding_for(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)

    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / WORKING_PATH).write_bytes(GOVERNED_BYTES)
    (workspace / "copy.md").write_bytes(GOVERNED_BYTES)

    refused = CliRunner().invoke(
        cli,
        [
            "playbill",
            "coverage",
            "resolve",
            "--root",
            str(workspace),
            "--bind",
            f"{WORKING_PATH}={WORKING_SOURCE}",
            "--file",
            "copy.md",
        ],
    )

    assert refused.exit_code != 0
    assert "no declared logical source binding" in refused.output
