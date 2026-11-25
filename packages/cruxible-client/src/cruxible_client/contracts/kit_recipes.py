"""Render a kit recipe: strict ``{{param}}`` substitution inside JSON string values.

A recipe file is one ``change_set`` authoring input in JSON whose string values
may name declared parameters as ``{{name}}``. Rendering is deliberately not a
template language: no expressions, filters, loops, conditionals or code. The
file is parsed as JSON first and substitution walks the parsed values, so a
parameter's value can never change the document's structure:

- a string that is exactly ``"{{name}}"`` takes the typed value (an
  ``integer`` or ``boolean`` parameter stays one);
- a placeholder inside a longer string is spelled as text (``true``/``false``
  for a boolean);
- every ``{{...}}`` must name a declared parameter, and none may appear in an
  object key; a stray ``{{`` or ``}}`` with no partner refuses;
- an optional parameter the file names must declare a default.

Each rendered recipe is validated on its own as one ``change_set`` authoring
input (:func:`validate_rendered_recipe`) before ``kit build`` composes it with
the recipes it depends on, and ``kit adopt`` applies the same validation.

``kit build`` renders each declared recipe with :func:`preflight_values` and
checks it authors; ``kit adopt`` renders it with :func:`resolve_params` over the
adopter's ``--param NAME=VALUE`` pairs.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.authoring.inputs import ChangeSetInput
from cruxible_client.contracts.kits import (
    KitRecipeParamSpec,
    KitRecipeParamValue,
    KitRecipeSpec,
    kit_recipe_order,
    kit_recipe_value_problem,
)
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_client.errors import ConfigError

#: Any ``{{...}}`` span: each must be exactly a declared parameter's name.
_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_EXACT_RE = re.compile(r"^\{\{([a-z][a-z0-9_]{0,63})\}\}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_INTEGER_RE = re.compile(r"^[+-]?[0-9]+$")
_TRUE = frozenset({"true", "yes", "1", "on"})
_FALSE = frozenset({"false", "no", "0", "off"})

#: Values ``kit build`` tries, in order, for a required string parameter that
#: declares neither a default nor an example; the first its pattern accepts wins.
_SYNTHETIC_STRINGS = ("example", "a", "x1", "example-1", "example.json", "1", "example_1")


class KitRecipeError(ConfigError):
    """A recipe that cannot render: an undeclared placeholder, or not JSON."""

    error_code = "cruxible.kit.recipe_invalid"

    def __init__(self, recipe: str, detail: str) -> None:
        self.recipe = recipe
        self.detail = detail
        super().__init__(f"{self.error_code}: recipe {recipe}: {detail}")


class KitRecipeParamError(ConfigError):
    """``--param`` values that do not fit the recipe: carries its parameter table."""

    error_code = "cruxible.kit.recipe_param_invalid"

    def __init__(self, kit_id: str, recipe: KitRecipeSpec, problems: list[str]) -> None:
        self.recipe = recipe
        self.problems = tuple(problems)
        lines = [f"{self.error_code}: {kit_id}:{recipe.name}: {'; '.join(problems)}"]
        lines.append("parameters:")
        lines.extend(f"  {line}" for line in recipe_param_table(recipe))
        lines.append(f"usage: {recipe_usage(kit_id, recipe)}")
        super().__init__("\n".join(lines))


def recipe_param_table(recipe: KitRecipeSpec) -> list[str]:
    """One terse line per parameter: name, type, required/default, constraint, description."""

    if not recipe.params:
        return ["(none)"]
    rows = []
    for name in sorted(recipe.params):
        spec = recipe.params[name]
        kind = spec.type if spec.type != "enum" else "enum(" + "|".join(spec.values) + ")"
        need = (
            "required"
            if spec.required
            else ("optional" if spec.default is None else f"default {_spell(spec.default)}")
        )
        extra = f", pattern {spec.pattern}" if spec.pattern else ""
        rows.append(f"{name}: {kind}, {need}{extra}: {spec.description}")
    return rows


def recipe_usage(kit_id: str, recipe: KitRecipeSpec) -> str:
    """``cruxible kit adopt KIT:NAME --param ...`` with each required parameter filled
    by a value that fits it (its example, else the synthetic value ``kit build``
    would use); every token shell-quoted, so the line pastes as is. A parameter no
    candidate fits shows ``<name>`` instead of a value its pattern would refuse."""

    target = kit_id if recipe.name == "default" else f"{kit_id}:{recipe.name}"
    argv = ["cruxible", "kit", "adopt", target]
    for name in sorted(recipe.params):
        spec = recipe.params[name]
        if spec.required:
            sample = spec.example if spec.example is not None else _fitting_sample(spec)
            shown = f"<{name}>" if sample is None else _spell(sample)
            argv.extend(["--param", f"{name}={shown}"])
    return " ".join(shlex.quote(token) for token in argv)


def _fitting_sample(spec: KitRecipeParamSpec) -> KitRecipeParamValue | None:
    """A deterministic value that fits ``spec``, or ``None`` when no candidate does.

    ``enum`` takes its first value that fits (an over-long value never does),
    ``integer`` 1, ``boolean`` false, and a string the first of the synthetic
    candidates its pattern accepts. Shared by ``kit build``'s preflight
    (:func:`preflight_values`) and usage lines.
    """

    if spec.type == "integer":
        return 1
    if spec.type == "boolean":
        return False
    candidates = spec.values if spec.type == "enum" else _SYNTHETIC_STRINGS
    return next(
        (
            candidate
            for candidate in candidates
            if kit_recipe_value_problem(spec, candidate) is None
        ),
        None,
    )


def _spell(value: KitRecipeParamValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_param_value(spec: KitRecipeParamSpec, raw: str) -> KitRecipeParamValue:
    """A ``--param`` string as the parameter's typed value; ``ValueError`` names why not."""

    value: KitRecipeParamValue
    if spec.type == "integer":
        if not _INTEGER_RE.fullmatch(raw.strip()):
            raise ValueError(f"{raw!r} is not an integer")
        value = int(raw.strip())
    elif spec.type == "boolean":
        folded = raw.strip().lower()
        if folded in _TRUE:
            value = True
        elif folded in _FALSE:
            value = False
        else:
            raise ValueError(f"{raw!r} is not a boolean (true or false)")
    else:
        value = raw
    problem = kit_recipe_value_problem(spec, value)
    if problem is not None:
        raise ValueError(f"{raw!r} {problem}")
    return value


