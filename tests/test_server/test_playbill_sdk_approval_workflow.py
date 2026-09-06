"""The public example composes real V3 review, signature, acceptance and repair."""

from __future__ import annotations

import runpy
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from cruxible_client import LocalEd25519ApprovalSigner, Playbill
from cruxible_client.transport.http import CruxibleClient


def test_public_claim_review_and_repair_example(playbill_http, tmp_path: Path) -> None:
    http, instance_id, private_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http
    workspace = tmp_path / "demo-workspace"
    workspace.mkdir()
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=workspace)
    private = serialization.load_ssh_private_key(private_path.read_bytes(), password=None)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=private_path,
        expected_public_key=private.public_key().public_bytes_raw().hex(),
        forbidden_roots=(workspace, tmp_path / "server-state"),
    )
    example = (
        Path(__file__).resolve().parents[2]
        / "packages/cruxible-client/examples/claim_review_repair.py"
    )
    run = runpy.run_path(str(example))["run"]
    reviews = []

    def approve(reviewed):
        details = reviewed.details
        assert details.candidate["tag"] == "playbill-validated-candidate-v3"
        assert details.members and not details.redactions
        assert details.provenance["actor_id"] == "operator"
        reviews.append(details.candidate_digest)
        return True

    result = run(pb, workspace, signer, review_decision=approve, accept_decision=lambda _: True)
    assert len(reviews) == 2 and reviews[0] != reviews[1]
    assert result["value"] == 49 and result["verdict"] == "supported"
    assert result["revision"] == 2
