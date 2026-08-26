"""MCP parity for accepted ChangeSet history."""

from __future__ import annotations

from cruxible_client import contracts
from cruxible_core.mcp import handlers


def test_mcp_since_delegates_the_frozen_request(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}
    coordinate = contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    values: dict[str, object] = {
        "coordinate": coordinate.model_dump(mode="json"),
        "generation": 4,
        "rows": [],
        "next_cursor": None,
        "truncated": False,
    }
    result = contracts.PlaybillSinceResult.model_validate(
        {
            **values,
            "result_digest": contracts._since_digest(  # type: ignore[attr-defined]
                "playbill-since-result-v1", values
            ),
        }
    )

    def stub(instance_id: str, *, request: contracts.PlaybillSinceRequest):
        assert instance_id == "inst_since"
        seen.update(request.model_dump(mode="json"))
        return result

    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_since", stub)
    actual = handlers.handle_playbill_since(
        "inst_since",
        generation=2,
        at=coordinate.model_dump(mode="json"),
        access_profile={
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "mcp-test",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": False,
        },
        max_rows=7,
        max_bytes=4096,
        cursor=None,
    )

    assert actual == result
    assert seen["generation"] == 2
    assert seen["at"] == coordinate.model_dump(mode="json")
    assert seen["max_rows"] == 7
    assert seen["max_bytes"] == 4096
