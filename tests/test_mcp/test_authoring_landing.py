"""MCP submits use the daemon's landing default and explicit opt-out."""

from types import SimpleNamespace

import pytest

from cruxible_core.mcp import handlers


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("accept", ["if_allowed", "never"])
def test_mcp_submit_forwards_accept_in_one_call(monkeypatch, staged, accept):
    seen = []
    sentinel = object()

    def submit(*args, **kwargs):
        seen.append((args, kwargs))
        return sentinel

    client = SimpleNamespace(submit_authoring_intent=submit, submit_authoring_input=submit)
    monkeypatch.setattr(handlers, "_daemon_call", lambda call, **kwargs: call(client))
    kwargs = (
        {"intent_id": "staged"}
        if staged
        else {"payload": {"kind": "subject", "subject_kind": "project.work_item", "key": "new"}}
    )
    if accept != "if_allowed":
        kwargs["accept"] = accept
    assert handlers.handle_playbill_authoring_submit("inst", **kwargs) is sentinel
    assert len(seen) == 1 and seen[0][1]["accept"] == accept
