from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.check_review_handoff import check_review_handoff


@dataclass
class _ListResult:
    items: list[dict[str, Any]]
    total: int


class _FakeClient:
    def __init__(
        self,
        reviews: list[dict[str, Any]],
        work_links: dict[str, list[str]],
        *,
        config_name: str = "project_state",
    ) -> None:
        self._reviews = reviews
        self._work_links = work_links
        self._config_name = config_name
        self.list_calls = 0

    def schema(self, instance_id: str) -> dict[str, Any]:
        assert instance_id == "inst_ops"
        return {"name": self._config_name}

    def list(
        self,
        instance_id: str,
        *,
        resource_type: str,
        entity_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        property_filter: dict[str, Any] | None = None,
    ) -> _ListResult:
        self.list_calls += 1
        assert instance_id == "inst_ops"
        assert resource_type == "entities"
        assert entity_type == "ReviewRequest"
        head = (property_filter or {}).get("change_head")
        matching = [
            review
            for review in self._reviews
            if review["properties"].get("change_head") == head
        ]
        page = matching[offset : offset + limit]
        return _ListResult(items=page, total=len(matching))

    def inspect_entity(
        self,
        instance_id: str,
        entity_type: str,
        entity_id: str,
        *,
        direction: str = "both",
        relationship_type: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        assert instance_id == "inst_ops"
        assert entity_type == "ReviewRequest"
        assert direction == "outgoing"
        assert relationship_type == "review_request_for_work_item"
        assert limit == 100
        return {
            "found": True,
            "neighbors": [
                {
                    "direction": "outgoing",
                    "relationship_type": "review_request_for_work_item",
                    "entity": {"entity_type": "WorkItem", "entity_id": work_item_id},
                }
                for work_item_id in self._work_links.get(entity_id, [])
            ],
        }


def _review(
    review_request_id: str,
    *,
    status: str,
    change_head: str = "abc123",
    change_repo: str = "cruxible-ai/cruxible-core",
) -> dict[str, Any]:
    return {
        "entity_type": "ReviewRequest",
        "entity_id": review_request_id,
        "properties": {
            "review_request_id": review_request_id,
            "title": f"Review {review_request_id}",
            "status": status,
            "change_head": change_head,
            "change_repo": change_repo,
        },
        "metadata": {},
    }


def test_approved_review_linked_to_work_item_passes() -> None:
    client = _FakeClient([_review("rr-approved", status="approved")], {"rr-approved": ["wi-1"]})

    result = check_review_handoff(
        client,
        instance_id="inst_ops",
        head="abc123",
        repo="cruxible-ai/cruxible-core",
        expected_config_name="project_state",
    )

    assert result.ok is True
    assert [candidate.review_request_id for candidate in result.approved] == ["rr-approved"]
    assert result.approved[0].work_item_ids == ["wi-1"]


def test_unapproved_review_fails_even_when_linked_to_work() -> None:
    client = _FakeClient(
        [_review("rr-requested", status="requested")],
        {"rr-requested": ["wi-1"]},
    )

    result = check_review_handoff(
        client,
        instance_id="inst_ops",
        head="abc123",
        repo="cruxible-ai/cruxible-core",
    )

    assert result.ok is False
    assert "rr-requested: status is 'requested', expected 'approved'" in result.failures


def test_config_name_mismatch_fails_before_review_lookup() -> None:
    client = _FakeClient(
        [_review("rr-approved", status="approved")],
        {"rr-approved": ["wi-1"]},
        config_name="agent_operation",
    )

    result = check_review_handoff(
        client,
        instance_id="inst_ops",
        head="abc123",
        repo="cruxible-ai/cruxible-core",
        expected_config_name="project_state",
    )

    assert result.ok is False
    assert result.config_name == "agent_operation"
    assert result.expected_config_name == "project_state"
    assert (
        "Target instance config is 'agent_operation', expected 'project_state'"
        in result.failures
    )
    assert client.list_calls == 0


def test_approved_review_without_work_item_link_fails() -> None:
    client = _FakeClient([_review("rr-approved", status="approved")], {})

    result = check_review_handoff(
        client,
        instance_id="inst_ops",
        head="abc123",
        repo="cruxible-ai/cruxible-core",
    )

    assert result.ok is False
    assert (
        "rr-approved: missing review_request_for_work_item WorkItem link"
        in result.failures
    )


def test_repo_mismatch_is_not_accepted() -> None:
    client = _FakeClient(
        [
            _review(
                "rr-other",
                status="approved",
                change_repo="cruxible-ai/other-repo",
            )
        ],
        {"rr-other": ["wi-1"]},
    )

    result = check_review_handoff(
        client,
        instance_id="inst_ops",
        head="abc123",
        repo="cruxible-ai/cruxible-core",
    )

    assert result.ok is False
    assert (
        "rr-other: change_repo is 'cruxible-ai/other-repo', "
        "expected 'cruxible-ai/cruxible-core'"
    ) in result.failures


def test_missing_review_request_for_head_fails() -> None:
    client = _FakeClient([_review("rr-approved", status="approved", change_head="other")], {})

    result = check_review_handoff(
        client,
        instance_id="inst_ops",
        head="abc123",
        repo="cruxible-ai/cruxible-core",
    )

    assert result.ok is False
    assert "No ReviewRequest found with change_head 'abc123'" in result.failures
