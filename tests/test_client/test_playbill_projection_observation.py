"""One-scanner projection observations and conservative coverage-card folds."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import contracts as api
from cruxible_client.authoring.workspace import (
    observe_playbill_next_workspace,
    observe_playbill_next_workspace_with_coverage,
    observe_playbill_projection_coverage,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_CARDS_PER_SOURCE,
    MAX_PROJECTION_SCAN_BYTES,
    MAX_PROJECTION_SOURCE_BYTES,
)
from tests.test_client.test_playbill_projection_repin import (
    COORDINATE,
    _repin,
    _RepinClient,
    _workspace,
)

SOURCE = {
    "tag": "playbill-logical-source-identity-v1",
    "plane": "external",
    "identity": "corpus.runbook",
}
PROFILE = {
    "tag": "playbill-coverage-access-profile-v1",
    "profile_id": "sdk-default",
    "permitted_access_classes": ["instance", "public"],
    "disclose_restricted_existence": True,
}


class _CoverageClient:
    def __init__(self, *, mode: str = "exact", needle: bytes = b"status: ready") -> None:
        self.mode = mode
        self.needle = needle
        self.calls: list[dict[str, Any]] = []

    def resolve_playbill_coverage(
        self, instance_id: str, **values: Any
    ) -> api.PlaybillCoverageResult:
        assert instance_id == "inst_projection"
        self.calls.append(values)
        (observation,) = values["observations"]
        content = base64.b64decode(observation["content_base64"], validate=True)
        start = content.index(self.needle)
        digest = "sha256:" + hashlib.sha256(self.needle).hexdigest()
        identity = typed_digest(
            Sha256Value,
            "playbill-coverage-occurrence-identity-v1",
            {"source": SOURCE, "observed_commitment_digest": digest, "ordinal": 0},
        ).tagged
        overlay = {
            "tag": "playbill-coverage-line-overlay-v1",
            "start_byte": start,
            "end_byte": start + len(self.needle),
            "start_line": 1,
            "end_line": 1,
        }
        card: dict[str, object] = {
            "match_state": "exact",
            "expected_commitment_digest": digest,
            "observed_commitment_digest": digest,
            "accepted_source": dict(SOURCE),
            "observed_source": dict(SOURCE),
            "occurrence_identity_digest": identity,
            "line_overlay": overlay,
            "citation_associations": [],
        }
        span: dict[str, object] = {
            "tag": "playbill-coverage-span-result-v3",
            "request": {"source": dict(SOURCE)},
            "health": "complete",
            "ambiguous_occurrence_count": 0,
            "omitted_card_count": 0,
            "cards": [card],
            "commitment_scan_proofs": [
                {
                    "tag": "playbill-coverage-commitment-scan-proof-v1",
                    "source": dict(SOURCE),
                    "commitment_digest": digest,
                    "byte_length": len(self.needle),
                    "complete": True,
                }
            ],
            "citation_window_observations": [],
        }
        coordinate = COORDINATE.model_dump(mode="json")
        profile: dict[str, object] = {
            **PROFILE,
            "profile_id": "playbill.coverage.read",
        }
        if self.mode == "partial":
            span["health"] = "partial"
            span["commitment_scan_proofs"] = []
        elif self.mode == "ambiguous":
            span["ambiguous_occurrence_count"] = 1
            card["match_state"] = "candidate"
        elif self.mode == "omitted":
            span["omitted_card_count"] = 1
            span["cards"] = []
            span["commitment_scan_proofs"] = []
        elif self.mode == "invalid_card":
            span["cards"] = [None]
        elif self.mode == "observed_source_mismatch":
            card["observed_source"] = {**SOURCE, "identity": "corpus.other"}
        elif self.mode == "accepted_source_mismatch":
            card["accepted_source"] = {**SOURCE, "identity": "corpus.other"}
        elif self.mode == "invalid_expected_digest":
            card["expected_commitment_digest"] = "not-a-digest"
        elif self.mode == "bad_overlay":
            overlay["start_byte"] = start + 1
        elif self.mode == "bad_identity":
            card["occurrence_identity_digest"] = "sha256:" + "f" * 64
        elif self.mode == "denied":
            profile["permitted_access_classes"] = ["public"]
        elif self.mode == "mismatched_coordinate":
            coordinate = {**coordinate, "git_oid": "f" * 64}
        elif self.mode == "missing_span":
            span["request"] = {
                "source": {**SOURCE, "identity": "corpus.other"},
            }
        elif self.mode == "same_source_candidate":
            card["match_state"] = "candidate"
            span["commitment_scan_proofs"] = []

        return api.PlaybillCoverageResult(
            coordinate=COORDINATE,
            result={"at": coordinate, "access_profile": profile, "spans": [span]},
        )


def _observe(
    client: _CoverageClient,
    root: Path,
    *,
    profile: Mapping[str, Any] = PROFILE,
) -> tuple[dict[str, object], api.PlaybillAcceptedCoordinate | None]:
    return observe_playbill_next_workspace_with_coverage(
        client,
        "inst_projection",
        root,
        coordinate=COORDINATE,
        access_profile=profile,
    )


def test_one_coordinate_pinned_coverage_read_enriches_existing_v1_without_replacing_it(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    _repin(_RepinClient(), tmp_path, claims=("CLM-first",))
    old = observe_playbill_next_workspace(tmp_path)
    client = _CoverageClient()

    enriched, coordinate = _observe(client, tmp_path)

    assert coordinate == COORDINATE
    assert len(client.calls) == 1
    assert client.calls[0]["at"] == COORDINATE
    assert client.calls[0]["budget"]["max_cards_per_span"] == MAX_PROJECTION_CARDS_PER_SOURCE
    assert client.calls[0]["scan_budget"]["max_scanned_bytes"] == MAX_PROJECTION_SCAN_BYTES
    assert old["source_observations"] == [
        {
            "source_id": "corpus.runbook",
            "document_id": "runbook",
            "observed_source_digest": enriched["source_observations"][0]["observed_source_digest"],
        }
    ]
    (source,) = enriched["source_observations"]
    assert source["tag"] == "playbill-next-source-observation-v4"
    assert source["marker_summaries"][0]["stamp"]["block_id"] == "summary"
    assert len(source["occurrences"]) == 1
    assert (
        source["commitment_scan_proofs"][0]["commitment_digest"]
        == (source["occurrences"][0]["observed_commitment_digest"])
    )


def test_relocated_unique_citation_keeps_identity_while_presentation_offsets_move(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path)
    _repin(_RepinClient(), tmp_path, claims=("CLM-first",))
    first, _ = _observe(_CoverageClient(), tmp_path)

    source.write_bytes(b"unrelated prefix\n" + source.read_bytes())
    moved, _ = _observe(_CoverageClient(), tmp_path)

    before = first["source_observations"][0]["occurrences"][0]
    after = moved["source_observations"][0]["occurrences"][0]
    assert before["identity_digest"] == after["identity_digest"]
    assert before["line_overlay"]["start_byte"] != after["line_overlay"]["start_byte"]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("partial", "coverage_partial"),
        ("ambiguous", "coverage_occurrence_ambiguous"),
        ("omitted", "coverage_cards_omitted"),
        ("bad_overlay", "coverage_occurrence_invalid"),
        ("bad_identity", "coverage_occurrence_ambiguous"),
        ("invalid_card", "coverage_card_invalid"),
        ("observed_source_mismatch", "coverage_source_mismatch"),
        ("accepted_source_mismatch", "coverage_source_mismatch"),
        ("invalid_expected_digest", "coverage_card_invalid"),
        ("denied", "coverage_access_mismatch"),
        ("mismatched_coordinate", "coverage_coordinate_mismatch"),
        ("missing_span", "coverage_span_missing"),
        ("same_source_candidate", "coverage_occurrence_unverified"),
    ],
)
def test_partial_ambiguous_denied_or_unverified_coverage_is_explicitly_unobserved(
    tmp_path: Path, mode: str, reason: str
) -> None:
    _workspace(tmp_path)
    client = _CoverageClient(mode=mode)

    observation, coordinate = _observe(client, tmp_path)

    (source,) = observation["source_observations"]
    assert reason in source["scan_notes"]
    if mode == "ambiguous":
        assert len(source["occurrences"]) == 1
    else:
        assert source["occurrences"] == []
    if mode == "partial":
        assert source["commitment_scan_proofs"] == []
    if mode in {
        "bad_overlay",
        "bad_identity",
        "invalid_card",
        "observed_source_mismatch",
        "accepted_source_mismatch",
        "invalid_expected_digest",
    }:
        assert source["commitment_scan_proofs"] == []
    if mode == "mismatched_coordinate":
        assert coordinate is None


def test_unstamped_bootstrap_has_its_own_note_and_is_never_an_invented_declaration(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)

    observation, _ = _observe(_CoverageClient(), tmp_path)

    (source,) = observation["source_observations"]
    assert source["scan_notes"] == []
    assert source["marker_summaries"] == []
    assert source["marker_notes"] == ["projection_block_unstamped"]


def test_malformed_marker_keeps_its_invalid_grammar_note(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    source.write_bytes(
        source.read_bytes().replace(
            b"<!-- /playbill:block:summary -->\n",
            b"<!-- /playbill:block:different -->\n",
        )
    )

    observation, _ = _observe(_CoverageClient(), tmp_path)

    (source_observation,) = observation["source_observations"]
    assert source_observation["scan_notes"] == []
    assert source_observation["marker_summaries"] == []
    assert source_observation["marker_notes"] == ["projection_marker_invalid"]


def test_missing_catalog_never_calls_coverage(tmp_path: Path) -> None:
    client = _CoverageClient()

    observation, coordinate = _observe(client, tmp_path)

    assert "source_observations" not in observation
    assert coordinate is None
    assert client.calls == []


def test_projection_coverage_combines_claim_markers_and_procedure_catalog(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    _repin(_RepinClient(), tmp_path, claims=("CLM-first",))
    catalog = tmp_path / ".playbill" / "sources.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + """\
  - kind: procedure
    procedure_identity:
      kind: Procedure
      name: release-guard
    locator: runbooks/release-guard.md
