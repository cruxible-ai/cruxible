"""A write's ``because`` that no record can hold refuses as a typed 422, never a 500.

``because`` is each Claim's rationale (one line of at most 280 characters) and
the change set's rationale (nonblank, no NUL). A value outside either law used
to pass the request model and fail a payload model deep in planning, which the
daemon answered as ``internal server error``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._write_support import KIND, seed_write_vocabulary

WI1 = f"{KIND}/wi-1"
CODE = "cruxible.claim.rationale_invalid"


@pytest.fixture
def served(playbill_http: tuple[TestClient, str, Path]) -> tuple[TestClient, str, str]:
    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    seed_write_vocabulary(get_playbill_manager().get(instance_id), actor_id=actor)
    base = f"/api/v1/{instance_id}"
    first = client.post(
        f"{base}/set",
        json={"subject": WI1, "field": "status", "value": "ready", "because": "Checked."},
    )
    assert first.status_code == 200, first.text
    return client, base, first.json()["changes"][0]["claim"]


def _claim_writes(because: str, *, dry_run: bool) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every write verb whose ``because`` becomes a Claim's rationale."""

    common = {"because": because, "dry_run": dry_run}
    return {
        "set": ("set", {"subject": WI1, "field": "status", "value": "blocked", **common}),
        "set_creating": (
            "set",
            {"subject": f"{KIND}/wi-new", "field": "status", "value": "ready", **common},
        ),
        "add": (
            "write",
            {
                "changes": [
                    {"op": "add", "subject": WI1, "field": "governs", "value": f"{KIND}/wi-2"}
                ],
                **common,
            },
        ),
        "create": (
            "write",
            {
                "changes": [{"op": "create", "label": "n", "subject_kind": KIND, "key": "wi-made"}],
                **common,
            },
        ),
        "write_set": (
            "write",
            {
                "subject": WI1,
                "changes": [{"op": "set", "field": "status", "value": "done"}],
                **common,
            },
        ),
    }


@pytest.mark.parametrize("dry_run", [False, True], ids=["commit", "dry_run"])
@pytest.mark.parametrize(
    ("because", "problem"),
    [
        ("x" * 281, "281 characters"),
        ("Checked.\nThe second line.", "a line break"),
        ("Checked.\rThe second line.", "a line break"),
        ("Checked. The second line.", "a line break"),
        ("   ", "no text"),
        ("\x00", "no text"),
    ],
    ids=["281_chars", "newline", "carriage_return", "line_separator", "whitespace", "only_nul"],
)
def test_a_because_no_claim_rationale_can_hold_is_a_typed_422(
    served: tuple[TestClient, str, str], because: str, problem: str, dry_run: bool
) -> None:
    client, base, _claim = served
    for verb, (route, body) in _claim_writes(because, dry_run=dry_run).items():
        response = client.post(f"{base}/{route}", json=body)
        assert response.status_code == 422, (verb, response.text)
        refusal = response.json()
        assert refusal["error_code"] == CODE, (verb, refusal)
        assert "one line of at most 280 characters" in refusal["message"], verb
        assert problem in refusal["message"], (verb, refusal["message"])
        change = refusal["repair"]["hand_edit"]["required_change"]
        assert refusal["repair"]["hand_edit"]["target"] == f"refusal/{CODE}"
        assert "Document section" in change and "cite" in change, change
        assert "because" not in change.split("Document section")[1], change


@pytest.mark.parametrize("dry_run", [False, True], ids=["commit", "dry_run"])
def test_a_because_of_280_characters_still_writes(
    served: tuple[TestClient, str, str], dry_run: bool
) -> None:
    client, base, _claim = served
    for verb, (route, body) in _claim_writes("y" * 280, dry_run=dry_run).items():
        response = client.post(f"{base}/{route}", json=body)
        assert response.status_code == 200, (verb, response.text)
        expected = {"would_accept"} if dry_run else {"accepted"}
        assert response.json()["status"] in expected, (verb, response.json())


def test_a_retire_keeps_a_long_or_multiline_because(
    served: tuple[TestClient, str, str],
) -> None:
    """A retire states no Claim: its ``because`` is only the change set's rationale."""

    client, base, claim = served
    long_reason = "Withdrawn.\n" + "z" * 400
    preview = client.post(
        f"{base}/retire", json={"target": claim, "because": long_reason, "dry_run": True}
    )
    assert preview.status_code == 200 and preview.json()["status"] == "would_accept", preview.text
    retired = client.post(
        f"{base}/write",
        json={
            "because": "Cleanup.",
            "changes": [{"op": "retire", "target": claim, "because": long_reason}],
        },
    )
    assert retired.status_code == 200 and retired.json()["status"] == "accepted", retired.text


