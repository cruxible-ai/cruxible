"""The floor's README and its changes/ files.

Accepted values come only from the accepted tree. Review prose is read from the
proposal note of the exact candidate each accepted change published, by path,
and is shown as attributed rationale, never as a Claim or proof of adoption.
No evidence bodies, working files, or authoring-intent exhaust are read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.primitives import pretty_json
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
)
from cruxible_core.indexes.history.history_index import AcceptedGenerationLocation, HistoryReader
from cruxible_core.proposals.proposal_notes import admission_bytes, evaluation_bytes
from cruxible_core.runtime.instance import PlaybillInstance

CHANGES_PREFIX = "changes/"
# Read each accepted candidate's note at the notes ref as it stands. Notes on an
# accepted candidate do not change after acceptance, so this reads what any
# notes commit since then would.
LIVE_NOTES = "live"

FLOOR_README = """\
# Playbill floor

Accepted state as plain files, for grep. Search here; confirm and act with the
verbs. The floor does no matching of its own: grep is the search.

## What to grep

- `current/<kind>/<id>.yaml`: one file per Subject. Its first line is
  `# <ref>  kind=<kind>  changed gen <n>`: the latest accepted generation that
  changed anything the file shows (the Subject, its Claims, the Claims
  pointing at it, or the ClaimTypes naming its fields). Then each field's
  value under its short name, each followed by `# CLM-...`, the Claim that
  states it. A many-valued field, or a single-valued one with several live
  Claims, lists every live value. A Subject-valued field shows the other
  Subject's ref. `flags:` marks a single-valued field holding more than one
  distinct live value `contested`. `incoming:` lists the live edges other
  Subjects point here, one `<field> <- <ref>` line each, so either end of an
  edge greps.
- `current/<kind>/<id>.<field>.txt`: a text value too long to inline, whole.
- `current/<kind>/INDEX`: one tab-separated line per Subject of that kind: its
  ref, a title-like value, and its state-like fields.
- `changes/<seq>.json`: the accepted change that introduced a current Claim
  revision: its time, actor, the rationale its proposal recorded, and the refs
  it changed.
