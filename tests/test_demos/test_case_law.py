"""Integration tests for the case-law monitoring demo providers and workflows."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.composer import compose_config_files
from cruxible_core.config.loader import save_config
from cruxible_core.demo_providers.case_law import (
    extract_matter_statutes,
    load_case_outcomes,
    load_firm_seed_data,
    load_public_courtlistener_rows,
)
from cruxible_core.provider.types import ProviderContext, ResolvedArtifact
from cruxible_core.service import (
    service_apply_workflow,
    service_lock,
    service_propose_workflow,
    service_query,
    service_resolve_group,
    service_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_LAW_DIR = REPO_ROOT / "demos" / "case-law-monitoring"


def _provider_context(artifact_path: Path | None) -> ProviderContext:
    artifact = None
    if artifact_path is not None:
        artifact = ResolvedArtifact(
            name="bundle",
            kind="directory",
            uri=str(artifact_path),
            local_path=str(artifact_path),
            sha256="sha256:test",
        )
    return ProviderContext(
        workflow_name="test",
        step_id="provider",
        provider_name="provider",
        provider_version="1.0.0",
        artifact=artifact,
    )


def _composed_case_law_config_path(tmp_path: Path) -> Path:
    composed = compose_config_files(
        base_path=CASE_LAW_DIR / "courtlistener-reference.yaml",
        overlay_path=CASE_LAW_DIR / "config.yaml",
    )
    config_path = tmp_path / "config.yaml"
    save_config(composed, config_path)
    return config_path


def _apply_canonical_workflow(instance: CruxibleInstance, workflow_name: str) -> None:
    preview = service_run(instance, workflow_name, {})
    assert preview.mode == "preview"
    assert preview.apply_digest is not None

    applied = service_apply_workflow(
        instance,
        workflow_name,
        {},
        expected_apply_digest=preview.apply_digest or "",
        expected_head_snapshot_id=preview.head_snapshot_id,
    )
    assert applied.committed_snapshot_id is not None


def _approve_workflow_group(instance: CruxibleInstance, workflow_name: str) -> int:
    proposed = service_propose_workflow(instance, workflow_name, {})
    assert proposed.group_id is not None
    resolved = service_resolve_group(instance, proposed.group_id, "approve")
    assert resolved.edges_created > 0
    return resolved.edges_created


# ---------------------------------------------------------------------------
# Unit tests for individual providers
# ---------------------------------------------------------------------------


def test_load_public_courtlistener_rows_reads_all_tables() -> None:
    payload = load_public_courtlistener_rows(
        {}, _provider_context(CASE_LAW_DIR / "data")
    )
    assert set(payload) == {
        "courts",
        "judges",
        "dockets",
        "opinions",
        "filings",
        "statutes",
        "opinion_docket",
        "opinion_judge",
        "opinion_statute",
        "opinion_citation",
        "filing_docket",
        "docket_court",
    }
    assert len(payload["courts"]) == 8
    assert len(payload["opinions"]) == 12
    assert len(payload["statutes"]) == 8
    # Check type parsing
    assert payload["judges"][0]["active"] is True
    assert isinstance(payload["filings"][0]["entry_number"], int)
    assert isinstance(payload["opinion_citation"][0]["depth"], int)


def test_load_firm_seed_data_reads_all_tables() -> None:
    payload = load_firm_seed_data(
        {}, _provider_context(CASE_LAW_DIR / "data" / "seed")
    )
    assert set(payload) == {
        "matters",
        "clients",
        "attorneys",
        "practice_areas",
        "positions",
        "deadlines",
        "matter_client",
        "matter_attorney",
        "matter_practice_area",
        "matter_position",
        "matter_deadline",
    }
    assert len(payload["matters"]) == 6
    assert len(payload["positions"]) == 8


def test_load_case_outcomes_derives_outcome_matter_edges() -> None:
    payload = load_case_outcomes(
        {}, _provider_context(CASE_LAW_DIR / "data" / "outcomes")
    )
    assert set(payload) == {"outcomes", "outcome_matter", "outcome_position"}
    assert len(payload["outcomes"]) == 3
    assert len(payload["outcome_matter"]) == 3
    assert len(payload["outcome_position"]) == 3
    # Check prevailed parsing
    assert payload["outcome_position"][0]["prevailed"] is True


def test_extract_matter_statutes_matches_by_keyword() -> None:
    payload = extract_matter_statutes(
        {
            "matters": [
                {
                    "entity_id": "MAT-1",
                    "properties": {
                        "title": "Acme Corp v. Consolidated Tech"
                        " - Sherman Act vertical restraint challenge",
                    },
                }
            ],
            "statutes": [
                {
                    "entity_id": "STAT-1",
                    "properties": {
                        "title": "Sherman Antitrust Act",
                        "citation": "15 USC 1-7",
                    },
                },
                {
                    "entity_id": "STAT-6",
                    "properties": {
                        "title": "Title VII Civil Rights Act",
                        "citation": "42 USC 2000e",
                    },
                },
            ],
        },
        _provider_context(None),
    )
    assert len(payload["candidates"]) >= 1
    matched_statutes = {c["statute_id"] for c in payload["candidates"]}
    assert "STAT-1" in matched_statutes
    assert "STAT-6" not in matched_statutes


# ---------------------------------------------------------------------------
# End-to-end workflow test
# ---------------------------------------------------------------------------


def test_case_law_demo_workflows_run_end_to_end(tmp_path: Path) -> None:
    config_path = _composed_case_law_config_path(tmp_path)
    instance = CruxibleInstance.init(tmp_path, str(config_path))

    lock_result = service_lock(instance)
    assert lock_result.providers_locked >= 7

    # Step 1: Build reference graph
    _apply_canonical_workflow(instance, "build_public_courtlistener_reference")

    graph = instance.load_graph()
    assert graph.entity_count("Court") == 8
    assert graph.entity_count("Judge") == 8
    assert graph.entity_count("Docket") == 10
    assert graph.entity_count("Opinion") == 12
    assert graph.entity_count("Filing") == 15
    assert graph.entity_count("Statute") == 8
    assert graph.edge_count("docket_in_court") == 10
    assert graph.edge_count("opinion_on_docket") == 12
    assert graph.edge_count("interprets") == 18
    assert graph.edge_count("cites") == 15

    # Step 2: Build fork state
    _apply_canonical_workflow(instance, "build_firm_case_state")

    graph = instance.load_graph()
    assert graph.entity_count("Matter") == 6
    assert graph.entity_count("Client") == 4
    assert graph.entity_count("Attorney") == 5
    assert graph.entity_count("PracticeArea") == 3
    assert graph.entity_count("Position") == 8
    assert graph.entity_count("Deadline") == 6
    assert graph.edge_count("matter_for_client") == 6
    assert graph.edge_count("matter_assigned_to") == 6
    assert graph.edge_count("matter_has_position") == 8
    assert graph.edge_count("matter_has_deadline") == 6

    # Step 3: Record case outcomes
    _apply_canonical_workflow(instance, "record_case_outcomes")

    graph = instance.load_graph()
    assert graph.entity_count("CaseOutcome") == 3
    assert graph.edge_count("outcome_of_matter") == 3
    assert graph.edge_count("outcome_resolved_position") == 3

    # Step 4: Propose matter-statute links
    _approve_workflow_group(instance, "propose_matter_statutes")

    graph = instance.load_graph()
    assert graph.edge_count("matter_turns_on_statute") > 0

    # Step 5: Propose opinion impact
    _approve_workflow_group(instance, "propose_opinion_impact")

    graph = instance.load_graph()
    assert graph.edge_count("opinion_affects_matter") > 0

    # Step 6: Propose position authority
    _approve_workflow_group(instance, "propose_position_authority")

    graph = instance.load_graph()
    assert graph.edge_count("opinion_supports_position") > 0

    # Step 7: Propose filing responses
    _approve_workflow_group(instance, "propose_filing_response")

    graph = instance.load_graph()
    assert graph.edge_count("filing_requires_response") > 0

    # Verify named queries return results
    # Find an opinion that affects a matter for impacted_matters query
    opinion_matter_edge = graph.list_edges("opinion_affects_matter")[0]
    impacted = service_query(
        instance, "impacted_matters", {"opinion_id": opinion_matter_edge["from_id"]}
    )
    assert impacted.total_results > 0

    # deadline_watch: find an attorney with deadlines
    deadline_watch = service_query(
        instance, "deadline_watch", {"attorney_id": "ATT-1"}
    )
    assert deadline_watch.total_results > 0

    # position_track_record: find outcomes for a resolved position
    track_record = service_query(
        instance, "position_track_record", {"position_id": "POS-3"}
    )
    assert track_record.total_results > 0
