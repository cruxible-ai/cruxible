"""The write service runs inside one request scope (projection binds, live position, timing).

A helper inserted above ``service_playbill_write`` once took its decorators, so
every write bound projections afresh (16 binds for a two-field dry run, not 3).
"""

from cruxible_core.service.authoring import write_verbs


def _wrapper_depth(function: object) -> int:
    depth = 0
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
        depth += 1
    return depth


def test_the_write_service_keeps_its_request_scope_decorators() -> None:
    # timed_operation, unpositioned_operation and projection_request each wrap it.
    assert _wrapper_depth(write_verbs.service_playbill_write) >= 3


def test_the_prefixed_id_hint_is_a_plain_helper() -> None:
    assert _wrapper_depth(write_verbs._prefixed_id_hint) == 0
