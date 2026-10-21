"""The ontology on the floor: definition files, owner markers, and their delivery cost."""

from __future__ import annotations

import json
from typing import Any

import yaml

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    parse_document,
    render_document,
)
from cruxible_client.contracts.kits import (
    KIT_RECEIPT_DOCUMENT_KIND,
    KitInstalledArtifact,
    KitReceipt,
    kit_content_digest,
    kit_receipt_document_id,
)
from cruxible_client.contracts.subject_kinds import (
    SubjectKind,
    SubjectKindMinted,
    parse_subject_kind,
    render_subject_kind,
    subject_kind_digest,
    subject_kind_path,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor_index import FloorRender, advance_floor_index
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_floor.test_floor_index import (
    _bytes,
    _cold,
    _set,
    _write,
    accept_edit,
    migrate_claim_type,
)

WI1 = f"{KIND}/wi-1"
CARD = f"ontology/kinds/{KIND}.yaml"
NOTE_KIND = "project.note"
KIT = "demo"


def _tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.tree_at(instance.accepted_coordinate().git_oid))


_EDITS = [0]


def _name(prefix: str) -> str:
    _EDITS[0] += 1
    return f"{prefix}-{_EDITS[0]}"


def revise_claim_type(
    instance: PlaybillInstance, field: str, *, state: str = "live", **update: Any
) -> None:
    """Accept a successor of one ClaimType of the seeded kind with ``update`` applied."""

    migrate_claim_type(instance, f"{KIND}.{field}", state=state, **update)


def declare_note_kind(instance: PlaybillInstance, description: str) -> None:
    path = subject_kind_path(NOTE_KIND)
    previous = _tree(instance).get(path)
    kind = SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=NOTE_KIND),
        description=description,
        identity_rule=SubjectKindMinted(),
        lifecycle=(
            ArtifactLifecycle()
            if previous is None
            else ArtifactLifecycle(
                predecessor_digest=subject_kind_digest(
                    parse_subject_kind(previous, path=path)
                ).tagged
            )
        ),
    )
    accept_edit(instance, _name("note-kind"), {path: render_subject_kind(kind)})


def remove_receipt(instance: PlaybillInstance, *, revision: int) -> None:
    """Accept the receipt a kit removal leaves: owning nothing."""

    install_receipt(instance, version="1.1.0", fields=(), revision=revision)


def install_receipt(
    instance: PlaybillInstance, *, version: str, fields: tuple[str, ...], revision: int
) -> None:
    """Accept a kit receipt owning the seeded ClaimTypes ``fields`` as they are held now."""

    tree = _tree(instance)
    artifacts = []
    for field in fields:
        path = claim_type_path(f"{KIND}.{field}")
        held = parse_claim_type(tree[path], path=path)
        digest = claim_type_digest(held).tagged
        artifacts.append(
            KitInstalledArtifact(
                path=path,
                release_digest=digest,
                installed_digest=digest,
                content_digest=kit_content_digest(json.loads(tree[path])),
            )
        )
    receipt = KitReceipt(
        kit_id=KIT,
        version=version,
        content_digest=kit_content_digest({"kit": KIT, "version": version}),
        owns=(f"{KIND}.",),
        artifacts=tuple(sorted(artifacts, key=lambda item: item.path)),
    )
    name = kit_receipt_document_id(KIT)
    path = document_path(name)
    previous = tree.get(path)
    shell = DocumentShell(
        identity=f"document:{name}",
        document_kind=KIT_RECEIPT_DOCUMENT_KIND,
        title=f"Kit {KIT}",
        media_type="application/json",
        body_digest=instance.store_document_body(receipt.model_dump_json().encode()).digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=revision),
        **(
            {}
            if previous is None
            else {"predecessor_digest": document_digest(parse_document(previous, path=path)).tagged}
        ),
    )
    accept_edit(instance, _name("kit-receipt"), {path: render_document(shell)})


def _ontology(render: FloorRender) -> dict[str, tuple[bytes, int]]:
    return {path: item for path, item in render.files.items() if path.startswith("ontology/")}


def _text(render: FloorRender, path: str) -> str:
    return render.files[path][0].decode()


