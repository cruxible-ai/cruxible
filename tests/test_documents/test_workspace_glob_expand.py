"""The daemon's glob listing for ``workspace.file`` (D-R41): sorted, bounded, no links."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.workspace_file import (
    WorkspaceFileGlobRequestV1,
    is_workspace_glob,
    parse_authored_workspace_glob,
)
from cruxible_core.documents.workspace_file import WorkspaceFileReader, WorkspaceFileReadRefused


def _request(glob: str, **changes: object) -> WorkspaceFileGlobRequestV1:
    return WorkspaceFileGlobRequestV1.model_validate(
        {
            "logical_source": "test.files",
            "glob": glob,
            "coordinate_type": "database-snapshot-v1",
            "coordinate": {},
            "selector_type": "relation-primary-key-v1",
            "selector": {},
            **changes,
        }
    )


def _reader(root: Path) -> WorkspaceFileReader:
    return WorkspaceFileReader(
        instance_id="inst_test", operating_profile="local", attached_roots=(root,)
    )


def test_a_glob_lists_its_matches_sorted_by_real_name(tmp_path: Path) -> None:
    (tmp_path / "data" / "b").mkdir(parents=True)
    (tmp_path / "data" / "a").mkdir()
    for path in ("data/b/2.json", "data/a/1.json", "data/a/z.json", "data/a/x.txt"):
        (tmp_path / path).write_text("{}")
    (tmp_path / "data" / "a" / ".hidden.json").write_text("{}")
    (tmp_path / "data" / "a" / "dir.json").mkdir()  # a directory never matches
    matches = _reader(tmp_path).expand(_request("data/*/*.json"))
    assert matches == ("data/a/1.json", "data/a/z.json", "data/b/2.json")
    assert _reader(tmp_path).expand(_request("data/a/.*.json")) == ("data/a/.hidden.json",)


def test_a_glob_never_walks_into_a_link_and_never_matches_control_paths(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.json").write_text("{}")
    os.symlink(tmp_path / "real", tmp_path / "linked")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "f.json").write_text("{}")
    assert _reader(tmp_path).expand(_request("*/f.json")) == ("real/f.json",)
    assert _reader(tmp_path).expand(_request(".*/f.json")) == ()


def test_more_matches_than_the_cap_refuse(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"{index}.json").write_text("{}")
    assert len(_reader(tmp_path).expand(_request("*.json", max_files=3))) == 3
    with pytest.raises(WorkspaceFileReadRefused) as refused:
        _reader(tmp_path).expand(_request("*.json", max_files=2))
    assert refused.value.path_class == "glob_over_cap"


@pytest.mark.parametrize("glob", ["data/**/x.json", "data/x.json", "../*.json", "/abs/*.json"])
def test_a_glob_names_a_pattern_within_its_workspace(glob: str) -> None:
    with pytest.raises(ValidationError):
        _request(glob)


def test_a_glob_input_is_told_apart_and_never_carries_the_binding_digest() -> None:
    assert is_workspace_glob({"glob": "*.json"})
    assert not is_workspace_glob({"relative_path": "a.json"})
    with pytest.raises(ValueError, match="resolved by the daemon"):
        parse_authored_workspace_glob({"glob": "*.json", "workspace_binding_digest": "x"})
