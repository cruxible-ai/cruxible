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
