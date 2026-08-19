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
    # The legacy runtime instance left in PC-F, so its name would assert
    # nothing. The surviving PC-H residue carries the obligation instead.
    _run_import_assertions(
        "import sys\n"
        "from cruxible_core.cli.main import cli\n"
        "assert cli is not None\n"
        "assert 'networkx' not in sys.modules\n"
        "donors = ('cruxible_core.config', 'cruxible_core.graph', 'cruxible_core.query')\n"
        "loaded = sorted(m for m in sys.modules "
        "if any(m == d or m.startswith(d + '.') for d in donors))\n"
        "assert not loaded, loaded\n"
    )


def test_importing_core_errors_keeps_http_client_unloaded() -> None:
    _run_import_assertions(
        "import sys\nimport cruxible_core.errors\nassert 'httpx' not in sys.modules\n"
    )