@pytest.mark.parametrize("dry_run", [False, True], ids=["commit", "dry_run"])
def test_a_because_no_change_set_rationale_can_hold_is_a_422(
    served: tuple[TestClient, str, str], dry_run: bool
) -> None:
    """Blank or NUL-bearing text refuses on every verb, a retire included."""

    client, base, claim = served
    cases: list[tuple[str, dict[str, Any]]] = [
        ("retire", {"target": claim, "because": "   "}),
        ("retire", {"target": claim, "because": "\x00"}),
        ("retire", {"target": claim, "because": "Withdrawn\x00."}),
        ("set", {"subject": WI1, "field": "status", "value": "done", "because": "a\x00b"}),
        (
            "write",
            {"because": "x", "changes": [{"op": "retire", "target": claim, "because": "   "}]},
        ),
        (
            "write",
            {"because": "x", "changes": [{"op": "retire", "target": claim, "because": "a\x00"}]},
        ),
    ]
    for route, body in cases:
        response = client.post(f"{base}/{route}", json={**body, "dry_run": dry_run})
        assert response.status_code == 422, (route, body, response.text)
        assert "because" in " ".join(response.json()["errors"]), response.json()


TOO_LONG = "cruxible.write.rationale_too_long"
LIMIT = 4096


def _assert_too_long(response: Any, status: int) -> None:
    assert response.status_code == status, response.text
    refusal = response.json()
    assert refusal["error_code"] == TOO_LONG, refusal
    assert f"at most {LIMIT} characters" in refusal["message"], refusal["message"]
    assert refusal["repair"]["hand_edit"]["target"] == f"refusal/{TOO_LONG}"
    assert "Document section" in refusal["repair"]["hand_edit"]["required_change"]


@pytest.mark.parametrize("dry_run", [False, True], ids=["commit", "dry_run"])
def test_a_retire_because_over_the_change_set_limit_is_a_typed_422(
    served: tuple[TestClient, str, str], dry_run: bool
) -> None:
    client, base, claim = served
    over = client.post(
        f"{base}/retire", json={"target": claim, "because": "x" * (LIMIT + 1), "dry_run": dry_run}
    )
    _assert_too_long(over, 422)
    # Trimmed first, as the planner joins it: surrounding space does not count.
    at_limit = client.post(
        f"{base}/retire",
        json={"target": claim, "because": f"  {'x' * LIMIT}\n", "dry_run": dry_run},
    )
    assert at_limit.status_code == 200, at_limit.text
    assert at_limit.json()["status"] == ("would_accept" if dry_run else "accepted")


def test_retire_notes_over_the_limit_together_are_a_typed_refusal(
    served: tuple[TestClient, str, str],
) -> None:
    """Each note fits; the joined change-set rationale does not, by exactly one character."""

    client, base, _claim = served
    labels = client.post(
        f"{base}/write",
        json={
            "because": "Labelled.",
            "subject": WI1,
            "changes": [{"op": "add", "field": "labels", "value": v} for v in ("a", "b", "c")],
        },
    )
    assert labels.status_code == 200 and labels.json()["status"] == "accepted", labels.text
    claims = [item["claim"] for item in labels.json()["changes"]]
    main = "Cleanup."
    # The planner's rationale: main, then "Retire CLAIM: because" per note, blank-line joined.
    fixed = len(main) + sum(len(f"\n\nRetire {claim}: ") for claim in claims)
    room = LIMIT - fixed
    sizes = [room // 3, room // 3, room - 2 * (room // 3)]
    assert all(size < LIMIT // 2 for size in sizes)

    def retire(extra: int, *, dry_run: bool) -> Any:
        return client.post(
            f"{base}/write",
            json={
                "because": main,
                "dry_run": dry_run,
                "changes": [
                    {"op": "retire", "target": claim, "because": "y" * (size + (extra and i == 0))}
                    for i, (claim, size) in enumerate(zip(claims, sizes, strict=True))
                ],
            },
        )

    _assert_too_long(retire(1, dry_run=True), 400)
    _assert_too_long(retire(1, dry_run=False), 400)
    at_limit = retire(0, dry_run=True)
    assert at_limit.status_code == 200 and at_limit.json()["status"] == "would_accept", (
        at_limit.text
    )
    committed = retire(0, dry_run=False)
    assert committed.status_code == 200 and committed.json()["status"] == "accepted", committed.text


def test_a_retire_because_with_surrounding_space_writes(
    served: tuple[TestClient, str, str],
) -> None:
    """The retire note is joined into the change set's rationale trimmed."""

    client, base, claim = served
    response = client.post(
        f"{base}/write",
        json={
            "because": "Cleanup.",
            "changes": [{"op": "retire", "target": claim, "because": "  Withdrawn.  "}],
        },
    )
    assert response.status_code == 200 and response.json()["status"] == "accepted", response.text


def test_control_characters_other_than_line_breaks_stay_admitted(
    served: tuple[TestClient, str, str],
) -> None:
    """A tab or escape is one line to the Claim law; only the refused shapes changed."""

    client, base, _claim = served
    for because in ("a\tb", "a\x07b", "a\x1bb", "a\x7fb"):
        response = client.post(
            f"{base}/set",
            json={
                "subject": WI1,
                "field": "status",
                "value": "blocked",
                "because": because,
                "dry_run": True,
            },
        )
        assert response.status_code == 200, (because, response.text)
        assert response.json()["status"] == "would_accept", response.json()
