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
