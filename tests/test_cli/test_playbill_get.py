"""`cruxible get REF`: values per kind as text, the whole result as JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.get_reads import (
    ByteRange,
    GetBody,
    GetClaimCard,
    GetCoordinate,
    GetRequest,
    GetResult,
    GetSubjectCard,
    GetSubjectClaim,
)
from cruxible_core.cli.main import cli

COORDINATE = GetCoordinate(git_oid="1" * 12, generation=7)
PREFIX = ["--server-url", "http://server", "--instance-id", "inst_get"]
WHEN = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


class _StubClient:
    def __init__(self, result: GetResult) -> None:
        self.result = result
        self.requests: list[GetRequest] = []

    def get(self, instance_id: str, *, request: GetRequest) -> Any:
        assert instance_id == "inst_get"
        self.requests.append(request)
        return self.result


def _stub(monkeypatch: pytest.MonkeyPatch, result: GetResult) -> _StubClient:
    client = _StubClient(result)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def _result(kind: str, **fields: Any) -> GetResult:
    return GetResult(
        ref="ref",
        kind=kind,  # type: ignore[arg-type]
        detail=fields.pop("detail", "summary"),
        coordinate=COORDINATE,
        evaluation_time=WHEN,
        **fields,
    )


def test_a_subject_prints_its_claims_as_an_aligned_value_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _stub(
        monkeypatch,
        _result(
            "subject",
            card=GetSubjectCard(
                subject="dev.roadmap_item/x",
                kind="dev.roadmap_item",
                lifecycle="live",
                claims=(
                    GetSubjectClaim(predicate="adoption_state", claim="CLM-1", value="adopted"),
                    GetSubjectClaim(
                        predicate="task_title", claim="CLM-2", value="Ship it", flags=("stale",)
                    ),
                ),
                incoming_count=2,
                next=("cruxible get dev.roadmap_item/x --detail why",),
            ),
        ),
    )

    result = CliRunner().invoke(cli, [*PREFIX, "get", "dev.roadmap_item/x"])

    assert result.exit_code == 0, result.output
    assert stub.requests[0].surface == "cli" and stub.requests[0].detail == "summary"
    assert "dev.roadmap_item/x  (live, 2 incoming)" in result.output
    assert "  adoption_state  adopted" in result.output
    assert "  task_title      Ship it  [stale]" in result.output
    assert "next: cruxible get dev.roadmap_item/x --detail why" in result.output


def test_a_claim_prints_its_value_and_verdict_and_json_carries_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = GetClaimCard(
        claim="CLM-" + "a" * 32,
        subject="dev.roadmap_item/x",
        predicate="adoption_state",
        predicate_full="dev.roadmap_item.adoption_state",
        value="adopted",
        verdict="supported",
        status="accepted",
        revision=2,
        flags=("contested",),
    )
    _stub(monkeypatch, _result("claim", card=card))

    text = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-aaaaaaaa"])
    raw = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-aaaaaaaa", "--json"])

    assert text.exit_code == 0, text.output
    assert "dev.roadmap_item/x  adoption_state = adopted" in text.output
    assert "verdict=supported status=accepted revision=2" in text.output
    assert "flags: contested" in text.output
    assert json.loads(raw.output)["card"]["predicate_full"] == "dev.roadmap_item.adoption_state"


def test_body_takes_a_byte_range_and_prints_the_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub(
        monkeypatch,
        _result(
            "document",
            detail="body",
            body=GetBody(
                document="design",
                media_type="text/markdown",
                size=100,
                body_digest="sha256:" + "a" * 64,
                range=ByteRange(start=0, end=10),
                text="# Design\n\n",
            ),
        ),
    )

    result = CliRunner().invoke(
        cli,
        [*PREFIX, "get", "Document:design", "--detail", "body", "--range", "0:10"],
    )

    assert result.exit_code == 0, result.output
    assert stub.requests[0].range == ByteRange(start=0, end=10)
    assert result.output.startswith("# Design\n\n")
    assert "next: --range 10:20" in result.output


def test_a_malformed_range_is_a_usage_error_with_an_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _result("document"))

    result = CliRunner().invoke(
        cli, [*PREFIX, "get", "Document:design", "--detail", "body", "--range", "x"]
    )

    assert result.exit_code == 2
    assert "range must be start:end" in result.output
    assert "example: cruxible get" in result.output


def test_other_cards_print_names_whole_and_nested_rows_readably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import (
        GetProposalCard,
        GetProposalChange,
    )

    _stub(
        monkeypatch,
        _result(
            "proposal",
            card=GetProposalCard(
                proposal="sha256:" + "1" * 64,
                status="open",
                verdict="candidate",
                changes=(GetProposalChange(path="documents/design.json", change="create"),),
                next=("cruxible proposal review sha256:" + "1" * 64,),
            ),
        ),
    )

    result = CliRunner().invoke(cli, [*PREFIX, "get", "sha256:11111111"])

    assert result.exit_code == 0, result.output
    assert "status: open" in result.output
    assert "changes:\n  documents/design.json  create" in result.output
    assert "next: cruxible proposal review sha256:" in result.output


def test_a_claim_type_card_maps_stored_playbill_contract_names_for_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored evidence contract names keep their playbill spelling; get's text does not."""

    from cruxible_client.contracts.claim_types import ClaimTypeContention
    from cruxible_client.contracts.get_reads import GetClaimTypeCard, GetEvidenceRule

    _stub(
        monkeypatch,
        _result(
            "claim_type",
            card=GetClaimTypeCard(
                predicate="status",
                subject_kinds=("work_item",),
                object="string",
                cardinality="one",
                roles=("normative",),
                evidence_requirement="self",
                revision_evidence="replace",
                evidence=("playbill.coordinator-self-source-v1",),
                evidence_rules=(
                    GetEvidenceRule(
                        rule_id="self",
                        roles=("normative",),
                        contracts=("CaptureContract:playbill.coordinator-self-source-v1",),
                        admission="direct",
                    ),
                ),
                contention=ClaimTypeContention(selector="only_contender", on_conflict="unresolved"),
                format=7,
                revision=1,
                path="claim-types/work_item/status.json",
                live_claims=0,
                subjects_with_value=0,
            ),
        ),
    )

    result = CliRunner().invoke(cli, [*PREFIX, "get", "ClaimType:status"])

    assert result.exit_code == 0, result.output
    assert "playbill." not in result.output
    assert "evidence: cruxible.coordinator-self-source-v1" in result.output
    assert "CaptureContract:cruxible.coordinator-self-source-v1" in result.output


