"""End-to-end proof that coverage is delivered through the reference surface.

§11.7 makes the CLI and the file-based context floor the reference surface, so
this drives the whole thing through ``cruxible ...`` argv against a served
instance: govern some exact bytes, export the floor with its coverage boundary,
put those bytes in a working file, resolve coverage over it, then edit the file
and resolve again.

Two authoring inputs, two reachable answers
-------------------------------------------
`DirectByteSpanSelectionV1` cites CAS, and a CAS reference deliberately names no
logical source, so by §11.6.1 a byte match at a *working* source is a labeled
`content_equivalent` candidate and nothing stronger. That is the first test
below, and it is not a gap: content-addressed evidence really does name no place
an edit could move content within.

`DirectForeignSourceSelectionV1` (PC-G-H1) cites one. It commits to exactly the
bytes a proposer presented and binds them to a declared *logical* external
source under a per-source self-asserted CaptureContract, which is what makes
`exact` and `drifted` reachable end to end from the served surface: the same
selection stays `exact` after it moves inside its source, becomes `drifted` when
its bytes change, and stays a labeled candidate when identical bytes appear
under a different logical source. Before that slice both states were
structurally unreachable from any served operation -- `build_ledger_capture` and
the external acquisition path produced logical-source-bound Captures and nothing
served called them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cruxible_client.contracts.authoring.inputs import (
    ClaimInput,
    LiteralObjectInput,
    SelfSourceInput,
    WorkingSelectionInput,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_core.cli.main import cli
from cruxible_core.playbill.coverage.render import BATCH_SUMMARY_PREFIX
from tests.test_cli.test_playbill_knowledge_loop_smoke import (  # noqa: F401
    _Cli,
    _proposal_id,
    _write,
    served_cli,
)
from tests.test_playbill._knowledge_loop_support import (
    subject_shell,
)
from tests.test_playbill.test_claims import _claim_type

GOVERNED_BYTES = (
    b"# Migration handbook\n\nThe reviewer accepted the migration plan on the second reading.\n"
)
WORKING_PATH = "docs/handbook.md"
WORKING_SOURCE = "external:workspace.handbook"

# The foreign corpus: a file this instance has never governed as a Document, the
# span inside it a Claim is about, and the logical name the harness declares it
# under on both the accepted and the working side.
GOVERNED_LINE = b"The reviewer accepted the migration plan on the second reading.\n"
FOREIGN_PREAMBLE = b"# Foreign migration handbook\n\n"
FOREIGN_TRAILER = b"\nFiled by the migration working group.\n"
FOREIGN_BYTES = FOREIGN_PREAMBLE + GOVERNED_LINE + FOREIGN_TRAILER
FOREIGN_PATH = "corpus/handbook.md"
FOREIGN_IDENTITY = "corpus.handbook"
FOREIGN_SOURCE = f"external:{FOREIGN_IDENTITY}"
COPY_PATH = "corpus/handbook-copy.md"
COPY_IDENTITY = "corpus.handbook-copy"
COPY_SOURCE = f"external:{COPY_IDENTITY}"


def _bootstrap(cruxible: _Cli, tmp_path: Path) -> None:
    cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    cruxible.bootstrap(tmp_path)


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

    proposed_subject = cruxible.json(
        "playbill",
        "subject",
        "propose",
        "--envelope",
        _write(tmp_path / "subject.json", subject_shell("wi-42").model_dump(mode="json")),
        "--name",
        "seed-subject",
    )
    cruxible.accept(_proposal_id(proposed_subject))

    authoring = ClaimInput(
        kind="claim",
        subject="project.work_item/wi-42",
        predicate=claim_type.predicate,
        object=LiteralObjectInput(kind="literal", value="ready"),
        role="observation",
        rationale="The handbook records the reviewer's acceptance.",
        source=SelfSourceInput(
            kind="self_source",
            body=GOVERNED_BYTES.decode("utf-8"),
        ),
    )
    created = cruxible.json(
        "playbill",
        "authoring",
        "create",
        _write(tmp_path / "claim.json", authoring.model_dump(mode="json")),
    )
    submitted = cruxible.json(
        "playbill",
        "authoring",
        "submit",
        str(created["intent"]["intent_id"]),
    )
    cruxible.accept(str(submitted["status"]["proposal_id"]))
    return f"Claim:{submitted['intent']['semantic_identity']}"


def _govern_a_foreign_span(
    cruxible: _Cli,
    tmp_path: Path,
    *,
    identity: str = FOREIGN_IDENTITY,
) -> str:
    """Accept one Claim whose Capture binds a span of a foreign *logical* source.

    The proposer presents the whole foreign file's bytes through the ordinary
    body store, then points at the one line the Claim is about. The daemon
    fetched nothing, so the Capture it builds is self-asserted -- but it cites
    `external:corpus.handbook` rather than a content digest, which is what makes
    the span's later fate measurable.

    ``identity`` is the logical source the Claim is authored against, and it is
    a parameter because it is exactly the string a working binding must later
    declare: the coverage hook suite governs a span under the identity its
    configured normalizer produces, and the two sides agreeing is the whole
    coupling.
    """

    contract = foreign_source_capture_contract(identity)
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="foreign-source",
                        claim_roles=("observation",),
                        capture_contract_digests=(capture_contract_digest(contract).tagged,),
                        evidence_kinds=("self_asserted",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    proposed = cruxible.json(
        "playbill",
        "claim-type",
        "propose",
        "--envelope",
        _write(tmp_path / "foreign-claim-type.json", claim_type.model_dump(mode="json")),
        "--name",
        "seed-claim-type",
    )
    cruxible.accept(_proposal_id(proposed))

    proposed_subject = cruxible.json(
        "playbill",
        "subject",
        "propose",
        "--envelope",
        _write(tmp_path / "foreign-subject.json", subject_shell("wi-77").model_dump(mode="json")),
        "--name",
        "seed-foreign-subject",
    )
    cruxible.accept(_proposal_id(proposed_subject))

    presented = tmp_path / "presented.md"
    presented.write_bytes(FOREIGN_BYTES)
    authoring = ClaimInput(
        kind="claim",
        subject="project.work_item/wi-77",
        predicate=claim_type.predicate,
        object=LiteralObjectInput(kind="literal", value="done"),
        role="observation",
        rationale="The foreign handbook records the reviewer's acceptance.",
        source=WorkingSelectionInput(
            kind="working_selection",
            source_id=identity,
        ),
        citation_role="evidence",
    )
    stub = _write(tmp_path / "foreign-claim.json", authoring.model_dump(mode="json"))
    preflight = cruxible.json(
        "playbill",
        "authoring",
        "bind",
        "--file",
        str(presented),
        "--anchor",
        GOVERNED_LINE.decode("utf-8").strip(),
        "--payload-file",
        stub,
    )
    submitted = cruxible.json(
        "playbill",
        "authoring",
        "submit",
        str(preflight["certificate"]["intent_id"]),
    )
    cruxible.accept(str(submitted["status"]["proposal_id"]))
    return f"Claim:{submitted['intent']['semantic_identity']}"


def _resolve_foreign(
    cruxible: _Cli,
    workspace: Path,
    *binds: str,
) -> tuple[str, dict[str, Any]]:
    """Resolve the declared foreign working set in both output modes."""

    argv: tuple[str, ...] = ("playbill", "coverage", "resolve", "--root", str(workspace))
    for entry in binds or (f"{FOREIGN_PATH}={FOREIGN_SOURCE}",):
        path, _, _ = entry.partition("=")
        argv = (*argv, "--bind", entry, "--file", path)
    return cruxible.run(*argv).stdout, cruxible.json(*argv)


def _card_for(payload: dict[str, Any], source: str) -> dict[str, Any]:
    """The single card the named logical source's span resolved to."""

    span = next(
        item
        for item in payload["spans"]
        if f"{item['request']['source']['plane']}:{item['request']['source']['identity']}" == source
    )
    assert len(span["cards"]) == 1, span["cards"]
    return dict(span["cards"][0])


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
    assert boundary["format"] == "playbill-coverage-manifest-v2"
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

    assert payload["tag"] == "playbill-coverage-result-v3"
    assert payload["summary"] == {
        "tag": "playbill-coverage-batch-summary-v3",
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
    assert card["tag"] == "playbill-coverage-card-v2"
    assert len(card["citation_associations"]) == 1
    reference = card["citation_associations"][0]["reference"]
    assert "legacy_semantics" not in reference
    assert reference["origin"] == "self_source"
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


def test_cli_delivers_exact_then_relocated_exact_then_drifted_for_a_foreign_source(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The whole §11.6 transcript, end to end, through argv against a served daemon.

    Unchanged span -> `exact`. Same span moved within its file -> still `exact`,
    with the *same* occurrence identity and a moved line overlay, because line
    movement alone may never break a verified match. Span edited -> `drifted`,
    carrying the complete §11.6.2 binding rather than an `exact` with a flag.
    """

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    claim_identity = _govern_a_foreign_span(cruxible, tmp_path)

    workspace = tmp_path / "workspace"
    (workspace / "corpus").mkdir(parents=True)
    working = workspace / FOREIGN_PATH
    working.write_bytes(FOREIGN_BYTES)

    # 1. The foreign file, unchanged: the governed span is verified against the
    #    accepted Claim and its logical source, not merely recognized by bytes.
    human, payload = _resolve_foreign(cruxible, workspace)

    assert payload["summary"]["exact"] == 1
    assert payload["summary"]["drifted"] == 0
    assert payload["summary"]["candidate"] == 0
    assert payload["health"] == "complete"

    # The daemon fetched nothing, and the accepted law evidence says so: every
    # Capture behind this Claim is graded self-asserted, including the one that
    # names a logical source.
    explained = cruxible.json("playbill", "claim", "explain", claim_identity)
    assert {item["provenance_grade"] for item in explained["law_evidence"]["verdict_captures"]} == {
        "self-asserted"
    }

    exact = _card_for(payload, FOREIGN_SOURCE)
    assert exact["match_state"] == "exact"
    assert exact["match_basis"] is None
    assert exact["accepted_source"] == exact["observed_source"]
    assert exact["accepted_source"]["plane"] == "external"
    assert exact["accepted_source"]["identity"] == FOREIGN_IDENTITY
    assert exact["observed_commitment_digest"] == exact["expected_commitment_digest"]
    assert exact["grants_mutation_authority"] is False
    assert claim_identity.endswith(Path(exact["claim_addresses"][0]["artifact_path"]).stem)
    assert exact["at"] == payload["at"]
    # The overlay is where the bytes currently sit: line 3 of the original file.
    assert exact["line_overlay"]["start_line"] == 3
    assert human.strip().splitlines()[0].startswith(f"exact  {FOREIGN_SOURCE}  lines 3-3  ")

    # 2. Relocate the same bytes inside the same file. Identity is
    #    (source, observed commitment, ordinal) and none of those moved, so the
    #    match survives and only the presentation overlay changes.
    working.write_bytes(GOVERNED_LINE + FOREIGN_PREAMBLE + FOREIGN_TRAILER)

    moved_human, moved = _resolve_foreign(cruxible, workspace)
    relocated = _card_for(moved, FOREIGN_SOURCE)

    assert moved["summary"]["exact"] == 1
    assert relocated["match_state"] == "exact"
    assert relocated["occurrence_identity_digest"] == exact["occurrence_identity_digest"]
    assert relocated["expected_commitment_digest"] == exact["expected_commitment_digest"]
    assert relocated["line_overlay"]["start_line"] == 1
    assert relocated["line_overlay"] != exact["line_overlay"]
    assert moved_human.strip().splitlines()[0].startswith(f"exact  {FOREIGN_SOURCE}  lines 1-1  ")

    # 3. Edit the governed span itself. That is drift, and drift is its own
    #    state and its own card.
    edited_bytes = FOREIGN_BYTES.replace(b"accepted", b"rejected")
    working.write_bytes(edited_bytes)

    drift_human, drifted_payload = _resolve_foreign(cruxible, workspace)
    drifted = _card_for(drifted_payload, FOREIGN_SOURCE)

    assert drifted_payload["summary"] == {
        "tag": "playbill-coverage-batch-summary-v3",
        "exact": 0,
        "drifted": 1,
        "candidate": 0,
        "none": 0,
        "returned_spans": 1,
        "omitted_card_count": 0,
    }
    assert drifted["match_state"] == "drifted"
    # The full §11.6.2 tuple: accepted Claim and Capture, accepted coordinate,
    # expected commitment, newly observed commitment, the source identity on
    # both sides, and the bounded dependent count.
    assert drifted["claim_addresses"] == exact["claim_addresses"]
    assert drifted["capture_digests"] == exact["capture_digests"]
    assert drifted["at"] == drifted_payload["at"]
    assert drifted["expected_commitment_digest"] == exact["expected_commitment_digest"]
    assert drifted["observed_commitment_digest"] != drifted["expected_commitment_digest"]
    assert drifted["accepted_source"] == drifted["observed_source"] == exact["accepted_source"]
    assert drifted["dependent_claim_count"] == 1
    assert list(drifted["reason_codes"]) == ["commitment_superseded"]
    assert drifted["grants_mutation_authority"] is False

    drift_lines = drift_human.strip().splitlines()
    assert drift_lines[0].startswith(f"drifted  {FOREIGN_SOURCE}  ")
    assert f"expected {drifted['expected_commitment_digest']}" in drift_lines[0]
    assert f"observed {drifted['observed_commitment_digest']}" in drift_lines[0]
    assert "dependents 1" in drift_lines[0]
    assert drift_lines[-3] == "Playbill coverage: 0 exact, 1 drifted, 0 candidates, 0 none"

    # 4. Nothing about accepted state moved across the whole transcript.
    assert drifted_payload["at"] == payload["at"] == moved["at"]


def test_cli_coverage_never_lets_identical_bytes_in_a_foreign_source_read_as_exact(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The §11.6.1 cross-source law, now testable end to end.

    The same bytes sit in two working files. One is the logical source the
    accepted Capture cites; the other is not. Only the first is `exact`, and the
    second is a labeled `content_equivalent` candidate that inherits nothing --
    which is precisely why coverage is not keyed on `content digest -> Claims`.
    """

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path)

    workspace = tmp_path / "workspace"
    (workspace / "corpus").mkdir(parents=True)
    (workspace / FOREIGN_PATH).write_bytes(FOREIGN_BYTES)
    (workspace / COPY_PATH).write_bytes(FOREIGN_BYTES)

    _, payload = _resolve_foreign(
        cruxible,
        workspace,
        f"{FOREIGN_PATH}={FOREIGN_SOURCE}",
        f"{COPY_PATH}={COPY_SOURCE}",
    )

    assert payload["summary"]["exact"] == 1
    assert payload["summary"]["candidate"] == 1
    assert payload["summary"]["drifted"] == 0

    cited = _card_for(payload, FOREIGN_SOURCE)
    foreign = _card_for(payload, COPY_SOURCE)

    assert cited["match_state"] == "exact"
    assert foreign["match_state"] == "candidate"
    assert foreign["match_basis"] == "content_equivalent"
    assert list(foreign["reason_codes"]) == ["foreign_occurrence"]
    # Identical bytes, identical commitment -- and still a different answer,
    # because the accepted source and the observed source disagree.
    assert foreign["expected_commitment_digest"] == cited["expected_commitment_digest"]
    assert foreign["observed_source"]["identity"] == COPY_IDENTITY
    assert foreign["accepted_source"]["identity"] == FOREIGN_IDENTITY
    assert foreign["resolves_equivalence"] is False
    assert foreign["grants_mutation_authority"] is False


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
