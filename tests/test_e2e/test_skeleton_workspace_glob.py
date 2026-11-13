"""``workspace.files`` (D-R41, ruling O2): one Capture per matched file, as a Capture list.

The call names ``glob`` and the slot the ``workspace.files`` capability. The
daemon lists the matches, sorted by their real names, and reads each as an
ordinary one-file read; the call's ``captures`` list field holds their Captures
in that order, and an extract over it merges their records, each citing its own file.
Past ``max_files`` the read refuses. A file that fails to read is dropped and
the list is incomplete. A file the glob no longer matches retires nothing: an
extract straight over a glob never judges absence.

Work counts: a glob over 10 and 40 files costs the same per file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
from cruxible_client.contracts.query.authored import QuerySpecInput
from tests.support.operation_world import author
from tests.support.procedures import procedure_limits
from tests.support.procedures import procedure_spec as build_spec
from tests.test_e2e.test_skeleton_capture_lists import install
from tests.test_e2e.test_skeleton_sbom_late_read import (
    COMPONENT,
    LISTED_IN,
    MEMBER_ROW,
    PURL,
    SBOM_KIND,
    Sbom,
    activate,
    proposed,
    run,
    sbom_world,
)

LINE = "test.glob-line"
EVERY = "test.every_component"


def glob_spec(*, glob: str = "inventories/*.json", max_files: int = 100) -> Any:
    """fetch every matched file -> extract their records -> claims (mirror over all held)."""

    return build_spec(
        "test.glob-import",
        reference_text=True,
        input={},
        output={},
        slots={"read": {"capability": "workspace.files@1"}},
        hard_caps=procedure_limits(provider_calls=64, capture_bytes=4_194_304, read_rows=500),
        steps=[
            {"read": "held", "query": EVERY},
            {
                "call": "files",
                "slot": "read",
                "captures": {"captures": SBOM_KIND},
                "input": {
                    "logical_source": "test.inventory.files",
                    "glob": glob,
                    "max_files": max_files,
                },
            },
            {
                "extract": "members",
                "capture": "$steps.files.captures",
                "records": "members",
                "duplicates": "refuse_key",
            },
            {
                "id": "write",
                "for_each": "$steps.members.records",
                "subject": {
                    "kind": COMPONENT,
                    "key": {"purl": "$item.key.purl"},
                    "create": "if_absent",
                },
                "expect": "$steps.held.rows",
                "claims": [
                    {
                        "type": LISTED_IN,
                        "value": "$item.fields.inventory",
                        "when_absent": "mirror",
                        "rationale": "The inventory file lists the component.",
                    }
                ],
            },
        ],
    )


def glob_world(tmp_path: Path, **spec: Any) -> tuple[Any, Path]:
    instance, root = sbom_world(tmp_path)
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": EVERY,
                    "description": "Every component.",
                    "from": {"c": COMPONENT},
                    "row": MEMBER_ROW.ref.spelling,
                    "fields": [{"purl": f"c.{PURL}"}],
                    "limit": 2000,
                }
            ),
        ),
        name="every-component",
    )
    install(instance, glob_spec(**spec), line=LINE)
    (root / "inventories").mkdir()
    return instance, root


def write(root: Path, name: str, inventory: str, purls: list[str]) -> None:
    members = [{"purl": purl, "inventory": inventory} for purl in purls]
    (root / "inventories" / name).write_text(json.dumps({"members": members}, sort_keys=True))


def listed(instance: Any) -> set[str]:
    """The live listed_in values, as (purl, inventory) text."""

    from tests.test_e2e.test_skeleton_osv_upsert import _paths

    purls = {path: purl for purl, path in _paths(instance, PURL).items()}
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    from cruxible_client.contracts.claims import parse_claim

    found = set()
    for path in tree:
        if path.startswith("claims/"):
            claim = parse_claim(tree[path], path=path)
            if claim.statement.predicate == LISTED_IN and claim.lifecycle.state == "live":
                found.add(
                    f"{purls[claim.statement.subject.artifact_path]}={claim.statement.object.value}"
                )
    return found


def _glob_run(instance: Any, root: Path, *, minute: int = 0) -> Any:
    return run(instance, root, Sbom(), minute=minute, line=LINE)


def _members(state: Any) -> tuple[str, ...]:
    (call,) = (node for node in state.nodes if node.node == "files")
    return tuple(call.summary.capture_digests)


A, B, C = "pkg:pypi/a@1", "pkg:pypi/b@1", "pkg:pypi/c@1"


def test_a_glob_reads_every_match_in_order_as_one_capture_each(tmp_path: Path) -> None:
    instance, root = glob_world(tmp_path)
    write(root, "b.json", "inv-b", [B])
    write(root, "a.json", "inv-a", [A])
    (root / "inventories" / "notes.txt").write_text("not matched")
    (root / "inventories" / ".hidden.json").write_text("{}")
    first = _glob_run(instance, root)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    members = _members(first)
    assert len(members) == 2
    claims = [c for c in proposed(instance, first) if c.statement.predicate == LISTED_IN]
    # Each Claim cites the one file its record came from: a.json sorts first.
    by_value = {c.statement.object.value: c.backing.capture_digests for c in claims}
    assert by_value == {"inv-a": (members[0],), "inv-b": (members[1],)}
    activate(instance, first)
    assert listed(instance) == {f"{A}=inv-a", f"{B}=inv-b"}


def test_a_file_the_glob_no_longer_matches_retires_nothing(tmp_path: Path) -> None:
    instance, root = glob_world(tmp_path)
    write(root, "a.json", "inv-a", [A])
    write(root, "b.json", "inv-b", [B])
    activate(instance, _glob_run(instance, root))
    os.remove(root / "inventories" / "b.json")
    write(root, "a.json", "inv-a", [A, C])
    second = _glob_run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    (egress,) = second.terminal_egress
    kinds = sorted(child.change_kind for child in egress.children if child.change_kind)
    assert "retirement" not in kinds, kinds
    activate(instance, second)
    assert listed(instance) == {f"{A}=inv-a", f"{B}=inv-b", f"{C}=inv-a"}


def test_a_glob_past_its_cap_refuses_before_reading(tmp_path: Path) -> None:
    instance, root = glob_world(tmp_path, max_files=2)
    for index in range(3):
        write(root, f"f{index}.json", f"inv-{index}", [f"pkg:pypi/x{index}@1"])
    state = _glob_run(instance, root)
    assert state.status == "node_refused", state.status
    terminal = state.terminal.model_dump(mode="json")
    assert terminal["code"] == "workspace_file_read_refused", terminal
    assert terminal["details"]["path_class"] == "glob_over_cap", terminal
    assert listed(instance) == set()


def test_a_member_that_fails_to_read_is_dropped_and_the_list_is_incomplete(
    tmp_path: Path,
) -> None:
    instance, root = glob_world(tmp_path)
    write(root, "a.json", "inv-a", [A])
    write(root, "real.json", "inv-c", [C])
    # A link matches the glob; its read refuses (never followed), so it is dropped.
    os.symlink(root / "inventories" / "real.json", root / "inventories" / "b.json")
    state = _glob_run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert len(_members(state)) == 2  # a.json and real.json; the link is dropped
    assert state.completeness is not None and not state.completeness.complete
    activate(instance, state)
    assert listed(instance) == {f"{A}=inv-a", f"{C}=inv-c"}


def test_an_empty_glob_reads_nothing_and_writes_nothing(tmp_path: Path) -> None:
    instance, root = glob_world(tmp_path)
    state = _glob_run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert _members(state) == ()


# -- work counts: linear in the files matched ----------------------------------------


def _counted(tmp_path: Path, files: int, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """One measured glob read of ``files`` members, counting SQL, opens, and the
    run-state reconstruction's own work: run-value hashing (F1) and directory
    enumeration (F2), both of which stay outside ``sql_statements``/``file_opens``.
    """

    from cruxible_core.service.procedures import procedure_runs
    from tests.test_scale._counters import counting, trace_sqlite

    held: dict[str, Any] = {"window": None, "hash_calls": 0}
    original_digest = procedure_runs.run_value_digest

    def counted_digest(domain: str, value: object) -> str:
        if held["window"] is not None:
            held["hash_calls"] += 1
        return original_digest(domain, value)

    monkeypatch.setattr(procedure_runs, "run_value_digest", counted_digest)

    original_listdir = os.listdir
    listing: dict[str, int] = {"calls": 0, "entries": 0}

    def counted_listdir(path: Any = ".", *args: Any, **kwargs: Any) -> list[str]:
        names = original_listdir(path, *args, **kwargs)
        if held["window"] is not None:
            listing["calls"] += 1
            listing["entries"] += len(names)
        return names

    monkeypatch.setattr(os, "listdir", counted_listdir)

    with trace_sqlite(tmp_path):
        instance, root = glob_world(tmp_path)
        for index in range(files):
            write(root, f"f{index:03d}.json", f"inv-{index}", [f"pkg:pypi/x{index}@1"])
        activate(instance, _glob_run(instance, root))
        with instance.bind_accepted_projection(instance.accepted_coordinate()):
            pass
        with counting(tmp_path) as window:
            held["window"] = window
            state = _glob_run(instance, root, minute=1)
        held["window"] = None
    assert state.status == "succeeded", state.terminal
    assert not window.unexplained, window.unexplained
    return {
        "provider_calls": state.receipt.budget.observed.provider_calls,
        "sql_statements": window.counts["sql_statements"],
        "file_opens": window.counts["file_opens"],
        "hash_calls": held["hash_calls"],
        "dir_listings": listing["calls"],
        "dir_entries": listing["entries"],
    }


@pytest.mark.parametrize("sizes", [(10, 25, 40)])
def test_a_glob_costs_the_same_per_file_at_any_count(
    tmp_path: Path, sizes: tuple[int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    measured = {}
    for size in sizes:
        (tmp_path / f"n{size}").mkdir()
        measured[size] = _counted(tmp_path / f"n{size}", size, monkeypatch)
    small, middle, large = (measured[size] for size in sizes)
    assert [measured[size]["provider_calls"] for size in sizes] == list(sizes), measured
    # One run-value hash per matched file (F1): the run-state reconstruction
    # folds each derived request's digest in once, never re-hashing the
    # others at every later invocation start.
    assert [measured[size]["hash_calls"] for size in sizes] == list(sizes), measured
    for counter in (
        "sql_statements",
        "file_opens",
        "dir_listings",
        "dir_entries",
    ):
        low = (middle[counter] - small[counter]) / (sizes[1] - sizes[0])
        high = (large[counter] - middle[counter]) / (sizes[2] - sizes[1])
        assert high <= low * 1.15 + 2.0, (counter, measured)
    report = os.environ.get("GLOB_COUNTS")
    if report:
        with open(report, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(measured) + "\n")
