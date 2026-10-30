"""Kit bundles carry inert files (README, examples, skills) outside the release identity."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.artifacts import pack_artifact, pack_files, unpack_artifact
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitArtifact,
    KitArtifactBytes,
    KitBundle,
    KitFile,
    KitFileBytes,
    KitManifest,
    kit_file_entry,
)
from cruxible_client.kits import (
    KIT_ARTIFACT,
    KitSourceError,
    install_kit_skills,
    read_kit_directory,
    write_kit_directory,
    write_kit_docs,
)

ARTIFACT = "claim-types/acme.account/seats.json"
FILES = {
    "README.md": b"# acme\n",
    "examples/automation.json": b"{}\n",
    "reference.md": b"# reference\n",
    "skills/acme-triage/SKILL.md": b"---\nname: acme-triage\n---\n",
    "skills/acme-triage/references/flow.md": b"flow\n",
    "skills/intake/SKILL.md": b"---\nname: intake\n---\n",
}


def _manifest(files: tuple[KitFile, ...] = ()) -> KitManifest:
    return KitManifest(
        kit_id="acme",
        version="1.0.0",
        owns=("acme.",),
        artifacts=(KitArtifact(path=ARTIFACT, artifact_digest="sha256:" + "a" * 64),),
        files=files,
    )


def _bundle(files: dict[str, bytes] = FILES) -> KitBundle:
    entries = tuple(kit_file_entry(path, content) for path, content in sorted(files.items()))
    return KitBundle(
        manifest=_manifest(entries),
        artifacts=(KitArtifactBytes.of(ARTIFACT, b'{"x": 1}\n'),),
        files=tuple(KitFileBytes.of(path, content) for path, content in sorted(files.items())),
    )


def test_roles_follow_the_path_and_files_stay_out_of_the_release_identity() -> None:
    bundle = _bundle()
    roles = {item.path: item.role for item in bundle.manifest.files}
    assert roles == {
        "README.md": "readme",
        "examples/automation.json": "example",
        "reference.md": "reference",
        "skills/acme-triage/SKILL.md": "skill",
        "skills/acme-triage/references/flow.md": "skill",
        "skills/intake/SKILL.md": "skill",
    }
    assert bundle.manifest.content_digest == _manifest().content_digest
    assert "files" not in _manifest().model_dump(mode="json")


@pytest.mark.parametrize(
    "path",
    [
        "../README.md",
        "/README.md",
        "a//b.md",
        "a/./b.md",
        "a\\b.md",
        ".hidden",
        "skills/.git/config",
        "artifacts/claim-types/x.json",
        "Providers/x.whl",
        "cruxible-kit.json",
        "x" * 241,
    ],
)
def test_a_kit_file_path_is_canonical_and_outside_the_definition_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        kit_file_entry(path, b"x")


def test_a_role_that_disagrees_with_its_path_is_refused() -> None:
    entry = kit_file_entry("README.md", b"x")
    with pytest.raises(ValidationError, match="is a readme, not a skill"):
        KitFile(path=entry.path, sha256=entry.sha256, role="skill")


@pytest.mark.parametrize(
    "paths",
    [("README.md", "readme.md"), ("docs", "docs/a.md")],
)
def test_two_files_that_would_land_on_one_name_are_refused(paths: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        _manifest(tuple(sorted((kit_file_entry(p, b"x") for p in paths), key=lambda f: f.path)))


def test_file_bytes_must_match_the_manifest() -> None:
    bundle = _bundle()
    with pytest.raises(ValidationError, match="differs from its sha256"):
        KitBundle(
            manifest=bundle.manifest,
            artifacts=bundle.artifacts,
            files=tuple(
                KitFileBytes.of(item.path, b"changed" if item.path == "README.md" else item.content)
                for item in bundle.files
            ),
        )
    with pytest.raises(ValidationError, match="cover exactly"):
        KitBundle(manifest=bundle.manifest, artifacts=bundle.artifacts, files=bundle.files[:1])


def test_files_travel_in_the_layer_under_files_and_round_trip() -> None:
    bundle = _bundle()
    image = pack_artifact(KIT_ARTIFACT, bundle)
    assert unpack_artifact(KIT_ARTIFACT, image) == bundle
    assert image.digest == pack_artifact(KIT_ARTIFACT, _bundle()).digest
    without = pack_artifact(KIT_ARTIFACT, _bundle({}))
    assert without.digest != image.digest  # the image moves; the definitions do not


def test_a_layer_missing_a_listed_file_is_refused() -> None:
    bundle = _bundle()
    image = pack_artifact(KIT_ARTIFACT, bundle)
    layer = pack_files({ARTIFACT: b'{"x": 1}\n', "files/README.md": FILES["README.md"]})
    with pytest.raises(ValueError, match="exactly its manifest's kit files"):
        KIT_ARTIFACT.unpack(image.config.content, (layer,))


def test_a_directory_holds_files_at_their_own_paths_and_ignores_the_rest(tmp_path: Path) -> None:
    bundle = _bundle()
    root = tmp_path / "acme"
    write_kit_directory(bundle, root)
    assert (root / "README.md").read_bytes() == FILES["README.md"]
    assert (root / "skills" / "intake" / "SKILL.md").is_file()
    (root / "source").mkdir()
    (root / "source" / "build.py").write_text("print()\n")
    assert read_kit_directory(root) == bundle

    (root / "README.md").write_bytes(b"edited\n")
    with pytest.raises(ValidationError, match="differs from its sha256"):
        read_kit_directory(root)
    (root / "README.md").unlink()
    with pytest.raises(ValueError, match="lacks the manifest's kit files"):
        read_kit_directory(root)


def test_a_directory_file_behind_a_link_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    write_kit_directory(_bundle(), root)
    outside = tmp_path / "outside"
    (root / "examples").rename(outside)
    (root / "examples").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        read_kit_directory(root)


def test_kit_add_never_sends_the_files() -> None:
    bundle = _bundle()
    request = KitAddRequest(bundle=bundle)
    assert request.bundle.files == ()
    assert request.bundle.manifest == bundle.manifest
    assert "files" not in request.model_dump(mode="json")["bundle"]


def test_build_keeps_and_moves_name_lines_because_a_procedure_names_no_build() -> None:
    from cruxible_client.contracts.provider_installation import ProviderMoveRequest

    bundle = _bundle()
    assert KitAddRequest(bundle=bundle, keep_build=("Line:acme.tick",)).keep_build == (
        "Line:acme.tick",
    )
    assert ProviderMoveRequest(provider="kit-call", only=("Line:acme.tick",)).only == (
        "Line:acme.tick",
    )
    # A Procedure identity would never match a Line binding the build, so it is
    # refused rather than silently keeping or moving nothing.
    with pytest.raises(ValidationError, match="keep_build names a Line as Line:NAME"):
        KitAddRequest(bundle=bundle, keep_build=("Procedure:acme.increment",))
    with pytest.raises(ValidationError, match="only names Lines as Line:NAME"):
        ProviderMoveRequest(provider="kit-call", only=("Procedure:acme.increment",))


def test_docs_write_at_their_paths_with_or_without_skills(tmp_path: Path) -> None:
    bundle = _bundle()
    written = write_kit_docs(bundle, tmp_path / "docs", skills=False)
    assert sorted(path.relative_to(tmp_path / "docs").as_posix() for path in written) == [
        "README.md",
        "examples/automation.json",
        "reference.md",
    ]
    with pytest.raises(FileExistsError):
        write_kit_docs(bundle, tmp_path / "docs")
    every = write_kit_docs(bundle, tmp_path / "all")
    assert len(every) == len(FILES)


def test_skills_install_as_kit_prefixed_directories_and_never_overwrite(tmp_path: Path) -> None:
    bundle = _bundle()
    installed = install_kit_skills(bundle, tmp_path / "skills")
    assert [path.name for path in installed] == ["acme-triage", "acme-intake"]
    assert (
        tmp_path / "skills" / "acme-triage" / "references" / "flow.md"
    ).read_bytes() == b"flow\n"
    assert (tmp_path / "skills" / "acme-intake" / "SKILL.md").is_file()
    with pytest.raises(KitSourceError, match="already exist"):
        install_kit_skills(bundle, tmp_path / "skills")


def test_a_bundle_stripped_of_its_files_cannot_write_docs(tmp_path: Path) -> None:
    stripped = _bundle().model_copy(update={"files": ()})
    with pytest.raises(KitSourceError, match="carries none of their bytes"):
        write_kit_docs(stripped, tmp_path / "docs")
    with pytest.raises(ValueError, match="carries none of their bytes"):
        write_kit_directory(stripped, tmp_path / "kit")
