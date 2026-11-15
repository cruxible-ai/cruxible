"""Defining a vocabulary through the typed SDK: the findings of its first real use.

Each test reproduces one finding of the 2026-11-13 SDK run (friction F6-F21):
the same request gives the same result on the SDK, the CLI and MCP.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.contracts.claim_type_inputs import (
    ClaimTypeInputRecord,
    lower_claim_type_input,
)
from cruxible_client.contracts.policies import ClaimAdmissionPolicy, ClaimResolutionPolicy
from cruxible_client.contracts.shapes import EnumType, StringType
from cruxible_client.transport.http import CruxibleClient
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
