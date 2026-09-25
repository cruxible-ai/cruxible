"""Hold rules the integration worlds do not reach: dependency basis and declared length."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.claim_types import (
    ClaimFreshnessDurationV1,
    ClaimType,
    claim_type_digest,
    render_claim_type,
)
from cruxible_core.claims.claim_type_inputs import (
    claim_type_input_template,
    lower_claim_type_input,
)
from cruxible_core.service.discovery.next import (
    DEFAULT_UNSURE_HOLD,
    PlaybillNextRepairV1,
    _Holds,
    _item,
    _UnsureHold,
)

AT = datetime(2026, 9, 1, tzinfo=UTC)
REFERENT = SimpleNamespace(git_oid="a" * 40)


def _claim_type(**update: object) -> ClaimType:
    return lower_claim_type_input(claim_type_input_template(), tree={}).model_copy(update=update)


def test_unsure_hold_for_is_a_v5_claim_type_field_absent_from_the_wire_unless_declared() -> None:
    plain = _claim_type()
    assert "unsure_hold_for" not in plain.model_dump(mode="json")
    declared = ClaimType.model_validate(
        {
            **plain.model_dump(mode="json"),
            "unsure_hold_for": {"microseconds": 7 * 86_400_000_000},
        }
    )
    assert declared.unsure_hold_for == ClaimFreshnessDurationV1(microseconds=7 * 86_400_000_000)
    assert claim_type_digest(declared) != claim_type_digest(plain)

    with pytest.raises(ValidationError, match="must be positive"):
        ClaimType.model_validate(
            {**plain.model_dump(mode="json"), "unsure_hold_for": {"microseconds": 0}}
        )


def _holds(*, seen: set[tuple[str, str]], holds: dict, current: dict, blob=None) -> _Holds:  # type: ignore[no-untyped-def,type-arg]
    @contextmanager
    def reader(*, at):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(
            artifact=lambda digest, *, identity: object() if (identity, digest) in seen else None
        )

    value = object.__new__(_Holds)
    value._instance = SimpleNamespace(accepted_history_reader=reader, blob_at=blob)  # type: ignore[attr-defined]
    value._coordinate = SimpleNamespace(git_oid="b" * 40)  # type: ignore[attr-defined]
    value._evaluation_time = AT  # type: ignore[attr-defined]
    value._current = current  # type: ignore[attr-defined]
    value._claims = {  # type: ignore[attr-defined]
        identity: SimpleNamespace(statement=SimpleNamespace(predicate="project.work_item.status"))
        for identity in current
    }
    value._hold_for = {}  # type: ignore[attr-defined]
    value._seen = {}  # type: ignore[attr-defined]
    value._holds = holds  # type: ignore[attr-defined]
    return value


def _dependency_row(upstream_digest: str | None):  # type: ignore[no-untyped-def]
    return _item(
        severity="warning",
        reason="claim_dependency_stale",
        subject_identity="Claim:CLM-dependent",
        detail={
            "stale_inputs": [
                {
                    "source_claim_identity": "Claim:CLM-upstream",
                    "used_artifact_digest": "sha256:" + "1" * 64,
                    "current_artifact_digest": upstream_digest,
                }
            ]
        },
        repair=PlaybillNextRepairV1(
            operation="hand_edit", target="Claim:CLM-dependent", required_change="x"
        ),
    )


def test_a_dependency_hold_covers_only_upstream_versions_its_examiner_saw() -> None:
    upstream_now = "sha256:" + "2" * 64
    current = {"Claim:CLM-dependent": "sha256:" + "d" * 64, "Claim:CLM-upstream": upstream_now}
    hold = _UnsureHold(referent=REFERENT, attested_at=AT, valid_until=None)  # type: ignore[arg-type]
    holds = {"Claim:CLM-dependent": [hold]}

    saw_revision = _holds(seen=set(current.items()), holds=holds, current=current)
    assert saw_revision.covers(_dependency_row(upstream_now))

    # The upstream was revised again after the examiner looked.
    missed = _holds(
        seen={("Claim:CLM-dependent", current["Claim:CLM-dependent"])},
        holds=holds,
        current=current,
    )
    assert not missed.covers(_dependency_row(upstream_now))
    # An upstream with no current version cannot have been seen.
    assert not saw_revision.covers(_dependency_row(None))


def test_a_standing_hold_lasts_as_long_as_its_claim_type_declares() -> None:
    declared = _claim_type(unsure_hold_for=ClaimFreshnessDurationV1(microseconds=86_400_000_000))
    current = {"Claim:CLM-a": "sha256:" + "a" * 64}
    uncovered = _item(
        severity="warning",
        reason="claim_uncovered",
        subject_identity="Claim:CLM-a",
        detail={},
        repair=PlaybillNextRepairV1(
            operation="hand_edit", target="Claim:CLM-a", required_change="x"
        ),
    )

    def at(evaluation: datetime, blob) -> bool:  # type: ignore[no-untyped-def]
        holds = _holds(
            seen=set(),
            holds={
                "Claim:CLM-a": [_UnsureHold(referent=REFERENT, attested_at=AT, valid_until=None)]
            },  # type: ignore[arg-type]
            current=current,
            blob=blob,
        )
        holds._evaluation_time = evaluation  # type: ignore[attr-defined]
        return holds.covers(uncovered)

    one_day = lambda *_args: render_claim_type(declared)  # noqa: E731
    assert at(AT + timedelta(hours=23), one_day)
    assert not at(AT + timedelta(days=1), one_day)
    # Undeclared, the engine default applies.
    undeclared = lambda *_args: render_claim_type(_claim_type())  # noqa: E731
    assert at(AT + DEFAULT_UNSURE_HOLD - timedelta(seconds=1), undeclared)
    assert not at(AT + DEFAULT_UNSURE_HOLD, undeclared)


def test_a_held_member_leaves_its_conflict_row_valid_and_rebuilt() -> None:
    from cruxible_core.service.discovery.next import (
        PlaybillNextItemV1,
        _apply_holds,
        _with_findings,
    )

    def row(reason: str, subject: str, **arguments: object):  # type: ignore[no-untyped-def]
        return _item(
            severity="blocking" if reason == "claim_conflicted" else "warning",
            reason=reason,  # type: ignore[arg-type]
            subject_identity=subject,
            detail={},
            repair=PlaybillNextRepairV1(
                operation="hand_edit", target=subject, required_change="x", arguments=arguments
            ),
        )

    conflict = _with_findings(
        row("claim_conflicted", "subjects/wi.json", claim_ids=["Claim:CLM-a", "Claim:CLM-b"]),
        [row("claim_uncovered", "Claim:CLM-a")],
    )
    current = {"Claim:CLM-a": "sha256:" + "a" * 64, "Claim:CLM-b": "sha256:" + "b" * 64}
    # Only CLM-a is held, so the conflict stands but its uncovered member is parked.
    holds = _holds(
        seen=set(current.items()),
        holds={
            "Claim:CLM-a": [
                _UnsureHold(referent=REFERENT, attested_at=AT, valid_until=AT + timedelta(days=1))  # type: ignore[arg-type]
            ]
        },
        current=current,
    )

    (kept,), held = _apply_holds((conflict,), holds)

    assert held == 1
    assert kept.reason == "claim_conflicted" and kept.findings == ()
    assert PlaybillNextItemV1.model_validate(kept.model_dump(mode="json")) == kept