- `sources/INDEX`: one tab-separated line per evidence source current Claims
  cite: the source, its CaptureContract, a locator (a Document ref, or an
  external source's selector type), how many current Claims cite it, and the
  generation it last changed. Every accepted Document is listed, cited or not.
- `projections/INDEX`: written by the client from its own workspace, never by
  the daemon: one line per workspace file bound to accepted state (a Document
  body, an evidence source, a rendered block), its role, the ref it is bound
  to, and the generation that ref last changed.

The floor shows accepted values, never verdicts: whether a value is still
supported moves with time and evidence, which no coordinate fixes. `get`
answers with the live verdict.

## The loop

1. `grep -rn "some text" .playbill/floor/current`
2. Read the hit's first line: its ref, and the generation it last changed.
3. `cruxible playbill get <ref>` for the live values and verdicts (`orient`
   says how far behind the floor is).
4. Change state with the write verbs (`cruxible playbill set|retire|write`).

An agent without a shell asks `query --contains "some text"` instead. A
Document's body is read with `get Document:<name> --detail body`.

## Retention

A ruling or other exact-content value shows its text, read by digest from the
body store. Accepted bodies are retained for as long as their Claim is in
accepted history, so that text is fixed by the coordinate. A body that is
nevertheless lost renders as `{exact_content: unavailable, ...}`: an integrity
incident `orient` and `next` report, not a change of accepted state.

## Not for grep

- `manifest.json` names the accepted coordinate and its generation, and binds
  every file by digest, and by the generation it last changed, into the floor
  digest. A refresh fetches only the files changed since the floor's own
  generation.
- Only with `floor export --with-discovery`: `subjects/`, `claim-types/` and
  `procedures/`, the discovery cards other tools read (they carry digests and
  addresses), and `coverage-manifest.json`, the export's coverage boundary.

History, rejected proposals, full evaluation transcripts, Document and evidence
bodies, and authoring-intent exhaust are not exported, so no match here does
not prove absence. The ledger clone is the audit path.
"""


def _render(value: object) -> bytes:
    return pretty_json(json.loads(canonical_bytes(value))).encode("utf-8") + b"\n"


_CLAIM_MEMBER = re.compile(r"claims/[0-9a-f]{2}/(CLM-[0-9a-f]{32})\.json")


def member_ref(path: str) -> str:
    """How a change names one member path: the handle ``get`` resolves."""

    if path.startswith("subjects/") and path.endswith(".json"):
        return path.removeprefix("subjects/").removesuffix(".json")
    if (match := _CLAIM_MEMBER.fullmatch(path)) is not None:
        return match.group(1)
    if path.startswith("documents/") and path.endswith(".json"):
        return "Document:" + path.removeprefix("documents/").removesuffix(".json")
    if path.startswith("claim-types/") and path.endswith(".json"):
        return "ClaimType:" + path.removeprefix("claim-types/").removesuffix(".json").replace(
            "/", "."
        )
    return path


def review_snapshot_oid(instance: PlaybillInstance) -> str | None:
    return instance._ledger.mirror_refs().get("refs/notes/playbill-eval")


def _candidate_commits(
    instance: PlaybillInstance, candidate_digests: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Every proposal commit that published each candidate digest."""

    digests = sorted(set(candidate_digests))
    if not digests:
        return {}
    evidence = instance.proposal_evidence()
    assert evidence.index is not None
    found: dict[str, set[str]] = {digest: set() for digest in digests}
    with evidence.index.read(evidence) as connection:
        for start in range(0, len(digests), 500):
            chunk = digests[start : start + 500]
            for digest, candidate, review in connection.execute(
                "SELECT candidate_digest,candidate_commit_oid,review_commit_oid FROM proposals "
                "WHERE candidate_digest IN (" + ",".join("?" for _ in chunk) + ")",
                chunk,
            ):
                found[str(digest)].update(oid for oid in (candidate, review) if oid is not None)
    return {digest: tuple(sorted(oids)) for digest, oids in found.items()}


def change_rationales(
    instance: PlaybillInstance,
    generations: Iterable[AcceptedGenerationLocation],
    notes_oid: str | None,
) -> dict[int, tuple[str, ...]]:
    """Each accepted change's review rationale, read by path from one notes commit.

    A change's rationale is what the proposals that published its exact
    candidate, by its own actor, recorded. A missing note is missing rationale,
    never invented. Notes on an accepted candidate do not change after
    acceptance, so any notes commit at or after it reads the same prose.
    """

    wanted = [item for item in generations if item.candidate_digest is not None]
    if notes_oid is None or not wanted:
        return {item.sequence: () for item in wanted}
    commits = _candidate_commits(instance, (str(item.candidate_digest) for item in wanted))
    pairs = sorted({("evaluation", oid) for oids in commits.values() for oid in oids})
    pinned = None if notes_oid == LIVE_NOTES else notes_oid
    notes = instance.read_proposal_notes(pairs, notes_commit=pinned) if pairs else {}
    result: dict[int, tuple[str, ...]] = {}
    for generation in wanted:
        by_proposal: dict[str, str] = {}
        for oid in commits.get(str(generation.candidate_digest), ()):
            content = notes.get(("evaluation", oid))
            if content is None:
                continue
            lines = content.splitlines(keepends=True)
            if len(lines) % 2:
                raise ProposalIntegrityError("floor review note has an incomplete record pair")
            for i in range(0, len(lines), 2):
                admission = ProposalAdmissionRecord.model_validate_json(lines[i])
                evaluation = ProposalEvaluationRecord.model_validate_json(lines[i + 1])
                if (
                    admission_bytes(admission) != lines[i]
                    or evaluation_bytes(evaluation) != lines[i + 1]
                    or admission.proposal_id != evaluation.proposal_id
                ):
                    raise ProposalIntegrityError(
                        "floor review note is not a canonical proposal pair"
                    )
                if (
                    evaluation.verdict == "candidate"
                    and evaluation.candidate_digest == generation.candidate_digest
                    and admission.actor_id == generation.actor_id
                ):
                    if admission.rationale:
                        by_proposal[admission.proposal_id] = admission.rationale
        result[generation.sequence] = tuple(
            dict.fromkeys(by_proposal[key] for key in sorted(by_proposal))
        )
    return result


def change_file(
    generation: AcceptedGenerationLocation,
    *,
    timestamp: str,
    member_paths: Iterable[str],
    rationale: tuple[str, ...],
) -> bytes:
    """``changes/<seq>.json``: one accepted change, its actor, rationale and refs."""

    return _render(
        {
            "sequence": generation.sequence,
            "timestamp": timestamp,
            "actor": generation.actor_id,
            "rationale": list(rationale),
            "changed": sorted({member_ref(path) for path in member_paths}),
        }
    )


def change_path(sequence: int) -> str:
    return f"{CHANGES_PREFIX}{sequence:020d}.json"


def render_changes(
    instance: PlaybillInstance,
    history: HistoryReader,
    sequences: Iterable[int],
    notes_oid: str | None,
) -> dict[int, bytes]:
    """Render the change files for ``sequences`` from the history index and notes.

    The members come from the history index and the time from the accepted
    commit (the ledger stamps it from the candidate's own timestamp), so no
    change-set record is re-read.
    """

    wanted = sorted(set(sequences))
    if not wanted:
        return {}
    generations = [history.generation(sequence) for sequence in wanted]
    rationales = change_rationales(instance, generations, notes_oid)
    members = history.member_paths_by_sequence(wanted)
    files: dict[int, bytes] = {}
    for generation in generations:
        files[generation.sequence] = change_file(
            generation,
            timestamp=instance._ledger.commit_timestamps(generation.git_oid)[0].strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            member_paths=members.get(generation.sequence, ()),
            rationale=rationales.get(generation.sequence, ()),
        )
    return files
