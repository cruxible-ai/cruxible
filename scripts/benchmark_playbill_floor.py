"""Measure floor export and installation on a private copied instance.

Never point this at a running daemon's managed root: opening an instance performs
recovery and initializes its local stores. Use a private copy and its public trust
root, and select a disposable workspace. No credential values are reported.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path

from cruxible_client import contracts
from cruxible_client.authoring.workspace import materialize_playbill_floor
from cruxible_client.contracts.types import PlaybillTrustRoot
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor import service_export_playbill_floor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copied-instance", required=True, type=Path)
    parser.add_argument("--trust-root", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--format-version", type=int, choices=(2, 3), default=3)
    args = parser.parse_args()
    start = time.perf_counter()
    instance = PlaybillInstance.open(
        args.copied_instance,
        trust_root=PlaybillTrustRoot.model_validate_json(args.trust_root.read_bytes()),
    )
    opened = time.perf_counter() - start
    args.workspace.mkdir(parents=True, exist_ok=True)
    samples = []
    for _ in range(3):
        start = time.perf_counter()
        files = service_export_playbill_floor(instance, format_version=args.format_version)
        service_seconds = time.perf_counter() - start
        manifest = json.loads(files["manifest.json"])
        export = contracts.PlaybillFloorExport(
            tag=manifest["format"],
            coordinate=manifest["coordinate"],
            manifest=manifest,
            files=[
                contracts.PlaybillFloorFile(
                    path=path, content_base64=base64.b64encode(content).decode("ascii")
                )
                for path, content in files.items()
            ],
        )
        start = time.perf_counter()
        materialize_playbill_floor(args.workspace, export=export)
        samples.append(
            {
                "service_seconds": service_seconds,
                "materialize_seconds": time.perf_counter() - start,
                "files": len(files),
                "bytes": sum(map(len, files.values())),
                "floor_digest": manifest["floor_digest"],
            }
        )
    print(
        json.dumps(
            {"open_seconds": opened, "samples": samples, "coordinate": manifest["coordinate"]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
