"""Focused compatibility tests for deprecate-then-remove surfaces.

The 0.4.0 removals took the retired declared-actor inputs, the ``approve`` and
``flag`` feedback actions, and the ``group_override`` write path off every
surface, so what this file pins for them is the REMOVAL: the fields are gone
from the request and record models, and nothing emits a warning for them any
more. The legacy outcome record/profile functions are the deprecations still on
the registry, so they are what still exercises the transport emitters.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import Response
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.decision.types import DecisionRecord
from cruxible_core.deprecation import (
    LEGACY_OUTCOME_PROFILE,
    LEGACY_OUTCOME_RECORD,
)
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.group.types import CandidateGroup, GroupResolution
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.server.request_models import (
    DecisionRecordCreateRequest,
    FeedbackFromQueryRequest,
    FeedbackRequest,
    OutcomeRequest,
    ProposeGroupRequest,
    ResolveGroupRequest,
)
from cruxible_core.server.routes import feedback as feedback_routes

_RETIRED_REQUEST_FIELDS = [
    (FeedbackRequest, "source"),
    (FeedbackFromQueryRequest, "source"),
    (OutcomeRequest, "source"),
    (ProposeGroupRequest, "proposed_by"),
    (ResolveGroupRequest, "resolved_by"),
    (DecisionRecordCreateRequest, "opened_by"),
    (FeedbackRequest, "group_override"),
    (FeedbackFromQueryRequest, "group_override"),
]


def _call_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


def _header_notices(response: Response) -> list[dict[str, str]]:
    return [json.loads(value) for value in response.headers.getlist("deprecation")]


@pytest.mark.parametrize(("model", "field"), _RETIRED_REQUEST_FIELDS)
def test_retired_request_fields_are_gone_from_the_models(model: Any, field: str) -> None:
    assert field not in model.model_fields


def test_retired_actor_axis_record_fields_no_longer_warn_or_write() -> None:
    """The inputs are gone; the derived read projections stay.

    Passing one is now an ordinary ignored extra on the record models (they do
    not forbid extras) or a validation error where extras are forbidden, and in
    neither case does anything emit a deprecation warning.
    """
    target = RelationshipInstance(
        from_type="Part",
        from_id="P-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        feedback = FeedbackRecord(action="accept", target=target)
        outcome = OutcomeRecord(receipt_id="RCP-1", outcome="correct")
        resolution = GroupResolution(
            resolution_id="RES-1",
            relationship_type="fits",
            group_signature="sig",
            action="reject",
            resolved_at=datetime.now(UTC),
        )
        group = CandidateGroup(group_id="GRP-1", relationship_type="fits", signature="sig")
        decision = DecisionRecord(question="Ship it?")

    assert {
        feedback.source,
        outcome.source,
        resolution.resolved_by,
        group.proposed_by,
        decision.opened_by,
    } == {"unknown"}


def test_retired_feedback_actions_fail_request_validation() -> None:
    for retired in ("approve", "flag"):
        with pytest.raises(ValidationError):
            FeedbackRequest(
                action=retired,
                from_type="Part",
                from_id="P-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            )


def test_legacy_outcome_tools_still_carry_their_structured_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "handle_outcome",
        lambda *_args, **_kwargs: contracts.OutcomeResult(outcome_id="OUT-1"),
    )
    monkeypatch.setattr(
        handlers,
        "handle_get_outcome_profile",
        lambda *_args, **_kwargs: contracts.OutcomeProfileResult(
            found=False,
            anchor_type="receipt",
        ),
    )
    server = create_server()

    outcome = _call_tool(
        server,
        "cruxible_outcome",
        {"instance_id": "inst-1", "receipt_id": "RCP-1", "outcome": "correct"},
    )
    assert outcome["deprecation_warnings"] == [LEGACY_OUTCOME_RECORD.as_dict()]

    profile = _call_tool(
        server,
        "cruxible_get_outcome_profile",
        {"instance_id": "inst-1", "anchor_type": "receipt"},
    )
    assert profile["deprecation_warnings"] == [LEGACY_OUTCOME_PROFILE.as_dict()]


def test_legacy_outcome_route_emits_the_header_without_expanding_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feedback_routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(
        feedback_routes.api,
        "outcome",
        lambda **_kwargs: contracts.OutcomeResult(outcome_id="OUT-1"),
    )

    outcome_response = Response()
    outcome_result = asyncio.run(
        feedback_routes.outcome(
            "inst-1",
            OutcomeRequest(receipt_id="RCP-1", outcome="correct"),
            outcome_response,
        )
    )
    assert _header_notices(outcome_response) == [LEGACY_OUTCOME_RECORD.as_dict()]
    assert outcome_result.model_dump() == {"outcome_id": "OUT-1"}
