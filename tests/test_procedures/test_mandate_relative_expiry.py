"""A Mandate's validity as a span (``expires: 90d``) and a defaulted ``valid_from``.

Authoring takes exactly one of ``expires_at`` or ``expires``; ``valid_from``
omitted is the intent's canonical timestamp. Lowering stores concrete UTC
instants, so one intent lowers to the same bytes however often it is lowered.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
from cruxible_client.contracts.authoring.models import ProcedureMandateAuthoringPayload
from cruxible_client.contracts.procedure_mandates import (
    parse_procedure_mandate,
    procedure_mandate_path,
)
from cruxible_client.contracts.temporal import parse_validity_span
from cruxible_core.authoring.lowering import AuthoringLoweringError
from tests.support.operation_world import NOW, author
from tests.test_procedures.test_claims_terminal import run
from tests.test_procedures.test_observation_settle_grant import LINE, _settle_world

#: ``author``'s intent timestamp: what an omitted ``valid_from`` lowers to.
INTENT_TIME = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)
PATH = procedure_mandate_path(LINE + "-settle")


def _input(**validity: Any) -> ProcedureMandateInput:
    return ProcedureMandateInput.model_validate(
        {
            "kind": "procedure_mandate",
            "name": LINE + "-settle",
            "line_name": LINE,
            "grants": "settle",
            "scope": "observations",
            **validity,
        }
    )


def _lowered(instance: Any, **validity: Any) -> Any:
    changed = author(instance, _input(**validity), name="relative-grant")
    return parse_procedure_mandate(changed[PATH], path=PATH)


# -- the span ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "span"),
    [
        ("36h", timedelta(hours=36)),
        ("90d", timedelta(days=90)),
        ("90D", timedelta(days=90)),
        ("12w", timedelta(weeks=12)),
        ("1y", timedelta(days=365)),
        ("P90D", timedelta(days=90)),
        ("PT24H", timedelta(hours=24)),
        (" 30d ", timedelta(days=30)),
    ],
)
def test_a_validity_span_parses_short_and_iso_forms(text: str, span: timedelta) -> None:
    assert parse_validity_span(text) == span


@pytest.mark.parametrize("text", ["abc", "0d", "-5d", "P0D", "PT0S", "5x", "d", "1.5d", "P1Y", ""])
def test_a_validity_span_refuses_other_text_and_zero(text: str) -> None:
    with pytest.raises(ValueError):
        parse_validity_span(text)


# -- authoring validation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("validity", "message"),
    [
        ({}, "give expires_at (an instant) or expires (a span)"),
        (
            {"expires_at": NOW + timedelta(days=1), "expires": "90d"},
            "give expires_at or expires, not both",
        ),
        ({"expires": "abc"}, "procedure_mandate expires: 'abc' is no span"),
        ({"expires": "0d"}, "is no span longer than zero"),
    ],
)
def test_authoring_takes_exactly_one_valid_expiry(validity: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        _input(**validity)
    with pytest.raises(ValidationError, match=re.escape(message)):
        ProcedureMandateAuthoringPayload.model_validate(
            {"name": "m", "line_name": "l", "grants": "propose", **validity}
        )


def test_an_absolute_payload_serializes_as_before() -> None:
    payload = ProcedureMandateAuthoringPayload(
        name="m",
        line_name="l",
        grants="propose",
        valid_from=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    dumped = payload.model_dump(mode="json")
    assert "expires" not in dumped and dumped["valid_from"] and dumped["expires_at"]
    relative = ProcedureMandateAuthoringPayload(
        name="m", line_name="l", grants="propose", expires="90d"
    ).model_dump(mode="json")
    assert relative["expires"] == "90d"
    assert "valid_from" not in relative and "expires_at" not in relative


# -- lowering to concrete instants ---------------------------------------------------


def test_expires_without_valid_from_counts_from_the_intents_time_and_settles(
    tmp_path: Path,
) -> None:
    instance, root = _settle_world(tmp_path)
    mandate = _lowered(instance, expires="90d")
    assert mandate.valid_from == INTENT_TIME
    assert mandate.expires_at == INTENT_TIME + timedelta(days=90)
    # Stored as instants: the accepted artifact carries no span.
    assert "expires" not in mandate.model_dump(mode="json")
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].settle_outcome == "settled", state.terminal_egress


def test_expires_counts_from_a_given_valid_from(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    start = NOW - timedelta(days=2)
    mandate = _lowered(instance, valid_from=start, expires="12w")
    assert mandate.valid_from == start
    assert mandate.expires_at == start + timedelta(weeks=12)


def test_expires_at_without_valid_from_starts_at_the_intents_time(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    end = NOW + timedelta(days=30)
    mandate = _lowered(instance, expires_at=end)
    assert mandate.valid_from == INTENT_TIME and mandate.expires_at == end


def test_expires_at_not_after_the_intents_time_refuses_by_name(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    with pytest.raises(AuthoringLoweringError) as refused:
        _lowered(instance, expires_at=INTENT_TIME)
    assert refused.value.code == "cruxible.authoring.procedure_mandate_window"
    assert "valid_from defaults to" in str(refused.value)


def test_both_instants_out_of_order_still_refuse_as_an_invalid_mandate(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    with pytest.raises(AuthoringLoweringError) as refused:
        _lowered(instance, valid_from=NOW, expires_at=NOW - timedelta(days=1))
    assert refused.value.code == "cruxible.authoring.procedure_mandate_invalid"


def test_one_intent_lowers_to_the_same_instants_every_time(tmp_path: Path) -> None:
    from cruxible_client.contracts.authoring.inputs import lower_authoring_input
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.proposals.proposals import AuthenticatedActor

    instance, _root = _settle_world(tmp_path)
    intent = (
        AuthoringIntentCoordinator.for_instance(instance)
        .create(
            actor=AuthenticatedActor(actor_id="owner"),
            payload=lower_authoring_input(_input(expires="1y")),
            canonical_timestamp="2026-10-29T11:00:00.000000Z",
        )
        .intent
    )
    # The intent keeps the span as authored; each lowering resolves it the same way.
    assert intent.payload.model_dump(mode="json")["expires"] == "1y"
    first = dict(lower_authoring(instance, intent=intent, actor_id="owner").changed_members)
    second = dict(lower_authoring(instance, intent=intent, actor_id="owner").changed_members)
    assert first[PATH] == second[PATH]
    mandate = parse_procedure_mandate(first[PATH], path=PATH)
    assert mandate.expires_at == INTENT_TIME + timedelta(days=365)
    assert b"1y" not in first[PATH]
