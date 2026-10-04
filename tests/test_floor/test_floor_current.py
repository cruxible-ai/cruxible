"""The values-first current/ layer: one readable, greppable file per Subject."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_path,
    render_document,
)
from cruxible_client.contracts.get_reads import ExactContentRef
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.floor.floor import service_export_playbill_floor
from cruxible_core.service.floor.floor_current import (
    INLINE_TEXT_BYTES,
    ValueRenderer,
    literal_scalar,
    yaml_scalar,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"
RULING = "Rulings are text.\nEvery line of this one greps on its own.\n"
LONG_RULING = "".join(f"Clause {index}: the floor keeps every word.\n" for index in range(80))
NOTE = "# Design note\n\nThe floor is the grep-first front door.\n"
BODY_READER = BodyAccessContext(principal_id="owner", can_read_body=True)


def _write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> WriteOutcome:
    request = WriteRequest.model_validate(
        {"because": "The writer checked it.", "changes": list(changes), **options}
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _set(subject: str, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


def _add(subject: str, field: str, value: object) -> dict[str, Any]:
    return {"op": "add", "subject": subject, "field": field, "value": value}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("floor-current"))
    written = _write(
        instance,
        _set(WI1, "title", "Tidy the CLI: part #1"),
        _set(WI1, "status", "ready"),
        _add(WI1, "governs", WI2),
        _add(WI1, "governs", WI3),
        _set(WI1, "ruling", RULING),
        _set(WI1, "measured", 3),
    )
    _write(instance, _set(WI3, "ruling", LONG_RULING))
    _add_document(instance, "design-note", NOTE.encode())
    first = _write(instance, _set(WI2, "status", "ready"))
    _write(instance, _set(WI2, "status", "blocked"))
    _write(instance, _set(WI2, "status", "done", contend=True), at=first.coordinate.git_oid)
    claims = {change.field: change.claim for change in written.changes}
    return {
        "instance": instance,
        "claims": claims,
        "governs": sorted(change.claim for change in written.changes if change.field == "governs"),
        "files": service_export_playbill_floor(instance),
        "readable": service_export_playbill_floor(instance, access=BODY_READER),
    }


def _add_document(instance: PlaybillInstance, name: str, body: bytes) -> None:
    shell = DocumentShell(
        identity=f"document:{name}",
        document_kind="design",
        title="Design note",
        media_type="text/markdown",
        body_digest=instance.store_document_body(body).digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[document_path(name)] = render_document(shell)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-30T11:59:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


def _current(world: dict[str, Any], subject_id: str) -> str:
    return world["files"][f"current/{KIND}/{subject_id}.yaml"].decode()


def test_the_header_is_one_line_naming_ref_kind_and_when_it_changed(
    world: dict[str, Any],
) -> None:
    instance: PlaybillInstance = world["instance"]
    head = instance.accepted_coordinate().git_oid
    manifest = json.loads(world["files"]["manifest.json"])
    assert manifest["coordinate"]["git_oid"] == head
    rows = {row["path"]: row for row in manifest["files"]}
    # wi-1 was written by the first write and wi-2 by the last: each file names
    # its own last change, not the export's head.
    wi1 = rows[f"current/{KIND}/wi-1.yaml"]["changed_at"]
    wi2 = rows[f"current/{KIND}/wi-2.yaml"]["changed_at"]
    assert wi1 < wi2 == manifest["generation"]
    first_line = _current(world, "wi-1").splitlines()[0]
    assert first_line == f"# {WI1}  kind={KIND}  changed gen {wi1}"


def test_values_come_first_under_short_names_with_their_claim(world: dict[str, Any]) -> None:
    text = _current(world, "wi-1")
    claims = world["claims"]
    assert f'title: "Tidy the CLI: part #1"  # {claims["title"]}\n' in text
    assert f"status: ready  # {claims['status']}\n" in text
    # A value line names its Claim, never the Captures behind it.
    assert "CAP-" not in text
    assert "measured: 3  # " in text
    governs = text[text.index("governs:\n") :].splitlines()[1:3]
    assert [line.split("  # ")[0] for line in governs] == [f"  - {WI2}", f"  - {WI3}"]
    assert sorted(line.split("  # ")[1].split()[0] for line in governs) == world["governs"]


def test_current_files_hold_no_digests_or_addresses(world: dict[str, Any]) -> None:
    for path, content in world["files"].items():
        if not path.startswith("current/"):
            continue
        text = content.decode()
        assert "sha256:" not in text, path
        assert "subjects/" not in text, path
        assert text.count(" changed gen ") == 1, path
        if path.endswith(".yaml"):
            assert len(content) < 2048, path


def test_every_current_file_parses_as_yaml_with_its_values(world: dict[str, Any]) -> None:
    parsed = yaml.safe_load(_current(world, "wi-1"))
    assert parsed["title"] == "Tidy the CLI: part #1"
    assert parsed["status"] == "ready"
    assert parsed["governs"] == [WI2, WI3]
    assert parsed["measured"] == 3
    assert parsed["ruling"] == RULING
    # No verdict flags: "measured" has no captured evidence, and only get says so.
    assert "flags" not in parsed
    for path, content in world["files"].items():
        if path.startswith("current/") and path.endswith(".yaml"):
            assert isinstance(yaml.safe_load(content), dict | None), path


def test_a_contested_slot_lists_every_live_value_and_is_flagged(world: dict[str, Any]) -> None:
    parsed = yaml.safe_load(_current(world, "wi-2"))
    assert sorted(parsed["status"]) == ["blocked", "done"]
    assert "contested" in parsed["flags"]["status"]


def test_the_floor_holds_no_provenance_mirror_or_source_bodies(world: dict[str, Any]) -> None:
    for files in (world["files"], world["readable"]):
        assert not any(path.startswith(("provenance/", "documents/")) for path in files)
        assert not any(NOTE.encode() in content for content in files.values())
    manifest = json.loads(world["files"]["manifest.json"])
    assert manifest["generation"] == len(world["instance"].accepted_history()) - 1


def test_identical_accepted_state_gives_identical_bytes(world: dict[str, Any]) -> None:
    instance: PlaybillInstance = world["instance"]
    instance.floor_export_memo.clear()
    instance.floor_structure_memo.clear()
    assert service_export_playbill_floor(instance) == world["files"]


@pytest.mark.parametrize(
    "value",
    [
        "ready",
        "Tidy the CLI",
        "yes",
        "No",
        "null",
        "2026-09-30",
        "12",
        "- dash",
        "a: b",
        "trailing ",
        "hash #tag",
        'quote "inside"',
        "tab\there",
        "line\u2028separator",
        "del\x7fchar",
        "",
        "ünïcode välue",
        "[flow]",
    ],
)
def test_scalars_round_trip_through_yaml(value: str) -> None:
    assert yaml.safe_load(f"k: {yaml_scalar(value)}\n") == {"k": value}


@pytest.mark.parametrize("value", [None, True, False, 0, -3, {"b": [1, "x"], "a": None}, ["z"]])
def test_literal_values_round_trip_through_yaml(value: object) -> None:
    assert yaml.safe_load(f"k: {literal_scalar(value)}\n") == {"k": value}


def test_exact_content_reads_as_its_text_and_each_line_greps(world: dict[str, Any]) -> None:
    text = _current(world, "wi-1")
    ruling = world["claims"]["ruling"]
    assert f"ruling: |  # {ruling}\n" in text
    assert "\n  Every line of this one greps on its own.\n" in text


def test_a_long_text_goes_whole_to_a_sibling_file_never_truncated(world: dict[str, Any]) -> None:
    parsed = yaml.safe_load(_current(world, "wi-3"))
    assert parsed["ruling"] == {
        "full_text": "wi-3.ruling.txt",
        "bytes": len(LONG_RULING.encode()),
        "lines": LONG_RULING.count("\n") + 1,
    }
    header, full = world["files"][f"current/{KIND}/wi-3.ruling.txt"].decode().split("\n", 1)
    assert header.startswith(f"# {WI3}  field=ruling  CLM-") and " gen " in header
    assert full == LONG_RULING
    assert len(LONG_RULING.encode()) > INLINE_TEXT_BYTES


def test_bytes_that_are_not_text_show_a_typed_marker_with_their_size(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance: PlaybillInstance = world["instance"]
    renderer = ValueRenderer(instance)
    monkeypatch.setattr(
        renderer._content,
        "of",
        lambda _obj: ExactContentRef(
            exact_content="binary", content_digest="sha256:" + "0" * 64, length=12
        ),
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        claim = projection.typed.source(f"Claim:{world['claims']['ruling']}")
    shown = renderer.shown(claim, text_name="wi-1.ruling.txt")
    assert shown.scalar == "{exact_content: binary, bytes: 12}"
    assert yaml.safe_load(f"k: {shown.scalar}") == {"k": {"exact_content": "binary", "bytes": 12}}


@pytest.mark.parametrize(("mode", "bodies"), [("READ_ONLY", False), ("GOVERNED_WRITE", True)])
def test_the_export_carries_bodies_only_for_a_caller_who_may_read_them(
    monkeypatch: pytest.MonkeyPatch, mode: str, bodies: bool
) -> None:
    from types import SimpleNamespace

    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope

    seen: list[BodyAccessContext] = []

    def export(_instance: object, **kwargs: Any) -> dict[str, bytes]:
        seen.append(kwargs["access"])
        raise LookupError("stop after the access decision")

    monkeypatch.setattr(playbill_api, "service_export_playbill_floor", export)
    monkeypatch.setattr(
        playbill_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _id: None)
    )
    with request_permission_scope(PermissionMode[mode]), pytest.raises(LookupError):
        playbill_api.playbill_export_floor("inst_floor")
    assert [item.can_read_body for item in seen] == [bodies]


def _export_envelope(files: dict[str, bytes]) -> Any:
    import base64

    from cruxible_client import contracts

    manifest = json.loads(files["manifest.json"])
    return contracts.FloorExport(
        tag=manifest["format"],
        coordinate=manifest["coordinate"],
        manifest=manifest,
        files=[
            contracts.FloorFile(path=path, content_base64=base64.b64encode(content).decode("ascii"))
            for path, content in files.items()
        ],
    )


def test_the_agent_path_carries_no_digests(world: dict[str, Any]) -> None:
    files = world["readable"]
    agent_path = {
        path: content.decode() for path, content in files.items() if path.startswith("current/")
    }
    assert agent_path
    for path, text in agent_path.items():
        assert "sha256:" not in text, path
        assert '"artifact_path"' not in text and "subjects/" not in text.split("\n", 1)[-1], path


def test_every_floor_reader_still_verifies_the_export(
    world: dict[str, Any], tmp_path_factory: pytest.TempPathFactory
) -> None:
    from cruxible_client.authoring.workspace import (
        inspect_workspace_floor,
        materialize_floor,
        record_floor_output,
        verified_floor_files,
    )
    from cruxible_client.contracts import AcceptedCoordinate
    from cruxible_core.coverage.middleware import FloorFreshnessManifestV2

    files = world["readable"]
    export = _export_envelope(files)
    assert verified_floor_files(export) == files
    manifest = FloorFreshnessManifestV2.model_validate(json.loads(files["manifest.json"]))
    assert manifest.floor_digest == json.loads(files["manifest.json"])["floor_digest"]
    workspace = tmp_path_factory.mktemp("floor-workspace")
    written = materialize_floor(workspace, export=export)
    assert written.file_count == len(files)
    record_floor_output(workspace, instance_id="inst_floor", server_socket="daemon.sock")
    status = inspect_workspace_floor(
        workspace,
        current_coordinate=AcceptedCoordinate.model_validate(
            manifest.coordinate.model_dump(mode="json")
        ),
    )
    assert status.status == "current"
    assert (workspace / ".playbill/floor/current" / KIND / "wi-1.yaml").read_bytes() == files[
        f"current/{KIND}/wi-1.yaml"
    ]


def test_each_kind_has_an_index_line_per_subject(world: dict[str, Any]) -> None:
    instance: PlaybillInstance = world["instance"]
    header, *rows = world["files"][f"current/{KIND}/INDEX"].decode().splitlines()
    assert header == (
        f"# {KIND} INDEX  3 subjects  columns: ref, title, states  "
        f"changed gen {len(instance.accepted_history()) - 1}"
    )
    assert rows == [
        f"{WI1}\tTidy the CLI: part #1\tstatus=ready",
        f"{WI2}\t-\tstatus=blocked|done",
        f"{WI3}\t-\t-",
    ]


def test_the_index_title_prefers_title_and_keeps_one_line() -> None:
    from cruxible_core.service.floor.floor_current import _index_title

    assert _index_title({"name": ("n",), "title": ("Line one\nline two",)}) == "Line one"
    assert _index_title({"task_title": ("T",), "status": ("s",)}) == "T"
    assert _index_title({"labels": ("a", "b")}) == ""
    assert len(_index_title({"title": ("x" * 500,)})) == 120


def test_a_later_export_renders_only_what_the_change_records_touched(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.floor import floor_index

    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"), _set(WI2, "status", "done"))
    before = service_export_playbill_floor(instance)
    rendered: list[str] = []
    render = floor_index.render_subject

    def counting(**kwargs: Any) -> Any:
        rendered.append(kwargs["path"])
        return render(**kwargs)

    monkeypatch.setattr(floor_index, "render_subject", counting)
    _write(instance, _set(WI3, "title", "Only this one moved"))
    after = service_export_playbill_floor(instance)
    assert rendered == [f"subjects/{WI3}.json"]
    # An untouched Subject keeps its bytes, stamp included.
    assert after[f"current/{WI1}.yaml"] == before[f"current/{WI1}.yaml"]
    assert "title: Only this one moved  # CLM-" in after[f"current/{WI3}.yaml"].decode()

    # However the floor got here, identical accepted state gives identical bytes.
    instance.floor_current_memo.clear()
    assert service_export_playbill_floor(instance) == after


def test_orient_reports_the_workspace_floor_and_how_far_behind_it_is(tmp_path: Any) -> None:
    from cruxible_client.authoring.workspace import (
        materialize_floor,
        workspace_floor_freshness,
    )
    from cruxible_core.service.discovery.orient import service_playbill_orient

    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    workspace = tmp_path / "floor-workspace"
    workspace.mkdir()
    assert workspace_floor_freshness(workspace, service_playbill_orient(instance)).floor is None

    _write(instance, _set(WI1, "status", "ready"))
    exported_at = instance.accepted_coordinate().git_oid
    materialize_floor(workspace, export=_export_envelope(service_export_playbill_floor(instance)))
    current = workspace_floor_freshness(workspace, service_playbill_orient(instance))
    assert current.floor is not None
    assert (current.floor.at, current.floor.generations_behind) == (exported_at, 0)

    _write(instance, _set(WI2, "status", "done"))
    _write(instance, _set(WI3, "status", "blocked"))
    stale = workspace_floor_freshness(workspace, service_playbill_orient(instance))
    assert stale.floor is not None
    assert (stale.floor.at, stale.floor.generations_behind) == (exported_at, 2)
    assert stale.model_dump(mode="json")["floor"] == {"at": exported_at, "generations_behind": 2}

    manifest_path = workspace / ".playbill/floor/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("generation")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    unknown = workspace_floor_freshness(workspace, service_playbill_orient(instance))
    assert unknown.floor is not None and unknown.floor.generations_behind is None

    config = workspace / ".playbill/coverage.json"
    config.write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "instance_id": "inst_another",
                "server_socket": "daemon.sock",
            }
        ),
        encoding="utf-8",
    )
    assert workspace_floor_freshness(workspace, service_playbill_orient(instance)).floor is None


def test_the_default_floor_leaves_the_discovery_cards_out(world: dict[str, Any]) -> None:
    instance: PlaybillInstance = world["instance"]
    default = set(world["files"])
    assert not any(
        path.startswith(("subjects/", "claim-types/", "procedures/")) for path in default
    )
    assert "coverage-manifest.json" not in default
    assert json.loads(world["files"]["manifest.json"])["format"] == "playbill-floor-export-v5"

    full = service_export_playbill_floor(instance, include=("discovery",))
    assert f"subjects/{KIND}/wi-1.profile.json" in full
    assert f"claim-types/{KIND}/status.card.json" in full
    assert "coverage-manifest.json" in full
    # The grep-first layer is the same bytes either way.
    assert {path: full[path] for path in default if path != "manifest.json"} == {
        path: content for path, content in world["files"].items() if path != "manifest.json"
    }
    with pytest.raises(ValueError, match="unsupported floor export part"):
        service_export_playbill_floor(instance, include=("everything",))  # type: ignore[arg-type]


def test_the_refresh_profile_records_opt_in_parts_and_rewrites_old_formats(
    tmp_path: Any,
) -> None:
    from cruxible_client.authoring.workspace import (
        WorkspaceError,
        configured_floor_output,
        record_floor_output,
        refresh_workspace_floor,
    )

    config = tmp_path / ".playbill/coverage.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "instance_id": "inst_floor",
                "server_socket": "daemon.sock",
                "floor_output": {
                    "tag": "playbill-floor-output-v1",
                    "format": "playbill-floor-export-v3",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceError, match="floor export --force"):
        configured_floor_output(tmp_path)

    record_floor_output(tmp_path, instance_id="inst_floor", include=("discovery",))
    written = json.loads(config.read_text(encoding="utf-8"))
    assert written["floor_output"] == {
        "tag": "playbill-floor-output-v1",
        "format": "playbill-floor-export-v5",
        "include": ["discovery"],
    }
    assert written["server_socket"] == "daemon.sock"
    assert configured_floor_output(tmp_path) == (".playbill/floor", ("discovery",))

    seen: list[dict[str, Any]] = []

    class _Client:
        def export_floor(self, _instance_id: str, **kwargs: Any) -> Any:
            seen.append(kwargs)
            raise LookupError("stop after the request")

        def floor_delta(self, _instance_id: str, **kwargs: Any) -> Any:
            seen.append(kwargs)
            raise LookupError("stop after the request")

    result = refresh_workspace_floor(_Client(), "inst_floor", workspace=tmp_path)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert seen == [{"at": None, "include": ("discovery",)}]

    record_floor_output(tmp_path, instance_id="inst_floor")
    assert "include" not in json.loads(config.read_text(encoding="utf-8"))["floor_output"]
    refresh_workspace_floor(_Client(), "inst_floor", workspace=tmp_path)  # type: ignore[arg-type]
    # The default floor is asked for as a delta from the floor this workspace holds.
    assert seen[-1] == {"at": None, "base_generation": None, "base_renderer": None}


def test_every_handle_the_floor_prints_resolves_through_get(world: dict[str, Any]) -> None:
    import re

    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    instance: PlaybillInstance = world["instance"]
    text = "".join(
        content.decode()
        for path, content in world["files"].items()
        if path.startswith("current/") and path.endswith(".yaml")
    )
    claims = sorted(set(re.findall(r"CLM-[0-9a-f]{32}\b", text)))
    refs = sorted(set(re.findall(rf"^# ({KIND}/[\w-]+) ", text, re.MULTILINE)))
    assert claims and refs
    for ref in (*claims, *refs):
        result = service_playbill_get(
            instance,
            request=GetRequest(ref=ref),
            access=BodyAccessContext(principal_id="owner"),
        )
        assert result.card is not None, ref


def test_the_shared_write_records_a_profile_only_where_it_can_name_a_daemon(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from cruxible_client.authoring.workspace import configured_floor_output, write_workspace_floor

    files = world["readable"]

    bare = tmp_path / "bare"
    bare.mkdir()
    export, written = write_workspace_floor(
        lambda: _export_envelope(files), instance_id="inst_floor", workspace=bare
    )
    assert written.file_count == len(files) and export.manifest["format"].endswith("-v5")
    assert not (bare / ".playbill/coverage.json").exists()

    named = tmp_path / "named"
    named.mkdir()
    write_workspace_floor(
        lambda: _export_envelope(files),
        instance_id="inst_floor",
        workspace=named,
        include=("discovery",),
        server_socket="daemon.sock",
    )
    assert configured_floor_output(named) == (".playbill/floor", ("discovery",))


def _spoil(instance: PlaybillInstance, digest: str, how: str) -> None:
    """Erase a body, or corrupt it in place as a rotting disk would.

    ``corrupt-keep-mtime`` also restores the file's times, so only its inode
    change time says it moved.
    """

    import os

    store = instance.body_store()
    if how == "erase":
        assert store.erase(digest)
        return
    path = store._path(digest)
    before = path.stat()
    os.chmod(path, 0o600)
    content = path.read_bytes()
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    if how == "corrupt-keep-mtime":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        assert path.stat().st_size == before.st_size


def _cold(instance: PlaybillInstance, **options: Any) -> dict[str, bytes]:
    """An export with no kept floor output and no remembered verdict derivation."""

    instance.floor_export_memo.clear()
    instance.floor_structure_memo.clear()
    instance.floor_current_memo.clear()
    return service_export_playbill_floor(instance, **options)


def _outcome(export: Any) -> Any:
    try:
        return export()
    except Exception as exc:  # noqa: BLE001 - the refusal itself is the outcome compared
        return (type(exc).__name__, str(exc))


@pytest.mark.parametrize("target", ["ruling", "long-ruling"])
def test_a_lost_body_refuses_a_fresh_render(tmp_path: Any, target: str) -> None:
    """Accepted-body retention: the floor renders exact content by digest.

    A body lost anyway refuses a fresh render as an integrity failure; the
    floor never publishes a marker, or any other bytes, in place of the text.
    """

    from cruxible_client.contracts.errors import ProjectionIntegrityError

    instance, _owner = seed_write_surface(tmp_path)
    written = _write(instance, _set(WI1, "ruling", RULING), _set(WI3, "ruling", LONG_RULING))
    warm = service_export_playbill_floor(instance, access=BODY_READER)
    assert warm[f"current/{KIND}/wi-3.ruling.txt"].decode().endswith(LONG_RULING)
    field = {"ruling": WI1, "long-ruling": WI3}[target]
    claim_id = next(change.claim for change in written.changes if change.subject == field)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        digest = projection.typed.source(f"Claim:{claim_id}").statement.object.content_digest
    _spoil(instance, digest, "erase")
    with pytest.raises(ProjectionIntegrityError, match="not retained"):
        _cold(instance, access=BODY_READER)


def test_kept_discovery_cards_agree_with_cold_ones_after_a_capture_is_erased(
    tmp_path: Any,
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    written = _write(instance, _set(WI1, "status", "ready"))
    options = {"access": BODY_READER, "include": ("discovery",)}
    service_export_playbill_floor(instance, **options)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        claim = projection.typed.source(f"Claim:{written.changes[0].claim}")
    (capture,) = claim.backing.capture_digests
    assert instance.body_store().erase(capture)

    warm = _outcome(lambda: service_export_playbill_floor(instance, **options))
    assert warm == _outcome(lambda: _cold(instance, **options))
