"""The vocabulary file: export, plan and apply against a served instance.

One YAML file states every kind and field; export writes it from accepted
state, plan diffs it against the export it names, and apply submits the
difference as one ordinary change set. The same request gives the same result
on the SDK, the CLI and MCP.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    changes.field(
        "dev.item", "seen", StringType(max_len=64), default_role="observation", sources=["osv.dev"]
    )
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
    assert item.fields["seen"].stated["anticipated_source_ids"] == ["osv.dev"]
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


def test_export_refuses_to_replace_uncommitted_edits_without_force(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, env=environment)
    cx.vocabulary.export()
    _edit(workspace, lambda data: data["kinds"]["dev.item"].update(description="Edited."))

    # Untracked edits, then committed ones: only the committed file is replaced.
    with pytest.raises(VocabularyExportRefused, match="not committed"):
        cx.vocabulary.export()
    git = ["git", "-C", str(workspace)]
    subprocess.run([*git, "add", VOCABULARY_FILE_PATH], check=True, env=environment)
    subprocess.run([*git, "commit", "-qm", "vocabulary"], check=True, env=environment)
    assert cx.vocabulary.export().status == "written"
    _edit(workspace, lambda data: data["kinds"]["dev.item"].update(description="Again."))
    with pytest.raises(VocabularyExportRefused, match="uncommitted edits"):
        cx.vocabulary.export()
    assert cx.vocabulary.export(force=True).status == "written"
    assert "Again." not in _file(workspace).read_text(encoding="utf-8")


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
    assert applied.next == "cruxible vocabulary export"
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
    _edit(
        workspace,
        lambda data: data["kinds"]["dev.item"]["fields"]["title"].update(retired=True),
    )
    plan = cx.vocabulary.plan()
    assert [(entry.definition, entry.action) for entry in plan.changes] == [
        ("ClaimType:dev.item.title", "retire")
    ]
    assert cx.vocabulary.apply().status == "landed"
    cx.refresh()
    assert "dev.item.title" not in set(cx.world().predicates)
    assert _claim_view(cx, claim).lifecycle.state == "retired"


def test_a_file_naming_an_unknown_export_refuses(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, workspace, cx = world
    cx.vocabulary.export()
    _edit(workspace, lambda data: data["based_on"].update(git_oid="0" * 40))
    with pytest.raises(VocabularyBaselineError):
        cx.vocabulary.plan()


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
