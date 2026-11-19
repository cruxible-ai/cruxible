"""Public route names survive both shapes of FastAPI's route table.

Before FastAPI 0.137 an included router's routes are copied into the including
router, so ``app.routes`` is a flat list of ``APIRoute``. From 0.137 an included
router stays a node of a tree (an entry carrying ``original_router``), and the
served routes are derived from the originals. ``_public_route_names`` has to
reach every ``APIRoute`` in either shape, or internal handler names
(``query_playbill``) leak into OpenAPI.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from cruxible_core.server.app import _public_route_names


def _router_with_internal_names() -> APIRouter:
    router = APIRouter()

    @router.get("/things")
    def list_playbill_things() -> dict[str, str]:
        return {}

    @router.post("/things")
    def playbill_make_thing() -> dict[str, str]:
        return {}

    return router


@dataclass
class _IncludedRouterNode(BaseRoute):
    """The tree-node shape FastAPI >= 0.137 puts in ``routes`` for an include."""

    original_router: APIRouter


def _api_routes(router: APIRouter) -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_tree_shaped_routes_are_renamed_through_original_router() -> None:
    inner = _router_with_internal_names()
    outer = APIRouter()
    outer.routes.append(_IncludedRouterNode(original_router=inner))

    _public_route_names(outer.routes)

    names = sorted(route.name for route in _api_routes(inner))
    assert names == ["list_things", "make_thing"]
    for route in _api_routes(inner):
        assert route.summary == route.name.replace("_", " ").title()
        assert route.operation_id is not None
        assert "playbill" not in route.operation_id


def test_renaming_is_idempotent_for_a_router_shared_by_two_apps() -> None:
    router = _router_with_internal_names()
    _public_route_names(router.routes)
    first = [(r.name, r.summary, r.operation_id) for r in _api_routes(router)]

    _public_route_names(router.routes)

    assert [(r.name, r.summary, r.operation_id) for r in _api_routes(router)] == first


def test_nested_included_routers_serve_public_names_in_openapi() -> None:
    """Whatever shape the installed FastAPI uses, OpenAPI carries public names."""

    inner = _router_with_internal_names()
    outer = APIRouter()
    outer.include_router(inner, prefix="/v1")
    _public_route_names(outer.routes)
    app = FastAPI()
    app.include_router(outer)
    _public_route_names(app.routes)

    operations = [
        operation for path in app.openapi()["paths"].values() for operation in path.values()
    ]
    assert len(operations) == 2
    for operation in operations:
        assert "playbill" not in operation["operationId"].lower()
        assert "playbill" not in operation["summary"].lower()
