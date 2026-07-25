"""CLI coverage for the outcome-contract subcommands.

The new subcommands join the EXISTING ``cruxible outcome`` group; the legacy
``record``/``profile``/``analyze`` lane must keep working untouched, so it is
asserted here rather than left to a later retirement to discover.
"""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

import json

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

CONTRACT = {
    "contract_id": "RSC-1",
    "entity_type": "Decision",
    "entity_id": "dd-1",
    "subject_content_digest": "sha256:abc",
    "declaration": {
        "description": "Service stays healthy",
        "check_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-09-01T00:00:00Z",
        "measurement": {
            "kind": "query",
            "query_name": "healthy_services",
            "params": {},
            "expect": {"min_count": 1, "condition_scope": "all"},
        },
    },
    "opened_at": "2026-07-25T12:00:00Z",
    "actor_context": {
        "actor_type": "human_user",
        "actor_id": "operator",
        "org_id": "local",
        "operation_id": "op-1",
        "timestamp": "2026-07-25T12:00:00Z",
    },
}


def test_outcome_help_lists_new_and_legacy_subcommands() -> None:
    result = CliRunner().invoke(cli, ["outcome", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("open", "resolve", "dispose", "list", "due"):
        assert command in result.output
    # The legacy feedback-lane subcommands retire separately; they must not be
    # collateral damage of adding the contract lane.
    for legacy in ("record", "profile", "analyze"):
        assert legacy in result.output


def test_legacy_outcome_record_still_dispatches(monkeypatch) -> None:
    calls: list[str] = []

    class StubClient:
        def outcome(self, instance_id, **kwargs):
            calls.append("outcome")
            return contracts.OutcomeResult(outcome_id="OUT-1")

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst-1",
            "outcome",
            "record",
            "--receipt",
            "RCP-1",
            "--outcome",
            "correct",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["outcome"]


def test_outcome_contract_commands_dispatch_and_emit_list_envelopes(monkeypatch) -> None:
    calls: list[str] = []

    class StubClient:
        def open_outcome_contract(self, instance_id, **kwargs):
            calls.append("open")
            return contracts.OutcomeContractResult(contract=CONTRACT, receipt_id="RCP-1")

        def resolve_outcome(self, instance_id, contract_id, **kwargs):
            calls.append("resolve")
            return contracts.OutcomeResolutionResult(
                resolution={
                    "resolution_id": "RSR-1",
                    "contract_id": contract_id,
                    "sequence": 1,
                    "verdict": kwargs["verdict"],
                },
                receipt_id="RCP-2",
            )

        def dispose_outcome_resolution(self, instance_id, resolution_id, **kwargs):
            calls.append("dispose")
            return contracts.OutcomeDispositionResult(
                disposition={
                    "disposition_id": "RSD-1",
                    "resolution_id": resolution_id,
                    "verdict": kwargs["verdict"],
                },
                receipt_id="RCP-3",
            )

        def list_outcome_contracts(self, instance_id, **kwargs):
            calls.append("list")
            return contracts.ListResult(
                items=[
                    {
                        "contract": CONTRACT,
                        "status": "open",
                        "expired": False,
                        "subject_present": True,
                        "subject_content_drifted": False,
                    }
                ],
                total=1,
                limit=100,
                offset=0,
                truncated=False,
                read_revision=4,
            )

        def outcome_due(self, instance_id, **kwargs):
            calls.append("due")
            return contracts.ListResult(
                items=[],
                total=0,
                limit=100,
                offset=0,
                truncated=False,
                read_revision=4,
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    runner = CliRunner()
    prefix = ["--server-url", "http://server", "--instance-id", "inst-1", "outcome"]
    opened = runner.invoke(
        cli,
        [
            *prefix,
            "open",
            "--entity-type",
            "Decision",
            "--entity-id",
            "dd-1",
            "--description",
            "Service stays healthy",
            "--check-at",
            "2026-08-01T00:00:00Z",
            "--expires-at",
            "2026-09-01T00:00:00Z",
            "--measurement",
            '{"kind":"query","query_name":"healthy_services","expect":{"min_count":1}}',
            "--json",
        ],
    )
    resolved = runner.invoke(
        cli,
        [
            *prefix,
            "resolve",
            "RSC-1",
            "--verdict",
            "satisfied",
            "--observed-at",
            "2026-08-02T00:00:00Z",
            "--evidence-ref",
            '{"source":"test","source_record_id":"record-1"}',
            "--json",
        ],
    )
    disposed = runner.invoke(
        cli,
        [*prefix, "dispose", "RSR-1", "--verdict", "overturned", "--json"],
    )
    listed = runner.invoke(cli, [*prefix, "list", "--json"])
    due = runner.invoke(cli, [*prefix, "due", "--queue", "overdue", "--json"])

    for result in (opened, resolved, disposed, listed, due):
        assert result.exit_code == 0, result.output
    assert json.loads(listed.output)["read_revision"] == 4
    assert set(json.loads(due.output)) == {
        "items",
        "total",
        "limit",
        "offset",
        "truncated",
        "read_revision",
    }
    assert calls == ["open", "resolve", "dispose", "list", "due"]


def test_outcome_open_rejects_non_json_measurement() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst-1",
            "outcome",
            "open",
            "--entity-type",
            "Decision",
            "--entity-id",
            "dd-1",
            "--description",
            "x",
            "--check-at",
            "2026-08-01T00:00:00Z",
            "--expires-at",
            "2026-09-01T00:00:00Z",
            "--measurement",
            "not-json",
        ],
    )
    assert result.exit_code != 0
    assert "must be valid JSON" in result.output
