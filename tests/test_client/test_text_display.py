"""``print()`` on an SDK result reads as the CLI prints it; ``repr()`` stays the model.

Before, ``print(cx.orient())`` was one ~2,000-character line, ``print(cx.coordinate)``
four digests, and ``print(intent)`` hid a refusal's diagnostics (findings F4, F5,
F9, F15). Each now renders through ``cruxible_client.contracts.text_display``,
which the CLI prints too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from cruxible_client.authoring import sdk
from cruxible_client.contracts.authoring_landing import AuthoringLanded, AuthoringProposed
from cruxible_client.contracts.orient import OrientResult
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.text_display import render_orient, write_outcome_lines
from cruxible_client.contracts.write import WriteOutcome

_OID = "0123456789ab" + "c" * 28
_COORDINATE = AcceptedCoordinate.model_validate(
    {
        "git_oid": _OID,
        "semantic_root": "sha256:" + "1" * 64,
        "generation_root": "sha256:" + "2" * 64,
        "compiler_digest": "sha256:" + "3" * 64,
    }
)


def test_a_coordinate_prints_as_its_short_git_oid_and_reprs_whole() -> None:
    assert str(_COORDINATE) == "0123456789ab"
    assert f"at {_COORDINATE}" == "at 0123456789ab"
    assert "semantic_root" in repr(_COORDINATE)


def test_an_orient_result_prints_the_cli_map() -> None:
    from cruxible_core.cli.commands.playbill import _render_orient

    result = OrientResult.model_validate(
        {
            "instance": "inst_demo",
            "generation": 3,
            "coordinate": _COORDINATE.model_dump(mode="json"),
            "accepted_at": "2026-11-14T00:00:00Z",
            "evaluation_time": "2026-11-14T00:00:00Z",
            "kinds": [{"kind": "shop.part", "subjects": 2, "fields": ["status"]}],
            "queries": [],
            "next": ['cx.orient(kind="shop.part")'],
        }
    )

    text = str(result)

    assert text == render_orient(result.model_dump(mode="json"))
    assert _render_orient is render_orient
    assert text.splitlines() == [
        "Cruxible inst_demo generation=3 at 0123456789ab accepted 2026-11-14T00:00:00Z",
        "Kinds (1):",
        "  shop.part  subjects=2  fields=status",
        "Next:",
        '  cx.orient(kind="shop.part")',
    ]
    assert repr(result).startswith("OrientResult(")


def test_an_authoring_landing_prints_status_and_next() -> None:
    landed = AuthoringLanded(accepted_coordinate=_COORDINATE)
    proposed = AuthoringProposed(
        reason="accept_never", message="kept as a proposal (accept=never)", next="activate it"
    )

    assert str(landed) == "landed: accepted at 0123456789ab"
    assert str(proposed) == "kept as a proposal (accept=never)\nnext: activate it"
    assert "accepted_coordinate" in repr(landed)


def test_a_write_outcome_prints_what_the_cli_prints() -> None:
    outcome = WriteOutcome.model_validate(
        {
            "status": "accepted",
            "coordinate": {"git_oid": _OID[:12], "generation": 4},
            "next": 'cx.get("shop.part/bp-1")',
        }
    )

    assert str(outcome).splitlines() == [
        f"accepted (generation 4, {_OID[:12]})",
        'next: cx.get("shop.part/bp-1")',
    ]
    assert str(outcome) == "\n".join(text for text, _err in write_outcome_lines(outcome))


def test_a_refused_intent_prints_its_diagnostics(tmp_path: Path) -> None:
    from cruxible_client import contracts as api

    class _Client:
        def whoami(self, _instance_id: str) -> object:
            return SimpleNamespace(coordinate=_COORDINATE)

        def close(self) -> None:
            return None

    cx = sdk.Cruxible(
        client=_Client(),  # type: ignore[arg-type]
        instance_id="inst_demo",
        workspace=tmp_path,
        access_profile=sdk.AccessProfile(
            profile_id="sdk-default",
            permitted_access_classes=("instance",),
            disclose_restricted_existence=True,
        ),
        clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
    ).at(_COORDINATE)
    diagnostic = SimpleNamespace(
        code="cruxible.claim_type.string_unbounded",
        stage="law",
        offending_element="$.members[0]",
        message="the string field has no bound",
        repairs=(SimpleNamespace(model_dump=lambda mode: {"description": "use an enum"}),),
        owner=None,
        disposition=None,
    )
    intent = sdk.Intent(
        cx,
        None,
        {"intent_id": "int-1", "intent_revision": 2},
        preflight=api.AuthoringPreflightResult.model_construct(
            verdict="refused", frontier=SimpleNamespace(diagnostics=(diagnostic,))
        ),
    )

    assert repr(intent) == "Intent('int-1', revision=2, refused, 1 diagnostics)"
    assert str(intent).splitlines() == [
        "intent int-1 revision 2: refused",
        "  cruxible.claim_type.string_unbounded at $.members[0]: the string field has no bound",
        "    repair: use an enum",
    ]


def test_vocabulary_results_print_what_the_cli_prints() -> None:
    """Walkthrough W20: print(cx.vocabulary.plan()) was a pydantic repr."""

    from cruxible_client.contracts.authoring_landing import AuthoringLanded
    from cruxible_client.contracts.vocabulary_file import (
        VocabularyApplyResult,
        VocabularyBaseline,
        VocabularyExportResult,
        VocabularyPlan,
        VocabularyPlanEntry,
        VocabularyPreflight,
    )

    baseline = VocabularyBaseline(
        git_oid=_OID,
        semantic_root=_COORDINATE.semantic_root,
        generation_root=_COORDINATE.generation_root,
        compiler_digest=_COORDINATE.compiler_digest,
        vocabulary_digest="sha256:" + "4" * 64,
    )
    plan = VocabularyPlan(
        path=".cruxible/vocabulary.yaml",
        based_on=baseline,
        entries=(
            VocabularyPlanEntry(
                definition="ClaimType:dev.item.title", action="retire", live_claims=2
            ),
            VocabularyPlanEntry(definition="ClaimType:dev.item.slug", action="not_in_file"),
        ),
        preflight=VocabularyPreflight(verdict="passed"),
    )
    assert str(plan).splitlines() == [
        "Vocabulary plan: .cruxible/vocabulary.yaml (based on 0123456789ab)",
        "Changes:",
        "  retire ClaimType:dev.item.title (2 live Claim(s) retire with it)",
        "Not in file (left as they are): 1",
        "  ClaimType:dev.item.slug",
        "Preflight: passed",
    ]
    applied = VocabularyApplyResult(
        status="landed",
        plan=plan,
        outcome=AuthoringLanded(accepted_coordinate=_COORDINATE),
        next="cruxible vocabulary export",
    )
    assert str(applied).splitlines()[-2:] == [
        "landed: accepted at 0123456789ab",
        "Next: cruxible vocabulary export",
    ]
    exported = VocabularyExportResult(
        path=".cruxible/vocabulary.yaml", status="written", based_on=baseline, kinds=2, fields=5
    )
    assert str(exported) == (
        "Wrote .cruxible/vocabulary.yaml (2 kind(s), 5 field(s)) at 0123456789ab"
    )
    for result in (plan, applied, exported):
        assert repr(result).startswith(type(result).__name__ + "(")


def test_a_query_page_prints_the_cli_table() -> None:
    """Walkthrough W20: print(cx.query(...)) showed no rows."""

    from cruxible_client.authoring.compact_query import QueryResult, render_query_table
    from cruxible_client.contracts.compact_query import QueryResultRecord

    page = QueryResultRecord.model_validate(
        {
            "kind": "dev.item",
            "columns": [{"name": "state", "predicate": "dev.item.state", "type": "string"}],
            "rows": [{"subject": "dev.item/a", "state": "open"}],
            "receipt": {
                "mode": "inline",
                "spec_digest": "sha256:" + "5" * 64,
                "coordinate": _COORDINATE.model_dump(mode="json"),
                "evaluation_time": "2026-11-15T00:00:00Z",
            },
        }
    )
    result = QueryResult(page)
    assert str(result) == render_query_table(page)
    assert "dev.item/a" in str(result) and "open" in str(result)
    assert repr(result).startswith("<QueryResult rows=1")
