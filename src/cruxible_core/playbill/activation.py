"""Parent-bound main settlement and ordered post-CAS publication."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.types import GenesisCoordinate
from cruxible_core.playbill.assembler import ProjectionAssembler, ProjectionCrashHook
from cruxible_core.playbill.cas import BodyProjectionProtocol
from cruxible_core.playbill.checkpoints import (
    DEFAULT_CHECKPOINT_INTERVAL,
    checkpoint_body,
    write_checkpoint,
)
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.projection import (
    AcceptedCoordinate,
    AcceptedProjectionCoordinate,
    AssemblerResult,
)
from cruxible_core.playbill.serving import (
    publish_serving_manifest,
    remove_exact_projection_build,
)
from cruxible_core.playbill.settlement import (
    VerifiedGenerationBundle,
    render_generation_descriptor,
)
from cruxible_core.playbill.witness import WitnessRecord, WitnessSink
from cruxible_core.storage.playbill_projection import bind_projection

MAIN_CAS: Final = "main.cas"
GENERATION_NOTE: Final = "generation.note"
SERVING_PUBLICATION: Final = "serving.publication"
WITNESS_PUBLICATION: Final = "witness.publication"
ORPHAN_CLEANUP: Final = "orphan.cleanup"

ACTIVATION_CRASH_POINTS: Final = (
    MAIN_CAS,
    GENERATION_NOTE,
    SERVING_PUBLICATION,
    WITNESS_PUBLICATION,
    ORPHAN_CLEANUP,
)


class ActivationCrashHook(Protocol):
    def __call__(self, checkpoint: str) -> None: ...


def _checkpoint(point: str, phase: str, hook: ActivationCrashHook | None) -> None:
    if point not in ACTIVATION_CRASH_POINTS or phase not in {"before", "after"}:
        raise SettlementIntegrityError("unknown activation crash checkpoint")
    if hook is not None:
        hook(f"{phase}:{point}")


@dataclass(frozen=True)
class ActivationResult:
    status: str
    accepted: AcceptedProjectionCoordinate | None
    projection: AssemblerResult | None


class ActivationPublisher:
    """The only path that may turn a verified candidate into accepted state."""

    def __init__(
        self,
        ledger: GitLedger,
        *,
        publication_directory: Path,
        bodies: BodyProjectionProtocol,
        witness: WitnessSink | None = None,
        accepted_coordinates_by_sequence: Mapping[int, AcceptedCoordinate] | None = None,
        checkpoint_directory: Path | None = None,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
        genesis: GenesisCoordinate | None = None,
    ) -> None:
        if checkpoint_interval < 1:
            raise SettlementIntegrityError("checkpoint interval must be at least one generation")
        self.ledger = ledger
        self.publication_directory = publication_directory.resolve(strict=True)
        self.bodies = bodies
        self.witness = witness
        self.accepted_coordinates_by_sequence = dict(accepted_coordinates_by_sequence or {})
        self.checkpoint_directory = checkpoint_directory
        self.checkpoint_interval = checkpoint_interval
        self.genesis = genesis

    def prebuild(
        self,
        bundle: VerifiedGenerationBundle,
        *,
        base: AcceptedProjectionCoordinate,
        crash_hook: ProjectionCrashHook | None = None,
    ) -> AssemblerResult:
        coordinate = bundle.projection_coordinate(base=base)
        accepted_coordinates = dict(self.accepted_coordinates_by_sequence)
        accepted_coordinates.setdefault(
            bundle.record.sequence - 1,
            AcceptedCoordinate(
                git_oid=base.git_oid,
                semantic_root=base.semantic_root,
                generation_root=base.generation_root,
                compiler_digest=base.compiler.rule_digest,
            ),
        )
        accepted_coordinates[bundle.record.sequence] = AcceptedCoordinate(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler_digest=coordinate.compiler.rule_digest,
        )
        assembler = ProjectionAssembler(
            self.ledger,
            accepted=coordinate,
            publication_directory=self.publication_directory,
            bodies=self.bodies,
            accepted_coordinates_by_sequence=accepted_coordinates,
        )
        stage = self.publication_directory / f".stage-{secrets.token_hex(12)}"
        return assembler.assemble(
            assembler.request(output_staging_directory=stage),
            crash_hook=crash_hook,
        )

    def activate(
        self,
        bundle: VerifiedGenerationBundle,
        projection: AssemblerResult,
        *,
        base: AcceptedProjectionCoordinate,
        crash_hook: ActivationCrashHook | None = None,
    ) -> ActivationResult:
        with self.ledger.activation_lock():
            return self._activate_locked(
                bundle,
                projection,
                base=base,
                crash_hook=crash_hook,
            )

    def _activate_locked(
        self,
        bundle: VerifiedGenerationBundle,
        projection: AssemblerResult,
        *,
        base: AcceptedProjectionCoordinate,
        crash_hook: ActivationCrashHook | None = None,
    ) -> ActivationResult:
        candidate_coordinate = bundle.projection_coordinate(base=base)
        expected_projection = {
            "instance_id": candidate_coordinate.instance_id,
            "git_object_format": candidate_coordinate.git_object_format,
            "git_oid": candidate_coordinate.git_oid,
            "semantic_root": candidate_coordinate.semantic_root,
            "generation_root": candidate_coordinate.generation_root,
            "compiler_digest": candidate_coordinate.compiler.rule_digest,
            "schema_version": candidate_coordinate.compiler.schema_version,
        }
        actual_projection = {
            "instance_id": projection.manifest.instance_id,
            "git_object_format": projection.manifest.git_object_format,
            "git_oid": projection.manifest.git_oid,
            "semantic_root": projection.manifest.semantic_root,
            "generation_root": projection.manifest.generation_root,
            "compiler_digest": projection.manifest.compiler_digest,
            "schema_version": projection.manifest.schema_version,
        }
        if actual_projection != expected_projection:
            raise SettlementIntegrityError("prebuilt projection differs from generation bundle")
        verification_coordinate = AcceptedProjectionCoordinate(
            instance_id=base.instance_id,
            repository_path=base.repository_path,
            git_object_format=base.git_object_format,
            git_oid=bundle.oid,
            semantic_root=bundle.semantic_root.tagged,
            generation_root=bundle.generation_root.tagged,
            compiler=base.compiler,
        )
        with bind_projection(
            Path(projection.manifest_path),
            expected=verification_coordinate,
        ):
            pass

        _checkpoint(MAIN_CAS, "before", crash_hook)
        won = self.ledger.compare_and_set_main(
            bundle.oid,
            expected_oid=bundle.settlement.base_oid,
        )
        _checkpoint(MAIN_CAS, "after", crash_hook)
        if not won:
            _checkpoint(ORPHAN_CLEANUP, "before", crash_hook)
            remove_exact_projection_build(projection, expected=projection.manifest)
            self.ledger.collect_unreachable_generation(bundle.oid)
            _checkpoint(ORPHAN_CLEANUP, "after", crash_hook)
            return ActivationResult(status="lost_cas", accepted=None, projection=None)

        accepted = verification_coordinate
        _checkpoint(GENERATION_NOTE, "before", crash_hook)
        self.ledger.write_generation_note(
            bundle.oid,
            render_generation_descriptor(bundle.descriptor),
        )
        _checkpoint(GENERATION_NOTE, "after", crash_hook)

        publish_serving_manifest(
            self.publication_directory,
            projection,
            crash_hook=crash_hook,
        )

        _checkpoint(WITNESS_PUBLICATION, "before", crash_hook)
        if self.witness is not None:
            self.witness.publish(
                WitnessRecord(
                    instance_id=base.instance_id,
                    object_format=base.git_object_format,
                    head_oid=bundle.oid,
                    semantic_root=bundle.semantic_root.tagged,
                    generation_root=bundle.generation_root.tagged,
                    sequence=bundle.record.sequence,
                )
            )
        _checkpoint(WITNESS_PUBLICATION, "after", crash_hook)
        self._advance_replay_checkpoint(bundle, base=base)
        return ActivationResult(status="accepted", accepted=accepted, projection=projection)

    def _advance_replay_checkpoint(
        self,
        bundle: VerifiedGenerationBundle,
        *,
        base: AcceptedProjectionCoordinate,
    ) -> None:
        """Summarize this coordinate every `checkpoint_interval` accepted generations.

        Writing on a stride rather than on every acceptance keeps the cost of
        rebuilding a full member manifest off the hot acceptance path while
        bounding the suffix a reopen has to replay. The write is the last thing
        activation does and is never load bearing: a torn, stale, or absent
        checkpoint only ever costs replay time.
        """

        if self.checkpoint_directory is None or self.genesis is None:
            return
        sequence = bundle.record.sequence
        if sequence % self.checkpoint_interval != 0:
            return
        parent = self.accepted_coordinates_by_sequence.get(sequence - 1)
        if parent is None:
            # Without the predecessor's replayed coordinate there is nothing to
            # chain this summary to, so no checkpoint is written and the next
            # reopen simply replays further.
            return
        body = checkpoint_body(
            instance_id=base.instance_id,
            object_format=base.git_object_format,
            compiler=base.compiler,
            genesis=self.genesis,
            sequence=sequence,
            git_oid=bundle.oid,
            semantic_root=bundle.semantic_root.tagged,
            generation_root=bundle.generation_root.tagged,
            parent_generation_root=parent.generation_root,
            tree=bundle.tree,
        )
        write_checkpoint(self.checkpoint_directory, body)


__all__ = [
    "ACTIVATION_CRASH_POINTS",
    "ActivationPublisher",
    "ActivationResult",
]
