"""Re-export of :mod:`cruxible_client.contracts.kit_recipes` (the recipe renderer).

The renderer is a pure contract the daemon's ``kit build`` preflight shares with
the CLI's ``kit adopt``, so it lives under ``contracts``; this name stays for
callers that import it from the package root.
"""

from __future__ import annotations

from cruxible_client.contracts.kit_recipes import (
    KitRecipeError,
    KitRecipeParamError,
    parse_param_pairs,
    parse_param_value,
    preflight_values,
    recipe_param_table,
    recipe_placeholders,
    recipe_usage,
    render_recipe,
    render_with_dependencies,
    resolve_params,
    validate_rendered_recipe,
)

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
