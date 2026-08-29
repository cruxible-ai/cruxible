"""One-call CLI Claim attestation signs locally before evidence append."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli
from tests.test_client._attestation_support import ServiceAttestationClient
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world


def test_cli_claim_attest_uses_the_real_local_key_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    client = ServiceAttestationClient(
        instance,
        actor_id="owner",
        state_dir=tmp_path / "server-state",
    )
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY_PATH", str(owner.private_key_path))

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            instance.descriptor.instance_id,
            "playbill",
            "claim",
            "attest",
            claim_id,
            "--support",
            "--note",
            "examined through CLI",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["tag"] == "playbill-claim-attestation-append-result-v1"
    assert len(instance.claim_attestation_evidence_store().events()) == 1
    serialized = json.dumps(payload, sort_keys=True)
    assert str(owner.private_key_path) not in serialized


@pytest.mark.parametrize("flags", [[], ["--support", "--unsure"]])
def test_cli_claim_attest_requires_exactly_one_stance(flags: list[str]) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_test",
            "playbill",
            "claim",
            "attest",
            "CLM-" + "a" * 32,
            *flags,
        ],
    )
    assert result.exit_code == 2