""",
        encoding="utf-8",
    )

    observed = observe_playbill_projection_coverage(tmp_path, coordinate=COORDINATE)

    assert observed is not None
    assert observed["coordinate"] == COORDINATE.model_dump(mode="json")
    assert observed["complete_kinds"] == ["Claim", "Procedure"]
    assert [item["artifact"] for item in observed["bindings"]] == [
        {"kind": "Claim", "name": "CLM-first"},
        {"kind": "Procedure", "name": "release-guard"},
    ]


def test_malformed_claim_markers_never_assert_claim_absence_but_leave_catalog_complete(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path)
    source.write_bytes(source.read_bytes().replace(b"/playbill:block", b"/playbill:broken"))
    catalog = tmp_path / ".playbill" / "sources.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + """\
  - kind: procedure
    procedure_identity:
      kind: Procedure
      name: release-guard
    locator: runbooks/release-guard.md
""",
        encoding="utf-8",
    )

    observed = observe_playbill_projection_coverage(tmp_path, coordinate=COORDINATE)

    assert observed is not None
    assert observed["complete_kinds"] == ["Procedure"]
    assert observed["bindings"] == [
        {
            "artifact": {"kind": "Procedure", "name": "release-guard"},
            "workspace_path": "runbooks/release-guard.md",
            "evidence_kind": "procedure_catalog",
        }
    ]


def test_oversized_source_remains_unobserved_without_calling_coverage(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    source.write_bytes(b"x" * (MAX_PROJECTION_SOURCE_BYTES + 1))
    client = _CoverageClient()

    observation, coordinate = _observe(client, tmp_path)

    assert observation["source_observations"] == []
    assert coordinate is None
    assert client.calls == []
