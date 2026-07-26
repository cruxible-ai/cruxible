"""CLI coverage for settled lifecycle noun-verbs and teaching help."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

import json

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


def test_lifecycle_noun_verbs_dispatch_with_reason_and_optional_evidence(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class StubClient:
        def supersede_claim(
            self, instance_id, claim_id, successor_claim_id, reason, *, evidence_ref=None
        ):
            calls.append(("supersede_claim", reason))
            # All four verbs hand the client the TYPED model (the claim verbs
            # used to hand it a `mode="python"` dict — an inconsistency, and one
            # that can carry non-JSON values into the request body). The client
            # serializes it wire-safely itself.
            assert isinstance(evidence_ref, contracts.EvidenceRef)
            assert evidence_ref.source == "test"
            assert evidence_ref.source_record_id == "cli-evidence"
            return contracts.ClaimLifecycleResult(
                action="supersede",
                claim={"claim_id": claim_id},
                successor={"claim_id": successor_claim_id},
                reason=reason,
                receipt_id="RCP-claim-supersede",
            )

        def retract_claim(self, instance_id, claim_id, reason, *, evidence_ref=None):
            calls.append(("retract_claim", reason))
            return contracts.ClaimLifecycleResult(
                action="retract",
                claim={"claim_id": claim_id},
                reason=reason,
                receipt_id="RCP-claim-retract",
            )

        def supersede_entity(
            self,
            instance_id,
            entity_type,
            entity_id,
            successor_entity_type,
            successor_entity_id,
            reason,
            *,
            evidence_ref=None,
        ):
            calls.append(("supersede_entity", reason))
            return contracts.EntityLifecycleResult(
                action="supersede",
                entity={"entity_type": entity_type, "entity_id": entity_id},
                successor={
                    "entity_type": successor_entity_type,
                    "entity_id": successor_entity_id,
                },
                reason=reason,
                receipt_id="RCP-entity-supersede",
            )

        def retire_entity(self, instance_id, entity_type, entity_id, reason, *, evidence_ref=None):
            calls.append(("retire_entity", reason))
            return contracts.EntityLifecycleResult(
                action="retire",
                entity={"entity_type": entity_type, "entity_id": entity_id},
                reason=reason,
                stranded_live_edge_count=2,
                receipt_id="RCP-entity-retire",
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    runner = CliRunner()
    prefix = ["--server-url", "http://server", "--instance-id", "inst-1"]
    commands = [
        [
            *prefix,
            "relationship",
            "supersede",
            "CLM-old",
            "CLM-new",
            "--reason",
            "replacement",
            "--evidence-ref",
            '{"source":"test","source_record_id":"cli-evidence"}',
            "--json",
        ],
        [
            *prefix,
            "relationship",
            "retract",
            "CLM-other",
            "--reason",
            "withdrawn",
            "--json",
        ],
        [
            *prefix,
            "entity",
            "supersede",
            "Part",
            "old",
            "Part",
            "new",
            "--reason",
            "renamed",
            "--json",
        ],
        [
            *prefix,
            "entity",
            "retire",
            "Part",
            "gone",
            "--reason",
            "discontinued",
            "--json",
        ],
    ]
    results = [runner.invoke(cli, command) for command in commands]
    for result in results:
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["receipt_id"]
    assert calls == [
        ("supersede_claim", "replacement"),
        ("retract_claim", "withdrawn"),
        ("supersede_entity", "renamed"),
        ("retire_entity", "discontinued"),
    ]


def test_quickfix_lifecycle_help_names_real_verbs() -> None:
    runner = CliRunner()
    expected = {
        ("entity", "add"): ("cruxible entity retire", "cruxible entity supersede"),
        ("entity", "update"): ("cruxible entity retire", "cruxible entity supersede"),
        ("relationship", "add"): (
            "cruxible relationship retract",
            "cruxible relationship supersede",
        ),
        ("relationship", "update"): (
            "cruxible relationship retract",
            "cruxible relationship supersede",
        ),
    }
    for path, verbs in expected.items():
        result = runner.invoke(cli, [*path, "--help"])
        assert result.exit_code == 0, result.output
        assert "wi-lifecycle-verbs" not in result.output
        normalized = " ".join(result.output.split())
        assert all(verb in normalized for verb in verbs)