def parse_param_pairs(pairs: tuple[str, ...] | list[str]) -> dict[str, str]:
    """``NAME=VALUE`` strings as a mapping; a malformed or repeated pair refuses."""

    found: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        name = name.strip()
        if not sep or not _NAME_RE.fullmatch(name):
            raise ValueError(f"--param takes NAME=VALUE, not {pair!r}")
        if name in found:
            raise ValueError(f"--param {name} is given twice")
        found[name] = value
    return found


def resolve_params(
    kit_id: str, recipe: KitRecipeSpec, given: Mapping[str, str]
) -> dict[str, KitRecipeParamValue]:
    """The recipe's parameter values: each given string parsed, defaults filled.

    Refuses :class:`KitRecipeParamError` (with the recipe's parameter table and a
    usage line) on an unknown name, a missing required parameter, or a value
    that does not fit its type, values or pattern; every problem is listed.
    """

    problems: list[str] = []
    values: dict[str, KitRecipeParamValue] = {}
    for name in sorted(given):
        if name not in recipe.params:
            problems.append(f"unknown parameter {name}")
            continue
        try:
            values[name] = parse_param_value(recipe.params[name], given[name])
        except ValueError as exc:
            problems.append(f"{name}: {exc}")
    for name in sorted(recipe.params):
        spec = recipe.params[name]
        if name in values or name in given:
            continue
        if spec.default is not None:
            values[name] = spec.default
        elif spec.required:
            problems.append(f"missing required parameter {name}")
    if problems:
        raise KitRecipeParamError(kit_id, recipe, problems)
    return values


def preflight_values(recipe: KitRecipeSpec) -> dict[str, KitRecipeParamValue]:
    """Values ``kit build`` renders the recipe with: each default, else its example,
    else a deterministic synthetic value that fits (refused when none does)."""

    values: dict[str, KitRecipeParamValue] = {}
    for name in sorted(recipe.params):
        spec = recipe.params[name]
        if spec.default is not None:
            values[name] = spec.default
        elif spec.example is not None:
            values[name] = spec.example
        else:
            fitting = _fitting_sample(spec)
            if fitting is None:
                raise KitRecipeError(
                    recipe.name,
                    f"parameter {name} declares no default or example and no synthetic "
                    f"value matches its pattern {spec.pattern}; declare an example",
                )
            values[name] = fitting
    return values


def recipe_placeholders(recipe_name: str, content: str | bytes) -> set[str]:
    """Every parameter name the recipe file uses; refuses malformed placeholders."""

    document = _load(recipe_name, content)
    found: set[str] = set()
    _walk(recipe_name, document, found, values=None)
    return found


def render_recipe(
    recipe: KitRecipeSpec,
    content: str | bytes,
    values: Mapping[str, KitRecipeParamValue],
) -> dict[str, Any]:
    """The recipe file with every placeholder substituted: one change set as a mapping.

    Refuses :class:`KitRecipeError` when the file is not a JSON object, names a
    placeholder the recipe does not declare, or one ``values`` does not set.
    """

    document = _load(recipe.name, content)
    used: set[str] = set()
    _walk(recipe.name, document, used, values=None)
    undeclared = sorted(used - set(recipe.params))
    if undeclared:
        raise KitRecipeError(
            recipe.name,
            f"names undeclared parameter(s) {', '.join(undeclared)}; declare them in the "
            "recipe's params",
        )
    no_default = sorted(
        name
        for name in used
        if not recipe.params[name].required and recipe.params[name].default is None
    )
    if no_default:
        raise KitRecipeError(
            recipe.name,
            f"names optional parameter(s) {', '.join(no_default)} that declare no default; "
            "declare a default or make them required",
        )
    unset = sorted(used - set(values))
    if unset:
        raise KitRecipeError(recipe.name, f"no value for parameter(s) {', '.join(unset)}")
    rendered = _walk(recipe.name, document, set(), values=values)
    assert isinstance(rendered, dict)
    return rendered


