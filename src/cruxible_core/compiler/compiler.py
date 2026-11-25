"""The Cruxible compiler coordinates this release installs, and its registries.

A new instance records the current coordinate at genesis and every accepted
generation carries one. This release installs two: the current revision, and
the revision 0.5.x released (73, ``retiring-query-traversal``), kept only as the
source of the governed upgrade edge to the current one (``compiler/upgrades.py``).
An instance whose head is still on revision 73 is served under revision 73's
laws: what revision 74 newly admits (``based-on`` Claim pins, an observation
settle grant carried across a pure provider build move) refuses there until the
upgrade is activated. Accepted state under any other coordinate is refused
(``ReseedRequired``): an earlier build's coordinates are not replayed, and a
later one needs the release that installs it.
"""

from __future__ import annotations

from types import MappingProxyType

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.errors import ReseedRequired
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    current_extension_registry,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.projection_artifacts import ARTIFACT_KINDS
from cruxible_core.proposals.candidate_cards import CARD_RENDERER_DIGEST


def _coordinate(semantic_revision: int, revision_name: str) -> CompilerCoordinate:
    """One coordinate from its exact preimage; the preimage of a shipped one never changes."""

    return CompilerCoordinate(
        rule_digest="sha256:"
        + canonical_digest(
            "playbill-compiler-v1",
            {
                "implementation": "python-reference",
                "schema_version": 1,
                "projection_content": "claims-procedures-runtime-v1",
                "semantic_revision": semantic_revision,
                "candidate_card_renderer_digest": CARD_RENDERER_DIGEST,
                "revision_name": revision_name,
            },
        )
    )


#: Revision 73, the coordinate 0.5.0 and 0.5.1 record. Installed only as the
#: upgrade source: its instances are served, and move forward by upgrade.
COMPILER_REVISION_73 = _coordinate(73, "retiring-query-traversal")
COMPILER_REVISION_73_NAME = "retiring-query-traversal"

#: The coordinate this release writes at genesis. Revision 74 admits a
#: ``based-on`` Claim pin (a judgment's pin to the observation it rests on) and
#: keeps an observation-only settle grant across a pure, non-widening provider
#: build move.
CURRENT_COMPILER = _coordinate(74, "based-on-observation-carry")
#: Display label only; the digest is the authority.
CURRENT_COMPILER_REVISION = "based-on-observation-carry"

#: Every coordinate this release serves, with its display label.
INSTALLED_COMPILERS = MappingProxyType(
    {
        COMPILER_REVISION_73: COMPILER_REVISION_73_NAME,
        CURRENT_COMPILER: CURRENT_COMPILER_REVISION,
    }
)

#: The deterministic projection registry the installed compilers run.
PROJECTION_REGISTRY: ProjectionExtensionRegistry = current_extension_registry()


def current_compiler_coordinate() -> CompilerCoordinate:
    return CURRENT_COMPILER


def compiler_revision_label(compiler: CompilerCoordinate) -> str | None:
    """The display label of an installed compiler, or None for one this release lacks."""

    return INSTALLED_COMPILERS.get(compiler)


def require_installed_compiler(compiler: CompilerCoordinate) -> None:
    """Refuse accepted state no compiler of this release interprets."""

    if compiler not in INSTALLED_COMPILERS:
        raise ReseedRequired(compiler_digest=compiler.rule_digest)


def admits_revision_74(compiler: CompilerCoordinate) -> bool:
    """Whether laws new in revision 74 judge under ``compiler``.

    A head still on revision 73 is judged exactly as a 0.5.x daemon judges it,
    so its generations replay on either release.
    """

    return compiler != COMPILER_REVISION_73


__all__ = [
    "ARTIFACT_KINDS",
    "COMPILER_REVISION_73",
    "COMPILER_REVISION_73_NAME",
    "CURRENT_COMPILER",
    "CURRENT_COMPILER_REVISION",
    "INSTALLED_COMPILERS",
    "PROJECTION_REGISTRY",
    "admits_revision_74",
    "compiler_revision_label",
    "current_compiler_coordinate",
    "require_installed_compiler",
]
