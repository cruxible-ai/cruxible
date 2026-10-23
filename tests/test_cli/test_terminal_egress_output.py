"""The CLI names a settle terminal's outcome and a capped terminal's authority."""

from types import SimpleNamespace

from cruxible_client.contracts.procedures.results import ProcedureTerminalEgress
from cruxible_core.cli.commands.playbill import _echo_terminal_egress

PROPOSAL = "sha256:" + "a" * 64
CANDIDATE = "sha256:" + "b" * 64


def _settle(**update):  # type: ignore[no-untyped-def]
    return ProcedureTerminalEgress(
        node_id="settle",
        kind="settle_change_set",
        verdict="delivered",
        required_authority="settle",
        effective_authority="settle",
        limiting_term="procedure_terminal_capability",
        proposal_id=PROPOSAL,
        candidate_digest=CANDIDATE,
        **update,
    )


def _echo(capsys, *egress) -> str:  # type: ignore[no-untyped-def]
    _echo_terminal_egress(SimpleNamespace(terminal_egress=egress))  # type: ignore[arg-type]
    return capsys.readouterr().out


def test_a_settled_terminal_prints_the_generation_it_accepted(capsys) -> None:
    out = _echo(capsys, _settle(settle_outcome="settled", accepted_git_oid="c" * 40))
    assert out == f"Settled settle: accepted {'c' * 40} (proposal {PROPOSAL})\n"


def test_a_fallen_back_settle_prints_its_proposal_and_why(capsys) -> None:
    out = _echo(
        capsys,
        _settle(settle_outcome="proposed", fallback_reason="cruxible.settle.condition_false"),
    )
    assert out == (
        f"Proposal settle: {PROPOSAL} candidate {CANDIDATE} "
        "(settle fell back: cruxible.settle.condition_false)\n"
    )


def test_an_outside_scope_settle_names_the_claim_types_it_left_out(capsys) -> None:
    out = _echo(
        capsys,
        _settle(
            settle_outcome="proposed",
            fallback_reason="cruxible.settle.outside_mandate_scope",
            outside_scope_claim_types=("ClaimType:security.advisory.summary",),
        ),
    )
    assert out == (
        f"Proposal settle: {PROPOSAL} candidate {CANDIDATE} "
        "(settle fell back: cruxible.settle.outside_mandate_scope; outside the mandate's "
        "scope: ClaimType:security.advisory.summary)\n"
    )


def test_a_capped_terminal_names_the_authority_it_needed_and_had(capsys) -> None:
    capped = ProcedureTerminalEgress(
        node_id="settle",
        kind="settle_change_set",
        verdict="refused_effective_authority",
        required_authority="settle",
        effective_authority="propose",
        limiting_term="line_max_authority",
    )
    assert _echo(capsys, capped) == (
        "Terminal settle: settle_change_set needs settle; "
        "the line_max_authority term allowed propose\n"
    )


def test_a_proposed_item_names_how_many_earlier_refusals_it_matches(capsys) -> None:
    from cruxible_client.contracts.procedures.results import ProcedureTerminalEgressChild
    from cruxible_client.contracts.refusal_receipts import PriorRefusalReceipt

    receipt = PriorRefusalReceipt(
        kind="claim_retired",
        claim_id="CLM-" + "1" * 32,
        decided_by="owner",
        decided_at="2026-09-12T11:00:00.000000Z",
        reason_code="was-wrong",
    )
    child = ProcedureTerminalEgressChild(
        child_index=0,
        item_key="0",
        manifest_digest="sha256:" + "c" * 64,
        path="claims/22/CLM-" + "2" * 32 + ".json",
        prior_refusals=(receipt,),
    )
    out = _echo(
        capsys,
        _settle(
            settle_outcome="proposed",
            fallback_reason="cruxible.settle.condition_false",
            children=(child,),
        ),
    )
    assert out.splitlines()[1:] == [
        "  1 item: 1 changed, 1 with earlier refusals",
        f"  {child.path} (1 earlier refusal of this statement on this evidence; "
        "see proposal review)",
    ]


def test_a_large_proposal_counts_its_items_and_lists_the_first_ten_changed(capsys) -> None:
    from cruxible_client.contracts.procedures.results import ProcedureTerminalEgressChild

    children = tuple(
        ProcedureTerminalEgressChild(
            child_index=index,
            item_key=f"item-{index}",
            manifest_digest="sha256:" + "c" * 64,
            path=f"claims/22/CLM-{index:032x}.json",
            unchanged=index >= 360,
        )
        for index in range(367)
    )
    out = _echo(
        capsys,
        _settle(
            settle_outcome="proposed",
            fallback_reason="cruxible.settle.condition_false",
            children=children,
        ),
    )
    lines = out.splitlines()
    assert lines[1] == "  367 items: 360 changed, 7 unchanged"
    assert lines[2:12] == [f"  claims/22/CLM-{index:032x}.json" for index in range(10)]
    assert lines[12:] == [f"  ... and 350 more: cruxible proposal review {PROPOSAL} (or --json)"]


def test_a_terminal_whose_items_all_restate_accepted_state_says_nothing_changed(capsys) -> None:
    from cruxible_client.contracts.procedures.results import ProcedureTerminalEgressChild

    children = tuple(
        ProcedureTerminalEgressChild(
            child_index=index,
            item_key=f"item-{index}",
            manifest_digest="sha256:" + "d" * 64,
            unchanged=True,
        )
        for index in range(3)
    )
    out = _echo(
        capsys,
        ProcedureTerminalEgress(
            node_id="propose",
            kind="propose_change_set",
            verdict="delivered",
            required_authority="propose",
            effective_authority="propose",
            limiting_term="procedure_terminal_capability",
            children=children,
        ),
    )
    assert out == "Terminal propose: nothing changed (3 items restate accepted state)\n"
