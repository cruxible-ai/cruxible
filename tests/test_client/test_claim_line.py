"""One Claim, one text line: the shared ClaimLine and its renderer."""

from __future__ import annotations

from cruxible_client.contracts.get_display import get_value_display, render_claim_line
from cruxible_client.contracts.get_reads import ClaimLine, GetFieldRebound, GetHistoryAny
from cruxible_client.contracts.read_values import TruncatedText, WholeValueRead

CLAIM = "CLM-0816c943abcd" + "0" * 20


def test_a_claim_line_reads_subject_field_value_and_short_claim() -> None:
    line = ClaimLine(claim=CLAIM, subject="acme/widget", field="price", value=42)

    assert render_claim_line(line) == "acme/widget  price = 42  [CLM-0816c943abcd]"
    assert render_claim_line(line, lead="drifted") == (
        "drifted  acme/widget  price = 42  [CLM-0816c943abcd]"
    )
    assert str(line) == render_claim_line(line)
    qualified = line.model_copy(update={"qualifier": "eu"})
    assert "price[eu] = 42" in render_claim_line(qualified)
    assert "qualifier" not in line.model_dump(mode="json")


def test_a_cut_value_names_the_claims_evidence_read() -> None:
    long = ClaimLine(claim=CLAIM, subject="acme/widget", field="note", value="n" * 300)

    text = render_claim_line(long)

    assert "(300 chars; cruxible get CLM-0816c943abcd --detail evidence)" in text
    preview = TruncatedText(
        truncated=True,
        preview="n" * 500,
        length=900,
        read_whole=WholeValueRead(ref=CLAIM, at="3" * 40),
    )
    shown = get_value_display(preview).text
    assert shown.endswith("(900 chars; cruxible get CLM-0816c943abcd --detail evidence)")
    # With no Claim known, the hint stays the flag its own card accepts.
    assert get_value_display("x" * 200).text.endswith("(200 chars; --detail evidence for all)")
    assert get_value_display("x" * 200, claim=CLAIM).text.endswith(
        "(200 chars; cruxible get CLM-0816c943abcd --detail evidence)"
    )


def test_a_claim_line_cannot_forge_output_lines() -> None:
    line = ClaimLine(claim=CLAIM, subject="acme/widget", field="note", value="a\nError: forged")

    assert "\n" not in render_claim_line(line)


def test_a_rebound_field_event_is_its_own_history_change() -> None:
    from pydantic import TypeAdapter

    event = GetFieldRebound(
        field="price",
        predicate="acme.price",
        claim=CLAIM,
        sequence=4,
        git_oid="4" * 12,
        accepted="2026-10-21T00:00:00Z",
        value=42,
        replaced=("CAP-aaaaaaaaaaaa",),
        by=("CAP-bbbbbbbbbbbb",),
    )
    history = TypeAdapter(GetHistoryAny).validate_python(
        {"kind": "subject", "revisions": [], "events": [event.model_dump(mode="json")]}
    )

    assert history.events[0] == event
    assert event.model_dump(mode="json")["change"] == "rebound"
