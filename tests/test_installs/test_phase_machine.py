"""The install phase machine: legal paths, refused paths, and their receipts."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import InstallNotFoundError, InstallPhaseTransitionError
from cruxible_core.installs.types import (
    INSTALL_PHASES,
    LEGAL_PHASE_TRANSITIONS,
    TERMINAL_INSTALL_PHASES,
    InstallPhase,
    legal_next_phases,
)
from cruxible_core.service import (
    service_advance_install_phase,
    service_get_install,
    service_list_installs,
)
from tests.test_installs.conftest import (
    actor,
    create_install,
    load_receipt,
    validation_details,
)

SUCCESS_PATH: tuple[InstallPhase, ...] = ("pending_acceptance", "active")
ROLLBACK_PATH: tuple[InstallPhase, ...] = ("failed", "rolling_back", "rolled_back")


def _advance_through(
    instance: CruxibleInstance,
    install_id: str,
    phases: tuple[InstallPhase, ...],
) -> None:
    for phase in phases:
        service_advance_install_phase(instance, install_id, to_phase=phase, actor_context=actor())


# ---------------------------------------------------------------------------
# The map itself
# ---------------------------------------------------------------------------


def test_transition_map_covers_every_phase() -> None:
    """A phase absent from the map would raise KeyError instead of refusing."""
    assert set(LEGAL_PHASE_TRANSITIONS) == set(INSTALL_PHASES)


def test_terminal_phases_have_no_successors() -> None:
    for phase in INSTALL_PHASES:
        if phase in TERMINAL_INSTALL_PHASES:
            assert legal_next_phases(phase) == ()
        else:
            assert legal_next_phases(phase) != ()


def test_every_phase_is_reachable_from_preparing() -> None:
    """No phase is declared that the machine can never actually enter."""
    reached = {"preparing"}
    frontier = ["preparing"]
    while frontier:
        current = frontier.pop()
        for successor in LEGAL_PHASE_TRANSITIONS[current]:  # type: ignore[index]
            if successor not in reached:
                reached.add(successor)
                frontier.append(successor)
    assert reached == set(INSTALL_PHASES)


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------


def test_create_opens_in_preparing_with_a_seed_history_event(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)

    assert record.phase == "preparing"
    detail = service_get_install(instance, record.install_id)
    assert [(event.from_phase, event.to_phase) for event in detail.phase_history] == [
        (None, "preparing")
    ]
    assert detail.phase_history[0].sequence == 1
    assert detail.phase_history[0].actor_context is not None
    assert detail.phase_history[0].actor_context.actor_id == "installer"


def test_success_path_reaches_active(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    _advance_through(instance, record.install_id, SUCCESS_PATH)

    detail = service_get_install(instance, record.install_id)
    assert detail.install.phase == "active"
    assert [event.to_phase for event in detail.phase_history] == [
        "preparing",
        "pending_acceptance",
        "active",
    ]
    assert [event.sequence for event in detail.phase_history] == [1, 2, 3]


def test_rollback_path_reaches_rolled_back(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    _advance_through(instance, record.install_id, ROLLBACK_PATH)

    detail = service_get_install(instance, record.install_id)
    assert detail.install.phase == "rolled_back"
    assert [event.to_phase for event in detail.phase_history] == [
        "preparing",
        "failed",
        "rolling_back",
        "rolled_back",
    ]


def test_failure_from_pending_acceptance_is_legal(instance: CruxibleInstance) -> None:
    """Acceptance can be declined; that is a failure, not an illegal move."""
    record = create_install(instance)
    _advance_through(instance, record.install_id, ("pending_acceptance", "failed"))

    assert service_get_install(instance, record.install_id).install.phase == "failed"


def test_failure_reason_is_recorded_on_the_install_and_its_event(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)
    service_advance_install_phase(
        instance,
        record.install_id,
        to_phase="failed",
        actor_context=actor(),
        reason="slot binding unresolvable",
    )

    detail = service_get_install(instance, record.install_id)
    assert detail.install.failure_reason == "slot binding unresolvable"
    assert detail.phase_history[-1].reason == "slot binding unresolvable"


def test_advance_returns_the_updated_record(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    advanced = service_advance_install_phase(
        instance, record.install_id, to_phase="pending_acceptance", actor_context=actor()
    )

    assert advanced.phase == "pending_acceptance"
    assert advanced.install_id == record.install_id
    assert advanced.updated_at >= record.created_at


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ((), "active"),
        ((), "rolling_back"),
        ((), "rolled_back"),
        ((), "preparing"),
        (("pending_acceptance",), "preparing"),
        (("pending_acceptance",), "rolling_back"),
        (("pending_acceptance", "active"), "failed"),
        (("pending_acceptance", "active"), "rolling_back"),
        (("failed",), "active"),
        (("failed",), "rolled_back"),
        (("failed", "rolling_back"), "active"),
        (("failed", "rolling_back", "rolled_back"), "preparing"),
    ],
)
def test_illegal_transitions_are_refused(
    instance: CruxibleInstance,
    path: tuple[InstallPhase, ...],
    illegal: InstallPhase,
) -> None:
    record = create_install(instance)
    _advance_through(instance, record.install_id, path)
    expected_phase = path[-1] if path else "preparing"

    with pytest.raises(InstallPhaseTransitionError) as excinfo:
        service_advance_install_phase(
            instance, record.install_id, to_phase=illegal, actor_context=actor()
        )

    error = excinfo.value
    assert error.actual_phase == expected_phase
    assert error.requested_phase == illegal
    assert error.legal_phases == list(legal_next_phases(expected_phase))
    # The message must name the ACTUAL phase: a resumed installer reads its
    # position off the refusal rather than guessing it.
    assert f"'{expected_phase}'" in str(error)


def test_refused_transition_leaves_the_phase_and_history_untouched(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)
    before = service_get_install(instance, record.install_id)

    with pytest.raises(InstallPhaseTransitionError):
        service_advance_install_phase(
            instance, record.install_id, to_phase="active", actor_context=actor()
        )

    after = service_get_install(instance, record.install_id)
    assert after.install.phase == before.install.phase
    assert len(after.phase_history) == len(before.phase_history)


def test_terminal_phase_refusal_says_terminal(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    _advance_through(instance, record.install_id, SUCCESS_PATH)

    with pytest.raises(InstallPhaseTransitionError) as excinfo:
        service_advance_install_phase(
            instance, record.install_id, to_phase="failed", actor_context=actor()
        )

    assert excinfo.value.legal_phases == []
    assert "terminal phase" in str(excinfo.value)


def test_advancing_an_unknown_install_raises_not_found(instance: CruxibleInstance) -> None:
    with pytest.raises(InstallNotFoundError):
        service_advance_install_phase(
            instance, "inst-nope", to_phase="active", actor_context=actor()
        )


def test_duplicate_install_id_is_refused(instance: CruxibleInstance) -> None:
    create_install(instance, install_id="inst-fixed")

    with pytest.raises(Exception) as excinfo:
        create_install(instance, install_id="inst-fixed")

    assert "already exists" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def test_create_mints_a_committed_receipt(instance: CruxibleInstance) -> None:
    record = create_install(instance)

    receipt = load_receipt(instance, record.receipt_id)
    assert receipt.operation_type == "install_create"
    assert receipt.committed is True
    assert receipt.actor_context is not None
    assert receipt.actor_context.actor_id == "installer"
    # Committed mutation receipts shed their payload under the default
    # `metadata` retention, so the durable evidence is the validation node plus
    # the content-addressed payload digest — not the parameters body.
    assert receipt.nodes[0].payload_metadata is not None
    assert validation_details(receipt) == [
        {"passed": True, "install_id": record.install_id, "phase": "preparing"}
    ]


def test_each_advance_mints_its_own_receipt(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    first = service_advance_install_phase(
        instance, record.install_id, to_phase="pending_acceptance", actor_context=actor()
    )
    second = service_advance_install_phase(
        instance, record.install_id, to_phase="active", actor_context=actor()
    )

    assert first.receipt_id != second.receipt_id
    transitions = (
        (first, "preparing", "pending_acceptance"),
        (second, "pending_acceptance", "active"),
    )
    for result, from_phase, to_phase in transitions:
        receipt = load_receipt(instance, result.receipt_id)
        assert receipt.operation_type == "install_phase_advance"
        assert receipt.committed is True
        assert validation_details(receipt) == [
            {
                "passed": True,
                "install_id": record.install_id,
                "from_phase": from_phase,
                "to_phase": to_phase,
            }
        ]


def test_phase_events_cite_the_receipt_that_wrote_them(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)
    advanced = service_advance_install_phase(
        instance, record.install_id, to_phase="pending_acceptance", actor_context=actor()
    )

    detail = service_get_install(instance, record.install_id)
    assert detail.phase_history[0].receipt_id == record.receipt_id
    assert detail.phase_history[1].receipt_id == advanced.receipt_id


def test_refused_transition_is_receipted_with_the_actual_phase(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)

    with pytest.raises(InstallPhaseTransitionError) as excinfo:
        service_advance_install_phase(
            instance, record.install_id, to_phase="active", actor_context=actor()
        )

    receipt = load_receipt(instance, excinfo.value.mutation_receipt_id)
    assert receipt.operation_type == "install_phase_advance"
    assert receipt.committed is False
    details = validation_details(receipt)
    assert any(
        detail.get("passed") is False
        and detail.get("actual_phase") == "preparing"
        and detail.get("requested_phase") == "active"
        for detail in details
    ), details


def test_listing_filters_by_phase(instance: CruxibleInstance) -> None:
    active = create_install(instance, artifact_id="a", install_id="inst-a")
    create_install(instance, artifact_id="b", install_id="inst-b")
    _advance_through(instance, active.install_id, SUCCESS_PATH)

    result = service_list_installs(instance, phase="active")
    assert result.total == 1
    assert [item["install_id"] for item in result.items] == ["inst-a"]

    by_artifact = service_list_installs(instance, artifact_id="b")
    assert [item["install_id"] for item in by_artifact.items] == ["inst-b"]


def test_listing_reports_the_standard_envelope(instance: CruxibleInstance) -> None:
    for index in range(3):
        create_install(instance, artifact_id=f"a{index}", install_id=f"inst-{index}")

    page = service_list_installs(instance, limit=2, offset=0)
    assert page.total == 3
    assert len(page.items) == 2
    assert page.truncated is True

    tail = service_list_installs(instance, limit=2, offset=2)
    assert len(tail.items) == 1
    assert tail.truncated is False


def test_get_install_on_unknown_id_raises_not_found(instance: CruxibleInstance) -> None:
    with pytest.raises(InstallNotFoundError):
        service_get_install(instance, "inst-missing")
