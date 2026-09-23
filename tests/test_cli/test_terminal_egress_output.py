"""The CLI names a settle terminal's outcome and a capped terminal's authority."""

from types import SimpleNamespace

from cruxible_client.contracts.procedures.results import ProcedureTerminalEgressV1
from cruxible_core.cli.commands.playbill import _echo_terminal_egress

PROPOSAL = "sha256:" + "a" * 64
CANDIDATE = "sha256:" + "b" * 64


def _settle(**update):  # type: ignore[no-untyped-def]
    return ProcedureTerminalEgressV1(
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
        _settle(settle_outcome="proposed", fallback_reason="playbill.settle.condition_false"),
    )
    assert out == (
        f"Proposal settle: {PROPOSAL} candidate {CANDIDATE} "
        "(settle fell back: playbill.settle.condition_false)\n"
    )


def test_a_capped_terminal_names_the_authority_it_needed_and_had(capsys) -> None:
    capped = ProcedureTerminalEgressV1(
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
