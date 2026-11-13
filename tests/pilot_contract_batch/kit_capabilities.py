"""The security kit's slots, converted to capability slots for the pilot's copy of the kit.

The kit checkout (read only) still authors every slot as ``{in, out, effect}``.
Since the slots slice a Procedure's slot names one capability, so the pilot
converts the kit's nine slots (six Procedures, seven distinct signatures) while
it builds its world, and authors the seven capabilities right after the kit's
vocabulary (every Contract they pin is accepted by then). The kit migration
slice reuses this table when it re-authors the kit itself.

Each capability pins the Contracts the kit and its providers use today
(``osv.match.request@1``, not the wave 0 example's ``security.osv.match.request@1``),
because a Line still binds a slot by the exact signature its capability derives
until the binding slice lands. Names, effects and descriptions follow the wave 0
examples (``examples/base``, ``examples/security``) where one exists; the other
two are written in the same style.

| Kit slot (request Contract, effect) | Capability | Declared in the kit at |
|---|---|---|
| workspace.file.request, pure | workspace.file | inventory/findings ``_SLOTS["read"]`` |
| sbom.components.request, pure | security.sbom.components | inventory ``_SLOTS["derive"]`` |
| sarif.findings.request, pure | security.sarif.findings | findings ``_SLOTS["derive"]`` |
| osv.query.request, external_read | security.osv.query | osv.osv_spec ``query`` |
| osv.match.request, pure | security.osv.match | osv.osv_spec ``match`` |
| web.fetch.request, external_read | web.fetch | definitions._spec ``fetch`` |
| web.fetch.pages.request, external_read | web.fetch.pages | definitions._spec ``fetch`` |

Each result Contract is the request's name with ``.result``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

EXAMPLES = Path(__file__).parent / "examples"


def _example(kit: str, name: str) -> tuple[str, str]:
    """``(effect, description)`` of a wave 0 example capability."""

    document = json.loads((EXAMPLES / kit / "capabilities" / name / "v1.json").read_text())
    return document["effect"], document["description"]


def _table() -> dict[str, tuple[str, str, str, str]]:
    """Request Contract name -> ``(capability, result Contract, effect, description)``."""

    def from_example(
        request: str, result: str, kit: str, capability: str
    ) -> tuple[str, tuple[str, str, str, str]]:
        effect, description = _example(kit, capability)
        return request, (capability, result, effect, description)

    return dict(
        [
            from_example(
                "workspace.file.request", "workspace.file.result", "base", "workspace.file"
            ),
            from_example("web.fetch.request", "web.fetch.result", "base", "web.fetch"),
            from_example(
                "sbom.components.request",
                "sbom.components.result",
                "security",
                "security.sbom.components",
            ),
            from_example("osv.query.request", "osv.query.result", "security", "security.osv.query"),
            from_example("osv.match.request", "osv.match.result", "security", "security.osv.match"),
            (
                "web.fetch.pages.request",
                (
                    "web.fetch.pages",
                    "web.fetch.pages.result",
                    "external_read",
                    "Fetches a bounded series of pages from one origin, one HTTP GET each, and "
                    "returns each page's exact body as one Capture, in order. A page that fails "
                    "is listed with why, so the series is known to be incomplete.",
                ),
            ),
            (
                "sarif.findings.request",
                (
                    "security.sarif.findings",
                    "sarif.findings.result",
                    "pure",
                    "Reads one SARIF log and returns one document of its findings, each with its "
                    "rule, location and level. A result the log does not place is counted but "
                    "never listed, so the count marks the listing incomplete.",
                ),
            ),
        ]
    )


#: Request Contract name -> ``(capability, result Contract, effect, description)``.
KIT_CAPABILITIES = _table()


def capability_slot(slot: Mapping[str, Any]) -> dict[str, Any]:
    """One kit slot ``{in, out, effect[, may_truncate]}`` as the capability slot it becomes."""

    if "capability" in slot:
        return dict(slot)
    request = str(slot["in"]).partition("@")[0]
    capability, result, effect, _description = KIT_CAPABILITIES[request]
    if str(slot["out"]).partition("@")[0] != result or slot["effect"] != effect:
        raise ValueError(f"kit slot {dict(slot)} does not match capability {capability}")
    converted: dict[str, Any] = {"capability": f"{capability}@1"}
    if slot.get("may_truncate"):
        converted["may_truncate"] = True
    return converted


def capability_slots(slots: Mapping[str, Any]) -> dict[str, Any]:
    return {name: capability_slot(slot) for name, slot in slots.items()}


def _converting(spec_function: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        spec = spec_function(*args, **kwargs)
        if isinstance(spec, dict) and "slots" in spec:
            spec = {**spec, "slots": capability_slots(spec["slots"])}
        return spec

    return wrapper


def capability_members(tree: Mapping[str, bytes]) -> dict[str, bytes]:
    """The capabilities' tree files, pinning the Contracts accepted in ``tree``.

    A capability the world already holds is left as it is held: once the base
    kit is installed at creation, ``workspace.file``, ``web.fetch`` and
    ``web.fetch.pages`` come with the bundled bytes and are never authored here.
    A capability whose Contracts the world does not hold is left out: the start
    world installs no SARIF provider, so ``security.sarif.findings`` waits for a
    world that does.
    """

    from cruxible_client.contracts.capabilities import (
        Capability,
        capability_path,
        render_capability,
    )
    from cruxible_client.contracts.shapes import contract_digest, contract_path, parse_contract

    def pin(name: str) -> dict[str, Any]:
        path = contract_path(name, 1)
        return {
            "name": name,
            "version": 1,
            "digest": contract_digest(parse_contract(tree[path], path=path)).tagged,
        }

    files: dict[str, bytes] = {}
    for request, (name, result, effect, description) in sorted(KIT_CAPABILITIES.items()):
        if capability_path(name, 1) in tree:
            continue
        if not {contract_path(request, 1), contract_path(result, 1)} <= set(tree):
            continue
        capability = Capability.model_validate(
            {
                "name": name,
                "version": 1,
                "description": description,
                "effect": effect,
                "in": pin(request),
                "out": pin(result),
            }
        )
        files[capability_path(name, 1)] = render_capability(capability)
    return files


def accept_kit_capabilities(instance: Any) -> None:
    from tests.core_support._identity_support import accept_tree

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    members = capability_members(tree)
    if members:
        accept_tree(instance, members, name="kit-capabilities")


@contextmanager
def converted_kit(kit: SimpleNamespace) -> Iterator[None]:
    """The kit's slot sources converted to capability slots while the block runs.

    ``inventory._SLOTS`` and ``findings._SLOTS`` are module-level slot tables the
    spec functions read when called; ``definitions._spec`` and ``osv.osv_spec``
    build their slots inline and are wrapped.
    """

    import sys

    findings: ModuleType | None = sys.modules.get("findings")
    if findings is None:
        import findings  # the kit gate's module, on PYTHONPATH

    with ExitStack() as stack:
        for module in (kit.inventory, findings):
            stack.enter_context(_replaced(module, "_SLOTS", capability_slots(module._SLOTS)))
        stack.enter_context(_replaced(kit.definitions, "_spec", _converting(kit.definitions._spec)))
        stack.enter_context(_replaced(kit.osv, "osv_spec", _converting(kit.osv.osv_spec)))
        yield


@contextmanager
def _replaced(target: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


__all__ = [
    "KIT_CAPABILITIES",
    "accept_kit_capabilities",
    "capability_members",
    "capability_slot",
    "capability_slots",
    "converted_kit",
]
