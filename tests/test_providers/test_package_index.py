"""Finding a provider release by name on a PEP 503 index, without the network.

Fetching and listing go through the provider toolchain, so most of these run
only with the cruxible-providers checkout (tests/support/provider_checkout.py).
"""

import hashlib
from pathlib import Path
from zipfile import ZipFile

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.providers.package_index import embedded_lock, fetch_release, find_release
from cruxible_core.providers.package_materialization import ArtifactTransport


def _wheel(directory: Path, name: str, version: str, *, lock: bytes | None = None) -> Path:
    module = name.replace("-", "_")
    path = directory / f"{module}-{version}-py3-none-any.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{module}-{version}.dist-info/METADATA", f"Name: {name}\n")
        if lock is not None:
            archive.writestr(f"{module}-{version}.dist-info/extra_metadata/uv.lock", lock)
    return path


def _index(root: Path, name: str, files: list[tuple[Path, str]]) -> str:
    """A file index: ``files`` are (wheel, anchor attributes) rows."""
    page = root / name
    page.mkdir(parents=True, exist_ok=True)
    rows = []
    for wheel, attributes in files:
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        rows.append(f'<a href="{wheel.as_uri()}#sha256={digest}"{attributes}>{wheel.name}</a>')
    (page / "index.html").write_text("<html><body>" + "\n".join(rows) + "</body></html>")
    return root.as_uri() + "/"


@pytest.mark.usefixtures("provider_runtime")
def test_by_name_is_the_newest_final_listed_release(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    index = _index(
        tmp_path / "simple",
        "cruxible-provider-web",
        [
            (_wheel(files, "cruxible-provider-web", "0.2.0"), ""),
            (_wheel(files, "cruxible-provider-web", "0.3.0"), ' data-yanked=""'),
            (_wheel(files, "cruxible-provider-web", "0.4.0rc1"), ""),
            (_wheel(files, "cruxible-provider-web", "0.2.1"), ""),
        ],
    )
    transport = ArtifactTransport(tmp_path)
    newest = find_release((index,), "cruxible_provider_web", None, transport)
    assert (newest.name, newest.version, newest.index_url) == (
        "cruxible-provider-web",
        "0.2.1",
        index,
    )
    # An exact version is honoured even when yanked or a prerelease.
    assert find_release((index,), "cruxible-provider-web", "0.3.0", transport).version == "0.3.0"
    assert (
        find_release((index,), "cruxible-provider-web", "0.4.0rc1", transport).version == "0.4.0rc1"
    )
    with pytest.raises(ConfigError, match="no installable wheel for cruxible-provider-web==9"):
        find_release((index,), "cruxible-provider-web", "9", transport)


@pytest.mark.usefixtures("provider_runtime")
def test_the_first_index_listing_a_package_is_the_only_one_consulted(tmp_path):
    first_files, second_files = tmp_path / "a", tmp_path / "b"
    first_files.mkdir()
    second_files.mkdir()
    empty = (tmp_path / "empty").as_uri() + "/"
    first = _index(
        tmp_path / "first", "cruxible-provider-web", [(_wheel(first_files, "x", "0.1.0"), "")]
    )
    second = _index(
        tmp_path / "second",
        "cruxible-provider-web",
        [(_wheel(second_files, "cruxible-provider-web", "0.9.0"), "")],
    )
    transport = ArtifactTransport(tmp_path)
    # An index without the package is skipped; the first that lists it decides,
    # even when it has nothing installable and a later index would.
    with pytest.raises(ConfigError, match="lists no installable wheel"):
        find_release((empty, first, second), "cruxible-provider-web", None, transport)
    assert find_release((empty, second), "cruxible-provider-web", None, transport).version == (
        "0.9.0"
    )
    with pytest.raises(ConfigError, match="no configured provider index lists"):
        find_release((empty,), "cruxible-provider-web", None, transport)


@pytest.mark.usefixtures("provider_runtime")
def test_unhashed_files_are_never_candidates(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    wheel = _wheel(files, "cruxible-provider-web", "0.2.0")
    page = tmp_path / "simple" / "cruxible-provider-web"
    page.mkdir(parents=True)
    (page / "index.html").write_text(f'<a href="{wheel.as_uri()}">{wheel.name}</a>')
    with pytest.raises(ConfigError, match="lists no installable wheel"):
        find_release(
            ((tmp_path / "simple").as_uri() + "/",),
            "cruxible-provider-web",
            None,
            ArtifactTransport(tmp_path),
        )


@pytest.mark.usefixtures("provider_runtime")
def test_fetched_bytes_must_match_the_listed_hash(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    wheel = _wheel(files, "cruxible-provider-web", "0.2.0", lock=b"lock")
    index = _index(tmp_path / "simple", "cruxible-provider-web", [(wheel, "")])
    transport = ArtifactTransport(tmp_path)
    release = find_release((index,), "cruxible-provider-web", None, transport)
    urls = (index, files.as_uri() + "/")
    assert fetch_release(release, urls, transport) == wheel.read_bytes()
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    from cruxible_provider_runtime.errors import RefusalError

    with pytest.raises(RefusalError):
        fetch_release(release, urls, transport)
    # A file origin the operator did not configure is never fetched from.
    wheel.write_bytes(wheel.read_bytes()[: -len(b"tampered")])
    with pytest.raises(RefusalError):
        fetch_release(release, (index,), transport)


def test_a_wheel_without_its_lock_cannot_install_by_name(tmp_path):
    with_lock = _wheel(tmp_path, "cruxible-provider-web", "0.2.0", lock=b"locked")
    assert embedded_lock(with_lock) == b"locked"
    without = _wheel(tmp_path, "cruxible-provider-docs", "0.2.0")
    with pytest.raises(ConfigError, match="does not embed its lock"):
        embedded_lock(without)


@pytest.mark.usefixtures("provider_runtime")
def test_a_release_this_python_cannot_run_is_never_chosen(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    index = _index(
        tmp_path / "simple",
        "cruxible-provider-web",
        [
            (_wheel(files, "cruxible-provider-web", "0.2.0"), ' data-requires-python="&gt;=3.11"'),
            (_wheel(files, "cruxible-provider-web", "0.3.0"), ' data-requires-python="&gt;=99"'),
        ],
    )
    transport = ArtifactTransport(tmp_path)
    assert find_release((index,), "cruxible-provider-web", None, transport).version == "0.2.0"
    with pytest.raises(ConfigError, match="no installable wheel for cruxible-provider-web==0.3.0"):
        find_release((index,), "cruxible-provider-web", "0.3.0", transport)


def test_an_oversized_embedded_lock_refuses_before_decompressing(tmp_path, monkeypatch):
    from zipfile import ZIP_DEFLATED

    from cruxible_core.providers import package_index

    monkeypatch.setattr(package_index, "_LOCK_LIMIT", 1024)
    wheel = tmp_path / "cruxible_provider_web-0.2.0-py3-none-any.whl"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("cruxible_provider_web-0.2.0.dist-info/METADATA", "")
        archive.writestr(
            "cruxible_provider_web-0.2.0.dist-info/extra_metadata/uv.lock", b"x" * 4096
        )
    assert wheel.stat().st_size < 1024
    with pytest.raises(ConfigError, match="embeds a lock over"):
        embedded_lock(wheel)
    monkeypatch.setattr(package_index, "_WHEEL_UNPACKED_LIMIT", 2048)
    with pytest.raises(ConfigError, match="unpacked-size limit"):
        embedded_lock(wheel)
