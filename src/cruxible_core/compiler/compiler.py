"""The one Cruxible compiler coordinate this release installs, and its registries.

A new instance records this coordinate at genesis and every accepted generation
carries it. Accepted state under any other coordinate is refused
(``ReseedRequired``): an earlier build's coordinates are not replayed (such an
instance is moved aside and re-imported fresh), and a later one needs the
release that installs it. A future revision arrives through a governed
compiler upgrade along an explicit edge (``compiler/upgrades.py``).
"""

from __future__ import annotations

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.errors import ReseedRequired
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    current_extension_registry,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.projection_artifacts import ARTIFACT_KINDS
from cruxible_core.proposals.candidate_cards import CARD_RENDERER_DIGEST

#: The coordinate's exact preimage. Its digest is what every accepted generation
#: names, so the preimage never changes; a new revision is a new coordinate.
CURRENT_COMPILER = CompilerCoordinate(
    rule_digest="sha256:"
    + canonical_digest(
        "playbill-compiler-v1",
        {
            "implementation": "python-reference",
            "schema_version": 1,
            "projection_content": "claims-procedures-runtime-v1",
            "semantic_revision": 39,
            "candidate_card_renderer_digest": CARD_RENDERER_DIGEST,
            "revision_name": "claim-grammar-v1",
        },
    )
)
#: Display label only; the digest is the authority.
CURRENT_COMPILER_REVISION = "claim-grammar-v1"

#: The deterministic projection registry the current compiler runs.
PROJECTION_REGISTRY: ProjectionExtensionRegistry = current_extension_registry()


def current_compiler_coordinate() -> CompilerCoordinate:
    return CURRENT_COMPILER


def compiler_revision_label(compiler: CompilerCoordinate) -> str | None:
    """The display label of an installed compiler, or None for one this release lacks."""

    return CURRENT_COMPILER_REVISION if compiler == CURRENT_COMPILER else None


def require_current_compiler(compiler: CompilerCoordinate) -> None:
    """Refuse accepted state this release's compiler does not interpret."""

    if compiler != CURRENT_COMPILER:
        raise ReseedRequired(compiler_digest=compiler.rule_digest)


__all__ = [
    "ARTIFACT_KINDS",
    "CURRENT_COMPILER",
    "CURRENT_COMPILER_REVISION",
    "PROJECTION_REGISTRY",
    "compiler_revision_label",
    "current_compiler_coordinate",
    "require_current_compiler",
]
