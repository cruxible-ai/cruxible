"""CLI parity for ``cruxible since``."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.main import cli


def test_cli_since_calls_the_frozen_client_operation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, int]] = []
    coordinate = contracts.AcceptedCoordinate(
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
        def since(self, instance_id: str, *, generation: int, **_values: object):
            calls.append((instance_id, generation))
            return contracts.SinceResult.model_validate(
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
            "since",
            "3",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("inst_since", 3)]
    assert '"tag": "playbill-since-result-v1"' in result.output


@pytest.mark.parametrize(
    ("flags", "max_rows"),
    [((), 100), (("--limit", "40"), 40), (("--max-rows", "7"), 7)],
)
def test_cli_since_limit_bounds_the_page(monkeypatch, flags, max_rows) -> None:  # type: ignore[no-untyped-def]
    """``--limit`` is the page bound (``--max-rows`` its wire spelling)."""

    sent: list[object] = []
    coordinate = contracts.AcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    values = {
        "coordinate": coordinate.model_dump(mode="json"),
        "generation": 900,
        "rows": [],
        "next_cursor": None,
        "truncated": False,
    }

    class StubClient:
        def since(self, instance_id: str, *, generation: int, max_rows: int, **_values: object):
            sent.append(max_rows)
            return contracts.SinceResult.model_validate(
                {
                    **values,
                    "result_digest": contracts._since_digest(  # type: ignore[attr-defined]
                        "playbill-since-result-v1", values
                    ),
                }
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    prefix = ["--server-url", "https://since.example.test", "--instance-id", "inst_since"]
    result = CliRunner().invoke(cli, [*prefix, "since", "835", *flags])

    assert result.exit_code == 0, result.output
    assert sent == [max_rows]
    too_many = CliRunner().invoke(cli, [*prefix, "since", "835", "--limit", "1001"])
    assert too_many.exit_code == 2 and "--limit" in too_many.output


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
            "since",
            "3",
            "--access-profile",
            str(profile),
        ],
    )

    assert result.exit_code == 1
    assert "cruxible.since.request_invalid" in result.output
    assert "cruxible.since.request_invalid" in result.output
    assert "$.access_profile" in result.output
    assert "Traceback" not in result.output


def test_cli_since_accepts_a_profile_file_relying_on_model_defaults(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    profiles: list[dict] = []
    coordinate = contracts.AcceptedCoordinate(
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
        def since(self, instance_id: str, *, access_profile, **_values: object):  # type: ignore[no-untyped-def]
            profiles.append(dict(access_profile))
            return contracts.SinceResult.model_validate(
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


def test_cli_since_text_continues_with_a_command_that_runs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """gapb-15: the More line is the next page's command, cursor on stdin."""

    import json
    import shlex

    coordinate = contracts.AcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    cursor = {
        "instance_id": "inst_since",
        "lower_generation": 3,
        "head_coordinate": coordinate.model_dump(mode="json"),
        "access_profile": {
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "cli-since",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": False,
        },
        "max_rows": 1,
        "max_bytes": 65_536,
        "last_generation": 4,
        "last_member_path": "claims/CLM-1.json",
    }
    cursor["cursor_digest"] = contracts._since_digest(  # type: ignore[attr-defined]
        "playbill-since-cursor-v1", cursor
    )
    seen: list[object] = []

    class StubClient:
        def since(self, instance_id: str, *, generation: int, cursor: object, **_values: object):
            seen.append(cursor)
            values = {
                "coordinate": coordinate.model_dump(mode="json"),
                "generation": 6,
                "rows": [],
                "next_cursor": cursor_value,
                "truncated": True,
            }
            return contracts.SinceResult.model_validate(
                {
                    **values,
                    "result_digest": contracts._since_digest(  # type: ignore[attr-defined]
                        "playbill-since-result-v1", values
                    ),
                }
            )

    cursor_value = contracts.SinceCursor.model_validate(cursor).model_dump(mode="json")
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    prefix = ["--server-url", "https://since.example.test", "--instance-id", "inst_since"]
    first = CliRunner().invoke(cli, [*prefix, "since", "3", "--limit", "1"])

    assert first.exit_code == 0, first.output
    (more,) = [line for line in first.output.splitlines() if line.startswith("More: ")]
    command, _, here = more.removeprefix("More: ").partition(" <<< ")
    argv = shlex.split(command)
    assert argv[:3] == ["cruxible", "since", "3"] and argv[-2:] == ["--cursor", "-"]
    # Running it as printed sends the same cursor back.
    second = CliRunner().invoke(cli, [*prefix, *argv[1:]], input=shlex.split(here)[0])
    assert second.exit_code == 0, second.output
    assert json.loads(json.dumps(seen[-1])) == cursor_value
