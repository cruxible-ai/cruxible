"""CLI parity for ``playbill since``."""

from __future__ import annotations

from pathlib import Path

import httpx
from click.testing import CliRunner

from cruxible_client import CruxibleClient, contracts
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


def test_cli_since_surfaces_a_typed_profile_refusal_without_traceback(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"tag":"playbill-coverage-access-profile-v1",'
        '"profile_id":"cli-since","permitted_access_classes":["unknown"],'
        '"disclose_restricted_existence":false}',
        encoding="utf-8",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid request reached the transport")

    client = CruxibleClient(base_url="https://since.example.test")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="https://since.example.test",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

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
            "--access-profile",
            str(profile),
        ],
    )

    assert result.exit_code == 1
    assert "PlaybillSinceRequestInvalid" in result.output
    assert "playbill.since.request_invalid" in result.output
    assert "$.access_profile" in result.output
    assert "Traceback" not in result.output


def test_cli_since_accepts_a_profile_file_relying_on_model_defaults(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    profiles: list[dict] = []
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
        def since_playbill(self, instance_id: str, *, access_profile, **_values: object):  # type: ignore[no-untyped-def]
            profiles.append(dict(access_profile))
            return contracts.PlaybillSinceResult.model_validate(
                {
                    **values,
                    "result_digest": contracts._since_digest(  # type: ignore[attr-defined]
                        "playbill-since-result-v1", values
                    ),
                }
            )

    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text(
        "profile_id: cli-custom\npermitted_access_classes: [instance, public]\n"
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
            "--access-profile",
            str(profile_file),
        ],
    )

    assert result.exit_code == 0, result.output
    (profile,) = profiles
    assert profile["tag"] == "playbill-coverage-access-profile-v1"
    assert profile["disclose_restricted_existence"] is True
