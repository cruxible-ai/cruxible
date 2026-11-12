"""Fit results and the refusal messages of binding by capability (design note §2.2, §2.6)."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.capability_fit import (
    UNFIT_CLAUSES,
    BindingFit,
    EffectUnequal,
    FitProblem,
    FitResult,
    Fits,
    FitSide,
    LineBindingRefusalCode,
    TruncationUnfit,
    Unfit,
    binding_undeclared_message,
    binding_unfit_message,
    render_unfit,
    slot_ambiguous_message,
    slot_unfilled_message,
)

FIT = TypeAdapter(FitResult)
BINDING = TypeAdapter(BindingFit)

CAPTURE_UNFIT = Unfit(
    side="result",
    path="document",
    problem="type",
    sender="capture<*>",
    receiver="capture<security.osv-matches>",
)
ENUM_UNFIT = Unfit(
    side="request",
    path="time_range",
    problem="type",
    sender="enum[day, month, year]",
    receiver="enum[day, month]",
)


def test_results_read_by_their_verdict() -> None:
    assert FIT.validate_python({"verdict": "fits", "fit": "identical"}) == Fits(fit="identical")
    assert FIT.validate_python(ENUM_UNFIT.model_dump(mode="json")) == ENUM_UNFIT
    assert BINDING.validate_python({"verdict": "truncation_unfit"}) == TruncationUnfit()
    assert BINDING.validate_python(
        {"verdict": "effect_unequal", "capability": "pure", "operation": "external_read"}
    ) == EffectUnequal(capability="pure", operation="external_read")
    with pytest.raises(ValidationError):
        FIT.validate_python({"verdict": "truncation_unfit"})
    with pytest.raises(ValidationError):
        FIT.validate_python({"verdict": "fits", "fit": "close_enough"})
    with pytest.raises(ValidationError):
        Unfit(side="request", path="", problem="type", sender="a", receiver="b")


def test_every_side_and_problem_has_a_clause() -> None:
    assert set(UNFIT_CLAUSES) == {
        (side, problem) for side in get_args(FitSide) for problem in get_args(FitProblem)
    }


@pytest.mark.parametrize(
    ("unfit", "text"),
    [
        (
            ENUM_UNFIT,
            "request.time_range: the slot may send enum[day, month, year]; the operation "
            "accepts enum[day, month]",
        ),
        (
            CAPTURE_UNFIT,
            "result.document: the operation returns capture<*>; the capability expects "
            "capture<security.osv-matches>",
        ),
        (
            Unfit(
                side="request",
                path="region",
                problem="undeclared",
                sender="string",
                receiver="absent",
            ),
            "request.region: the slot may send string; the operation does not accept it",
        ),
        (
            Unfit(
                side="result", path="extra", problem="undeclared", sender="int", receiver="absent"
            ),
            "result.extra: the operation returns int; the capability does not declare it",
        ),
        (
            Unfit(
                side="request",
                path="language",
                problem="optional",
                sender="string?",
                receiver="string",
            ),
            "request.language: the slot may omit it; the operation requires string",
        ),
        (
            Unfit(side="result", path="count", problem="required", sender="absent", receiver="int"),
            "result.count: the capability requires int; the operation never returns it",
        ),
        (
            Unfit(
                side="result",
                path="document.layout.records.not_checked",
                problem="layout",
                sender="absent",
                receiver="at /not_checked/*",
            ),
            "result.document.layout.records.not_checked: the operation's copy declares absent; "
            "the instance's layout declares at /not_checked/*",
        ),
        (
            EffectUnequal(capability="pure", operation="external_read"),
            "effect: the capability is pure; the operation is external_read",
        ),
        (
            TruncationUnfit(),
            "this operation may return a truncated listing; slot `search` treats its result "
            "as complete",
        ),
    ],
)
def test_an_unfit_reason_names_the_path_and_both_sides(unfit, text: str) -> None:
    assert render_unfit(unfit, slot="search") == text


def test_the_binding_codes_are_a_closed_vocabulary() -> None:
    assert set(get_args(LineBindingRefusalCode)) == {
        "cruxible.line.slot_unfilled",
        "cruxible.line.slot_ambiguous",
        "cruxible.line.binding_unfit",
        "cruxible.line.binding_undeclared",
    }


def test_the_messages_read_as_the_design_note_writes_them() -> None:
    assert slot_unfilled_message(
        "match", "security.osv.match@1", "pure", unfit=[("other-osv#match", CAPTURE_UNFIT)]
    ) == (
        "No live operation serves slot `match` (security.osv.match@1, pure). Declares it but "
        "does not fit: `other-osv#match`: result.document: the operation returns capture<*>; "
        "the capability expects capture<security.osv-matches>."
    )
    assert binding_unfit_message("search", "web.search@1", "brave#search", ENUM_UNFIT) == (
        "Slot `search` needs web.search@1. `brave#search` declares it but does not fit: "
        "request.time_range: the slot may send enum[day, month, year]; the operation accepts "
        "enum[day, month]."
    )
    assert binding_undeclared_message("search", "web.search@1", "web#web.fetch", ["web.fetch"]) == (
        "Slot `search` needs web.search@1. `web#web.fetch` serves web.fetch, not web.search."
    )


def test_unfilled_and_ambiguous_slots_read_plainly() -> None:
    assert slot_unfilled_message("fetch", "web.fetch@1", "external_read", within="Line:feeds") == (
        "No live operation in Line:feeds serves slot `fetch` (web.fetch@1, external_read)."
    )
    assert slot_ambiguous_message(
        "fetch", "web.fetch@1", "external_read", ["a#web.fetch", "b#web.fetch"], more=True
    ) == (
        "Several live operations serve slot `fetch` (web.fetch@1, external_read): "
        "`a#web.fetch`, `b#web.fetch`, ..."
        "."
    )
    assert binding_unfit_message(
        "fetch",
        "web.fetch@1",
        "a#web.fetch",
        EffectUnequal(capability="external_read", operation="pure"),
    ).endswith("does not fit: effect: the capability is external_read; the operation is pure.")
