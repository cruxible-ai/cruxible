"""Tests for hosted governed actor context normalization."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
    load_actor_context,
)


def test_actor_context_normalizes_and_dumps_json_shape() -> None:
    actor = GovernedActorContext(
        actor_type="human_user",
        actor_id=" usr_1 ",
        org_id=" org_1 ",
        operation_id=" op_1 ",
        timestamp=datetime(2026, 6, 5, 12, 0, 0),
        request_id=" req_1 ",
    )

    assert actor.actor_id == "usr_1"
    assert actor.org_id == "org_1"
    assert actor.operation_id == "op_1"
    assert actor.timestamp.tzinfo == timezone.utc
    assert dump_actor_context(actor) == {
        "actor_type": "human_user",
        "actor_id": "usr_1",
        "org_id": "org_1",
        "operation_id": "op_1",
        "timestamp": "2026-06-05T12:00:00+00:00",
        "request_id": "req_1",
    }


def test_actor_context_rejects_blank_required_fields_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        GovernedActorContext.model_validate(
            {
                "actor_type": "human_user",
                "actor_id": " ",
                "org_id": "org_1",
                "operation_id": "op_1",
                "timestamp": "2026-06-05T12:00:00Z",
            }
        )

    with pytest.raises(ValidationError):
        GovernedActorContext.model_validate(
            {
                "actor_type": "human_user",
                "actor_id": "usr_1",
                "org_id": "org_1",
                "operation_id": "op_1",
                "timestamp": "2026-06-05T12:00:00Z",
                "unexpected": "nope",
            }
        )


def test_load_actor_context_fails_closed_for_invalid_persisted_values() -> None:
    assert load_actor_context("not-json-object") is None
    assert load_actor_context({"actor_type": "system"}) is None