def test_the_kind_card_greps_to_its_field_and_the_indexes_head_their_columns(
    tmp_path: Any,
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    render = advance_floor_index(instance)
    card = _text(render, CARD)
    first, *rest = card.splitlines()
    assert first == f"# {KIND}  ontology  owner=local  changed gen {render.files[CARD][1]}"
    assert f'identity: "keys: {KIND}.slug (unique)"  # SubjectKind:{KIND}' in rest
    # A field's key line carries its signature: a grep hit on ``status:`` describes it.
    assert any(
        line.startswith("  status:  # enum(blocked|done|ready)  one  role observation  evidence ")
        for line in rest
    )
    # Past its header line the card is YAML, like a current/ file.
    parsed = yaml.safe_load(card)
    assert parsed["kind"] == KIND
    status = parsed["fields"]["status"]
    assert status["predicate"] == f"{KIND}.status" and status["owner"] == "local"
    assert status["type"] == "enum(blocked|done|ready)" and status["cardinality"] == "one"
    assert status["settled_by"] == ["review"]
    assert parsed["pointed_at_by"] == [f"governs <- {KIND}  {KIND}.governs  many"]
    headers = {
        "ontology/INDEX": "# ontology INDEX  1 kinds  columns: kind, identity, fields, owner, "
        "description",
        # The seeded fields, the slug key and the two builtin descriptor types.
        "ontology/claim-types/INDEX": "# claim-types INDEX  10 claim types  columns: predicate, "
        "subject kinds, type, cardinality, default role, evidence, stale_after, contention, owner",
        "ontology/contracts/INDEX": "# contracts INDEX",
        "ontology/queries/INDEX": "# queries INDEX  0 queries",
        "ontology/lines/INDEX": "# lines INDEX  0 lines",
        "ontology/kits/INDEX": "# kits INDEX  0 kits",
    }
    for path, header in headers.items():
        assert _text(render, path).startswith(header), (path, _text(render, path))
    (row,) = [
        line
        for line in _text(render, "ontology/claim-types/INDEX").splitlines()
        if line.startswith(f"{KIND}.status\t")
    ]
    assert row.split("\t")[:4] == [f"{KIND}.status", KIND, "enum(blocked|done|ready)", "one"]
    assert row.split("\t")[-1] == "local"
    assert (
        _text(render, "ontology/INDEX")
        .splitlines()[1]
        .startswith(f"{KIND}\tkeys: {KIND}.slug (unique)\t8\tlocal\t")
    )
    # A cold render is the same bytes.
    assert _bytes(render) == _bytes(_cold(instance))


def test_descriptions_never_break_a_card_or_a_row(tmp_path: Any) -> None:
    from cruxible_core.service.floor.floor_ontology import _text_entry

    # Prose keeps its lines as a literal block; a header-like line stays text.
    text = "# Not a header.\nSecond\tline with a tab.\n"
    lines = _text_entry("description", text, "    ")
    assert lines == [
        "    description: |",
        "      # Not a header.",
        "      Second\tline with a tab.",
    ]
    assert yaml.safe_load("x:\n" + "\n".join(lines) + "\n")["x"]["description"] == text
    instance, _owner = seed_write_surface(tmp_path)
    declare_note_kind(instance, "A note.\tTabbed. Second sentence.")
    render = advance_floor_index(instance)
    note = _text(render, f"ontology/kinds/{NOTE_KIND}.yaml")
    assert note.splitlines()[2] == f"identity: minted  # SubjectKind:{NOTE_KIND}"
    parsed = yaml.safe_load(note)
    assert parsed["description"] == "A note.\tTabbed. Second sentence."
    assert parsed["fields"] == {}
    rows = _text(render, "ontology/INDEX").splitlines()[1:]
    assert f"{NOTE_KIND}\tminted\t0\tlocal\tA note." in rows
    assert all(len(row.split("\t")) == 5 for row in rows)
    for path, (content, _changed) in render.files.items():
        if path.startswith("ontology/") and path.endswith(".yaml"):
            assert isinstance(yaml.safe_load(content), dict), path
    assert _bytes(render) == _bytes(_cold(instance))


def test_a_generation_without_definitions_renders_no_ontology_bytes(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from cruxible_core.service.floor import floor_ontology

    instance, _owner = seed_write_surface(tmp_path)
    before = advance_floor_index(instance)
    rendered: list[str] = []
    render = floor_ontology._Renderer.render

    def counting(self: Any, path: str, changed_at: int) -> Any:
        rendered.append(path)
        return render(self, path, changed_at)

    monkeypatch.setattr(floor_ontology._Renderer, "render", counting)
    _write(instance, _set(WI1, "status", "ready"), _set(WI1, "title", "One"))
    after = advance_floor_index(instance)
    assert rendered == []
    assert not [path for path in after.steps[-1].written if path.startswith("ontology/")]
    for path, item in _ontology(before).items():
        assert after.files[path] is item, path


def test_a_claim_type_edit_reaches_only_the_files_it_shows_on(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from cruxible_core.service.floor import floor_ontology

    instance, _owner = seed_write_surface(tmp_path)
    before = advance_floor_index(instance)
    rendered: list[str] = []
    render = floor_ontology._Renderer.render

    def counting(self: Any, path: str, changed_at: int) -> Any:
        rendered.append(path)
        return render(self, path, changed_at)

    monkeypatch.setattr(floor_ontology._Renderer, "render", counting)
    revise_claim_type(instance, "title", literal_schema={"type": "string", "minLength": 1})
    after = advance_floor_index(instance)
    written = sorted(path for path in after.steps[-1].written if path.startswith("ontology/"))
    # The one kind card it shows on and the ClaimType INDEX: never the
    # contracts it admits (unchanged), the other INDEX files or other cards.
    assert written == ["ontology/claim-types/INDEX", CARD]
    assert sorted(rendered) == written
    assert after.files[CARD][1] == after.generation
    for path, item in _ontology(before).items():
        if path not in written:
            assert after.files[path] is item, path
    monkeypatch.undo()
    assert _bytes(after) == _bytes(_cold(instance))


def test_owner_markers_follow_the_receipt_and_the_held_content(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    install_receipt(instance, version="1.0.0", fields=("status", "title"), revision=1)
    installed = advance_floor_index(instance)
    card = _text(installed, CARD)
    fields = yaml.safe_load(card)["fields"]
    assert fields["status"]["owner"] == f"kit={KIT}"
    assert fields["priority"]["owner"] == "local"
    (kit_row,) = _text(installed, "ontology/kits/INDEX").splitlines()[1:]
    receipt_gen = installed.generation
    assert kit_row.split("\t") == [
        KIT,
        "1.0.0",
        f"{KIND}.",
        str(receipt_gen),
        str(receipt_gen),
        "2",
        "-",
        "-",
        "-",
        "-",
    ]
    # A local edit marks the owner edited, in its card section and the kit row.
    revise_claim_type(
        instance,
        "status",
        literal_schema={"enum": ["blocked", "done", "ready", "waiting"], "type": "string"},
    )
    edited = advance_floor_index(instance)
    fields = yaml.safe_load(_text(edited, CARD))["fields"]
    assert fields["status"]["owner"] == f"kit={KIT} edited"
    assert fields["status"]["type"] == "enum(blocked|done|ready|waiting)"
    assert f"ClaimType:{KIND}.status" in _text(edited, "ontology/kits/INDEX")
    # An upgrade that takes the held content back as installed clears it, and
    # touches only the files of the definitions the receipt ever owned.
    install_receipt(instance, version="1.1.0", fields=("status",), revision=2)
    upgraded = advance_floor_index(instance)
    written = sorted(path for path in upgraded.steps[-1].written if path.startswith("ontology/"))
    assert set(written) <= {CARD, "ontology/claim-types/INDEX", "ontology/kits/INDEX"}
    assert "ontology/kits/INDEX" in written
    card = _text(upgraded, CARD)
    fields = yaml.safe_load(card)["fields"]
    assert fields["status"]["owner"] == f"kit={KIT}"
    assert fields["title"]["owner"] == "local"
    (kit_row,) = _text(upgraded, "ontology/kits/INDEX").splitlines()[1:]
    assert kit_row.split("\t")[:6] == [
        KIT,
        "1.1.0",
        f"{KIND}.",
        str(receipt_gen),
        str(upgraded.generation),
        "1",
    ]
    assert _bytes(upgraded) == _bytes(_cold(instance))


def test_a_definition_only_generation_has_its_change_file(tmp_path: Any) -> None:
    from cruxible_core.service.floor.floor_content import change_path, member_ref

    assert member_ref(subject_kind_path(NOTE_KIND)) == f"SubjectKind:{NOTE_KIND}"
    assert member_ref("capture-contracts/repo.notes.json") == "CaptureContract:repo.notes"
    assert member_ref("procedure-mandates/m.json") == "ProcedureMandate:m"
    assert member_ref("lines/l.json") == "Line:l"
    assert member_ref("triggers/t.json") == "Trigger:t"
    assert member_ref("query-definitions/q.json") == "query:q"
    instance, _owner = seed_write_surface(tmp_path)
    declare_note_kind(instance, "A note.")
    declared = advance_floor_index(instance)
    card = f"ontology/kinds/{NOTE_KIND}.yaml"
    stamp = declared.files[card][1]
    assert stamp == declared.generation
    change = json.loads(declared.files[change_path(stamp)][0])
    assert change["changed"] == [f"SubjectKind:{NOTE_KIND}"]
    # Its successor's change replaces it: the floor shows the current revisions'.
    declare_note_kind(instance, "A note, revised.")
    revised = advance_floor_index(instance)
    assert change_path(stamp) not in revised.files
    assert change_path(revised.generation) in revised.files
    assert _bytes(revised) == _bytes(_cold(instance))
