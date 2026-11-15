"""How far a workspace's vocabulary file is behind accepted state.

The file names the generation it was exported at (``based_on.git_oid``). The
definitions accepted since then are the ClaimType and SubjectKind paths the
change records in ``(that generation, head]`` touched: one point lookup for the
generation and one ranged read of those records (the gate floor delivery uses
for the world stub), so the cost follows the changes since the export, never the
instance. ``next`` turns a file that is behind into a row; ``orient`` shows it
as status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cruxible_client.contracts.subject_kinds import SUBJECT_KIND_PREFIX

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.runtime.instance import PlaybillInstance

CLAIM_TYPE_PREFIX = "claim-types/"
#: How many changed definitions a row names; the count covers them all.
VOCABULARY_DRIFT_SAMPLE = 20


@dataclass(frozen=True)
class VocabularyFileDrift:
    """The file's baseline against the accepted coordinate it is read at."""

    state: Literal["current", "behind", "invalid"]
    at: str | None
    behind: int = 0
    #: The first changed definitions, as ``ClaimType:<predicate>`` / ``SubjectKind:<kind>``.
    changed: tuple[str, ...] = ()
    #: Why an invalid baseline is invalid: ``unreadable`` or ``unknown``.
    reason: Literal["unreadable", "unknown"] | None = None


def _identity(path: str) -> str:
    if path.startswith(SUBJECT_KIND_PREFIX):
        return "SubjectKind:" + path.removeprefix(SUBJECT_KIND_PREFIX).removesuffix(".json")
    namespace, _, name = path.removeprefix(CLAIM_TYPE_PREFIX).removesuffix(".json").rpartition("/")
    return f"ClaimType:{namespace}.{name}"


def vocabulary_file_drift(
    instance: PlaybillInstance,
    *,
    baseline: str | None,
    coordinate: AcceptedCoordinate,
) -> VocabularyFileDrift:
    """Compare the file's baseline Git OID with ``coordinate``.

    ``baseline`` None is a file whose header could not be read. An OID this
    history does not hold (another instance's, a rewound one, a typo) is an
    invalid baseline, never an error.
    """

    if baseline is None:
        return VocabularyFileDrift(state="invalid", at=None, reason="unreadable")
    with instance.accepted_history_reader(at=coordinate) as history:
        located = history.generation_for_oid(baseline)
        if located is None:
            return VocabularyFileDrift(state="invalid", at=baseline, reason="unknown")
        count, paths = history.touched_paths_under(
            (CLAIM_TYPE_PREFIX, SUBJECT_KIND_PREFIX),
            located.sequence,
            history.sequence,
            sample=VOCABULARY_DRIFT_SAMPLE,
        )
    if count == 0:
        return VocabularyFileDrift(state="current", at=baseline)
    return VocabularyFileDrift(
        state="behind",
        at=baseline,
        behind=count,
        changed=tuple(_identity(path) for path in paths),
    )


__all__ = ["VOCABULARY_DRIFT_SAMPLE", "VocabularyFileDrift", "vocabulary_file_drift"]
