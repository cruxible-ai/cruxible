"""CLI tests for workflow lock/plan/run/test commands."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli
from cruxible_core.config.loader import load_config
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service import service_create_snapshot
from cruxible_core.workflow.compiler import compute_lock_config_digest

_KITS_ROOT = Path(__file__).resolve().parents[2] / "kits"


def _copy_kit(tmp_path: Path, kit_name: str) -> Path:
    kit_dir = tmp_path / kit_name
    shutil.copytree(_KITS_ROOT / kit_name, kit_dir)
    return kit_dir


def _read_lock_yaml(kit_dir: Path) -> dict[str, object]:
    payload = yaml.safe_load((kit_dir / "cruxible.lock.yaml").read_text())
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _chdir_run(runner: CliRunner, directory: Path, args: list[str]) -> object:
    original = os.getcwd()
    try:
        os.chdir(directory)
        return runner.invoke(cli, args)
    finally:
        os.chdir(original)


def _assert_local_mutation_disabled(
    runner: CliRunner,
    directory: Path,
    args: list[str],
    label: str,
) -> None:
    result = _chdir_run(runner, directory, args)
    assert result.exit_code == 2
    assert f"Local mutation disabled for {label}" in result.output


@pytest.fixture
def workflow_project(tmp_path: Path, workflow_config_yaml: str) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(workflow_config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Product",
            entity_id="SKU-123",
            properties={"sku": "SKU-123", "category": "soda"},
        )
    )
    instance.save_graph(graph)
    return instance


@pytest.fixture
def workflow_input_file(workflow_project: CruxibleInstance) -> Path:
    path = workflow_project.root / "input.yaml"
    path.write_text("sku: SKU-123\nstart_date: '2026-03-01'\nend_date: '2026-03-07'\n")
    return path


@pytest.fixture
def proposal_workflow_project(
    tmp_path: Path, proposal_workflow_config_yaml: str
) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(proposal_workflow_config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Campaign",
            entity_id="CMP-1",
            properties={"campaign_id": "CMP-1", "region": "north"},
        )
    )
    for sku in ("SKU-123", "SKU-456"):
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id=sku,
                properties={"sku": sku, "category": "beverages"},
            )
        )
    instance.save_graph(graph)
    return instance


@pytest.fixture
def proposal_input_file(proposal_workflow_project: CruxibleInstance) -> Path:
    path = proposal_workflow_project.root / "input.yaml"
    path.write_text("campaign_id: CMP-1\n")
    return path


@pytest.fixture
def canonical_input_file(canonical_workflow_instance: CruxibleInstance) -> Path:
    path = canonical_workflow_instance.root / "input.yaml"
    path.write_text("{}\n")
    return path


class TestWorkflowCli:
    def test_lock_writes_lock_file(
        self, runner: CliRunner, workflow_project: CruxibleInstance
    ) -> None:
        result = _chdir_run(runner, workflow_project.root, ["lock"])
        assert result.exit_code == 0
        assert (workflow_project.root / ".cruxible" / "cruxible.lock.yaml").exists()
        assert "digest=" in result.output

    def test_lock_kit_dir_writes_standalone_kit_lock(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CRUXIBLE_KIT_DEV_RESOLVE", raising=False)
        kit_dir = _copy_kit(tmp_path, "kev-reference")
        (kit_dir / "cruxible.lock.yaml").unlink()

        result = runner.invoke(cli, ["lock", "--kit-dir", str(kit_dir)])

        assert result.exit_code == 0, result.output
        lock_path = kit_dir / "cruxible.lock.yaml"
        assert lock_path.exists()
        payload = _read_lock_yaml(kit_dir)
        assert result.output.startswith(f"Wrote lock file to {lock_path}")
        assert f"digest={payload['lock_digest']}" in result.output
        assert "providers=2 artifacts=1" in result.output
        assert "CRUXIBLE_KIT_DEV_RESOLVE" not in os.environ

    def test_lock_kit_dir_pins_kit_layer_only(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The kit-root lock pins the kit LAYER, not the manifest-composed
        # state: composing would embed base-kit providers and machine-absolute
        # artifact URIs into a committed, distributable file. Deliberately no
        # base sibling here — an overlay must lock without its target_state
        # base present.
        monkeypatch.delenv("CRUXIBLE_KIT_DEV_RESOLVE", raising=False)
        overlay_dir = _copy_kit(tmp_path, "case-law-monitoring")
        committed = _read_lock_yaml(overlay_dir)
        (overlay_dir / "cruxible.lock.yaml").unlink()

        result = runner.invoke(cli, ["lock", "--kit-dir", str(overlay_dir)])

        assert result.exit_code == 0, result.output
        lock_payload = _read_lock_yaml(overlay_dir)
        layer = load_config(overlay_dir / "config.yaml")
        assert lock_payload["config_digest"] == compute_lock_config_digest(layer)
        # Regen of a pristine kit copy is a noop (timestamp aside).
        assert lock_payload["lock_digest"] == committed["lock_digest"]
        for name, artifact in lock_payload["artifacts"].items():
            assert not str(artifact["uri"]).startswith("/"), (name, artifact["uri"])
        assert f"digest={lock_payload['lock_digest']}" in result.output
        assert "CRUXIBLE_KIT_DEV_RESOLVE" not in os.environ

    def test_lock_kit_dir_refuses_artifact_digest_mismatch_without_force(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        kit_dir = _copy_kit(tmp_path, "kev-reference")
        (kit_dir / "cruxible.lock.yaml").unlink()
        source_data = kit_dir / "data" / "known_exploited_vulnerabilities.csv"
        source_data.write_text(source_data.read_text() + "\n# local drift\n")

        result = runner.invoke(cli, ["lock", "--kit-dir", str(kit_dir)])

        assert result.exit_code == 1
        assert not (kit_dir / "cruxible.lock.yaml").exists()
        assert "Artifact 'public_kev_bundle' digest mismatch." in result.output
        assert "Run 'cruxible lock --force'" in result.output

    def test_lock_kit_dir_force_accepts_artifact_digest_mismatch(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        kit_dir = _copy_kit(tmp_path, "kev-reference")
        (kit_dir / "cruxible.lock.yaml").unlink()
        original_digest = load_config(kit_dir / "config.yaml").artifacts["public_kev_bundle"].digest
        source_data = kit_dir / "data" / "known_exploited_vulnerabilities.csv"
        source_data.write_text(source_data.read_text() + "\n# accepted local drift\n")

        result = runner.invoke(cli, ["lock", "--kit-dir", str(kit_dir), "--force"])

        assert result.exit_code == 0, result.output
        lock_payload = _read_lock_yaml(kit_dir)
        artifact_payload = lock_payload["artifacts"]
        assert isinstance(artifact_payload, dict)
        public_bundle = artifact_payload["public_kev_bundle"]
        assert isinstance(public_bundle, dict)
        assert public_bundle["digest"] != original_digest
        assert str(public_bundle["digest"]).startswith("sha256:")
        assert f"digest={lock_payload['lock_digest']}" in result.output

    def test_lock_kit_dir_reports_missing_config(self, runner: CliRunner, tmp_path: Path) -> None:
        kit_dir = tmp_path / "empty-kit"
        kit_dir.mkdir()

        result = runner.invoke(cli, ["lock", "--kit-dir", str(kit_dir)])

        assert result.exit_code == 2
        assert f"--kit-dir must contain config.yaml: {kit_dir / 'config.yaml'}" in result.output

    def test_plan_prints_compiled_plan(
        self,
        runner: CliRunner,
        workflow_project: CruxibleInstance,
        workflow_input_file: Path,
    ) -> None:
        _chdir_run(runner, workflow_project.root, ["lock"])
        result = _chdir_run(
            runner,
            workflow_project.root,
            ["plan", "--workflow", "evaluate_promo", "--input-file", str(workflow_input_file)],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["workflow"] == "evaluate_promo"
        assert payload["steps"][1]["provider_version"] == "1.2.0"
        assert payload["steps"][1]["artifact_digest"] == "abc123"

    def test_run_executes_workflow(
        self,
        runner: CliRunner,
        workflow_project: CruxibleInstance,
        workflow_input_file: Path,
    ) -> None:
        _chdir_run(runner, workflow_project.root, ["lock"])
        _assert_local_mutation_disabled(
            runner,
            workflow_project.root,
            ["run", "--workflow", "evaluate_promo", "--input-file", str(workflow_input_file)],
            "run",
        )

    def test_run_supports_inline_input(
        self,
        runner: CliRunner,
        workflow_project: CruxibleInstance,
    ) -> None:
        _chdir_run(runner, workflow_project.root, ["lock"])
        _assert_local_mutation_disabled(
            runner,
            workflow_project.root,
            [
                "run",
                "--workflow",
                "evaluate_promo",
                "--input",
                '{"sku":"SKU-123","start_date":"2026-03-01","end_date":"2026-03-07"}',
            ],
            "run",
        )

    def test_run_uses_empty_input_by_default_for_empty_contract(
        self,
        runner: CliRunner,
        canonical_workflow_instance: CruxibleInstance,
    ) -> None:
        _chdir_run(runner, canonical_workflow_instance.root, ["lock"])
        _assert_local_mutation_disabled(
            runner,
            canonical_workflow_instance.root,
            ["run", "--workflow", "build_reference"],
            "run",
        )

    def test_run_reports_clear_error_for_missing_required_input(
        self,
        runner: CliRunner,
        workflow_project: CruxibleInstance,
    ) -> None:
        _chdir_run(runner, workflow_project.root, ["lock"])
        _assert_local_mutation_disabled(
            runner,
            workflow_project.root,
            ["run", "--workflow", "evaluate_promo"],
            "run",
        )

    def test_test_executes_config_defined_tests(
        self,
        runner: CliRunner,
        workflow_project: CruxibleInstance,
    ) -> None:
        _chdir_run(runner, workflow_project.root, ["lock"])
        result = _chdir_run(runner, workflow_project.root, ["test"])
        assert result.exit_code == 0
        assert "1 passed, 0 failed, 1 total" in result.output
        assert "[PASS] promo_margin_smoke" in result.output

    def test_propose_bridges_workflow_into_candidate_group(
        self,
        runner: CliRunner,
        proposal_workflow_project: CruxibleInstance,
        proposal_input_file: Path,
    ) -> None:
        _chdir_run(runner, proposal_workflow_project.root, ["lock"])
        _assert_local_mutation_disabled(
            runner,
            proposal_workflow_project.root,
            [
                "propose",
                "--workflow",
                "propose_campaign_recommendations",
                "--input-file",
                str(proposal_input_file),
            ],
            "propose",
        )

    def test_snapshot_create_list_and_clone(
        self,
        runner: CliRunner,
        proposal_workflow_project: CruxibleInstance,
        tmp_path: Path,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            proposal_workflow_project.root,
            ["snapshot", "create", "--label", "baseline"],
            "snapshot create",
        )
        snapshot_id = service_create_snapshot(
            proposal_workflow_project,
            label="baseline",
        ).snapshot.snapshot_id

        listed = _chdir_run(runner, proposal_workflow_project.root, ["snapshot", "list"])
        assert listed.exit_code == 0
        assert snapshot_id in listed.output

        clone_root = tmp_path / "cloned-cli"
        _assert_local_mutation_disabled(
            runner,
            proposal_workflow_project.root,
            ["clone", "--snapshot", snapshot_id, "--root-dir", str(clone_root)],
            "clone",
        )

    def test_apply_commits_canonical_workflow(
        self,
        runner: CliRunner,
        canonical_workflow_instance: CruxibleInstance,
        canonical_input_file: Path,
    ) -> None:
        _chdir_run(runner, canonical_workflow_instance.root, ["lock"])
        _assert_local_mutation_disabled(
            runner,
            canonical_workflow_instance.root,
            ["run", "--workflow", "build_reference", "--input-file", str(canonical_input_file)],
            "run",
        )
        _assert_local_mutation_disabled(
            runner,
            canonical_workflow_instance.root,
            [
                "apply",
                "--workflow",
                "build_reference",
                "--input-file",
                str(canonical_input_file),
                "--apply-digest",
                "sha256:test",
            ],
            "apply",
        )

    def test_run_save_preview_writes_file(
        self,
        runner: CliRunner,
        canonical_workflow_instance: CruxibleInstance,
    ) -> None:
        _chdir_run(runner, canonical_workflow_instance.root, ["lock"])
        preview_file = canonical_workflow_instance.root / "preview.json"

        result = _chdir_run(
            runner,
            canonical_workflow_instance.root,
            [
                "run",
                "--workflow",
                "build_reference",
                "--save-preview",
                str(preview_file),
            ],
        )
        assert result.exit_code == 2
        assert "Local mutation disabled for run" in result.output
        assert not preview_file.exists()

    def test_run_save_preview_non_canonical_errors(
        self,
        runner: CliRunner,
        proposal_workflow_project: CruxibleInstance,
        proposal_input_file: Path,
    ) -> None:
        _chdir_run(runner, proposal_workflow_project.root, ["lock"])
        preview_file = proposal_workflow_project.root / "preview.json"

        result = _chdir_run(
            runner,
            proposal_workflow_project.root,
            [
                "run",
                "--workflow",
                "propose_campaign_recommendations",
                "--input-file",
                str(proposal_input_file),
                "--save-preview",
                str(preview_file),
            ],
        )

        assert result.exit_code != 0
        assert "Local mutation disabled for run" in result.output
        assert not preview_file.exists()

    def test_apply_from_preview_file(
        self,
        runner: CliRunner,
        canonical_workflow_instance: CruxibleInstance,
    ) -> None:
        preview_file = canonical_workflow_instance.root / "preview.json"
        preview_file.write_text(
            json.dumps(
                {
                    "kind": "workflow_preview",
                    "version": 1,
                    "workflow": "build_reference",
                    "input": {},
                    "apply_digest": "sha256:test",
                    "head_snapshot_id": "snap_test",
                }
            )
        )
        applied = _chdir_run(
            runner,
            canonical_workflow_instance.root,
            ["apply", "--preview-file", str(preview_file)],
        )
        assert applied.exit_code == 2
        assert "Local mutation disabled for apply" in applied.output

    @pytest.mark.parametrize(
        ("extra_args", "label"),
        [
            (["--workflow", "build_reference"], "workflow"),
            (["--input", "{}"], "input"),
            (["--input-file", "INPUT_FILE"], "input-file"),
            (["--apply-digest", "sha256:manual"], "apply-digest"),
            (["--head-snapshot", "snap_manual"], "head-snapshot"),
            (["--from-last-preview"], "from-last-preview"),
        ],
    )
    def test_apply_preview_file_rejects_mixed_flags(
        self,
        runner: CliRunner,
        canonical_workflow_instance: CruxibleInstance,
        canonical_input_file: Path,
        extra_args: list[str],
        label: str,
    ) -> None:
        preview_file = canonical_workflow_instance.root / f"mixed-{label}.json"
        preview_file.write_text(
            json.dumps(
                {
                    "kind": "workflow_preview",
                    "version": 1,
                    "workflow": "build_reference",
                    "input": {},
                    "apply_digest": "sha256:test",
                    "head_snapshot_id": "snap_test",
                }
            )
        )

        args = ["apply", "--preview-file", str(preview_file)]
        if extra_args == ["--input-file", "INPUT_FILE"]:
            args.extend(["--input-file", str(canonical_input_file)])
        else:
            args.extend(extra_args)

        result = _chdir_run(runner, canonical_workflow_instance.root, args)

        assert result.exit_code != 0
        assert "--preview-file cannot be combined" in result.output

    @pytest.mark.parametrize(
        ("contents", "message"),
        [
            ("{not-json", "is not valid JSON"),
            (json.dumps({"kind": "not_preview", "version": 1}), "unsupported kind"),
            (json.dumps({"kind": "workflow_preview", "version": 2}), "unsupported version"),
        ],
    )
    def test_apply_preview_file_rejects_malformed(
        self,
        runner: CliRunner,
        canonical_workflow_instance: CruxibleInstance,
        contents: str,
        message: str,
    ) -> None:
        preview_file = canonical_workflow_instance.root / "bad-preview.json"
        preview_file.write_text(contents)

        result = _chdir_run(
            runner,
            canonical_workflow_instance.root,
            ["apply", "--preview-file", str(preview_file)],
        )

        assert result.exit_code != 0
        assert message in result.output
