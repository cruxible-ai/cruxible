from __future__ import annotations

import json

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


def _result() -> contracts.PlaybillPolicyInForceList:
    return contracts.PlaybillPolicyInForceList(
        coordinate=contracts.PlaybillAcceptedCoordinate(
            git_oid="1" * 40,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        policies=[
            contracts.PlaybillPolicyInForce(
                placement="standalone",
                policy_kind="approval_policy",
                declaring_artifact_identity="ApprovalPolicy:instance",
                declaring_artifact_kind="ApprovalPolicy",
                declaring_artifact_digest="sha256:" + "5" * 64,
                path="governance/approval-policy.json",
                field_path="/",
                policy={"tag": "playbill-approval-policy-v1", "mode": "self_approval_allowed"},
            )
        ],
    )


def test_cli_policy_list_uses_the_shared_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class Client:
        def list_playbill_policies_in_force(
            self, instance_id: str, *, limit: int | None = None, cursor: str | None = None
        ) -> contracts.PlaybillPolicyInForceList:
            assert instance_id == "inst_policy"
            assert limit == contracts.PLAYBILL_POLICY_LIST_DEFAULT_LIMIT
            return _result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", Client)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_policy",
            "playbill",
            "policy",
            "list",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["policies"][0]["policy_kind"] == "approval_policy"


def test_cli_policy_list_pages_with_a_cursor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[int | None, str | None]] = []

    class Client:
        def list_playbill_policies_in_force(
            self, instance_id: str, *, limit: int | None = None, cursor: str | None = None
        ) -> contracts.PlaybillPolicyInForceList:
            seen.append((limit, cursor))
            return _result().model_copy(update={"truncated": True, "next_cursor": "c2"})

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", Client)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_policy",
            "playbill",
            "policy",
            "list",
            "--limit",
            "1",
            "--cursor",
            "c1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [(1, "c1")]
    assert "Truncated. Next: --cursor c2" in result.output
