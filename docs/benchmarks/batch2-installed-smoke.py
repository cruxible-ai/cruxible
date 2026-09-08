"""Run the served write-loop harness against installed wheels, not checkout imports.

Invoke with a private interpreter containing both wheels and offline dependencies:
  /tmp/wheel-env/bin/python docs/benchmarks/batch2-installed-smoke.py \
    --repo /path/to/checkout --population 32 --history 1 --repeats 2 \
    --claims-per-write 2 --orphan-proposals 0 --world --no-server-profile \
    --reopen-after --output /tmp/installed-smoke.json

The checkout supplies only fixture/test helpers and the existing harness. Every
production package must resolve inside the invoking virtualenv's site-packages.
The subprocess daemon runs this same entrypoint and repeats that assertion.
Set BATCH2_CLIENT_PYTHON to a separate client-only wheel interpreter to also
verify actual HTTP readback with Core absent from the client environment.
No live instance, credentials, source files or daemon are touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _check_installed() -> dict[str, str]:
    import cruxible_client
    import cruxible_client._persistent
    import cruxible_core
    import cruxible_core.playbill.derived_runtime
    import cruxible_core.playbill.derived_state

    result = {}
    for module in (
        cruxible_client,
        cruxible_client._persistent,
        cruxible_core,
        cruxible_core.playbill.derived_runtime,
        cruxible_core.playbill.derived_state,
    ):
        path = Path(module.__file__).absolute()
        assert path.is_relative_to(Path(sys.prefix).absolute()), (module.__name__, str(path))
        assert "site-packages" in path.parts, (module.__name__, str(path))
        result[module.__name__] = str(path)
    print("installed-wheel-modules: " + json.dumps(result, sort_keys=True), file=sys.stderr)
    return result


def _client_only_readback(socket: object, instance: str, workspace: object, row: dict) -> dict:
    interpreter = os.environ.get("BATCH2_CLIENT_PYTHON")
    if not interpreter:
        return {"skipped": "Set BATCH2_CLIENT_PYTHON to the client-only wheel interpreter."}
    code = """
import importlib.util, json, pathlib, sys
from cruxible_client import Playbill
import cruxible_client
assert importlib.util.find_spec('cruxible_core') is None
assert pathlib.Path(cruxible_client.__file__).is_relative_to(pathlib.Path(sys.prefix))
request = json.load(sys.stdin)
with Playbill.connect(target='unix:' + request['socket'], instance=request['instance'],
                      workspace=request['workspace']) as pb:
    views = pb.claim_views(request['ids'])
    assert len(views) == len(request['ids'])
    assert all(item.value == 'ready' for item in views)
    assert pb.coordinate.model_dump(mode='json') == request['coordinate']
    print(json.dumps({'client_module':cruxible_client.__file__, 'core_available':False,
                      'claim_count':len(views), 'values':['ready'] * len(views)}))
"""
    result = subprocess.run(
        [interpreter, "-I", "-c", code],
        input=json.dumps(
            {
                "socket": str(socket),
                "instance": instance,
                "workspace": str(workspace),
                "ids": row["readback_identities"],
                "coordinate": row["accepted_coordinate"],
            }
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    harness = Path(__file__).with_name("write-loop-served.py")
    source = harness.read_text()
    checkout_roots = 'roots = [repo / "src", repo / "packages/cruxible-client/src", repo]'
    server_import = "    from cruxible_core.server.app import run_server\n"
    replacements = {
        checkout_roots: "roots = [repo]  # Fixture imports only.",
        server_import: server_import + "    _check_installed()\n",
        '            report["rows"].append(row)\n': (
            '            row["client_only_readback"] = _client_only_readback(\n'
            "                socket, instance_id, workspace, row)\n"
            '            report["rows"].append(row)\n'
        ),
        "    report = {\n": "    report = {\n        'installed_modules': _check_installed(),\n",
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise RuntimeError("served harness changed; review installed-smoke adaptation")
        source = source.replace(old, new)
    scope = {
        "__name__": "__main__",
        "__file__": str(Path(__file__).resolve()),
        "_check_installed": _check_installed,
        "_client_only_readback": _client_only_readback,
    }
    exec(compile(source, str(harness), "exec"), scope)


if __name__ == "__main__":
    main()
