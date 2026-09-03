#!/usr/bin/env python3
"""Rebuild deterministic candidate-card renderer vectors."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.candidate_cards import (
    CARD_RENDERER_DIGEST,
    CARD_TEMPLATE_DIGESTS,
    candidate_card_path,
    render_candidate_card,
    render_removal_card,
)
from cruxible_core.playbill.projection_artifacts import P2_C_ARTIFACT_KINDS

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests/goldens/playbill/card-renderer-v1.json"


def main() -> None:
    artifact_path = "procedures/demo.json"
    artifact = {
        "tag": "playbill-procedure-card-fixture-v1",
        "identity": {"kind": "Procedure", "name": "demo"},
        "purpose": "exercise deterministic card rendering",
    }
    artifact_bytes = canonical_bytes(artifact)
    payload = {
        "artifact": artifact,
        "artifact_path": artifact_path,
        "card_path": candidate_card_path(artifact_path),
        "rendered": render_candidate_card(
            artifact_path,
            artifact_bytes,
            artifact_kinds=P2_C_ARTIFACT_KINDS,
        ).decode("utf-8"),
        "removal": render_removal_card(coordinate="a" * 40).decode("utf-8"),
        "renderer_digest": CARD_RENDERER_DIGEST,
        "template_digests": list(CARD_TEMPLATE_DIGESTS),
    }
    TARGET.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
