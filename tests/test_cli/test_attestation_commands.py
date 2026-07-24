"""CLI command-family coverage for Stage D attestations."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

import json

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


def test_attestation_help_registers_all_stage_d_commands() -> None:
    result = CliRunner().invoke(cli, ["attest", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("record", "list", "queue", "resolve"):
        assert command in result.output


def test_attestation_commands_dispatch_and_emit_list_envelopes(monkeypatch) -> None:
    calls: list[str] = []
    attestation = {
        "attestation_id": "ATT-1",
        "relationship_type": "protected_by",
        "from_type": "Service",
        "from_id": "svc-1",
        "to_type": "Control",
        "to_id": "ctl-1",
        "claim_content_digest": "sha256:digest",
        "claim_state_at_record": "live",
        "stance": "support",
        "evidence_refs": [],
        "observed_at": "2026-07-24T11:00:00Z",
        "recorded_at": "2026-07-24T12:00:00Z",
        "actor_context": {
            "actor_type": "human_user",
            "actor_id": "operator",
            "org_id": "local",
            "operation_id": "op-1",
            "timestamp": "2026-07-24T12:00:00Z",
        },
    }

    class StubClient:
        def attest(self, instance_id, **kwargs):
            calls.append("record")
            return contracts.AttestationRecordResult(attestation=attestation)

        def list_attestations(self, instance_id, **kwargs):
            calls.append("list")
            return contracts.ListResult(
                items=[{"attestation": attestation}],
                total=1,
                limit=100,
                offset=0,
                truncated=False,
                read_revision=4,
            )

        def attestation_queue(self, instance_id, **kwargs):
            calls.append("queue")
            return contracts.ListResult(
                items=[],
                total=0,
                limit=100,
                offset=0,
                truncated=False,
                read_revision=4,
            )

        def resolve_attestation(self, instance_id, attestation_id, **kwargs):
            calls.append("resolve")
            return contracts.AttestationDispositionResult(
                disposition={
                    "disposition_id": "ATD-1",
                    "attestation_id": attestation_id,
                    "verdict": kwargs["verdict"],
                }
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    runner = CliRunner()
    prefix = ["--server-url", "http://server", "--instance-id", "inst-1", "attest"]
    recorded = runner.invoke(
        cli,
        [
            *prefix,
            "record",
            "--relationship",
            "protected_by",
            "--from-type",
            "Service",
            "--from-id",
            "svc-1",
            "--to-type",
            "Control",
            "--to-id",
            "ctl-1",
            "--stance",
            "support",
            "--observed-at",
            "2026-07-24T11:00:00Z",
            "--evidence-ref",
            '{"source":"test","source_record_id":"record-1"}',
            "--json",
        ],
    )
    listed = runner.invoke(cli, [*prefix, "list", "--json"])
    queued = runner.invoke(cli, [*prefix, "queue", "--json"])
    resolved = runner.invoke(
        cli,
        [*prefix, "resolve", "ATT-1", "--verdict", "upheld", "--json"],
    )
    for result in (recorded, listed, queued, resolved):
        assert result.exit_code == 0, result.output
    assert json.loads(listed.output)["read_revision"] == 4
    assert set(json.loads(queued.output)) == {
        "items",
        "total",
        "limit",
        "offset",
        "truncated",
        "read_revision",
    }
    assert calls == ["record", "list", "queue", "resolve"]
