"""The vocabulary file: export, plan and apply against a served instance.

One YAML file states every kind and field; export writes it from accepted
state, plan diffs it against the export it names, and apply submits the
difference as one ordinary change set. The same request gives the same result
on the SDK, the CLI and MCP.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.authoring.vocabulary_file import lower_entry
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.claims import ClaimArtifact
from cruxible_client.contracts.shapes import EnumType, StringType
from cruxible_client.contracts.vocabulary_file import (
    VocabularyBaselineError,
    VocabularyExportRefused,
    VocabularyFileError,
    load_vocabulary_file,
)
from cruxible_client.contracts.workspace_layout import VOCABULARY_FILE_PATH
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from tests.test_server.test_playbill_kits import worlds  # noqa: F401 - the kit fixture

SLUG = StringType(pattern="^[a-z0-9][a-z0-9-]*$", min_len=1, max_len=80)
STATE = {"open": "Not done.", "done": "Done.", "wontfix": "Will not be done."}


def _transport(http: TestClient) -> CruxibleClient:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    return transport


def _cx(http: TestClient, instance_id: str, workspace: Path) -> Cruxible:
    (workspace / ".cruxible").mkdir(parents=True, exist_ok=True)
    return Cruxible._from_client(_transport(http), instance_id=instance_id, workspace=workspace)


def _vocabulary(cx: Cruxible) -> None:
    """A realistic vocabulary: keyed and minted kinds, enum meanings, links, a source."""

    changes = cx.changes(rationale="Project items.")
    changes.field("dev.item", "slug", SLUG, description="The item's short name.", key=True)
    changes.field("dev.item", "title", StringType(min_len=1, max_len=120), description="Its label.")
    changes.field(
        "dev.item",
        "state",
        EnumType(members=tuple(STATE)),
        description="Whether it is done.",
        members=STATE,
        default_role="normative",
    )
    changes.field(
        "dev.item", "blocks", "subject<dev.item>", description="Items it blocks.", many=True
    )
    changes.field("dev.item", "seen", StringType(max_len=64), description="Where it was seen.")
    changes.subject_kind(name="dev.item", description="A planned piece of work.")
    changes.field("dev.note", "body", StringType(max_len=200), description="One line.")
    changes.subject_kind(name="dev.note", minted=True, description="A note.")
    intent = changes.prepare()
    assert not intent.refused, intent.diagnostics
    assert intent.submit().outcome.status == "landed"
    cx.refresh()


def _accepted(cx: Cruxible, predicate: str) -> ClaimType:
    return ClaimType.model_validate(
        cx.get(f"ClaimType:{predicate}", detail="proof").value["envelope"]
    )


def _file(workspace: Path) -> Path:
    return workspace / VOCABULARY_FILE_PATH


def _edit(workspace: Path, edit) -> None:  # type: ignore[no-untyped-def]
    path = _file(workspace)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def world(playbill_http: tuple[TestClient, str, Path], tmp_path: Path):  # type: ignore[no-untyped-def]
    http, instance_id, _ = playbill_http
    workspace = tmp_path / "workspace"
    cx = _cx(http, instance_id, workspace)
    _vocabulary(cx)
    return http, instance_id, workspace, cx


def test_export_writes_every_definition_so_it_lowers_back_to_its_digest(world) -> None:  # type: ignore[no-untyped-def]
    _http, instance_id, workspace, cx = world

    result = cx.vocabulary.export()

    assert result.status == "written" and result.path == VOCABULARY_FILE_PATH
    content = _file(workspace).read_text(encoding="utf-8")
    parsed = load_vocabulary_file(content)
    assert parsed.instance == instance_id and parsed.based_on == result.based_on
    item = parsed.kinds["dev.item"]
    # Shorthand where it round-trips: the key, the enum's meanings, the link.
    assert list(item.fields)[0] == "slug" and item.fields["slug"].key is True
    assert item.fields["state"].enum == STATE
    assert item.fields["blocks"].link == "dev.item" and item.fields["blocks"].many is True
    assert parsed.kinds["dev.note"].minted is True
    for kind, entry in parsed.kinds.items():
        for name, field in entry.fields.items():
            accepted = _accepted(cx, f"{kind}.{name}")
            lowered = lower_entry(f"{kind}.{name}", field, None).model_copy(
                update={"lifecycle": accepted.lifecycle}
            )
            assert claim_type_digest(lowered) == claim_type_digest(accepted), name
    # The same vocabulary writes the same bytes; an unedited file plans nothing.
    assert cx.vocabulary.export().status == "unchanged"
    plan = cx.vocabulary.plan()
    assert plan.changes == () and plan.claims == ()
    assert plan.preflight.verdict == "not_run"


def test_export_replaces_the_file_only_when_nothing_in_it_would_be_lost(world) -> None:  # type: ignore[no-untyped-def]
    """No Git anywhere: the workspace is a plain folder."""

    _http, _instance_id, workspace, cx = world
    assert not (workspace / ".git").exists()
    cx.vocabulary.export()
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["title"].update(description="Edited."),
    )

    # An edit accepted state lacks: export refuses, naming it.
    with pytest.raises(VocabularyExportRefused, match=r"ClaimType:dev\.item\.title"):
        cx.vocabulary.export()
    # Applied, nothing is lost: export replaces the file, no commit needed.
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    assert cx.vocabulary.export().status == "written"

    # Behind but unedited (someone else changed a field since): still replaced.
    other = cx.changes(rationale="A concurrent edit.")
    other.field("dev.item", "state", EnumType(members=tuple(STATE)), description="Theirs.")
    assert other.submit().outcome.status == "landed"
    cx.refresh()
    assert cx.vocabulary.export().status == "written"
    assert "Theirs." in _file(workspace).read_text(encoding="utf-8")

    # A file that does not parse refuses; --force replaces it.
    _file(workspace).write_text("format: [unclosed\n", encoding="utf-8")
    with pytest.raises(VocabularyExportRefused, match="cannot be read"):
        cx.vocabulary.export()
    # An enum mapping not yet applied is a pending change too.
    cx.vocabulary.export(force=True)
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["state"]["enum"].update(
            wontfix={"becomes": "done"}
        ),
    )
    with pytest.raises(VocabularyExportRefused, match=r"ClaimType:dev\.item\.state"):
        cx.vocabulary.export()
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    # The applied mapping names a member the enum no longer has: not lost.
    assert cx.vocabulary.export().status == "written"


def test_export_keeps_a_new_definition_another_writer_accepted_differently(world) -> None:  # type: ignore[no-untyped-def]
    """Review R2: a new field's omissions are not edits to the head's same-named field."""

    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"].update(
            collision={"value": "string{max_len: 100}"}
        ),
    )
    other = cx.changes(rationale="Someone else defines it first.")
    other.field("dev.item", "collision", "string{max_len: 100}", many=True)
    assert other.submit().outcome.status == "landed"
    cx.refresh()

    with pytest.raises(VocabularyExportRefused, match=r"ClaimType:dev\.item\.collision"):
        cx.vocabulary.export()
    # Accepted with the same meaning, the new definition is applied.
    same = cx.changes(rationale="Make it one-valued, as the file says.")
    same.field("dev.item", "collision", "string{max_len: 100}", many=False)
    assert same.submit().outcome.status == "landed"
    cx.refresh()
    assert cx.vocabulary.export().status == "written"


def test_export_keeps_a_retirement_of_a_definition_accepted_after_the_export(world) -> None:  # type: ignore[no-untyped-def]
    """Review R6: a retirement is applied only once the head no longer holds the definition."""

    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    later = cx.changes(rationale="Accepted after the export.")
    later.field("dev.item", "late", "string{max_len: 40}")
    later.subject_kind(name="dev.later", minted=True)
    assert later.submit().outcome.status == "landed"
    cx.refresh()

    def retire(data):  # type: ignore[no-untyped-def]
        data["kinds"]["dev.item"]["fields"]["late"] = {"retired": True}
        data["kinds"]["dev.later"] = {"retired": True}

    _edit(workspace, retire)
    with pytest.raises(VocabularyExportRefused) as refused:
        cx.vocabulary.export()
    assert "ClaimType:dev.item.late" in str(refused.value)
    assert "SubjectKind:dev.later" in str(refused.value)
    text = _file(workspace).read_text(encoding="utf-8")
    assert "late:\n        retired: true" in text


def test_export_keeps_a_file_whose_header_does_not_match_and_reads_no_claim(
    world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review R7: an unestablished baseline keeps the file; the guard reads definitions only."""

    _http, _instance_id, workspace, cx = world
    for name in "abcdefghi":
        _claim(cx, f"dev.item/{name}", "dev.item.state", "wontfix")
    cx.refresh()
    cx.vocabulary.export()

    def edit(data):  # type: ignore[no-untyped-def]
        fields = data["kinds"]["dev.item"]["fields"]
        fields["collision"] = {"value": "string{max_len: 100}"}
        fields["state"]["enum"]["wontfix"] = {"becomes": "done"}
        data["based_on"]["vocabulary_digest"] = "sha256:" + "0" * 64

    _edit(workspace, edit)
    other = cx.changes(rationale="Someone else defines it first.")
    other.field("dev.item", "collision", "string{max_len: 100}", many=True)
    assert other.submit().outcome.status == "landed"
    cx.refresh()
    before = _file(workspace).read_bytes()

    claim_reads: list[object] = []
    original_batch = cx._client.get_batch
    original_query = cx._client.query

    def batch(instance_id, *, request):  # type: ignore[no-untyped-def]
        claim_reads.extend(ref for ref in request.refs if ref.startswith("Claim:"))
        return original_batch(instance_id, request=request)

    def query(instance_id, *, request):  # type: ignore[no-untyped-def]
        claim_reads.append(request)
        return original_query(instance_id, request=request)

    monkeypatch.setattr(cx._client, "get_batch", batch)
    monkeypatch.setattr(cx._client, "query", query)
    with pytest.raises(VocabularyExportRefused, match="header does not match") as refused:
        cx.vocabulary.export()
    assert "cx.vocabulary.export(force=True)" in str(refused.value)
    assert claim_reads == []
    assert _file(workspace).read_bytes() == before
    assert cx.vocabulary.export(force=True).status == "written"


