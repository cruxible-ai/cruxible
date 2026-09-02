"""Closed served refusal vocabularies always carry structured repair."""

from __future__ import annotations

from cruxible_client.contracts.repairs import HandEditRepairV1, ServedRepairEnvelopeV1
from cruxible_core.service.playbill_refusal_catalog import (
    ALL_SERVED_REFUSAL_CODES,
    CLOSED_SERVED_REFUSAL_VOCABULARIES,
    hand_edit_next_reasons,
    repair_for_refusal,
)


def test_every_registered_refusal_resolves_without_prose_parsing() -> None:
    assert ALL_SERVED_REFUSAL_CODES
    assert all(CLOSED_SERVED_REFUSAL_VOCABULARIES.values())

    for code in ALL_SERVED_REFUSAL_CODES:
        repair = repair_for_refusal(code)
        assert ServedRepairEnvelopeV1(repair=repair).repair == repair
        if isinstance(repair, HandEditRepairV1):
            assert repair.hand_edit.target
            assert repair.hand_edit.required_change


def test_hand_edit_next_membership_is_client_owned_and_positive() -> None:
    assert hand_edit_next_reasons() == {
        "procedure_projection_missing",
        "provider_lane_unavailable",
    }


def test_unregistered_free_string_is_not_promoted_to_authority() -> None:
    try:
        repair_for_refusal("compiler diagnostic prose")
    except KeyError as exc:
        assert "unregistered served refusal" in str(exc)
    else:  # pragma: no cover - decisive guard
        raise AssertionError("free diagnostic prose entered the v1 refusal catalog")
