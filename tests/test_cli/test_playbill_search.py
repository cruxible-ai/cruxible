"""CLI search/list/orient remain thin headless adapters."""

from __future__ import annotations

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_search_and_orient_call_the_same_wire(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str | None]] = []

    class StubClient:
        def search_playbill(
            self,
            instance_id: str,
            *,
            mode: str,
            query: str | None,
            **_values: object,
        ) -> contracts.PlaybillSearchResult:
            calls.append((mode, query))
            return contracts.PlaybillSearchResult(
                mode=mode,  # type: ignore[arg-type]
                coordinate=COORDINATE,
                evaluation_time="2026-08-21T14:00:00.000000Z",
                rows=[],
                orientation=(
                    None
                    if mode != "orient"
                    else {
                        "generation": 1,
                        "counts_by_kind": [{"key": "demand", "count": 0}],
                        "kind_availability": [{"kind": "demand", "availability": "not_installed"}],
                    }
                ),
                selection_basis_digest="sha256:" + "5" * 64,
                truncated=False,
                result_digest="sha256:" + "6" * 64,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    base = [
        "--server-url",
        "https://search.example.test",
        "--instance-id",
        "inst_search",
        "playbill",
    ]
    runner = CliRunner()
    searched = runner.invoke(cli, [*base, "search", "release", "--kind", "brief", "--json"])
    oriented = runner.invoke(cli, [*base, "orient"])

    assert searched.exit_code == 0, searched.output
    assert oriented.exit_code == 0, oriented.output
    assert calls == [("search", "release"), ("orient", None)]
    assert "demand: not_installed" in oriented.output
