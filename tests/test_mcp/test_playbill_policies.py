from __future__ import annotations

from cruxible_client import contracts
from cruxible_core.mcp import handlers


def test_mcp_policies_in_force_delegates_to_the_shared_runtime(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    expected = contracts.PlaybillPolicyInForceList(
        coordinate=contracts.PlaybillAcceptedCoordinate(
            git_oid="1" * 40,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        policies=[],
    )

    def stub(instance_id: str) -> contracts.PlaybillPolicyInForceList:
        assert instance_id == "inst_policy"
        return expected

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_policies_in_force",
        stub,
    )

    assert handlers.handle_playbill_policies_in_force("inst_policy") == expected
