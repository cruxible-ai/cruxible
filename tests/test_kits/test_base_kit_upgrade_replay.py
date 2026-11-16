"""An instance created with one released base kit opens and runs under a later bundle.

Replay re-checks every accepted generation, the base kit's acceptance included,
under whatever core opens the instance. Once base 1.0.0 is released its artifacts
are fixed (``cruxible_core.kits.base_kit_releases``), so a later bundle only adds:
here base 1.1.0 adds ``web.fetch@2`` and ``web.fetch.request@2``. The instance is
created with today's bundle, then opened in a fresh process (no per-process memo
carries over) under the later bundle, with 1.0.0 recorded as released, and runs
its builtin-backed Line again.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.kits import KitManifest
from cruxible_client.contracts.procedures.authored import ReturnFields
from cruxible_core.kits.base_kit import bundled_base_kit
from tests.core_support._identity_support import accept_tree
from tests.core_support._light_capture_support import file_contract
from tests.support.operation_world import author_line, author_procedure, contract_files
from tests.test_kits.test_base_kit import _create
from tests.test_procedures.test_b2_operations import SOURCE_LINE, run_sourced, sourced_spec

_REPO = Path(__file__).resolve().parents[2]
_SOURCE = _REPO / "scripts" / "base_kit"

# Runs in a fresh process: point core at the later bundle, record 1.0.0 as
# released, then open the instance (replaying it) and run its Line again.
_REOPEN = """
import json, sys
from datetime import timedelta
from pathlib import Path

args = json.loads(sys.argv[1])
from cruxible_core.kits import BundledKitPin, base_kit, base_kit_releases

base_kit.base_kit_directory = lambda: Path(args["bundle"])
base_kit.BASE_KIT_PIN = BundledKitPin(**args["pin"])
base_kit.bundled_base_kit.cache_clear()
base_kit_releases.BASE_KIT_RELEASES = (
    base_kit_releases.ReleasedBaseKit(
        version=args["released"]["version"],
        content_digest=args["released"]["content_digest"],
        artifacts=tuple(tuple(item) for item in args["released"]["artifacts"]),
    ),
)
assert base_kit.bundled_base_kit().manifest.version == args["pin"]["version"]

from cruxible_client.contracts.types import TrustRoot
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.kits import service_kit_status
from tests.test_procedures.test_b2_operations import NOW, run_sourced

instance = PlaybillInstance.open(
    Path(args["root"]), trust_root=TrustRoot.model_validate(args["trust_root"])
)
(base,) = [kit for kit in service_kit_status(instance).kits if kit.kit_id == "base"]
run = run_sourced(instance, Path(args["workspace"]), at=NOW + timedelta(minutes=5))
print(json.dumps({"base": [base.version, base.latest_available], "run": run.status}))
"""


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_base_kit", _REPO / "scripts" / "build_base_kit.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _later_bundle(root: Path) -> tuple[Path, dict[str, str]]:
    """Base 1.1.0: today's sources plus ``web.fetch.request@2`` and ``web.fetch@2``."""

    source = root / "source"
    shutil.copytree(_SOURCE, source)
    kit = json.loads((source / "kit.json").read_bytes())
    (source / "kit.json").write_text(json.dumps({**kit, "version": "1.1.0"}))
    held = bundled_base_kit().manifest.digests()

    def successor(path: str) -> dict[str, object]:
        return {"predecessor_digest": held[path]}

    request = json.loads((source / "contracts/web.fetch.request/v1.json").read_bytes())
    (source / "contracts/web.fetch.request/v2.json").write_text(
        json.dumps(
            {
                **request,
                "version": 2,
                "description": "Version 2 of the request.",
                **successor("contracts/web.fetch.request/v1.json"),
            }
        )
    )
    fetch = json.loads((source / "capabilities/web.fetch/v1.json").read_bytes())
    (source / "capabilities/web.fetch/v2.json").write_text(
        json.dumps(
            {
                **fetch,
                "version": 2,
                "in": "web.fetch.request@2",
                **successor("capabilities/web.fetch/v1.json"),
            }
        )
    )
    artifacts, manifest, _pin = _script().render(source)
    bundle = root / "bundle"
    for path, content in artifacts.items():
        target = bundle / "artifacts" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (bundle / "cruxible-kit.json").write_bytes(manifest)
    parsed = KitManifest.model_validate_json(manifest)
    return bundle, {
        "kit_id": parsed.kit_id,
        "version": parsed.version,
        "content_digest": parsed.content_digest,
    }


def test_a_base_1_0_0_instance_opens_and_runs_under_a_later_bundle(tmp_path: Path) -> None:
    instance, _owner, _reviewer = _create(tmp_path / "instance")
    workspace = tmp_path / "instance" / "workspace"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "doc.json").write_text('{"value": 7}')
    contract = file_contract()
    accept_tree(
        instance,
        {
            **contract_files(),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
        },
        name="builtin-fixtures",
    )
    spec = sourced_spec()
    spec = spec.model_copy(
        update={
            "steps": spec.steps[:1],
            "returns": ReturnFields(
                fields={"document": {"$ref": "step", "step": "doc", "path": ["capture"]}}
            ),
        }
    )
    author_procedure(instance, spec)
    author_line(instance, name=SOURCE_LINE, procedure="test.sourced", parameters={})
    assert run_sourced(instance, workspace).status == "succeeded"

    released = bundled_base_kit().manifest
    assert released.version == "1.0.0"
    bundle, pin = _later_bundle(tmp_path / "later")
    assert pin["version"] == "1.1.0"
    arguments = {
        "bundle": str(bundle),
        "pin": pin,
        "released": {
            "version": released.version,
            "content_digest": released.content_digest,
            "artifacts": sorted(released.digests().items()),
        },
        "root": str(instance.root),
        "trust_root": instance.trust_root.model_dump(mode="json"),
        "workspace": str(workspace),
    }
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(_REPO), str(_REPO / "src"), str(_REPO / "packages/cruxible-client/src"))
        ),
    }
    reopened = subprocess.run(
        [sys.executable, "-c", _REOPEN, json.dumps(arguments)],
        cwd=_REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert reopened.returncode == 0, reopened.stderr[-4000:]
    outcome = json.loads(reopened.stdout.strip().splitlines()[-1])
    assert outcome == {"base": ["1.0.0", "1.1.0"], "run": "succeeded"}