def test_apply_adds_and_edits_and_never_retires_what_the_file_leaves_out(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    before_title = _accepted(cx, "dev.item.title")

    def edit(data):  # type: ignore[no-untyped-def]
        fields = data["kinds"]["dev.item"]["fields"]
        # Omitted entirely: never retired. Omitted property: kept.
        del fields["blocks"]
        del fields["title"]["description"]
        fields["title"]["value"] = "string{min_len: 1, max_len: 200}"
        fields["state"]["description"] = "Where the item stands."
        fields["owner"] = {"link": "dev.note", "description": "Who owns it."}

    _edit(workspace, edit)
    plan = cx.vocabulary.plan()

    actions = {entry.definition: entry.action for entry in plan.entries}
    assert actions == {
        "ClaimType:dev.item.owner": "add",
        "ClaimType:dev.item.state": "replace",
        "ClaimType:dev.item.title": "replace",
        "ClaimType:dev.item.blocks": "not_in_file",
    }
    title = next(entry for entry in plan.entries if entry.definition.endswith(".title"))
    assert title.changed == ("value",)
    assert plan.preflight.verdict == "passed", plan.preflight

    applied = cx.vocabulary.apply(rationale="Owners and longer titles.")
    assert applied.status == "landed", applied
    assert applied.next == "cx.vocabulary.export()"
    cx.refresh()
    assert _accepted(cx, "dev.item.title").description == before_title.description
    assert _accepted(cx, "dev.item.title").value == StringType(min_len=1, max_len=200)
    assert _accepted(cx, "dev.item.state").description == "Where the item stands."
    assert _accepted(cx, "dev.item.owner").value.kind == "subject"
    assert "dev.item.blocks" in set(cx.world().predicates)


def test_apply_refuses_a_field_someone_changed_since_the_export(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["state"].update(description="Mine."),
    )
    # Someone else edits the same field after the export.
    other = cx.changes(rationale="A concurrent edit.")
    other.field("dev.item", "state", EnumType(members=tuple(STATE)), description="Theirs.")
    assert other.submit().outcome.status == "landed"
    cx.refresh()

    refused = cx.vocabulary.apply()

    assert refused.status == "refused", refused
    assert refused.frontier is not None
    (diagnostic,) = refused.frontier.diagnostics
    assert diagnostic.code == "cruxible.authoring.claim_type_succession_invalid"
    assert "does not pin the current ClaimType" in diagnostic.message
    assert _accepted(cx, "dev.item.state").description == "Theirs."
    # The refusal names the moved definition and the repair in the SDK's calls.
    assert refused.moved == ("ClaimType:dev.item.state",)
    assert refused.next is not None and "cx.vocabulary.export(force=True)" in refused.next
    assert "Changed since the file's export: ClaimType:dev.item.state" in str(refused)

    # A field nobody touched since the export still applies at the moved head.
    cx.vocabulary.export(force=True)
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["title"].update(description="New."),
    )
    unrelated = cx.changes(rationale="An unrelated edit.")
    unrelated.field("dev.note", "body", StringType(max_len=200), description="One line, still.")
    assert unrelated.submit().outcome.status == "landed"
    assert cx.vocabulary.apply().status == "landed"


