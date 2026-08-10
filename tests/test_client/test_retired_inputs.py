"""The client input contracts REFUSE the inputs the 0.4.0 removals retired.

``FeedbackBatchItemInput`` and ``FeedbackFromQueryInput`` are the two client
contracts a caller populates by hand, and pydantic ignores unknown keys -- so a
stale caller's ``group_override=True`` used to construct cleanly, serialize
without it, and post a request that looked deliberate and was not.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.http_client import CruxibleClient

_TARGET = {
    "from_type": "Part",
    "from_id": "BP-1",
    "relationship_type": "fits",
    "to_type": "Vehicle",
    "to_id": "V-1",
}
_BATCH_ITEM = {"receipt_id": "RCP-1", "action": "accept", "target": _TARGET}
_FROM_QUERY = {"receipt_id": "RCP-1", "result_index": 0, "action": "accept"}

_RETIRED_CONTRACT_INPUTS = [
    (contracts.FeedbackBatchItemInput, _BATCH_ITEM, "source", "actor_context"),
    (contracts.FeedbackBatchItemInput, _BATCH_ITEM, "group_override", "no public replacement"),
    (contracts.FeedbackFromQueryInput, _FROM_QUERY, "source", "actor_context"),
    (contracts.FeedbackFromQueryInput, _FROM_QUERY, "group_override", "no public replacement"),
]


@pytest.mark.parametrize(("model", "base", "key", "guidance"), _RETIRED_CONTRACT_INPUTS)
def test_retired_contract_inputs_raise_a_typed_error(
    model: type,
    base: dict[str, Any],
    key: str,
    guidance: str,
) -> None:
    retired_value: Any = True if key == "group_override" else "agent"

    with pytest.raises(ValidationError) as raised:
        model(**base, **{key: retired_value})

    message = str(raised.value)
    assert f"'{key}' was removed in 0.4.0" in message
    assert guidance in message


@pytest.mark.parametrize(
    ("model", "base"),
    [
        (contracts.FeedbackBatchItemInput, _BATCH_ITEM),
        (contracts.FeedbackFromQueryInput, _FROM_QUERY),
    ],
)
def test_an_unrelated_unknown_key_still_constructs(model: type, base: dict[str, Any]) -> None:
    """The refusal is by NAME, not an extras ban."""
    assert model(**base, not_a_retired_input="whatever") is not None


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("feedback", "source"),
        ("feedback", "group_override"),
        ("feedback_from_query", "source"),
        ("feedback_from_query", "group_override"),
        ("outcome", "source"),
        ("propose_group", "proposed_by"),
        ("resolve_group", "resolved_by"),
        ("create_decision_record", "opened_by"),
    ],
)
def test_retired_client_method_keywords_no_longer_exist(method: str, key: str) -> None:
    """No ``**kwargs`` catch-all: a retired keyword is a ``TypeError`` at the call."""
    import inspect

    parameters = inspect.signature(getattr(CruxibleClient, method)).parameters
    assert key not in parameters
    assert not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def test_a_retired_client_keyword_raises_before_any_request() -> None:
    """No transport is touched: binding fails at the call itself."""
    with CruxibleClient(base_url="http://cruxible.invalid") as client:
        with pytest.raises(TypeError, match="group_override"):
            client.feedback(  # type: ignore[call-arg]
                "inst-1",
                action="accept",
                from_type="Part",
                from_id="BP-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
                group_override=True,
            )
