"""Guardrail: every Click command in ``cli/commands`` is reachable from CLI_COMMANDS.

``CLI_COMMANDS`` in ``cli/main.py`` is the import-free command inventory that
top-level help, shell completion, the reference-docs guardrail, and the
mutating-command inventory all read. It is hand-maintained, and a command that
is missing from it still WORKS: ``LazyGroup._load()`` imports the real group,
which already carries every command its decorators registered. A forgotten
entry therefore produces a live, invisible surface rather than a broken one,
and every guardrail that walks the lazy map is blind to it by construction.
Three commands have shipped that way. This guardrail re-derives the real
registrations from the source tree and fails naming any command the map does
not know about.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

import click

from cruxible_core.cli import commands as commands_package
from cruxible_core.cli.main import CLI_COMMANDS, LazyCommandSpec


def _command_modules() -> list[ModuleType]:
    """Import every module in the commands package, discovered from the tree."""
    modules = [
        importlib.import_module(f"{commands_package.__name__}.{info.name}")
        for info in pkgutil.iter_modules(commands_package.__path__)
    ]
    # DP-0B intentionally reduces this package to the four public groups plus
    # their shared formatting/dispatch helper.
    assert {module.__name__.rsplit(".", 1)[-1] for module in modules} == {
        "_common",
        "context",
        "credentials",
        "playbill",
        "server",
    }
    return modules


def _defined_click_objects() -> dict[int, tuple[click.Command, str]]:
    """Map ``id(command)`` -> (command, ``module:attr``) for the whole package.

    Keyed by identity because a command defined in one module and imported by
    another is one registration, not two.
    """
    found: dict[int, tuple[click.Command, str]] = {}
    for module in _command_modules():
        for attr, value in vars(module).items():
            if isinstance(value, click.Command):
                found.setdefault(id(value), (value, f"{module.__name__}:{attr}"))
    return found


def _resolve(spec: LazyCommandSpec) -> click.Command:
    module = importlib.import_module(str(spec.module))
    target = getattr(module, str(spec.attr), None)
    assert isinstance(target, click.Command), (
        f"CLI_COMMANDS points at {spec.module}:{spec.attr}, which is not a Click command"
    )
    return target


def _walk_lazy_map(
    specs: dict[str, LazyCommandSpec],
    prefix: tuple[str, ...] = (),
) -> tuple[dict[int, tuple[str, frozenset[str]]], dict[int, str]]:
    """Return (group claims, leaf claims) keyed by the target object's identity.

    A group claim carries the group's CLI path and the child names the map
    declares for it; a leaf claim carries the command's CLI path.
    """
    groups: dict[int, tuple[str, frozenset[str]]] = {}
    leaves: dict[int, str] = {}
    for name, spec in specs.items():
        path = prefix + (name,)
        label = "cruxible " + " ".join(path)
        if spec.commands is None:
            leaves[id(_resolve(spec))] = label
            continue
        if spec.module is not None and spec.attr is not None:
            groups[id(_resolve(spec))] = (label, frozenset(spec.commands))
        child_groups, child_leaves = _walk_lazy_map(spec.commands, path)
        groups.update(child_groups)
        leaves.update(child_leaves)
    return groups, leaves


def test_every_command_registered_on_a_group_is_in_the_lazy_cli_map() -> None:
    """A decorator-registered subcommand the map omits is a hidden surface."""
    group_claims, _ = _walk_lazy_map(CLI_COMMANDS)
    defined = _defined_click_objects()
    groups = [(obj, origin) for obj, origin in defined.values() if isinstance(obj, click.Group)]
    assert len(groups) == 21, f"expected 21 Playbill/host groups, found {len(groups)}"

    problems: list[str] = []
    for group, origin in groups:
        claim = group_claims.get(id(group))
        if claim is None:
            problems.append(f"{origin}: group is not registered in CLI_COMMANDS")
            continue
        label, declared = claim
        # The reverse direction is covered by
        # test_every_command_defined_in_the_commands_package_is_reachable. This
        # remains a subset assertion because invoking an earlier lazy group can
        # back-fill only the already-declared children onto its real object.
        for name in sorted(set(group.commands) - declared):
            problems.append(f"{label} {name}: registered on {origin} but missing from CLI_COMMANDS")
    assert problems == [], (
        "Click registrations the lazy CLI map does not know about:\n" + "\n".join(problems)
    )


def test_every_command_defined_in_the_commands_package_is_reachable() -> None:
    """A command defined but never registered is dead or invisible, never fine."""
    group_claims, leaf_claims = _walk_lazy_map(CLI_COMMANDS)
    assert len(leaf_claims) == 91, (
        f"expected 91 Playbill/host leaf commands, found {len(leaf_claims)}"
    )

    reachable = set(leaf_claims)
    for group, _origin in _defined_click_objects().values():
        if not isinstance(group, click.Group):
            continue
        claim = group_claims.get(id(group))
        if claim is None:
            continue
        _label, declared = claim
        reachable.update(id(group.commands[name]) for name in declared if name in group.commands)

    problems = sorted(
        f"{origin}: defined but not reachable from CLI_COMMANDS"
        for key, (command, origin) in _defined_click_objects().items()
        if not isinstance(command, click.Group) and key not in reachable
    )
    assert problems == [], "Click commands no CLI path reaches:\n" + "\n".join(problems)


def test_every_lazy_map_entry_points_at_the_kind_of_object_it_claims() -> None:
    """Group entries must resolve to groups; command entries to commands."""
    group_claims, leaf_claims = _walk_lazy_map(CLI_COMMANDS)
    defined = _defined_click_objects()

    mistyped = sorted(
        label
        for key, (label, _declared) in group_claims.items()
        if key in defined and not isinstance(defined[key][0], click.Group)
    )
    assert mistyped == [], f"CLI_COMMANDS group entries resolving to plain commands: {mistyped}"
    unknown = sorted(
        label
        for key, label in ({**leaf_claims, **{k: v[0] for k, v in group_claims.items()}}).items()
        if key not in defined
    )
    assert unknown == [], f"CLI_COMMANDS entries resolving outside the commands package: {unknown}"