def test_detail_why_prints_a_readable_summary_and_json_keeps_the_whole_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--detail why`` is a short readable summary; ``--json`` keeps the full envelope."""

    why: dict[str, Any] = {
        "tag": "playbill-claim-explanation-v2",
        "coordinate": {"git_oid": "1" * 64},
        "law_evidence": {
            "rule_id": "self",
            "admission": "direct",
            "capture_contracts": ["playbill.coordinator-self-source-v1"],
        },
        "verdict": {"state": "supported"},
        "source_handles": [{"kind": "self_asserted", "ref": "x"}],
    }
    client = _stub(monkeypatch, _result("claim", detail="why", why=dict(why)))

    result = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-1", "--detail", "why"])

    assert result.exit_code == 0, result.output
    # Readable summary, not a raw JSON dump: no braces, and the stored
    # playbill name maps to its Cruxible display name.
    assert "{" not in result.output
    assert "playbill." not in result.output
    assert "cruxible.coordinator-self-source-v1" in result.output
    assert "state: supported" in result.output

    client.result = _result("claim", detail="why", why=dict(why))
    json_result = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-1", "--detail", "why", "--json"])
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["why"]["law_evidence"]["capture_contracts"] == [
        "playbill.coordinator-self-source-v1"
    ]


def test_a_subject_history_prints_each_field_s_timeline_with_its_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import (
        GetChangeSetReason,
        GetFieldChanged,
        GetFieldRetired,
        GetFieldStated,
        GetRevision,
        GetSubjectHistory,
    )

    def common(sequence: int, claim: str, reason: str) -> dict[str, Any]:
        return {
            "claim": claim,
            "sequence": sequence,
            "git_oid": str(sequence) * 12,
            "accepted": f"2026-10-1{sequence}T09:12:00Z",
            "actor": "ada",
            "reason": GetChangeSetReason(text=reason, proposal="sha256:" + "p" * 64),
        }

    stub = _stub(
        monkeypatch,
        _result(
            "subject",
            detail="history",
            history=GetSubjectHistory(
                revisions=(
                    GetRevision(
                        revision=1,
                        sequence=1,
                        git_oid="1" * 12,
                        accepted="2026-10-11T00:00:00Z",
                        actor="ada",
                        lifecycle="live",
                        digest="sha256:abc",
                    ),
                ),
                events=(
                    GetFieldRetired(
                        field="status",
                        predicate="app.service.status",
                        value="done",
                        retirement="was-wrong",
                        **common(4, "CLM-2", "It never shipped.\nError: forged"),
                    ),
                    GetFieldChanged(
                        field="owner",
                        predicate="app.service.owner",
                        value="linus",
                        before="grace",
                        **common(3, "CLM-1", "Linus took over search."),
                    ),
                    GetFieldStated(
                        field="owner",
                        predicate="app.service.owner",
                        value="grace",
                        **common(2, "CLM-1", "Pins from each service."),
                    ),
                ),
            ),
            truncated=True,
            next_cursor="CURSOR",
        ),
    )

    printed = CliRunner().invoke(
        cli, [*PREFIX, "get", "app.service/search", "--detail", "history", "--field", "owner"]
    )

    assert printed.exit_code == 0, printed.output
    assert stub.requests[0].field == "owner"
    lines = printed.output.splitlines()
    owner = lines.index("owner")
    assert lines[owner + 1].startswith(f"  seq 3  {'3' * 12}  2026-10-13T09:12:00Z  by ada  ")
    assert "changed  grace -> linus" in lines[owner + 1]
    assert lines[owner + 2] == "      reason: Linus took over search."
    assert "stated  grace" in lines[owner + 3]
    status = lines.index("status")
    assert "retired (was-wrong)  done" in lines[status + 1]
    # Authored text cannot forge a line of output.
    assert lines[status + 2] == "      reason: It never shipped.\\nError: forged"
    assert (
        "next: cruxible get app.service/search --detail history --field owner --cursor CURSOR"
        in printed.output
    )


def test_history_pages_print_the_next_command_and_long_values_say_they_were_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import GetHistory, GetRevision
    from cruxible_client.contracts.read_values import TruncatedText, WholeValueRead

    stub = _stub(
        monkeypatch,
        _result(
            "claim",
            detail="history",
            history=GetHistory(
                revisions=(
                    GetRevision(
                        revision=3,
                        sequence=9,
                        git_oid="9" * 12,
                        accepted="2026-09-01T00:00:00Z",
                        actor="owner",
                        value="done",
                        digest="sha256:abc",
                    ),
                )
            ),
            truncated=True,
            next_cursor="CURSOR",
        ),
    )

    paged = CliRunner().invoke(
        cli, [*PREFIX, "get", "CLM-aaaa", "--detail", "history", "--limit", "1"]
    )

    assert paged.exit_code == 0, paged.output
    assert stub.requests[0].limit == 1 and stub.requests[0].cursor is None
    assert f"rev 3  seq 9 at {'9' * 12}" in paged.output
    assert "next: cruxible get CLM-aaaa --detail history --cursor CURSOR" in paged.output

    long_row = GetSubjectClaim(
        predicate="note",
        claim="CLM-3",
        value=TruncatedText(
            truncated=True,
            preview="n" * 500,
            length=900,
            read_whole=WholeValueRead(ref="CLM-3", at="3" * 64),
        ),
    )
    _stub(
        monkeypatch,
        _result(
            "subject",
            card=GetSubjectCard(
                subject="dev.roadmap_item/x",
                kind="dev.roadmap_item",
                lifecycle="live",
                claims=(long_row,),
                incoming_count=0,
            ),
        ),
    )
    summary = CliRunner().invoke(cli, [*PREFIX, "get", "dev.roadmap_item/x"])
    as_json = CliRunner().invoke(cli, [*PREFIX, "get", "dev.roadmap_item/x", "--json"])

    assert summary.exit_code == 0, summary.output
    # A Subject card takes no --detail evidence: the cut names its Claim's read.
    assert "(900 chars; cruxible get CLM-3 --detail evidence)" in summary.output
    assert "--detail evidence for all" not in summary.output
    payload = json.loads(as_json.output)
    assert payload["card"]["claims"][0]["value"] == {
        "truncated": True,
        "preview": "n" * 500,
        "length": 900,
        "read_whole": {"ref": "CLM-3", "detail": "evidence", "at": "3" * 64},
    }
    assert payload["coordinate"] == {"git_oid": "1" * 12, "generation": 7}
    assert "accepted_coordinate" not in payload


def test_exact_content_prints_as_text_and_a_marker_says_why_when_it_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import GetEvidence
    from cruxible_client.contracts.read_values import ExactContentRef

    digest = "sha256:" + "ab" * 32
    card = GetSubjectCard(
        subject="legal.case/c-1",
        kind="legal.case",
        lifecycle="live",
        claims=(
            GetSubjectClaim(
                predicate="ruling", claim="CLM-1", value="Affirmed.", content_digest=digest
            ),
            GetSubjectClaim(
                predicate="exhibit",
                claim="CLM-2",
                value=ExactContentRef(exact_content="binary", content_digest=digest, length=6),
                content_digest=digest,
            ),
        ),
        incoming_count=0,
    )
    _stub(monkeypatch, _result("subject", card=card))
    text = CliRunner().invoke(cli, [*PREFIX, "get", "legal.case/c-1"])

    assert text.exit_code == 0, text.output
    assert "  ruling   Affirmed." in text.output
    assert "  exhibit  <binary 6 bytes sha256:abababababab>" in text.output

    evidence = GetEvidence(
        value="Affirmed, in full.", content_digest=digest, captures=(), attestations=()
    )
    _stub(monkeypatch, _result("claim", detail="evidence", evidence=evidence))
    whole = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-1", "--detail", "evidence"])

    assert whole.exit_code == 0, whole.output
    assert "value: Affirmed, in full." in whole.output
    assert f"content_digest: {digest}" in whole.output


@pytest.mark.parametrize("value", ["x" * 300, "line\n" * 30])
def test_cli_display_cuts_offer_runnable_evidence_even_below_the_summary_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    import shlex

    from cruxible_client._error_base import printable
    from cruxible_client.contracts.read_values import CLAIM_LINE_ID_CHARS
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.core_support._exact_content_support import seed_exact_content

    instance, claims = seed_exact_content(tmp_path, {"wi-42": value.encode()})
    claim = claims["wi-42"]

    class LocalClient:
        def get(self, _instance_id, *, request):
            return service_playbill_get(
                instance,
                request=request,
                access=BodyAccessContext(principal_id="reader", can_read_body=True),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", LocalClient)
    for ref, detail, width in (
        (claim.subject, "summary", 120),
        (claim.claim_id, "summary", 120),
        (claim.claim_id, "history", 80),
    ):
        result = CliRunner().invoke(cli, [*PREFIX, "get", ref, "--detail", detail])
        assert result.exit_code == 0, result.output
        # A summary card's cut names its Claim's read (a Subject card takes no
        # --detail evidence); a history row is on the Claim's own card.
        hint = (
            f"cruxible get {claim.claim_id[:CLAIM_LINE_ID_CHARS]} --detail evidence"
            if detail == "summary"
            else "--detail evidence for all"
        )
        assert f"{printable(value)[: width - 1]}… ({len(value)} chars; {hint})" in result.output
        step = next(
            line.removeprefix("next: ")
            for line in result.output.splitlines()
            if line.startswith("next: ")
        )
        assert claim.claim_id in step and "--detail evidence" in step
        assert ("--at " in step) == (detail == "history")
        read = CliRunner().invoke(cli, [*PREFIX, *shlex.split(step)[1:]])
        assert read.exit_code == 0, read.output
        assert f"value: {printable(value)}" in read.output
        assert "--detail evidence for all" not in read.output


@pytest.mark.parametrize("width", [80, 120])
def test_cli_value_width_boundary_and_evidence_objects_are_not_silently_cut(
    monkeypatch: pytest.MonkeyPatch,
    width: int,
) -> None:
    from cruxible_client.contracts.get_reads import GetEvidence
    from cruxible_core.cli.commands.playbill import _get_value_text

    assert _get_value_text("x" * width, width=width) == "x" * width
    # Metadata without evidence detail keeps the same width without an invalid hint.
    assert (
        _get_value_text("x" * (width + 1), width=width, evidence_hint=False)
        == "x" * (width - 1) + "…"
    )
    cut = _get_value_text("x" * (width + 1), width=width)
    assert cut == "x" * (width - 1) + f"… ({width + 1} chars; --detail evidence for all)"
    value = {"note": "x" * 300}
    _stub(
        monkeypatch,
        _result(
            "claim",
            detail="evidence",
            evidence=GetEvidence(
                value=value,
                captures=(),
                attestations=(),
            ),
        ),
    )
    result = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-1", "--detail", "evidence"])
    assert result.exit_code == 0, result.output
    assert "value: " + json.dumps(value) in result.output
    assert "--detail evidence for all" not in result.output


class _RangedBodyClient:
    """Answers each body range from one binary body, as the daemon would."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.requests: list[GetRequest] = []

    def get(self, instance_id: str, *, request: GetRequest) -> Any:
        import base64

        self.requests.append(request)
        assert request.range is not None
        end = min(request.range.end, len(self.content))
        chunk = self.content[request.range.start : end]
        return _result(
            "document",
            detail="body",
            body=GetBody(
                document="blob",
                media_type="application/octet-stream",
                size=len(self.content),
                body_digest="sha256:" + "b" * 64,
                range=ByteRange(start=request.range.start, end=end),
                content_base64=base64.b64encode(chunk).decode("ascii"),
            ),
        )