def test_apply_refuses_a_subject_kind_someone_changed_since_the_export(world) -> None:  # type: ignore[no-untyped-def]
    """Review R1: the daemon refuses on the export's SubjectKind digest; no client re-read."""

    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(workspace, lambda data: data["kinds"]["dev.item"].update(description="Mine."))
    plan = cx.vocabulary.plan()
    assert [(entry.definition, entry.action) for entry in plan.changes] == [
        ("SubjectKind:dev.item", "replace")
    ]
    other = cx.changes(rationale="A concurrent edit.")
    other.subject_kind(name="dev.item", keys=["dev.item.slug"], description="Theirs.")
    assert other.submit().outcome.status == "landed"
    cx.refresh()

    refused = cx.vocabulary.apply()

    assert refused.status == "refused", refused
    assert refused.frontier is not None
    (diagnostic,) = refused.frontier.diagnostics
    assert diagnostic.code == "cruxible.authoring.subject_kind_predecessor_moved"
    assert refused.moved == ("SubjectKind:dev.item",)
    described = cx.get("SubjectKind:dev.item").value
    assert described.description == "Theirs."
    # The unapplied edit is protected; re-exported (forced) and merged, it lands.
    with pytest.raises(VocabularyExportRefused, match="SubjectKind:dev.item"):
        cx.vocabulary.export()
    cx.vocabulary.export(force=True)
    _edit(workspace, lambda data: data["kinds"]["dev.item"].update(description="Mine."))
    assert cx.vocabulary.apply().status == "landed"
    assert cx.get("SubjectKind:dev.item").value.description == "Mine."


