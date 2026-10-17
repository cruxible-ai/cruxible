"""Builtin adoption selects daemon-owned definitions, never raw client envelopes."""

from types import SimpleNamespace

from cruxible_core.mcp import handlers


def test_builtin_adoption_sends_only_the_builtin_name(monkeypatch) -> None:
    sent = {}
    result = object()

    def propose(instance_id, **values):
        assert instance_id == "inst"
        sent.update(values)
        return result

    monkeypatch.setattr(
        handlers,
        "_daemon_call",
        lambda call, **_kwargs: call(SimpleNamespace(propose_builtin_claim_type=propose)),
    )
    assert (
        handlers.handle_playbill_adopt_builtin_claim_type("inst", "semantic.alias", "adopt-alias")
        is result
    )
    assert sent == {
        "builtin": "semantic.alias",
        "proposal_name": "adopt-alias",
        "dry_run": None,
        "at": None,
    }
