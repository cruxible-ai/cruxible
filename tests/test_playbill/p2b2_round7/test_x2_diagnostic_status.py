"""Round-7 regressions for operator-readable observation diagnostics."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator


def test_available_lane_detail_surfaces_the_bounded_diagnostic_summary(
    short_root: Path,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    store.record_diagnostic(
        ProviderLocalRuntimeRefused("provider_process_lease_invalid", "first table hiccup")
    )
    store.record_diagnostic(
        ProviderLocalRuntimeRefused("provider_process_lease_invalid", "last table hiccup")
    )

    state, code, detail = operator.lane_status()

    assert (state, code) == ("available", None)
    assert detail is not None
    assert "observation_diagnostics: count=2" in detail
    assert "provider_process_lease_invalid" in detail
    assert "last table hiccup" in detail
    projected = contracts.ProviderLaneStatusV1(state=state, code=code, detail=detail)
    assert projected.detail == detail


def test_cli_status_renders_available_lane_diagnostics(
    short_root: Path,
    monkeypatch,
) -> None:
    detail = (
        "observation_diagnostics: count=2; last=provider_process_lease_invalid: last table hiccup"
    )

    class StubClient:
        def server_info(self) -> contracts.ServerInfoResult:
            return contracts.ServerInfoResult(
                server_required=False,
                state_root=str(short_root),
                version="0.5.1",
                instance_count=1,
                auth_enabled=False,
                auth_required=False,
                provider_lane=contracts.ProviderLaneStatusV1(
                    state="available",
                    code=None,
                    detail=detail,
                ),
            )

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(short_root / "cli-context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands.server._get_client", lambda: StubClient())

    result = CliRunner().invoke(
        cli,
        ["--server-url", "http://server", "server", "status"],
    )

    assert result.exit_code == 0, result.output
    assert "Provider lane: available" in result.output
    assert f"Provider lane detail: {detail}" in result.output


def test_observation_diagnostics_are_documented_without_a_new_status_field() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    docs = (repository_root / "docs/cli-reference.md").read_text(encoding="utf-8")
    changelog = (repository_root / "CHANGELOG.md").read_text(encoding="utf-8")
    contract = (
        repository_root / "packages/cruxible-client/src/cruxible_client/contracts/__init__.py"
    ).read_text(encoding="utf-8")

    assert "observation-diagnostic count and last typed message" in docs
    assert "bounded observation diagnostics in Provider-lane detail" in docs
    assert "bounded count and last typed message" in changelog
    assert "observation_diagnostics" not in contract
