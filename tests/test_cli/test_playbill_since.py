"""CLI parity for ``playbill since``."""

from __future__ import annotations

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


def test_cli_since_calls_the_frozen_client_operation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, int]] = []
    coordinate = contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    values = {
        "coordinate": coordinate.model_dump(mode="json"),
        "generation": 6,
        "rows": [],
        "next_cursor": None,
        "truncated": False,
    }

    class StubClient:
        def since_playbill(self, instance_id: str, *, generation: int, **_values: object):
            calls.append((instance_id, generation))
            return contracts.PlaybillSinceResult.model_validate(
                {
                    **values,
                    "result_digest": contracts._since_digest(  # type: ignore[attr-defined]
                        "playbill-since-result-v1", values
                    ),
                }
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://since.example.test",
            "--instance-id",
            "inst_since",
            "playbill",
            "since",
            "3",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("inst_since", 3)]
    assert '"tag": "playbill-since-result-v1"' in result.output