def test_output_writes_a_whole_binary_body_across_ranges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.contracts.get_reads import GET_BODY_RANGE_MAX_BYTES

    content = bytes(range(256)) * ((GET_BODY_RANGE_MAX_BYTES // 256) + 3)
    client = _RangedBodyClient(content)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    target = tmp_path / "blob.bin"

    result = CliRunner().invoke(
        cli,
        [*PREFIX, "get", "Document:blob", "--detail", "body", "--output", str(target)],
    )

    assert result.exit_code == 0, result.output
    assert target.read_bytes() == content
    assert len(client.requests) == 2
    # Every range after the first is read at the first read's coordinate.
    assert client.requests[1].at == COORDINATE.git_oid
    refused = CliRunner().invoke(
        cli, [*PREFIX, "get", "Document:blob", "--output", str(tmp_path / "x")]
    )
    assert refused.exit_code != 0 and "--detail body" in refused.output


def test_incoming_prints_each_pointing_edge_and_the_next_page_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import GetIncoming, GetIncomingClaim

    stub = _stub(
        monkeypatch,
        _result(
            "subject",
            detail="incoming",
            incoming=GetIncoming(
                claims=(
                    GetIncomingClaim(
                        claim="CLM-0001",
                        subject="project.work_item/wi-2",
                        predicate="project.work_item.governs",
                    ),
                )
            ),
            truncated=True,
            next_cursor="CURSOR",
        ),
    )

    paged = CliRunner().invoke(
        cli,
        [*PREFIX, "get", "project.work_item/wi-1", "--detail", "incoming", "--limit", "1"],
    )

    assert paged.exit_code == 0, paged.output
    assert stub.requests[0].detail == "incoming" and stub.requests[0].limit == 1
    assert "project.work_item/wi-2  project.work_item.governs  CLM-0001" in paged.output
    assert (
        "next: cruxible get project.work_item/wi-1 --detail incoming --cursor CURSOR"
        in paged.output
    )


def test_evidence_capture_source_renders_its_public_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import GetCaptureEvidence, GetEvidence

    capture = GetCaptureEvidence(
        capture="CAP-0123456789ab",
        contract="CaptureContract:playbill.coordinator-self-source-v1",
        version=1,
        source="playbill.coordinator-authoring",
        observed_at=WHEN,
        role="copy",
        origin="self_source",
        status="admitted",
    )
    _stub(
        monkeypatch,
        _result(
            "claim",
            detail="evidence",
            evidence=GetEvidence(value="Affirmed.", captures=(capture,), attestations=()),
        ),
    )
    result = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-1", "--detail", "evidence"])

    assert result.exit_code == 0, result.output
    assert "source=cruxible.coordinator-authoring" in result.output
    assert "playbill." not in result.output


@pytest.mark.parametrize(
    ("role", "origin", "status", "shown"),
    [
        ("copy", "self_source", "admitted", "own words: admitted"),
        ("copy", "independent", "not_evidence", "passage of docs.page: not evidence"),
        ("evidence", "independent", "not_admitted", "evidence: not admitted"),
    ],
)
def test_evidence_capture_lines_show_three_states_and_label_copies_by_origin(
    monkeypatch: pytest.MonkeyPatch, role: str, origin: str, status: str, shown: str
) -> None:
    from cruxible_client.contracts.get_reads import GetCaptureEvidence, GetEvidence

    capture = GetCaptureEvidence(
        capture="CAP-0123456789ab",
        contract="CaptureContract:docs.page-v1",
        version=1,
        source="docs.page",
        observed_at=WHEN,
        role=role,
        origin=origin,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
    )
    _stub(
        monkeypatch,
        _result(
            "claim",
            detail="evidence",
            evidence=GetEvidence(value="Affirmed.", captures=(capture,), attestations=()),
        ),
    )
    result = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-1", "--detail", "evidence"])

    assert result.exit_code == 0, result.output
    (line,) = [item for item in result.output.splitlines() if item.startswith("capture ")]
    assert line.endswith(f"  {shown}")


def test_evidence_attestation_lines_say_where_each_lives_and_whether_it_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import GetAttestationEvidence, GetEvidence

    attestations = (
        GetAttestationEvidence(
            stance="contradict",
            principal="owner",
            at=WHEN,
            current=True,
            record="event",
            basis="examined_existing",
            counts=False,
            counts_reason="cites_no_capture",
            event="sha256:" + "e" * 64,
        ),
        GetAttestationEvidence(
            stance="unsure",
            principal="reviewer",
            at=WHEN,
            current=True,
            record="event",
            basis="examined_existing",
            counts=True,
            counts_reason="consequence_policy",
            rule="two-unsure",
        ),
    )
    _stub(
        monkeypatch,
        _result(
            "claim",
            detail="evidence",
            evidence=GetEvidence(value="Affirmed.", captures=(), attestations=attestations),
        ),
    )
    result = CliRunner().invoke(cli, [*PREFIX, "get", "CLM-1", "--detail", "evidence"])

    assert result.exit_code == 0, result.output
    lines = [item for item in result.output.splitlines() if item.startswith("attestation ")]
    assert lines == [
        f"attestation contradict by owner at {WHEN.isoformat()}  recorded  "
        "counts toward verdict: no (cites no Capture)",
        f"attestation unsure by reviewer at {WHEN.isoformat()}  recorded  "
        "counts toward verdict: yes (consequence policy rule two-unsure)",
    ]