def _claim(cx: Cruxible, subject: str, predicate: str, value: str) -> str:
    outcome = cx.set(subject, predicate, value, because=f"It is {value}.")
    assert outcome.status == "accepted", outcome
    claim = outcome.changes[0].claim
    assert claim is not None
    return claim


def _claim_view(cx: Cruxible, claim_id: str) -> ClaimArtifact:
    from cruxible_client.authoring.sdk import _claim_from_public_view

    return _claim_from_public_view(cx.get(f"Claim:{claim_id}", detail="proof").value)


def test_an_enum_mapping_re_authors_its_claims_and_cites_their_captures_again(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    kept = _claim(cx, "dev.item/a", "dev.item.state", "open")
    moved = [_claim(cx, f"dev.item/{name}", "dev.item.state", "wontfix") for name in "bc"]
    captures = {claim: _claim_view(cx, claim).backing.capture_digests for claim in moved}
    cx.refresh()
    cx.vocabulary.export()

    def edit(data):  # type: ignore[no-untyped-def]
        data["kinds"]["dev.item"]["fields"]["state"]["enum"]["wontfix"] = {"becomes": "done"}

    _edit(workspace, edit)
    plan = cx.vocabulary.plan()

    assert [(item.claim, item.action, item.becomes) for item in plan.claims] == [
        (claim, "re_author", "done") for claim in sorted(moved)
    ]
    assert plan.preflight.verdict == "passed", plan.preflight
    applied = cx.vocabulary.apply()
    assert applied.status == "landed", applied
    cx.refresh()
    assert _accepted(cx, "dev.item.state").value == EnumType(members=("open", "done"))
    for claim in moved:
        view = _claim_view(cx, claim)
        assert view.statement.object.value == "done"
        assert view.lifecycle.state == "live"
        # The re-authored Claim keeps its identity and cites its Capture again.
        assert view.backing.capture_digests == captures[claim]
    assert _claim_view(cx, kept).statement.object.value == "open"


def test_a_mapping_can_map_some_members_and_retire_the_rest(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    moved = _claim(cx, "dev.item/a", "dev.item.state", "wontfix")
    retired = _claim(cx, "dev.item/b", "dev.item.state", "open")
    cx.refresh()
    cx.vocabulary.export()

    def edit(data):  # type: ignore[no-untyped-def]
        members = data["kinds"]["dev.item"]["fields"]["state"]["enum"]
        members["wontfix"] = {"becomes": "done"}
        members["open"] = {"retire": "superseded"}

    _edit(workspace, edit)
    plan = cx.vocabulary.plan()
    assert {(item.claim, item.action) for item in plan.claims} == {
        (moved, "re_author"),
        (retired, "retire"),
    }
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    assert _accepted(cx, "dev.item.state").value == EnumType(members=("done",))
    assert _claim_view(cx, moved).statement.object.value == "done"
    assert _claim_view(cx, retired).lifecycle.state == "retired"


def test_a_mapping_moves_claims_written_after_the_export(world) -> None:  # type: ignore[no-untyped-def]
    """Walkthrough P1: data written after the export is normal; the mapping finds it."""

    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    moved = [_claim(cx, f"dev.item/{name}", "dev.item.state", "wontfix") for name in "abc"]
    retired = _claim(cx, "dev.item/d", "dev.item.state", "open")
    cx.refresh()

    def edit(data):  # type: ignore[no-untyped-def]
        members = data["kinds"]["dev.item"]["fields"]["state"]["enum"]
        members["wontfix"] = {"becomes": "done"}
        members["open"] = {"retire": "superseded"}

    _edit(workspace, edit)
    plan = cx.vocabulary.plan()
    assert {(item.claim, item.action) for item in plan.claims} == {
        *((claim, "re_author") for claim in moved),
        (retired, "retire"),
    }
    assert plan.preflight.verdict == "passed", plan.preflight
    assert cx.vocabulary.apply().status == "landed"
    for claim in moved:
        assert _claim_view(cx, claim).statement.object.value == "done"
    assert _claim_view(cx, retired).lifecycle.state == "retired"


def test_dropping_a_member_that_claims_state_names_the_mapping(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _claim(cx, "dev.item/a", "dev.item.state", "wontfix")
    cx.refresh()
    _edit(
        workspace, lambda data: data["kinds"]["dev.item"]["fields"]["state"]["enum"].pop("wontfix")
    )

    with pytest.raises(VocabularyFileError, match=r"wontfix: \{becomes: <member>\}") as refused:
        cx.vocabulary.plan()
    assert refused.value.error_code == "cruxible.vocabulary.mapping_required"
    # A member no live Claim states can simply leave the enum.
    _edit(workspace, lambda data: data["kinds"]["dev.item"]["fields"]["state"]["enum"].pop("open"))
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["state"]["enum"].update(
            wontfix={"becomes": "done"}
        ),
    )
    assert cx.vocabulary.plan().preflight.verdict == "passed"


def test_a_mapping_names_a_member_the_enum_does_not_have(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["state"]["enum"].update(
            wontfix={"becomes": "closed"}
        ),
    )
    with pytest.raises(Exception, match="which the enum does not keep"):
        cx.vocabulary.plan()


def test_retired_true_retires_a_field_and_its_claims(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    claim = _claim(cx, "dev.item/a", "dev.item.title", "First")
    cx.refresh()
    cx.vocabulary.export()
    later = _claim(cx, "dev.item/b", "dev.item.title", "Second")
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["title"].update(retired=True),
    )
    plan = cx.vocabulary.plan()
    # The count is the head's: a Claim written after the export retires too.
    assert [(entry.definition, entry.action, entry.live_claims) for entry in plan.changes] == [
        ("ClaimType:dev.item.title", "retire", 2)
    ]
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    assert "dev.item.title" not in set(cx.world().predicates)
    assert _claim_view(cx, claim).lifecycle.state == "retired"
    assert _claim_view(cx, later).lifecycle.state == "retired"
    # Walkthrough P2: the applied retirement is not a pending change; export
    # replaces the file without --force and drops the field.
    assert cx.vocabulary.export().status == "written"
    assert "title:" not in _file(workspace).read_text(encoding="utf-8")


def test_a_field_with_an_inverse_is_added_edited_and_retired_through_the_file(world) -> None:  # type: ignore[no-untyped-def]
    """Walkthrough S16: a ClaimType with an inverse exports and plans like any other."""

    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"].update(
            affects={
                "link": "dev.item",
                "many": True,
                "description": "Items it affects.",
                "inverse": {"key": {"kind": "object"}, "evidence_rules": ["self"]},
            }
        ),
    )
    assert cx.vocabulary.apply(rationale="Affects, with absence.").status == "landed"
    cx.refresh()
    assert _accepted(cx, "dev.item.affects").inverse is not None

    # Export writes it and an unedited file plans nothing.
    assert cx.vocabulary.export().status == "written"
    plan = cx.vocabulary.plan()
    assert plan.changes == (), plan.changes

    # An edit to the field keeps its inverse.
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["affects"].update(
            description="Items it changes."
        ),
    )
    assert [(entry.definition, entry.action) for entry in cx.vocabulary.plan().changes] == [
        ("ClaimType:dev.item.affects", "replace")
    ]
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    edited = _accepted(cx, "dev.item.affects")
    assert edited.description == "Items it changes." and edited.inverse is not None

    # And it retires.
    assert cx.vocabulary.export().status == "written"
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["affects"].update(retired=True),
    )
    assert [(entry.definition, entry.action) for entry in cx.vocabulary.plan().changes] == [
        ("ClaimType:dev.item.affects", "retire")
    ]
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    assert "dev.item.affects" not in set(cx.world().predicates)


