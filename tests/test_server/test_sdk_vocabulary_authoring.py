"""Defining a vocabulary through the typed SDK: the findings of its first real use.

Each test reproduces one finding of the 2026-11-13 SDK run (friction F6-F21):
the same request gives the same result on the SDK, the CLI and MCP.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.contracts.claim_type_inputs import (
    ClaimTypeInputRecord,
    lower_claim_type_input,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    retired_successor,
)
from cruxible_client.contracts.policies import ClaimAdmissionPolicy, ClaimResolutionPolicy
from cruxible_client.contracts.shapes import (
    ContractFormatError,
    EnumType,
    StringType,
    coerce_field_type,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from tests.support.claim_types import claim_type as build_claim_type

SLUG = StringType(pattern="^[a-z0-9][a-z0-9-]*$", min_len=1, max_len=80)


def _cx(http: TestClient, instance_id: str, tmp_path: Path) -> Cruxible:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    workspace = tmp_path / "vocabulary-world"
    (workspace / ".cruxible").mkdir(parents=True)
    return Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)


def _bare_vocabulary(cx: Cruxible) -> None:
    """A kind keyed by a slug and one enum field, both with no evidence rules."""

    changes = cx.changes(rationale="A bare vocabulary.")
    changes.claim_type(
        build_claim_type(
            "dev.item.slug",
            value_type=SLUG,
            permitted_roles=("normative",),
            canonical_form=("nfc", "trim"),
            description="The item's short name.",
        )
    )
    changes.claim_type(
        build_claim_type(
            "dev.item.state",
            value_type=EnumType(members=("open", "done")),
            permitted_roles=("normative",),
            description="Whether the item is done.",
        )
    )
    changes.subject_kind(name="dev.item", keys=["dev.item.slug"], description="An item.")
    intent = changes.prepare()
    codes = {(item.artifact, item.code) for item in intent.warnings}
    # F21: prepare() on a change set returns the lint the propose route returns.
    assert (
        "ClaimType:dev.item.state",
        "cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
    ) in codes
    # A key's Claims are covered by their Subject's creation: no warning.
    assert all(artifact != "ClaimType:dev.item.slug" for artifact, _code in codes), codes
    assert intent.submit().outcome.status == "landed"
    cx.refresh()


def test_prepare_lints_a_field_with_no_evidence_rules_and_the_write_says_uncovered(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    _bare_vocabulary(cx)

    outcome = cx.set("dev.item/a", "dev.item.state", "open", because="It is open.")

    assert outcome.status == "accepted"
    assert [item.verdict for item in outcome.changes] == ["uncovered"]
    # F21: a write whose values all land uncovered says so in its hint.
    assert outcome.hint is not None and "uncovered" in outcome.hint


def _drafted_field(cx: Cruxible, name: str, value: object) -> object:
    return cx.claim_type(
        predicate=f"dev.item.{name}",
        subject_kinds=["dev.item"],
        value_type=value,  # type: ignore[arg-type]
        cardinality="one",
        permitted_roles=["normative"],
        referent_sensitivity="identity",
        sources=[],
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one", eligible_verdicts=("supported",), selector="only_contender"
        ),
        pins=[],
        evidence_freshness=None,
        canonical_form=("nfc", "trim"),
    )


def test_a_field_drafted_with_no_sources_takes_the_short_forms_own_words_rule(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    """F21: `cx.claim_type(sources=[])` admitted nothing, so every write landed uncovered."""

    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    slug = _drafted_field(cx, "slug", SLUG)
    state = _drafted_field(cx, "state", EnumType(members=("open", "done")))
    short_form = lower_claim_type_input(
        ClaimTypeInputRecord(predicate="dev.item.state", cardinality="one"), tree={}
    )
    assert state.definition.evidence_admission_policy == short_form.evidence_admission_policy

    changes = cx.changes(rationale="A vocabulary drafted with cx.claim_type.")
    changes.claim_type(slug)
    changes.claim_type(state)
    changes.subject_kind(name="dev.item", keys=["dev.item.slug"], description="An item.")
    assert changes.submit().outcome.status == "landed"
    cx.refresh()

    outcome = cx.set("dev.item/a", "dev.item.state", "open", because="It is open.")
    assert [item.verdict for item in outcome.changes] == ["supported"]
    assert outcome.hint is None


STATE = {"open": "Not done.", "done": "Done."}


def _field_vocabulary(cx: Cruxible) -> None:
    """F6/F7: a kind keyed by a slug, a label, an enum with meanings and a relation."""

    changes = cx.changes(rationale="Project items, defined with the short form.")
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
    changes.subject_kind(name="dev.item", description="A planned piece of work.")
    intent = changes.prepare()
    assert not intent.refused, intent.diagnostics
    assert intent.warnings == ()
    assert intent.submit().outcome.status == "landed"
    cx.refresh()


def test_a_field_lowers_through_the_short_forms_defaults(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    _field_vocabulary(cx)

    # The same request through claim-type propose's lowering gives the same bytes.
    accepted = ClaimType.model_validate(
        cx.get("ClaimType:dev.item.state", detail="proof").value["envelope"]
    )
    short_form = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate(
            {
                "predicate": "dev.item.state",
                "cardinality": "one",
                "value": EnumType(members=tuple(STATE)),
                "description": "Whether it is done.",
                "member_descriptions": [
                    {"member": member, "description": meaning} for member, meaning in STATE.items()
                ],
                "default_role": "normative",
            }
        ),
        tree={},
    )
    assert claim_type_digest(accepted) == claim_type_digest(short_form)

    outcome = cx.set("dev.item/a", "dev.item.state", "open", because="It is open.")
    assert [item.verdict for item in outcome.changes] == ["supported"]

    # Edit: what is not given keeps its accepted value; live Claims are carried.
    cx.refresh()
    edit = cx.changes(rationale="Say more about the state.")
    edit.field(
        "dev.item",
        "state",
        EnumType(members=tuple(STATE)),
        description="Whether the item is done.",
    )
    assert edit.submit().outcome.status == "landed"
    cx.refresh()
    edited = ClaimType.model_validate(
        cx.get("ClaimType:dev.item.state", detail="proof").value["envelope"]
    )
    assert edited.description == "Whether the item is done."
    assert edited.member_descriptions == accepted.member_descriptions
    assert cx.get("dev.item/a").value is not None


def test_retiring_fields_is_a_change_set_step_that_retires_their_claims(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    """F12: retiring a field took migrate, a hand-built request and an approval."""

    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    _field_vocabulary(cx)
    written = cx.set("dev.item/a", "dev.item.state", "open", because="It is open.")
    cx.set("dev.item/a", "dev.item.title", "First item", because="Its label.")
    cx.refresh()

    retired = cx.changes(rationale="State and title move to notes.")
    retired.retire_claim_type("dev.item.state")
    retired.retire_claim_type("dev.item.title")
    intent = retired.prepare()
    assert not intent.refused, intent.diagnostics
    landed = intent.submit()
    # It lands when policy allows, like any other change set.
    assert landed.outcome.status == "landed"

    cx.refresh()
    predicates = set(cx.world().predicates)
    assert "dev.item.state" not in predicates and "dev.item.title" not in predicates
    claim = cx.get(f"Claim:{written.changes[0].claim}", detail="proof").value
    assert claim.statement.lifecycle == "retired"


def _cli(http: TestClient, instance_id: str, monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    result = CliRunner().invoke(
        cli, ["--server-url", "http://cruxible", "--instance-id", instance_id, *args]
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_retires_fields_in_one_command_and_migrate_lands_when_allowed(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F12/F16: `claim-type retire`, and `migrate` no longer always stops at a proposal."""

    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    _field_vocabulary(cx)
    cx.set("dev.item/a", "dev.item.state", "open", because="It is open.")

    output = _cli(
        http,
        instance_id,
        monkeypatch,
        "claim-type",
        "retire",
        "dev.item.state",
        "--because",
        "State moves to notes.",
    )
    assert "landed: accepted at" in output

    # migrate: the same accept option as a change set (if_allowed by default).
    cx.refresh()
    title = ClaimType.model_validate(
        cx.get("ClaimType:dev.item.title", detail="proof").value["envelope"]
    )
    request = tmp_path / "migrate.json"
    request.write_text(
        json.dumps(
            {
                "tag": "playbill-claim-type-migration-request-v3",
                "mode": "submit",
                "successor": retired_successor(title).model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    output = _cli(http, instance_id, monkeypatch, "claim-type", "migrate", str(request))
    assert "landed: accepted at" in output

    cx.refresh()
    predicates = set(cx.world().predicates)
    assert "dev.item.state" not in predicates and "dev.item.title" not in predicates


def test_an_enum_spelling_that_cannot_hold_a_member_names_the_fix() -> None:
    """F14: `enum[0.5.0, next]` failed with "cannot read the type ... at 8" and no hint."""

    with pytest.raises(ContractFormatError, match=r"EnumType\(members=") as refused:
        coerce_field_type("enum[0.5.0, next]")
    assert 'enum["0.5.0", next]' in str(refused.value)
    # Both repairs it names read.
    assert coerce_field_type('enum["0.5.0", next]') == EnumType(members=("0.5.0", "next"))


def test_describe_shows_types_keys_member_meanings_and_the_kinds_description(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    """F10: every non-relation field read `literal`; no key, member meanings or kind text."""

    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    _field_vocabulary(cx)

    described = cx.world().describe().splitlines()
    start = described.index("  dev.item -- A planned piece of work.")
    assert described[start + 1 : start + 7] == [
        "    blocks: -> dev.item, many -- Items it blocks.",
        '    slug: string{pattern: "^[a-z0-9][a-z0-9-]*$", min_len: 1, max_len: 80}, one, key'
        " -- The item's short name.",
        "    state: enum, one [open, done] -- Whether it is done.",
        "      done: Done.",
        "      open: Not done.",
        "    title: string{min_len: 1, max_len: 120}, one -- Its label.",
    ]


def test_definition_lint_names_ledger_fields_prose_strings_and_enums_of_things(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    """F17/F19: warnings, not refusals, from prepare and the propose route alike."""

    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    changes = cx.changes(rationale="A vocabulary with three modelling slips.")
    changes.field("dev.item", "slug", SLUG, description="Its short name.", key=True)
    changes.field("dev.item", "decided_by", SLUG, description="Who decided.")
    changes.field("dev.item", "criteria", StringType(max_len=256), description="What done means.")
    changes.field(
        "dev.item",
        "release",
        EnumType(members=("0.5.0", "post-0.5.0", "unscheduled")),
        description="The release it is planned for.",
    )
    changes.subject_kind(name="dev.item", description="An item.")
    intent = changes.prepare()
    assert not intent.refused, intent.diagnostics
    found = {(item.artifact, item.code) for item in intent.warnings}
    expected = {
        ("ClaimType:dev.item.decided_by", "cruxible.claim_type.field_restates_ledger"),
        ("ClaimType:dev.item.criteria", "cruxible.claim_type.string_holds_prose"),
        ("ClaimType:dev.item.release", "cruxible.claim_type.enum_members_name_things"),
    }
    assert found == expected
    release = next(
        item for item in intent.warnings if item.artifact == "ClaimType:dev.item.release"
    )
    assert release.replacement_rule_fragment["members"] == ["0.5.0", "post-0.5.0"]

    # The propose route (claim-type propose) lints the same definition the same.
    proposed = http.post(
        f"/api/v1/{instance_id}/claim-types/proposals",
        json={
            "input": {
                "predicate": "dev.item.criteria",
                "cardinality": "one",
                "value": {"kind": "string", "max_len": 256},
                "description": "What done means.",
                "allowed_subject_kinds": ["dev.item"],
            },
            "proposal_name": "criteria",
            "dry_run": True,
        },
    )
    assert proposed.status_code == 200, proposed.text
    assert "cruxible.claim_type.string_holds_prose" in {
        item["code"] for item in proposed.json()["lint"]["warnings"]
    }
