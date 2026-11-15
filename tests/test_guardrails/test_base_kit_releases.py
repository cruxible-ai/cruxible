"""Released base kit artifacts are append-only, the way genesis seed sets are.

An instance keeps the base kit artifacts it accepted, and replay re-checks them
under whatever core opens it. ``cruxible_core.kits.base_kit_releases`` records
every released base kit; a later bundle may add artifacts and versions but never
change or drop a released one, and changes content only under a new kit version.
``scripts/build_base_kit.py --check`` applies the same rules.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from cruxible_client.contracts.capabilities import (
    CAPABILITY_RESERVED,
    capability_path,
    parse_capability,
)
from cruxible_core.capabilities.artifact import bundled_version_problems
from cruxible_core.kits import base_kit_releases
from cruxible_core.kits.base_kit import bundled_base_kit
from cruxible_core.kits.base_kit_releases import (
    ReleasedBaseKit,
    release_problems,
    render_releases_module,
)

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_base_kit.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_base_kit", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle() -> tuple[str, str, dict[str, str]]:
    manifest = bundled_base_kit().manifest
    return manifest.version, manifest.content_digest, manifest.digests()


def _released(version: str, content: str, artifacts: dict[str, str]) -> ReleasedBaseKit:
    return ReleasedBaseKit(
        version=version, content_digest=content, artifacts=tuple(sorted(artifacts.items()))
    )


OTHER = "sha256:" + "f" * 64


def test_the_bundle_holds_the_released_record() -> None:
    assert release_problems(*_bundle()) == []
    assert _script().release_differences() == []


def test_a_released_artifact_never_changes_or_disappears() -> None:
    version, content, artifacts = _bundle()
    record = (_released(version, content, artifacts),)
    assert release_problems(version, content, artifacts, record) == []

    contract = "contracts/web.fetch.request/v1.json"
    changed = {**artifacts, contract: OTHER}
    problems = release_problems("1.1.0", "sha256:" + "1" * 64, changed, record)
    assert len(problems) == 1 and contract in problems[0]
    assert "add the change as a new version" in problems[0]

    dropped = {path: digest for path, digest in artifacts.items() if path != contract}
    (problem,) = release_problems("1.1.0", "sha256:" + "1" * 64, dropped, record)
    assert contract in problem and "never dropped" in problem


def test_a_released_version_keeps_its_content() -> None:
    version, content, artifacts = _bundle()
    record = (_released(version, content, artifacts),)
    problems = release_problems(version, "sha256:" + "1" * 64, artifacts, record)
    assert any("bump the kit version" in problem for problem in problems), problems


def test_a_later_version_adds_versions_and_succeeds_a_capture_kind() -> None:
    version, content, artifacts = _bundle()
    record = (_released(version, content, artifacts),)
    later = {
        **artifacts,
        "capabilities/web.fetch/v2.json": OTHER,
        "contracts/web.fetch.request/v2.json": OTHER,
        # A CaptureKind's path names no version: its successor ships there.
        "capture-contracts/web.search-results.json": OTHER,
    }
    assert release_problems("1.1.0", "sha256:" + "1" * 64, later, record) == []
    # ... but not under the released version.
    assert release_problems(version, content, later, record) != []


def test_the_record_renders_and_reads_back() -> None:
    source = Path(base_kit_releases.__file__).read_text(encoding="utf-8")
    assert render_releases_module(base_kit_releases.BASE_KIT_RELEASES) == source
    version, content, artifacts = _bundle()
    record = (_released(version, content, artifacts),)
    namespace: dict[str, object] = {}
    exec(compile(render_releases_module(record), "base_kit_releases.py", "exec"), namespace)
    read = namespace["BASE_KIT_RELEASES"]
    assert isinstance(read, tuple)
    assert [(item.version, item.content_digest, item.artifacts) for item in read] == [
        (item.version, item.content_digest, item.artifacts) for item in record
    ]


def test_replay_accepts_any_released_workspace_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace capability v1 a released base kit held stays acceptable after the
    bundle moves on, not only the newest bundle's bytes."""

    path = capability_path("workspace.file", 1)
    held = parse_capability(bundled_base_kit().contents()[path], path=path)
    earlier = held.model_copy(update={"description": "An earlier release's wording."})
    from cruxible_client.contracts.capabilities import capability_digest

    refused = bundled_version_problems(earlier)
    assert [problem.code for problem in refused] == [CAPABILITY_RESERVED]
    monkeypatch.setattr(
        base_kit_releases,
        "BASE_KIT_RELEASES",
        (_released("1.0.0", OTHER, {path: capability_digest(earlier).tagged}),),
    )
    assert bundled_version_problems(earlier) == ()
    assert bundled_version_problems(held) == ()


def test_the_release_cut_records_the_bundle(tmp_path: Path) -> None:
    """``build_base_kit.py --release`` appends the bundle as it renders today."""

    target = tmp_path / "base_kit_releases.py"
    assert _script().release(releases_module=target) == bundled_base_kit().manifest.version
    namespace: dict[str, object] = {}
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), namespace)
    (entry,) = namespace["BASE_KIT_RELEASES"]  # type: ignore[misc]
    version, content, artifacts = _bundle()
    assert (entry.version, entry.content_digest, dict(entry.artifacts)) == (
        version,
        content,
        artifacts,
    )
