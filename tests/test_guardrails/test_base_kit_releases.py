"""Released base kit artifacts are append-only, the way genesis seed sets are.

An instance keeps the base kit artifacts it accepted, and replay re-checks them
under whatever core opens it. ``cruxible_core.kits.base_kit_releases`` records
every released base kit; a later bundle may add artifacts and versions but never
change or drop a released one, and changes content only under a new kit version.
``scripts/build_base_kit.py --check`` applies the same rules.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import shutil
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
    RELEASE_PINS_RELATIVE,
    ReleasedBaseKit,
    ReleasePins,
    load_release_pins,
    pin_of,
    record_problems,
    release_problems,
    render_release_pins,
    render_releases_module,
)

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "build_base_kit.py"


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
    # The real record against its real pins, then the bundle against the record.
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


def _cut(tmp_path: Path) -> tuple[tuple[object, ...], Path]:
    """Record today's bundle as release 1 the way the cut does, frozen: ``(record, pins)``."""

    module, pins = tmp_path / "base_kit_releases.py", tmp_path / "pins.json"
    pins.write_text(render_release_pins(ReleasePins()), encoding="utf-8")
    version, ordinal = _script().release(releases_module=module, pins_path=pins)
    assert (version, ordinal) == (bundled_base_kit().manifest.version, 1)
    frozen = load_release_pins(pins).model_copy(update={"frozen_through": 1})
    pins.write_text(render_release_pins(frozen), encoding="utf-8")
    namespace: dict[str, object] = {}
    exec(compile(module.read_text(encoding="utf-8"), str(module), "exec"), namespace)
    record = namespace["BASE_KIT_RELEASES"]
    assert isinstance(record, tuple)
    return record, pins


def test_the_record_holds_its_pins() -> None:
    pins = load_release_pins(_REPO / RELEASE_PINS_RELATIVE)
    assert record_problems(pins) == []
    # Nothing is released yet: nothing is frozen.
    assert (pins.frozen_through, pins.entries) == (0, ())


def test_the_release_cut_records_the_bundle_and_its_pin(tmp_path: Path) -> None:
    """``build_base_kit.py --release`` appends the bundle as it renders today, with its pin."""

    record, pins = _cut(tmp_path)
    (entry,) = record
    version, content, artifacts = _bundle()
    assert (entry.version, entry.content_digest, dict(entry.artifacts)) == (  # type: ignore[attr-defined]
        version,
        content,
        artifacts,
    )
    (pin,) = load_release_pins(pins).entries
    assert (pin.ordinal, pin.version, pin.entry_digest) == (1, version, entry.entry_digest())  # type: ignore[attr-defined]
    assert _script().release_differences(releases=record, pins_path=pins) == []


def test_an_edited_frozen_entry_fails_the_check_by_name(tmp_path: Path) -> None:
    record, pins = _cut(tmp_path)
    (entry,) = record
    path = capability_path("workspace.file", 1)
    edited = dataclasses.replace(
        entry,  # type: ignore[type-var]
        artifacts=tuple(
            (item, OTHER if item == path else digest)
            for item, digest in entry.artifacts  # type: ignore[attr-defined]
        ),
    )
    (problem,) = _script().release_differences(releases=(edited,), pins_path=pins)
    assert problem.startswith("Released entry 1 (base 1.0.0) was edited, reordered or removed")


def test_a_truncated_record_fails_the_check_by_name(tmp_path: Path) -> None:
    _record, pins = _cut(tmp_path)
    (problem,) = _script().release_differences(releases=(), pins_path=pins)
    assert problem.startswith("Released entry 1 (base 1.0.0) is missing from BASE_KIT_RELEASES")


def test_a_removed_entry_fails_the_check_by_name(tmp_path: Path) -> None:
    """Two releases frozen; dropping the first moves the second into its place."""

    record, pins = _cut(tmp_path)
    (first,) = record
    second = dataclasses.replace(first, version="1.1.0")  # type: ignore[type-var]
    frozen = load_release_pins(pins)
    pins.write_text(
        render_release_pins(
            frozen.model_copy(
                update={"entries": (*frozen.entries, pin_of(2, second)), "frozen_through": 2}  # type: ignore[arg-type]
            )
        ),
        encoding="utf-8",
    )
    assert _script().release_differences(releases=(first, second), pins_path=pins) == []
    problems = _script().release_differences(releases=(second,), pins_path=pins)
    assert problems[0].startswith("Released entry 1 (base 1.0.0) was edited, reordered or removed")
    assert problems[1].startswith("Released entry 2 (base 1.1.0) is missing")


def test_an_unpinned_entry_fails_the_check(tmp_path: Path) -> None:
    record, _pins = _cut(tmp_path)
    empty = tmp_path / "empty.json"
    empty.write_text(render_release_pins(ReleasePins()), encoding="utf-8")
    (problem,) = _script().release_differences(releases=record, pins_path=empty)
    assert problem.startswith("entry 1 (base 1.0.0) has no pin")


def test_a_bundle_rewriting_a_released_v1_is_refused_at_check_time(tmp_path: Path) -> None:
    """The review's cold-replay case, refused before any instance can meet it.

    Release 1.0.0 is recorded and frozen; a later bundle (1.2.0) rewrites the
    released ``workspace.file@1``. Intact, the record refuses the bundle; edited to
    the rewritten digest, or emptied, the record is refused by its pins. (An
    intact record's later bundle opening a released instance cold is
    ``tests/test_kits/test_base_kit_upgrade_replay.py``.)
    """

    record, pins = _cut(tmp_path)
    source = tmp_path / "source"
    shutil.copytree(_REPO / "scripts" / "base_kit", source)
    kit = json.loads((source / "kit.json").read_bytes())
    (source / "kit.json").write_text(json.dumps({**kit, "version": "1.2.0"}))
    rewritten_path = source / "capabilities/workspace.file/v1.json"
    capability = json.loads(rewritten_path.read_bytes())
    rewritten_path.write_text(json.dumps({**capability, "description": "Rewritten."}))
    path = capability_path("workspace.file", 1)

    script = _script()
    (problem,) = script.release_differences(source, releases=record, pins_path=pins)
    assert problem.startswith(f"{path} was released in base kit 1.0.0")

    artifacts, _manifest, _pin = script.render(source)
    from cruxible_core.claims.closure import parse_dependency_artifact

    state = parse_dependency_artifact(path, artifacts[path])
    assert state is not None
    (entry,) = record
    forged = dataclasses.replace(
        entry,  # type: ignore[type-var]
        artifacts=tuple(
            (item, state.artifact_digest if item == path else digest)
            for item, digest in entry.artifacts  # type: ignore[attr-defined]
        ),
    )
    for edited in ((forged,), ()):
        problems = script.release_differences(source, releases=edited, pins_path=pins)
        assert len(problems) == 1 and problems[0].startswith("Released entry 1 (base 1.0.0)")
