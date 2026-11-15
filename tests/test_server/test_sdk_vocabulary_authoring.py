"""Defining a vocabulary through the typed SDK: the findings of its first real use.

Each test reproduces one finding of the 2026-11-13 SDK run (friction F6-F21):
the same request gives the same result on the SDK, the CLI and MCP.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client import Cruxible
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
