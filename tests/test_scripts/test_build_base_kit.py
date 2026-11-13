"""scripts/build_base_kit.py: the bundled base kit renders from its sources; CI checks it."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO_ROOT / "tests" / "pilot_contract_batch" / "examples" / "base"


def _load_script() -> ModuleType:
    path = _REPO_ROOT / "scripts" / "build_base_kit.py"
    spec = importlib.util.spec_from_file_location("build_base_kit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_committed_kit_and_pin_are_in_step_with_the_sources() -> None:
    script = _load_script()
    assert script.differences() == []
    assert script.main(["--check"]) == 0


def test_check_fails_when_a_source_moves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    source = tmp_path / "source"
    shutil.copytree(script.SOURCE, source)
    directory, pin = tmp_path / "base", tmp_path / "base_kit_pin.py"
    script.write(source, directory, pin)
    assert script.differences(source, directory, pin) == []

    target = source / "capabilities" / "web.fetch" / "v1.json"
    payload = json.loads(target.read_bytes())
    target.write_text(json.dumps({**payload, "description": "Fetches one page."}))
    found = script.differences(source, directory, pin)
    assert "artifacts/capabilities/web.fetch/v1.json: differs" in found
    assert "cruxible-kit.json: differs" in found
    assert "base_kit_pin.py: differs" in found

    script.write(source, directory, pin)
    (directory / "artifacts" / "contracts" / "stray" / "v1.json").parent.mkdir(parents=True)
    (directory / "artifacts" / "contracts" / "stray" / "v1.json").write_text("{}\n")
    assert script.differences(source, directory, pin) == [
        "artifacts/contracts/stray/v1.json: not rendered from any source"
    ]


def test_check_exits_one_and_names_the_repair(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "differences", lambda: ["cruxible-kit.json: differs"])
    assert script.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "cruxible-kit.json: differs" in err
    assert "python scripts/build_base_kit.py" in err


def test_the_settled_capabilities_render_byte_for_byte_as_the_examples() -> None:
    """The five capabilities settled in wave 0, and their Contracts, are copied exactly."""

    artifacts, _manifest, _pin = _load_script().render()
    examples = {
        path.relative_to(_EXAMPLES).as_posix(): path.read_bytes()
        for path in _EXAMPLES.rglob("*.json")
        if path.relative_to(_EXAMPLES).parts[0] in {"capabilities", "contracts"}
    }
    assert examples
    for path, content in examples.items():
        assert artifacts[path] == content, path
