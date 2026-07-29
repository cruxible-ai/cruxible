"""Focused compatibility tests for deprecate-then-remove surfaces."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import Response

from cruxible_client import contracts
from cruxible_core.decision.types import DecisionRecord
from cruxible_core.deprecation import (
    APPROVE_FEEDBACK_ACTION,
    DECISION_OPENED_BY_INPUT,
    FEEDBACK_SOURCE_INPUT,
    GROUP_OVERRIDE,
    GROUP_PROPOSED_BY_INPUT,
    GROUP_RESOLVED_BY_INPUT,
    LEGACY_OUTCOME_PROFILE,
    LEGACY_OUTCOME_RECORD,
    OUTCOME_SOURCE_INPUT,
)
from cruxible_core.errors import ConfigError
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
from cruxible_core.server.routes import decision_records as decision_routes
from cruxible_core.server.routes import feedback as feedback_routes
from cruxible_core.server.routes import groups as group_routes
from cruxible_core.service.feedback import _validate_feedback_request_values


def _call_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


def _header_notices(response: Response) -> list[dict[str, str]]:
    return [json.loads(value) for value in response.headers.getlist("deprecation")]


def test_retired_actor_axis_request_fields_are_accepted_as_ignored_inputs() -> None:
    feedback = FeedbackRequest(
        action="accept",
        from_type="Part",
        from_id="P-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        source="human",
    )
    from_query = FeedbackFromQueryRequest(
        receipt_id="RCP-1",
        result_index=0,
        action="accept",
        source="agent",
    )
    outcome = OutcomeRequest(
        receipt_id="RCP-1",
        outcome="correct",
        source="human",
    )
    proposed = ProposeGroupRequest(
        relationship_type="fits",
        members=[],
        proposed_by="agent",
    )
    resolved = ResolveGroupRequest(
        action="reject",
        expected_pending_version=1,
        resolved_by="human",
    )
    decision = DecisionRecordCreateRequest(
        question="Ship it?",
        opened_by="human",
    )

    assert feedback.source == "human"
    assert from_query.source == "agent"
    assert outcome.source == "human"
    assert proposed.proposed_by == "agent"
    assert resolved.resolved_by == "human"
    assert decision.opened_by == "human"


def test_retired_actor_axis_record_fields_warn_and_remain_derived() -> None:
    target = RelationshipInstance(
        from_type="Part",
        from_id="P-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
    )
    with pytest.warns(DeprecationWarning) as warning_info:
        feedback = FeedbackRecord(action="accept", target=target, source="human")
        outcome = OutcomeRecord(
            receipt_id="RCP-1",
            outcome="correct",
            source="human",
        )
        resolution = GroupResolution(
            resolution_id="RES-1",
            relationship_type="fits",
            group_signature="sig",
            action="reject",
            resolved_at=datetime.now(UTC),
            resolved_by="human",
        )
        group = CandidateGroup(
            group_id="GRP-1",
            relationship_type="fits",
            signature="sig",
            proposed_by="human",
        )
        decision = DecisionRecord(question="Ship it?", opened_by="human")

    assert len(warning_info) == 5
    assert {
        feedback.source,
        outcome.source,
        resolution.resolved_by,
        group.proposed_by,
        decision.opened_by,
    } == {"unknown"}
    assert [json.loads(str(item.message)) for item in warning_info] == [
        FEEDBACK_SOURCE_INPUT.as_dict(),
        OUTCOME_SOURCE_INPUT.as_dict(),
        GROUP_RESOLVED_BY_INPUT.as_dict(),
        GROUP_PROPOSED_BY_INPUT.as_dict(),
        DECISION_OPENED_BY_INPUT.as_dict(),
    ]


def test_flag_reaches_the_structured_service_refusal() -> None:
    request = FeedbackRequest(
        action="flag",
        from_type="Part",
        from_id="P-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
    )
    assert request.action == "flag"

    with pytest.raises(ConfigError) as exc_info:
        _validate_feedback_request_values(action="flag", corrections=None)
    message = str(exc_info.value)
    assert '"surface":"feedback action \'flag\'"' in message
    assert '"replacement":"attest --stance contradict"' in message
    assert '"removal_version":"0.4.0"' in message


def test_mcp_aliases_emit_additive_structured_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "handle_feedback",
        lambda **_kwargs: contracts.FeedbackResult(feedback_id="FB-1", applied=True),
    )
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
    monkeypatch.setattr(
        handlers,
        "handle_propose_group",
        lambda *_args, **_kwargs: contracts.ProposeGroupToolResult(
            signature="sig",
            status="pending_review",
            review_priority="review",
            member_count=0,
        ),
    )
    monkeypatch.setattr(
        handlers,
        "handle_resolve_group",
        lambda *_args, **_kwargs: contracts.ResolveGroupToolResult(
            group_id="GRP-1",
            action="reject",
            edges_created=0,
            edges_skipped=0,
        ),
    )
    monkeypatch.setattr(
        handlers,
        "handle_create_decision_record",
        lambda *_args, **_kwargs: contracts.DecisionRecordResult(record={}),
    )
    server = create_server()

    feedback = _call_tool(
        server,
        "cruxible_feedback",
        {
            "instance_id": "inst-1",
            "action": "approve",
            "from_type": "Part",
            "from_id": "P-1",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-1",
            "group_override": True,
            "source": "human",
        },
    )
    assert feedback["deprecation_warnings"] == [
        APPROVE_FEEDBACK_ACTION.as_dict(),
        GROUP_OVERRIDE.as_dict(),
        FEEDBACK_SOURCE_INPUT.as_dict(),
    ]

    outcome = _call_tool(
        server,
        "cruxible_outcome",
        {
            "instance_id": "inst-1",
            "receipt_id": "RCP-1",
            "outcome": "correct",
            "source": "human",
        },
    )
    assert outcome["deprecation_warnings"] == [
        LEGACY_OUTCOME_RECORD.as_dict(),
        OUTCOME_SOURCE_INPUT.as_dict(),
    ]

    profile = _call_tool(
        server,
        "cruxible_get_outcome_profile",
        {"instance_id": "inst-1", "anchor_type": "receipt"},
    )
    assert profile["deprecation_warnings"] == [LEGACY_OUTCOME_PROFILE.as_dict()]

    proposed = _call_tool(
        server,
        "cruxible_propose_group",
        {
            "instance_id": "inst-1",
            "relationship_type": "fits",
            "members": [],
            "proposed_by": "agent",
        },
    )
    assert proposed["deprecation_warnings"] == [GROUP_PROPOSED_BY_INPUT.as_dict()]

    resolved = _call_tool(
        server,
        "cruxible_resolve_group",
        {
            "instance_id": "inst-1",
            "group_id": "GRP-1",
            "action": "reject",
            "expected_pending_version": 1,
            "resolved_by": "human",
        },
    )
    assert resolved["deprecation_warnings"] == [GROUP_RESOLVED_BY_INPUT.as_dict()]

    decision = _call_tool(
        server,
        "cruxible_create_decision_record",
        {
            "instance_id": "inst-1",
            "question": "Ship it?",
            "opened_by": "human",
        },
    )
    assert decision["deprecation_warnings"] == [DECISION_OPENED_BY_INPUT.as_dict()]


def test_http_routes_emit_headers_without_expanding_warningless_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feedback_routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(group_routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(decision_routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(
        feedback_routes.api,
        "feedback",
        lambda **_kwargs: contracts.FeedbackResult(feedback_id="FB-1", applied=True),
    )
    monkeypatch.setattr(
        feedback_routes.api,
        "outcome",
        lambda **_kwargs: contracts.OutcomeResult(outcome_id="OUT-1"),
    )
    monkeypatch.setattr(
        group_routes.api,
        "propose_group",
        lambda **_kwargs: contracts.ProposeGroupToolResult(
            signature="sig",
            status="pending_review",
            review_priority="review",
            member_count=0,
        ),
    )
    monkeypatch.setattr(
        decision_routes.api,
        "create_decision_record",
        lambda *_args, **_kwargs: contracts.DecisionRecordResult(record={}),
    )

    feedback_response = Response()
    feedback_result = asyncio.run(
        feedback_routes.feedback(
            "inst-1",
            FeedbackRequest(
                action="approve",
                from_type="Part",
                from_id="P-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
            ),
            feedback_response,
        )
    )
    assert _header_notices(feedback_response) == [APPROVE_FEEDBACK_ACTION.as_dict()]
    assert feedback_result.model_dump() == {
        "feedback_id": "FB-1",
        "applied": True,
        "receipt_id": None,
    }

    outcome_response = Response()
    outcome_result = asyncio.run(
        feedback_routes.outcome(
            "inst-1",
            OutcomeRequest(
                receipt_id="RCP-1",
                outcome="correct",
                source="human",
            ),
            outcome_response,
        )
    )
    assert _header_notices(outcome_response) == [
        LEGACY_OUTCOME_RECORD.as_dict(),
        OUTCOME_SOURCE_INPUT.as_dict(),
    ]
    assert outcome_result.model_dump() == {"outcome_id": "OUT-1"}

    group_response = Response()
    group_result = asyncio.run(
        group_routes.propose_group(
            "inst-1",
            ProposeGroupRequest(
                relationship_type="fits",
                members=[],
                proposed_by="agent",
            ),
            group_response,
        )
    )
    assert _header_notices(group_response) == [GROUP_PROPOSED_BY_INPUT.as_dict()]
    assert "deprecation_warnings" not in group_result.model_dump()

    decision_response = Response()
    decision_result = asyncio.run(
        decision_routes.create_decision_record(
            "inst-1",
            DecisionRecordCreateRequest(question="Ship it?", opened_by="human"),
            decision_response,
        )
    )
    assert _header_notices(decision_response) == [DECISION_OPENED_BY_INPUT.as_dict()]
    assert "deprecation_warnings" not in decision_result.model_dump()
