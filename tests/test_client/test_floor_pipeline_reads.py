"""All workspace inputs reachable from floor writing refuse FIFO opens promptly."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from cruxible_client import _safe_files
from cruxible_client.authoring import workspace as authoring
from cruxible_client.authoring.workspace import WorkspaceError
from cruxible_client.contracts.source_catalog import SourceCatalog, SourceCatalogEntry
from tests.support.fifos import call_with_fifo_timeout
from tests.test_client.test_playbill_workspace import _delta, _export


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    floor = workspace / ".playbill/floor"
    (floor / "sources").mkdir(parents=True)
    (floor / "sources/LEDGER").write_bytes(b"# sources LEDGER  0 sources  changed gen 0\n")
    return workspace


def _catalog(workspace, *, portable=".playbill/sources.yaml"):
    catalog = SourceCatalog(
        catalog_kind="portable",
        entries=(
            SourceCatalogEntry(
                name="page",
                locator="page.md",
                document_id="page",
                document_kind="note",
                title="Page",
                media_type="text/markdown",
                governance_scope=(),
            ),
        ),
    )
    (workspace / portable).write_text(yaml.safe_dump(catalog.model_dump(mode="json")))
    (workspace / "page.md").write_bytes(b"page\n")
    return catalog


def _swap_at_open(monkeypatch, path):
    original = os.open
    seen = []

    def open_file(name, flags, mode=0o777, *, dir_fd=None):
        if not seen and dir_fd is None and Path(name) == path:
            path.unlink()
            os.mkfifo(path)
            seen.append(True)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(_safe_files.os, "open", open_file)
    return seen


@pytest.mark.parametrize("reader", ["delivery-profile", "profile-recording"])
def test_fifo_coverage_config_returns_promptly(tmp_path, reader):
    workspace = _workspace(tmp_path)
    fifo = workspace / ".playbill/coverage.json"
    os.mkfifo(fifo)

    def call():
        if reader == "delivery-profile":
            return authoring.configured_floor_output(workspace)
        return authoring.record_floor_output(
            workspace, instance_id="inst_fifo", server_socket=str(tmp_path / "socket")
        )

    with pytest.raises(WorkspaceError, match="not a regular file"):
        call_with_fifo_timeout(fifo, call)


def test_fifo_projection_manifest_keeps_the_existing_skip_behavior(tmp_path):
    workspace = _workspace(tmp_path)
    _catalog(workspace)
    digest = "a" * 64
    (workspace / "page.md").write_bytes(
        b"<!-- playbill:block:test:ref:" + digest[:12].encode() + b" -->\n"
    )
    manifests = workspace / ".playbill/manifests"
    manifests.mkdir()
    fifo = manifests / (digest + ".json")
    os.mkfifo(fifo)
    assert call_with_fifo_timeout(fifo, lambda: authoring.write_projection_index(workspace)) == 0


@pytest.mark.parametrize(
    "relative", [".playbill/sources.yaml", "sources.yaml", ".playbill/sources.local.yaml"]
)
def test_catalog_swap_to_fifo_keeps_the_existing_invalid_catalog_fallback(
    tmp_path, monkeypatch, relative
):
    workspace = _workspace(tmp_path)
    catalog = _catalog(
        workspace,
        portable="sources.yaml" if relative == "sources.yaml" else ".playbill/sources.yaml",
    )
    fifo = workspace / relative
    if relative.endswith("sources.local.yaml"):
        fifo.write_text(
            yaml.safe_dump(
                SourceCatalog(catalog_kind="local", entries=catalog.entries).model_dump(mode="json")
            )
        )
    seen = _swap_at_open(monkeypatch, fifo)
    assert call_with_fifo_timeout(fifo, lambda: authoring.write_projection_index(workspace)) == 0
    assert seen


def test_bound_source_swap_to_fifo_keeps_the_existing_unreadable_source_fallback(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    _catalog(workspace)
    fifo = workspace / "page.md"
    seen = _swap_at_open(monkeypatch, fifo)
    assert call_with_fifo_timeout(fifo, lambda: authoring.write_projection_index(workspace)) == 0
    assert seen


@pytest.mark.parametrize("force", [True, False])
def test_full_export_comparison_refuses_or_repairs_a_fifo_promptly(tmp_path, force):
    workspace = _workspace(tmp_path)
    export = _export()
    authoring.materialize_floor(workspace, export=export)
    fifo = workspace / ".playbill/floor/cards/fresh.json"
    fifo.unlink()
    os.mkfifo(fifo)

    def call():
        return authoring.materialize_floor(workspace, export=export, force=force)

    if force:
        assert call_with_fifo_timeout(fifo, call).status == "written"
        assert fifo.is_file()
    else:
        with pytest.raises(WorkspaceError, match="non-empty directory"):
            call_with_fifo_timeout(fifo, call)


def test_fifo_git_exclude_refuses_after_applying_the_floor(tmp_path):
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    fifo = workspace / ".git/info/exclude"
    fifo.unlink()
    os.mkfifo(fifo)
    with pytest.raises(WorkspaceError, match="Git info/exclude cannot be read"):
        call_with_fifo_timeout(
            fifo,
            lambda: authoring.write_workspace_floor_delta(
                lambda *_: _delta(),
                instance_id="inst_fifo",
                workspace=workspace,
                server_socket=str(tmp_path / "socket"),
            ),
        )
    assert (workspace / ".playbill/floor/manifest.json").is_file()


def test_regular_catalog_overlay_and_bound_source_keep_their_results(tmp_path):
    from cruxible_client.authoring.selectors import WorkspaceSources

    workspace = _workspace(tmp_path)
    portable = _catalog(workspace)
    local = SourceCatalog(catalog_kind="local", entries=portable.entries)
    (workspace / ".playbill/sources.local.yaml").write_text(
        yaml.safe_dump(local.model_dump(mode="json"))
    )
    sources = WorkspaceSources(workspace)
    assert sources.select("page.md").content == b"page\n"
    assert authoring.write_projection_index(workspace) == 0


@pytest.mark.parametrize("boundary", ["open", "stage"])
def test_git_info_fifo_swap_refuses_or_persists_through_the_held_descriptor(
    tmp_path, monkeypatch, boundary
):
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    info = workspace / ".git/info"
    held = info.with_name("info-held")
    identity = info.stat().st_ino
    exclude = info / "exclude"
    exclude.chmod(0o640)
    old_bytes = exclude.read_bytes()
    original = os.open
    swapped = []

    def open_file(name, flags, mode=0o777, *, dir_fd=None):
        at_open = dir_fd is None and Path(name) == info
        at_stage = dir_fd is not None and os.fstat(dir_fd).st_ino == identity and flags & os.O_CREAT
        if not swapped and ((boundary == "open" and at_open) or (boundary == "stage" and at_stage)):
            info.rename(held)
            os.mkfifo(info)
            swapped.append(True)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(authoring.os, "open", open_file)

    def write():
        return authoring.write_workspace_floor_delta(
            lambda *_: _delta(),
            instance_id="inst_fifo",
            workspace=workspace,
            server_socket=str(tmp_path / "socket"),
        )

    previous_umask = os.umask(0o077)
    try:
        if boundary == "open":
            with pytest.raises(WorkspaceError, match="Git info/exclude cannot be read"):
                call_with_fifo_timeout(info, write)
            assert (held / "exclude").read_bytes() == old_bytes
        else:
            assert call_with_fifo_timeout(info, write)[1].status == "written"
            assert (held / "exclude").read_bytes() == old_bytes + b"/.playbill/coverage.json\n"
            assert (held / "exclude").stat().st_mode & 0o777 == 0o640
            assert sorted(path.name for path in held.iterdir()) == ["exclude"]
    finally:
        os.umask(previous_umask)
    assert swapped and info.is_fifo()
    assert (workspace / ".playbill/floor/manifest.json").is_file()
