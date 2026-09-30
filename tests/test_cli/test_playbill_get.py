"""`cruxible playbill get REF`: values per kind as text, the whole result as JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.get_reads import (
    PlaybillByteRangeV1,
    PlaybillGetBodyV1,
    PlaybillGetClaimCardV1,
    PlaybillGetCoordinateV1,
    PlaybillGetRequestV1,
    PlaybillGetResultV1,
    PlaybillGetSubjectCardV1,
    PlaybillGetSubjectClaimV1,
)
from cruxible_core.cli.main import cli

COORDINATE = PlaybillGetCoordinateV1(git_oid="1" * 12, generation=7)
PREFIX = ["--server-url", "http://server", "--instance-id", "inst_get"]
WHEN = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


class _StubClient:
    def __init__(self, result: PlaybillGetResultV1) -> None:
        self.result = result
        self.requests: list[PlaybillGetRequestV1] = []

    def playbill_get(self, instance_id: str, *, request: PlaybillGetRequestV1) -> Any:
        assert instance_id == "inst_get"
        self.requests.append(request)
        return self.result


def _stub(monkeypatch: pytest.MonkeyPatch, result: PlaybillGetResultV1) -> _StubClient:
    client = _StubClient(result)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def _result(kind: str, **fields: Any) -> PlaybillGetResultV1:
    return PlaybillGetResultV1(
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
            card=PlaybillGetSubjectCardV1(
                subject="dev.roadmap_item/x",
                kind="dev.roadmap_item",
                lifecycle="live",
                claims=(
                    PlaybillGetSubjectClaimV1(
                        predicate="adoption_state", claim="CLM-1", value="adopted"
                    ),
                    PlaybillGetSubjectClaimV1(
                        predicate="task_title", claim="CLM-2", value="Ship it", flags=("stale",)
                    ),
                ),
                incoming_count=2,
                next=("cruxible playbill get dev.roadmap_item/x --detail why",),
            ),
        ),
    )

    result = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "dev.roadmap_item/x"])

    assert result.exit_code == 0, result.output
    assert stub.requests[0].surface == "cli" and stub.requests[0].detail == "summary"
    assert "dev.roadmap_item/x  (live, 2 incoming)" in result.output
    assert "  adoption_state  adopted" in result.output
    assert "  task_title      Ship it  [stale]" in result.output
    assert "next: cruxible playbill get dev.roadmap_item/x --detail why" in result.output


def test_a_claim_prints_its_value_and_verdict_and_json_carries_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = PlaybillGetClaimCardV1(
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

    text = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "CLM-aaaaaaaa"])
    raw = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "CLM-aaaaaaaa", "--json"])

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
            body=PlaybillGetBodyV1(
                document="design",
                media_type="text/markdown",
                size=100,
                range=PlaybillByteRangeV1(start=0, end=10),
                text="# Design\n\n",
            ),
        ),
    )

    result = CliRunner().invoke(
        cli,
        [*PREFIX, "playbill", "get", "Document:design", "--detail", "body", "--range", "0:10"],
    )

    assert result.exit_code == 0, result.output
    assert stub.requests[0].range == PlaybillByteRangeV1(start=0, end=10)
    assert result.output.startswith("# Design\n\n")
    assert "next: --range 10:20" in result.output


def test_a_malformed_range_is_a_usage_error_with_an_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _result("document"))

    result = CliRunner().invoke(
        cli, [*PREFIX, "playbill", "get", "Document:design", "--detail", "body", "--range", "x"]
    )

    assert result.exit_code == 2
    assert "range must be start:end" in result.output
    assert "example: cruxible playbill get" in result.output


def test_other_cards_print_names_whole_and_nested_rows_readably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import (
        PlaybillGetProposalCardV1,
        PlaybillGetProposalChangeV1,
    )

    _stub(
        monkeypatch,
        _result(
            "proposal",
            card=PlaybillGetProposalCardV1(
                proposal="sha256:" + "1" * 64,
                status="open",
                verdict="candidate",
                changes=(
                    PlaybillGetProposalChangeV1(path="documents/design.json", change="create"),
                ),
                next=("cruxible playbill proposal review sha256:" + "1" * 64,),
            ),
        ),
    )

    result = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "sha256:11111111"])

    assert result.exit_code == 0, result.output
    assert "status: open" in result.output
    assert "changes:\n  documents/design.json  create" in result.output
    assert "next: cruxible playbill proposal review sha256:" in result.output


def test_history_pages_print_the_next_command_and_long_values_say_they_were_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import (
        PlaybillGetHistoryV1,
        PlaybillGetRevisionV1,
        PlaybillGetTruncatedTextV1,
    )

    stub = _stub(
        monkeypatch,
        _result(
            "claim",
            detail="history",
            history=PlaybillGetHistoryV1(
                revisions=(
                    PlaybillGetRevisionV1(
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
        cli, [*PREFIX, "playbill", "get", "CLM-aaaa", "--detail", "history", "--limit", "1"]
    )

    assert paged.exit_code == 0, paged.output
    assert stub.requests[0].limit == 1 and stub.requests[0].cursor is None
    assert f"rev 3  seq 9 at {'9' * 12}" in paged.output
    assert "next: cruxible playbill get CLM-aaaa --detail history --cursor CURSOR" in paged.output

    long_row = PlaybillGetSubjectClaimV1(
        predicate="note",
        claim="CLM-3",
        value=PlaybillGetTruncatedTextV1(value="n" * 500, length=900),
    )
    _stub(
        monkeypatch,
        _result(
            "subject",
            card=PlaybillGetSubjectCardV1(
                subject="dev.roadmap_item/x",
                kind="dev.roadmap_item",
                lifecycle="live",
                claims=(long_row,),
                incoming_count=0,
            ),
        ),
    )
    summary = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "dev.roadmap_item/x"])
    as_json = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "dev.roadmap_item/x", "--json"])

    assert summary.exit_code == 0, summary.output
    assert "(900 chars; --detail evidence for all)" in summary.output
    payload = json.loads(as_json.output)
    assert payload["card"]["claims"][0]["value"]["truncated"] is True
    assert payload["coordinate"] == {"git_oid": "1" * 12, "generation": 7}
    assert "accepted_coordinate" not in payload


def test_exact_content_prints_as_text_and_a_marker_says_why_when_it_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.get_reads import (
        PlaybillExactContentRefV1,
        PlaybillGetEvidenceV1,
    )

    digest = "sha256:" + "ab" * 32
    card = PlaybillGetSubjectCardV1(
        subject="legal.case/c-1",
        kind="legal.case",
        lifecycle="live",
        claims=(
            PlaybillGetSubjectClaimV1(
                predicate="ruling", claim="CLM-1", value="Affirmed.", content_digest=digest
            ),
            PlaybillGetSubjectClaimV1(
                predicate="exhibit",
                claim="CLM-2",
                value=PlaybillExactContentRefV1(
                    exact_content="binary", content_digest=digest, length=6
                ),
                content_digest=digest,
            ),
        ),
        incoming_count=0,
    )
    _stub(monkeypatch, _result("subject", card=card))
    text = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "legal.case/c-1"])

    assert text.exit_code == 0, text.output
    assert "  ruling   Affirmed." in text.output
    assert "  exhibit  <binary 6 bytes sha256:abababababab>" in text.output

    evidence = PlaybillGetEvidenceV1(
        value="Affirmed, in full.", content_digest=digest, captures=(), attestations=()
    )
    _stub(monkeypatch, _result("claim", detail="evidence", evidence=evidence))
    whole = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "CLM-1", "--detail", "evidence"])

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
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.core_support._exact_content_support import seed_exact_content

    instance, claims = seed_exact_content(tmp_path, {"wi-42": value.encode()})
    claim = claims["wi-42"]

    class LocalClient:
        def playbill_get(self, _instance_id, *, request):
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
        result = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", ref, "--detail", detail])
        assert result.exit_code == 0, result.output
        assert (
            f"{printable(value)[: width - 1]}… ({len(value)} chars; --detail evidence for all)"
            in result.output
        )
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
    from cruxible_client.contracts.get_reads import PlaybillGetEvidenceV1
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
            evidence=PlaybillGetEvidenceV1(
                value=value,
                captures=(),
                attestations=(),
            ),
        ),
    )
    result = CliRunner().invoke(cli, [*PREFIX, "playbill", "get", "CLM-1", "--detail", "evidence"])
    assert result.exit_code == 0, result.output
    assert "value: " + json.dumps(value) in result.output
    assert "--detail evidence for all" not in result.output
