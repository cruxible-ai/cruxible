"""`retire` on a definition names the form that retires it (coldkit-12)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from cruxible_client.contracts.write import RetireRequest
from cruxible_core.cli.main import cli


@pytest.mark.parametrize(
    "target,names",
    [
        ("Procedure:oncall.debug", '"retire": true'),
        ("Line:oncall.digest", "cruxible authoring submit"),
        ("ClaimType:oncall.shift.role", "vocabulary file"),
    ],
)
def test_a_definition_target_is_one_refusal_naming_how_it_retires(target: str, names: str):
    with pytest.raises(ValidationError) as caught:
        RetireRequest(target=target, because="Debug only.")
    (error,) = caught.value.errors(include_url=False)
    assert error["type"] == "retire_target_is_definition"
    assert names in error["msg"]
    assert "retire ends a Claim (CLM-...) or a Subject (kind/id)" in error["msg"]


def test_claim_and_subject_targets_are_unchanged():
    claim = "CLM-" + "0" * 32
    assert RetireRequest(target=f"Claim:{claim}", because="x").target == f"Claim:{claim}"
    assert RetireRequest(target="oncall.shift/x", because="x").target == "oncall.shift/x"


def test_the_cli_refuses_before_any_daemon_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CRUXIBLE_NO_WORKSPACE", "1")
    monkeypatch.setenv("CRUXIBLE_NO_AUTOSTART", "1")
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    def no_daemon(*_args, **_kwargs):  # pragma: no cover - decisive guard
        raise AssertionError("retire reached the daemon")

    monkeypatch.setattr("cruxible_core.cli.commands.playbill._server_call", no_daemon)
    result = CliRunner().invoke(
        cli, ["retire", "Procedure:oncall.debug", "--because", "Debug only."]
    )
    assert result.exit_code == 2, result.output
    assert '"retire": true' in result.output