def test_an_applied_subject_kind_retirement_lets_export_replace_the_file(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(workspace, lambda data: data["kinds"]["dev.note"].update(retired=True))
    assert [(entry.definition, entry.action) for entry in cx.vocabulary.plan().changes] == [
        ("SubjectKind:dev.note", "retire")
    ]
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    assert cx.vocabulary.export().status == "written"
    assert "minted" not in _file(workspace).read_text(encoding="utf-8")


def test_a_file_naming_an_unknown_export_refuses(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(workspace, lambda data: data["based_on"].update(git_oid="0" * 40))
    with pytest.raises(VocabularyBaselineError):
        cx.vocabulary.plan()


def test_next_and_orient_name_a_file_behind_accepted_state(world) -> None:  # type: ignore[no-untyped-def]
    from cruxible_client.authoring.sdk_types import Duration

    _http, _instance_id, workspace, cx = world
    within = Duration.days(count=7)

    def rows():  # type: ignore[no-untyped-def]
        return [
            item for item in cx.next(expiring_within=within).items if "vocabulary" in item.reason
        ]

    cx.vocabulary.export()
    assert rows() == []
    assert cx.orient().vocabulary_file.state == "current"

    later = cx.changes(rationale="One more field.")
    later.field("dev.item", "priority", EnumType(members=("p0", "p1")))
    assert later.submit().outcome.status == "landed"
    cx.refresh()
    (row,) = rows()
    assert row.reason == "vocabulary_file_behind"
    # Walkthrough W10: the SDK takes the CLI's default window (seven days).
    assert [item.item_id for item in cx.next().items if "vocabulary" in item.reason] == [
        row.item_id
    ]
    assert row.detail["definitions_behind"] == 1
    assert row.detail["changed"] == ["ClaimType:dev.item.priority"]
    assert row.repair.command == "cx.vocabulary.export()"
    status = cx.orient().vocabulary_file
    assert status.state == "behind" and status.definitions_behind == 1
    # Walkthrough W9: next= counts accepted state; the text says where the row is.
    from cruxible_client.contracts.text_display import render_orient

    assert "the vocabulary file row is in `cruxible next`" in render_orient(
        cx.orient().model_dump(mode="json")
    )

    # A Claim write moves the head, not the vocabulary: still one behind.
    _claim(cx, "dev.item/a", "dev.item.state", "open")
    assert rows()[0].detail["definitions_behind"] == 1

    _edit(workspace, lambda data: data["based_on"].update(git_oid="0" * 40))
    (invalid,) = rows()
    assert invalid.reason == "vocabulary_file_baseline_invalid"
    # Export keeps a file it cannot read the meaning of: the repair is the forced one.
    assert invalid.repair.command == "cx.vocabulary.export(force=True)"
    from cruxible_client.contracts.text_display import render_orient

    assert "cruxible vocabulary export --force" in render_orient(
        cx.orient().model_dump(mode="json")
    )
    assert cx.orient().vocabulary_file.state == "invalid"

    # Review R4: a malformed OID is an unreadable header, never a request error.
    _edit(workspace, lambda data: data["based_on"].update(git_oid="not-an-oid"))
    (malformed,) = rows()
    assert malformed.detail["cause"] == "unreadable"
    assert cx.orient().vocabulary_file.state == "invalid"

    _file(workspace).write_text("format: [unclosed\n", encoding="utf-8")
    (unreadable,) = rows()
    assert unreadable.detail["cause"] == "unreadable"
    assert cx.orient().vocabulary_file.state == "invalid"

    cx.vocabulary.export(force=True)
    assert rows() == []


def test_plan_is_the_same_on_the_sdk_the_cli_and_mcp(
    world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["state"].update(description="Edited."),
    )
    sdk = cx.vocabulary.plan().model_dump(mode="json")

    transport = _transport(http)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    monkeypatch.setattr(handlers, "_get_client", lambda: transport)
    monkeypatch.chdir(workspace)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://cruxible",
            "--instance-id",
            instance_id,
            "vocabulary",
            "plan",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == sdk

    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    mcp = handlers.handle_playbill_vocabulary_plan(instance_id).model_dump(mode="json")
    assert mcp == sdk
    assert [entry["action"] for entry in sdk["entries"]] == ["replace"]


def test_a_kit_owned_field_is_marked_and_its_local_edit_applies(
    worlds,  # type: ignore[no-untyped-def]  # noqa: F811 - the imported kit fixture
) -> None:
    from cruxible_core.runtime import playbill_api
    from tests.test_server.test_playbill_kits import PLAN, SEATS, _claim_type, _installed, _path

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    consumer.pb.refresh()
    workspace = Path(consumer.pb._workspace)

    consumer.pb.vocabulary.export()
    content = _file(workspace).read_text(encoding="utf-8")
    assert "      # kit: acme\n      plan:" in content
    assert "      # kit: acme\n      seats:" in content
    _edit(
        workspace,
        lambda data: data["kinds"]["acme.account"]["fields"]["plan"].update(
            description="The plan this adopter sells."
        ),
    )

    plan = consumer.pb.vocabulary.plan()
    assert [(entry.definition, entry.action, entry.kit) for entry in plan.changes] == [
        (f"ClaimType:{PLAN}", "replace", "acme")
    ]
    assert consumer.pb.vocabulary.apply().status == "landed"
    assert consumer.claim_type(PLAN).description == "The plan this adopter sells."
    # The kit sees the adopter's edit as its own drift, as for any local edit.
    (kit,) = _installed(playbill_api.playbill_kit_status(consumer.instance_id))
    assert _path(PLAN) in kit.drifted


def test_plan_prints_each_lint_with_what_it_means(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"].update(
            notes={"value": "string{max_len: 240}", "description": "Free notes."}
        ),
    )

    text = str(cx.vocabulary.plan())

    assert (
        "lint cruxible.claim_type.string_holds_prose (ClaimType:dev.item.notes): "
        "a long free string holds prose"
    ) in text
