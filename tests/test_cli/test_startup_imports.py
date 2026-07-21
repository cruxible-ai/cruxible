"""Import-graph guardrails for latency-sensitive CLI startup paths."""

from __future__ import annotations

import subprocess
import sys


def _run_import_assertions(source: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", source],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_importing_cli_keeps_graph_runtime_unloaded() -> None:
    _run_import_assertions(
        "import sys\n"
        "from cruxible_core.cli.main import cli\n"
        "assert cli is not None\n"
        "assert 'networkx' not in sys.modules\n"
        "assert 'cruxible_core.runtime.instance' not in sys.modules\n"
    )


def test_importing_core_errors_keeps_http_client_unloaded() -> None:
    _run_import_assertions(
        "import sys\nimport cruxible_core.errors\nassert 'httpx' not in sys.modules\n"
    )


def test_server_stats_keeps_local_graph_runtime_unloaded() -> None:
    _run_import_assertions(
        "import importlib\n"
        "import sys\n"
        "from types import SimpleNamespace\n"
        "from click.testing import CliRunner\n"
        "from cruxible_core.cli.main import cli\n"
        "common = importlib.import_module('cruxible_core.cli.commands._common')\n"
        "client = SimpleNamespace(stats=lambda _instance_id: SimpleNamespace(\n"
        "    entity_count=0, edge_count=0, entity_counts={}, relationship_counts={},\n"
        "    status_counts={}, head_snapshot_id=None, read_revision=0))\n"
        "common._get_client = lambda: client\n"
        "result = CliRunner().invoke(cli, ['--server-url', 'http://server.invalid',\n"
        "    '--instance-id', 'startup-latency', 'stats', '--json'])\n"
        "assert result.exit_code == 0, result.output\n"
        "assert 'networkx' not in sys.modules\n"
        "assert 'cruxible_core.runtime.instance' not in sys.modules\n"
    )
