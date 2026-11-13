from __future__ import annotations

import json

from click.testing import CliRunner

from cruxible_core.cli.commands.playbill import playbill_group


def test_capture_contract_templates_need_no_daemon():
    runner = CliRunner()
    minimal = runner.invoke(playbill_group, ["capture-contract", "propose", "--template"])
    assert minimal.exit_code == 0, minimal.output
    assert json.loads(minimal.stdout) == {"name": "example.observation", "shape": "http_response"}
    expanded = runner.invoke(
        playbill_group, ["capture-contract", "propose", "--template", "--expanded"]
    )
    assert expanded.exit_code == 0, expanded.output
    value = json.loads(expanded.stdout)
    assert value["replay"] == "attested"
    assert value["retention"] == "P30D"
    assert "components" not in value
