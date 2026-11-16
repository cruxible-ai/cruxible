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
    # With no Claim known, a value cut only for width is whole in --json, which
    # every read takes; a preview with no Claim names no read: the bare flag is
    # refused on a Subject's or a file's card (gapb-11).
    assert get_value_display("x" * 200).text.endswith("(200 chars; --json for all)")
    assert get_value_display(
        TruncatedText(truncated=True, preview="n" * 500, length=900)
    ).text.endswith("(900 chars, cut)")
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


def test_history_names_who_approved_beside_who_accepted() -> None:
    """Ruling 11: a revision and a field event print `by owner, approved by alice`."""

    from pydantic import TypeAdapter

    from cruxible_client.contracts.get_reads import GetFieldStated, GetHistory, GetRevision
    from cruxible_client.contracts.get_text import history_lines

    common = {"sequence": 4, "git_oid": "4" * 12, "accepted": "2026-10-21T00:00:00Z"}
    revisions = GetHistory(
        revisions=(
            GetRevision(
                revision=2, digest="d" * 12, actor="owner", approved_by=("alice", "bob"), **common
            ),
            GetRevision(revision=1, digest="e" * 12, actor="owner", **common),
        )
    )
    lines = history_lines(revisions)
    assert "  by owner, approved by alice, bob" in lines[0]
    assert "by owner" in lines[1] and "approved by" not in lines[1]

    event = GetFieldStated(
        field="price",
        predicate="acme.price",
        claim=CLAIM,
        actor="owner",
        approved_by=("alice",),
        value=42,
        **common,
    )
    history = TypeAdapter(GetHistoryAny).validate_python(
        {"kind": "subject", "revisions": [], "events": [event.model_dump(mode="json")]}
    )
    (field_line,) = [line for line in history_lines(history) if line.startswith("  seq")]
    assert "by owner, approved by alice  stated  42" in field_line