_CHANGE_SET: TypeAdapter[ChangeSetInput] = TypeAdapter(ChangeSetInput)


def validate_rendered_recipe(recipe_name: str, rendered: Mapping[str, Any]) -> ChangeSetInput:
    """One rendered recipe as a ``change_set`` authoring input, or a typed refusal.

    ``kit build`` validates each recipe this way on its own, before composing it
    with the recipes it depends on, and ``kit adopt`` validates what it submits
    the same way: a recipe whose ``members`` is null, a number, or empty refuses
    ``cruxible.kit.recipe_invalid`` instead of borrowing a dependency's members.
    """

    try:
        return _CHANGE_SET.validate_python(rendered)
    except ValidationError as exc:
        raise KitRecipeError(
            recipe_name,
            "renders an invalid change set: cruxible.authoring.input_invalid: "
            + validation_summary(exc),
        ) from exc
    except (TypeError, ValueError) as exc:
        raise KitRecipeError(recipe_name, f"renders an invalid change set: {exc}") from exc


def render_with_dependencies(
    recipes: tuple[KitRecipeSpec, ...],
    name: str,
    contents: Mapping[str, str | bytes],
    values: Mapping[str, KitRecipeParamValue] | None = None,
) -> dict[str, Any]:
    """Recipe ``name`` rendered after each recipe it depends on, as one change set.

    ``contents`` maps recipe paths to their bytes. Dependencies render with
    :func:`preflight_values`; ``name`` with ``values`` (default: its own
    preflight values). Each rendered recipe is validated on its own
    (:func:`validate_rendered_recipe`) before its members join the others. Used
    by ``kit build`` to check a recipe that builds on another
    (``security:schedule`` naming ``security.maintain``).
    """

    members: list[Any] = []
    rationale: Any = None
    combined: dict[str, Any] = {"kind": "change_set"}
    by_name = {item.name: item for item in recipes}
    for current in kit_recipe_order(recipes, name):
        spec = by_name[current]
        chosen = values if current == name and values is not None else preflight_values(spec)
        rendered = render_recipe(spec, contents[spec.path], chosen)
        validate_rendered_recipe(current, rendered)
        members.extend(rendered["members"])
        if current == name:
            rationale = rendered.get("rationale")
            combined = {key: value for key, value in rendered.items() if key != "members"}
    combined["members"] = members
    if rationale is None:
        combined.pop("rationale", None)
    return combined


def _load(recipe_name: str, content: str | bytes) -> Any:
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
    except UnicodeDecodeError as exc:
        raise KitRecipeError(recipe_name, "is not UTF-8 text") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KitRecipeError(recipe_name, f"is not JSON ({exc.msg} at line {exc.lineno})") from exc
    if not isinstance(document, dict):
        raise KitRecipeError(recipe_name, "is not a JSON object (one change_set authoring input)")
    return document


def _walk(
    recipe_name: str,
    node: Any,
    used: set[str],
    *,
    values: Mapping[str, KitRecipeParamValue] | None,
) -> Any:
    """Collect placeholders into ``used``; with ``values``, return the substituted copy."""

    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if _PLACEHOLDER_RE.search(key) is not None or "{{" in key or "}}" in key:
                raise KitRecipeError(
                    recipe_name, f"object key {key!r} holds a placeholder; only values may"
                )
            out[key] = _walk(recipe_name, value, used, values=values)
        return out
    if isinstance(node, list):
        return [_walk(recipe_name, item, used, values=values) for item in node]
    if not isinstance(node, str):
        return node
    stray = _PLACEHOLDER_RE.sub("", node)
    if "{{" in stray or "}}" in stray:
        raise KitRecipeError(
            recipe_name,
            f"{node!r} holds an unmatched '{{{{' or '}}}}'; write each placeholder as "
            "{{name}}, and no other double braces",
        )
    names = []
    for match in _PLACEHOLDER_RE.finditer(node):
        name = match.group(1)
        if not _NAME_RE.fullmatch(name):
            raise KitRecipeError(
                recipe_name,
                f"{match.group(0)!r} is not a placeholder: write {{{{name}}}} with a "
                "lowercase parameter name and nothing else",
            )
        names.append(name)
    used.update(names)
    if values is None or not names:
        return node
    exact = _EXACT_RE.fullmatch(node)
    if exact is not None:
        return values[exact.group(1)]
    return _PLACEHOLDER_RE.sub(lambda match: _spell(values[match.group(1)]), node)


__all__ = [
    "KitRecipeError",
    "KitRecipeParamError",
    "parse_param_pairs",
    "parse_param_value",
    "preflight_values",
    "recipe_param_table",
    "recipe_placeholders",
    "recipe_usage",
    "render_recipe",
    "render_with_dependencies",
    "resolve_params",
    "validate_rendered_recipe",
]
