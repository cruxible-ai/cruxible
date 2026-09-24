"""An autouse fixture: every served query is also answered over whole-state facts."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.service.discovery import query as served


def _outcome(call: Any) -> Any:
    try:
        return ("result", call())
    except Exception as exc:  # noqa: BLE001 - refusals must match exactly too
        return ("refused", type(exc), str(exc))


@pytest.fixture(autouse=True)
def _scoped_facts_answer_as_whole_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A served query reads only the Subjects it can reach; the answer must not change.

    Each scoped build also builds the whole-state facts, and each evaluation over
    scoped facts is repeated over the whole ones: result or refusal must match.
    """
    build = served.build_accepted_query_facts
    evaluate = served.evaluate_claim_query
    whole: dict[int, Any] = {}

    def scoped_build(*args: Any, **kwargs: Any) -> Any:
        facts = build(*args, **kwargs)
        if kwargs.get("subject_kinds") is not None:
            whole[id(facts)] = build(*args, **{**kwargs, "subject_kinds": None})
        return facts

    def checked_evaluate(definition: Any, *, facts: Any, **kwargs: Any) -> Any:
        scoped = _outcome(lambda: evaluate(definition, facts=facts, **kwargs))
        reference = whole.pop(id(facts), None)
        if reference is not None:
            assert scoped == _outcome(lambda: evaluate(definition, facts=reference, **kwargs))
        if scoped[0] == "refused":
            return evaluate(definition, facts=facts, **kwargs)  # re-raise the same refusal
        return scoped[1]

    monkeypatch.setattr(served, "build_accepted_query_facts", scoped_build)
    monkeypatch.setattr(served, "evaluate_claim_query", checked_evaluate)
