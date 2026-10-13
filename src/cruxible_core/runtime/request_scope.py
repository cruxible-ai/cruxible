"""One request, one bind per accepted coordinate (see ``PlaybillInstance.projection_scope``)."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Concatenate, ParamSpec, TypeVar

_P = ParamSpec("_P")
_I = TypeVar("_I")
_R = TypeVar("_R")


def projection_request(
    service: Callable[Concatenate[_I, _P], _R],
) -> Callable[Concatenate[_I, _P], _R]:
    """Run a service call inside its instance's projection scope.

    Every lookup the call makes at one accepted coordinate then reuses one bound
    projection on this thread instead of re-binding and re-verifying it. A
    stand-in instance without a scope runs the service unchanged.
    """

    @functools.wraps(service)
    def scoped(instance: _I, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        scope = getattr(instance, "projection_scope", None)
        if scope is None:
            return service(instance, *args, **kwargs)
        with scope():
            return service(instance, *args, **kwargs)

    return scoped


__all__ = ["projection_request"]
