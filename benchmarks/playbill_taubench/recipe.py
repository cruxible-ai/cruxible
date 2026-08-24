"""The executable §11.8 TauBench arm recipe.

Everything an integrator needs to stand up the four arms of §11.8 lives in this
directory: this module, the bundle beside it, and the README. Nothing here is a
description of how it would be done. Each function below is the step, and
`tests/test_cli/test_playbill_taubench_arms.py` runs all of them end to end at
miniature scale.

The four arms, and the one thing that separates the last two
------------------------------------------------------------
§11.8 names them:

1. a standard agent over ordinary files and tools;
2. the same, plus a genuine notebook/scratchpad control;
3. the Playbill reference floor, without transparent tool decoration;
4. the same, with transparent Read/Grep/Edit/Write coverage delivery.

"Arms 3 and 4 share the same model, harness loop, task corpus, accepted ledger,
Playbill state, and tool implementations; only the coverage-delivery adapter
changes." That sentence is a *testable* claim and this module makes it one:
:func:`build_arm` constructs arms 3 and 4 identically -- same workspace bytes,
same configuration, same middleware object -- and the two `ArmSetupV1` records
differ in exactly one field, the boolean :attr:`ArmSetupV1.deliver_coverage`.
:func:`run_turn` reads that boolean to decide whether to call `after_tool`, and
that call is the entire difference between the arms. There is no second code
path, no alternate tool implementation, and no branch anywhere else.

The file floor is the pointer-model v2 export
---------------------------------------------
Arms 3 and 4 get the same deterministic floor-v2 cards, accepted Documents,
and coverage boundary. The unshipped native projection is deliberately absent:
the pointer-model surface is ordinary source material plus a compact, read-only
floor, not a second editable knowledge tree.

Determinism
-----------
No wall clock reaches any content this module produces. The render read time is
a parameter with a fixed default, the seed bundle is committed bytes, and the
run manifest pins the resolver's index/overlay/manifest digests, the accepted
generation root, the rule set, and the plan digest -- so two runs of the same
bundle at the same generation produce the same manifest apart from the
identifiers the daemon allocates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for _candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "src"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from click.testing import CliRunner  # noqa: E402

from cruxible_client.authoring.seed import (  # noqa: E402
    SEED_BODY_DIRECTORY,
    plan_seed_bundle,
    seed_plan_digest,
)
from cruxible_core.cli.commands import _common  # noqa: E402
from cruxible_core.cli.context import load_cli_context  # noqa: E402
from cruxible_core.cli.main import cli  # noqa: E402
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1  # noqa: E402
from cruxible_core.playbill.coverage.contracts import CoverageResultV2  # noqa: E402
from cruxible_core.playbill.coverage.middleware import (  # noqa: E402
    CONFIG_RELATIVE_PATH,
    CoverageMiddlewareV1,
    CoverageWorkspaceConfigV2,
    FloorGenerationPairV1,
    HarnessLineRangeV1,
    HarnessToolEventV1,
    ResolveCoverage,
    ResolveFloorGenerations,
    coverage_middleware,
    grep_event,
)
from cruxible_core.playbill.projection import AcceptedCoordinate  # noqa: E402

BUNDLE_DIR = Path(__file__).resolve().parent / "seed-example"
SIGNER_ID = "operator"
RUN_EVALUATION_TIME = "2026-08-20T12:00:00+00:00"
"""A fixed evaluation label recorded in the run manifest."""

FLOOR_MANIFEST = "manifest.json"
COVERAGE_BOUNDARY = "coverage-manifest.json"

ARM_LABELS: dict[int, str] = {
    1: "files",
    2: "files+scratchpad",
    3: "playbill-surface",
    4: "playbill-surface+coverage-delivery",
}

ArmNumber = Literal[1, 2, 3, 4]


# -- the resolver embedding -------------------------------------------------
#
# Verbatim from PC-G-H2. The middleware takes its resolve callable by injection,
# and this closure is the whole TauBench seam: observations in, one frozen
# coverage result out, over the ordinary served operation. Nothing in the
# coverage package reaches the service layer, which is what lets the same
# middleware embed in a benchmark harness that owns its tool executor.


def _resolver(client: Any, instance_id: str) -> ResolveCoverage:
    """The embedding recipe: observations in, one frozen coverage result out."""

    def resolve(observations: Sequence[WorkingSourceObservationV1]) -> CoverageResultV2:
        answered = client.resolve_playbill_coverage(
            instance_id,
            observations=[item.model_dump(mode="json") for item in observations],
        )
        return CoverageResultV2.model_validate(answered.result)

    return resolve


def _floor_generation_resolver(client: Any, instance_id: str) -> ResolveFloorGenerations:
    """Use the same search-orient wire as the CLI hook for floor freshness."""

    def generation(at: AcceptedCoordinate | None) -> int:
        answer = client.search_playbill(
            instance_id,
            mode="orient",
            kinds=("brief", "claim", "demand", "procedure"),
            at=None if at is None else at.model_dump(mode="json"),
        )
        if answer.orientation is None:
            raise RuntimeError("Playbill orient returned no floor generation")
        value = answer.orientation.get("generation")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("Playbill orient returned an invalid floor generation")
        return value

    def resolve(coordinate: AcceptedCoordinate) -> FloorGenerationPairV1:
        return FloorGenerationPairV1(
            floor_generation=generation(coordinate),
            current_generation=generation(None),
        )

    return resolve


# -- driving the CLI --------------------------------------------------------


def run_cli(*args: str) -> str:
    """Invoke `cruxible ...` in process and return its stdout.

    In process rather than as a subprocess so a harness can point the recipe at
    a daemon it already holds a transport for, which is what the smoke test
    does. The argv is the same argv an operator would type.
    """

    result = CliRunner().invoke(cli, list(args))
    if result.exit_code != 0:
        raise RuntimeError(f"cruxible {' '.join(args)} failed:\n{result.output}")
    return result.stdout


def run_cli_json(*args: str) -> Any:
    return json.loads(run_cli(*args, "--json"))


# -- step 1: stand up a served instance -------------------------------------


def bootstrap(*, key_dir: Path, server_url: str) -> str:
    """Allocate a host, bootstrap it, and remember it as the CLI's target.

    ``server_url`` is required rather than inferred: the recipe stands up a
    *fresh* instance, so there is nothing remembered yet for it to fall back on,
    and silently reusing whatever a previous run left in the CLI context is how
    two arms end up seeded against different worlds.
    """

    host = run_cli_json("--server-url", server_url, "playbill", "host", "create")
    run_cli_json("playbill", "init", "--key-dir", str(key_dir), "--principal-id", SIGNER_ID)
    return str(host["instance_id"])


def approve_and_activate(proposal_id: str, *, key_dir: Path) -> dict[str, Any]:
    """The two governed acts the seed command deliberately does not perform."""

    run_cli(
        "playbill",
        "proposal",
        "approve",
        proposal_id,
        "--signer-id",
        SIGNER_ID,
        "--key",
        str(key_dir / f"{SIGNER_ID}.ed25519"),
        "--yes",
        "--json",
    )
    activated = run_cli_json(
        "playbill",
        "proposal",
        "activate",
        proposal_id,
        "--workspace-root",
        str(key_dir.parent),
    )
    if activated["status"] != "accepted":  # pragma: no cover - a refusal raises earlier
        raise RuntimeError(f"proposal {proposal_id} did not settle: {activated}")
    return dict(activated)


# -- step 2: seed the world -------------------------------------------------


def seed(bundle_dir: Path = BUNDLE_DIR, *, name: str, key_dir: Path) -> dict[str, Any]:
    """Apply every planned group, approving and activating between them.

    The loop is the whole point of the seed command's shape. `--plan` is offline
    and names the groups in dependency order; each `apply` opens exactly one
    proposal, because a proposal settles against the base it was admitted at and
    two proposals opened against one head cannot both activate. Approval and
    activation are separate governed acts and the harness -- this function --
    performs them, never the seeding convenience.
    """

    plan = run_cli_json("playbill", "seed", "apply", str(bundle_dir), "--name", name, "--plan")
    applied: list[dict[str, Any]] = []
    for group in plan["groups"]:
        submitted = run_cli_json(
            "playbill",
            "seed",
            "apply",
            str(bundle_dir),
            "--name",
            name,
            "--group",
            group["group_id"],
        )
        activated = approve_and_activate(submitted["proposal_id"], key_dir=key_dir)
        applied.append(
            {
                "group_id": group["group_id"],
                "operation": group["operation"],
                "proposal_id": submitted["proposal_id"],
                "accepted_coordinate": activated["accepted_coordinate"],
            }
        )
    return {"plan_digest": plan["plan_digest"], "groups": applied}


# -- step 3: export the arm file surface ------------------------------------


def export_arm_surface(destination: Path) -> Path:
    """Write floor-v2 artifacts and the coverage boundary as one tree."""

    run_cli_json(
        "playbill",
        "floor",
        "export",
        "--output",
        str(destination),
    )
    return destination


def corpus_files(bundle_dir: Path = BUNDLE_DIR) -> dict[str, bytes]:
    """The bundle's committed bodies, keyed by the working path they belong at.

    A bundle stores its bodies under `bodies/`, mirroring the working tree the
    Claims were authored against; every arm's workspace gets exactly these bytes,
    so the task corpus really is shared across all four.
    """

    root = bundle_dir / SEED_BODY_DIRECTORY
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# -- step 4: build the arms -------------------------------------------------


def coverage_config(bundle_dir: Path = BUNDLE_DIR) -> dict[str, Any]:
    """The declared bindings, whose normalizer produces the accepted identities.

    `corpus/handbook.md` under prefix `corpus/` and identity prefix `corpus.` is
    `corpus.handbook.md` -- extension and all, because
    `playbill-coverage-path-identity-v1` is non-lossy. That string has to be
    exactly the `logical_source_identity` the bundle's Claim was authored
    against, and it is; nothing infers it on either side.
    """

    return {
        "tag": "playbill-coverage-workspace-config-v2",
        "rules": [
            {
                "tag": "playbill-coverage-path-prefix-rule-v1",
                "path_prefix": "corpus/",
                "plane": "external",
                "identity_prefix": "corpus.",
                "normalizer": "playbill-coverage-path-identity-v1",
            }
        ],
        "floor_output": {
            "tag": "playbill-floor-output-v1",
            "path": "playbill-floor",
            "format": "playbill-floor-export-v2",
        },
    }


@dataclass(frozen=True)
class ArmSetupV1:
    """One arm, ready to run. Arms 3 and 4 differ in one field and no other."""

    arm: ArmNumber
    label: str
    workspace: Path
    deliver_coverage: bool
    middleware: CoverageMiddlewareV1 | None


def build_arm(
    arm: ArmNumber,
    *,
    root: Path,
    surface: Path | None = None,
    bundle_dir: Path = BUNDLE_DIR,
) -> ArmSetupV1:
    """Materialize one arm's workspace and adapter.

    Every arm gets the identical task corpus. Arm 2 adds a real scratchpad
    directory -- the notebook control, a place the agent may write freely that
    grants nothing. Arms 3 and 4 additionally get the exported Playbill file
    surface and the binding configuration, and both construct the middleware:
    arm 3 holds it and never calls it, which is what makes "only the delivery
    adapter changes" true rather than approximately true.
    """

    workspace = root / f"arm{arm}"
    for relative, content in corpus_files(bundle_dir).items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    if arm == 2:
        scratchpad = workspace / "scratchpad"
        scratchpad.mkdir(parents=True, exist_ok=True)
        (scratchpad / "NOTES.md").write_bytes(
            b"# Scratchpad\n\nFree-form notes. Nothing here is governed.\n"
        )

    middleware: CoverageMiddlewareV1 | None = None
    if arm in {3, 4}:
        if surface is None:
            raise ValueError("arms 3 and 4 need the exported Playbill file surface")
        _copy_tree(surface, workspace / "playbill-floor")
        config_path = workspace / CONFIG_RELATIVE_PATH
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(coverage_config(bundle_dir), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        client = _common._get_client()
        instance_id = load_cli_context().instance_id
        if client is None or not instance_id:  # pragma: no cover - bootstrap ran first
            raise RuntimeError("no remembered Playbill instance to resolve coverage against")
        middleware = coverage_middleware(
            root=workspace,
            config=CoverageWorkspaceConfigV2.model_validate(coverage_config(bundle_dir)),
            resolve=_resolver(client, instance_id),
            resolve_floor_generations=_floor_generation_resolver(client, instance_id),
        )

    return ArmSetupV1(
        arm=arm,
        label=ARM_LABELS[arm],
        workspace=workspace,
        # The one boolean. Arm 3 and arm 4 are otherwise the same record.
        deliver_coverage=arm == 4,
        middleware=middleware,
    )


def _copy_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


# -- step 5: the scripted agent turn ----------------------------------------

GOVERNED_PATH = "corpus/handbook.md"
GOVERNED_LINE = b"The reviewer accepted the migration plan on the second reading.\n"
DRIFTED_LINE = b"The reviewer rejected the migration plan on the second reading.\n"


def scripted_turn(workspace: Path) -> tuple[HarnessToolEventV1, ...]:
    """One canned agent turn: read the governed span, search, then edit it.

    Deterministic and vendor-neutral. These are the four §11.7 tool kinds in the
    only vocabulary the middleware understands, so a harness whose tools are
    called `open_file` and `search` maps them here and everything downstream is
    identical.
    """

    content = (workspace / GOVERNED_PATH).read_text(encoding="utf-8")
    line_number = content.splitlines().index(GOVERNED_LINE.decode("utf-8").rstrip("\n")) + 1
    grep_output = f"{GOVERNED_PATH}:{line_number}:{GOVERNED_LINE.decode('utf-8').rstrip()}\n"
    return (
        HarnessToolEventV1(
            kind="read",
            tool_name="Read",
            ranges=(
                HarnessLineRangeV1(
                    path=GOVERNED_PATH, start_line=line_number, end_line=line_number
                ),
            ),
            original_output=f"{line_number}\t{GOVERNED_LINE.decode('utf-8').rstrip()}",
        ),
        grep_event(grep_output, tool_name="Grep"),
        HarnessToolEventV1(
            kind="edit",
            tool_name="Edit",
            paths=(GOVERNED_PATH,),
            original_output=f"The file {GOVERNED_PATH} has been updated successfully.",
        ),
    )


def run_turn(setup: ArmSetupV1) -> tuple[dict[str, Any], ...]:
    """Run the scripted turn and return what the model would have seen.

    The edit lands on disk before the `edit` event is delivered, exactly as a
    real harness would order it: the middleware answers about the file as it now
    is, which is what makes drift visible in the same turn.

    `deliver_coverage` is the only branch. Everything above it -- the events, the
    file writes, the tool outputs -- is identical in arms 3 and 4.
    """

    transcript: list[dict[str, Any]] = []
    for event in scripted_turn(setup.workspace):
        if event.kind == "edit":
            target = setup.workspace / GOVERNED_PATH
            target.write_bytes(target.read_bytes().replace(GOVERNED_LINE, DRIFTED_LINE))

        raw = event.original_output
        cards: tuple[str, ...] = ()
        result: CoverageResultV2 | None = None
        if setup.deliver_coverage:
            assert setup.middleware is not None
            delivery = setup.middleware.after_tool(event)
            cards = delivery.lines
            result = delivery.result
            model_visible = delivery.spliced()
        else:
            model_visible = raw

        transcript.append(
            {
                "arm": setup.arm,
                "tool": event.tool_name or event.kind,
                "kind": event.kind,
                "original_output": raw,
                "model_visible_output": model_visible,
                "coverage_lines": list(cards),
                "result": result,
            }
        )
    return tuple(transcript)


# -- step 6: the run manifest ----------------------------------------------


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def run_manifest(
    *,
    instance_id: str,
    seeded: dict[str, Any],
    surface: Path,
    transcripts: dict[int, tuple[dict[str, Any], ...]],
    bundle_dir: Path = BUNDLE_DIR,
    evaluation_time: str = RUN_EVALUATION_TIME,
) -> dict[str, Any]:
    """Pin everything §11.8 requires pinned per run.

    "Resolver, index, manifest, hook-adapter, and accepted-generation versions
    are pinned per run." Each of those is read off an artifact the run actually
    produced rather than restated from configuration: the index, overlay, and
    manifest digests plus the epoch come out of a coverage result the hooked arm
    received; the generation root and format come out of the floor manifest;
    and the hook adapter is named with its envelope version, which is `null` here
    because the owned-harness middleware has no vendor envelope at all.
    """

    floor = json.loads((surface / FLOOR_MANIFEST).read_text(encoding="utf-8"))
    boundary = json.loads((surface / COVERAGE_BOUNDARY).read_text(encoding="utf-8"))
    files = {
        path.relative_to(bundle_dir).as_posix(): path.read_bytes()
        for path in sorted(bundle_dir.rglob("*"))
        if path.is_file()
    }
    plan = plan_seed_bundle(files, proposal_name="run-manifest")

    resolved = next(
        (entry["result"] for entry in transcripts.get(4, ()) if entry["result"] is not None),
        None,
    )
    return {
        "tag": "playbill-taubench-run-manifest-v1",
        "instance_id": instance_id,
        "evaluation_time": evaluation_time,
        "arms": {str(arm): ARM_LABELS[arm] for arm in sorted(ARM_LABELS)},
        "hook_adapter": {
            "adapter": "playbill-coverage-middleware-v1",
            # The owned-harness middleware is reached directly, so there is no
            # vendor hook envelope to version. Recorded as absent rather than
            # omitted, because "not applicable" and "forgotten" must not look
            # alike in a pinned manifest.
            "envelope_version": None,
            "rule_set_digest": _digest(coverage_config(bundle_dir)),
            "rule_set": coverage_config(bundle_dir)["rules"],
        },
        "seed": {
            "bundle": bundle_dir.name,
            "plan_digest": seed_plan_digest(plan).tagged,
            "applied_plan_digest": seeded["plan_digest"],
            "groups": seeded["groups"],
        },
        "accepted": {
            "generation_root": floor["coordinate"]["generation_root"],
            "semantic_root": floor["coordinate"]["semantic_root"],
            "compiler_digest": floor["coordinate"]["compiler_digest"],
            "floor_digest": floor["floor_digest"],
            "format": floor["format"],
        },
        "coverage": {
            "boundary_format": boundary["format"],
            "completeness": boundary["completeness"],
            "index_digest": None if resolved is None else resolved.index_digest,
            "overlay_digest": None if resolved is None else resolved.overlay_digest,
            "manifest_digest": None if resolved is None else resolved.manifest_digest,
            "epoch": None if resolved is None else resolved.epoch,
        },
    }


# -- the whole recipe, in order ---------------------------------------------


def run_all(root: Path, *, server_url: str) -> dict[str, Any]:
    """Stand up, seed, export, build all four arms, run the turn, pin the run."""

    key_dir = root / "custody"
    instance_id = bootstrap(key_dir=key_dir, server_url=server_url)
    seeded = seed(name="taubench-seed", key_dir=key_dir)
    surface = export_arm_surface(root / "arm-surface")

    setups = {
        arm: build_arm(arm, root=root, surface=surface if arm in {3, 4} else None)
        for arm in (1, 2, 3, 4)
    }
    transcripts = {arm: run_turn(setup) for arm, setup in setups.items()}
    manifest = run_manifest(
        instance_id=instance_id,
        seeded=seeded,
        surface=surface,
        transcripts=transcripts,
    )
    (root / "run-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"manifest": manifest, "setups": setups, "transcripts": transcripts}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path, help="Scratch directory to build in.")
    parser.add_argument("--server-url", required=True, help="Cruxible daemon base URL.")
    arguments = parser.parse_args(argv)
    arguments.root.mkdir(parents=True, exist_ok=True)

    outcome = run_all(arguments.root, server_url=arguments.server_url)
    for arm in (3, 4):
        print(f"\n--- arm {arm}: {ARM_LABELS[arm]} ---")
        for entry in outcome["transcripts"][arm]:
            print(f"[{entry['tool']}] {entry['model_visible_output']}")
    print(f"\nRun manifest: {arguments.root / 'run-manifest.json'}")
    print(json.dumps(outcome["manifest"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - the standalone entry point
    raise SystemExit(main())


__all__ = [
    "ARM_LABELS",
    "BUNDLE_DIR",
    "DRIFTED_LINE",
    "GOVERNED_LINE",
    "GOVERNED_PATH",
    "RUN_EVALUATION_TIME",
    "ArmSetupV1",
    "approve_and_activate",
    "bootstrap",
    "build_arm",
    "corpus_files",
    "coverage_config",
    "export_arm_surface",
    "run_all",
    "run_cli",
    "run_cli_json",
    "run_manifest",
    "run_turn",
    "scripted_turn",
    "seed",
]
