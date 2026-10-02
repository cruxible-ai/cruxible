"""The floor's README and its changes/ files.

Accepted values come only from the accepted tree. Review prose is read from the
proposal note of the exact candidate each accepted change published, by path,
and is shown as attributed rationale, never as a Claim or proof of adoption.
No evidence bodies, working files, or authoring-intent exhaust are read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

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
NOTES_REF = "refs/notes/playbill-eval"

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
- `sources/INDEX`: one tab-separated line per evidence source: the source, its
  CaptureContract(s), where it lives (the workspace path the client's source
  catalog binds it to, marked `(missing)` when no such file exists; else a
  Document ref or the external `coordinate/selector` type pairs), how many
  current Claims cite it, and the generation it last changed. Every accepted Document is a
  source, cited or not. The client writes it from `sources/LEDGER` and its own
  catalog after every refresh; the daemon never sees workspace paths.
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
body store, and `sources/LEDGER` reads each cited Capture's envelope the same
way. Accepted bodies and Captures are retained for as long as their Claim is in
accepted history, so what the floor shows is fixed by the coordinate. One that
is nevertheless lost refuses a fresh render as an integrity failure; the floor
is never published differently for the same accepted state.

`manifest.json` names the review notes the change rationale was read from
(`notes_digest`); a rationale revised after acceptance makes a refresh replace
the floor whole.

## Not for grep

- `sources/LEDGER`: `sources/INDEX` as accepted state alone gives it, with
  only ledger-derived locators (a Document ref, else every external
  `coordinate/selector` type pair, comma-separated, or `-`); the client joins
  workspace paths into `sources/INDEX`.
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
    """The review notes commit the notes ref names now: one immutable snapshot."""

    return instance._ledger._resolve_ref(NOTES_REF)


def notes_changed_between(
    instance: PlaybillInstance, before: str | None, after: str | None
) -> frozenset[str] | None:
    """The commits whose note differs between two notes snapshots; None for every one."""

    if before == after:
        return frozenset()
    if before is None or after is None:
        return None
    return frozenset(
        change.path.replace("/", "") for change in instance._ledger.changed_entries(before, after)
    )


@dataclass(frozen=True)
class ChangeNote:
    """What one accepted change's review notes say: its proposals' commits and rationale."""

    commits: tuple[str, ...]
    rationale: tuple[str, ...]


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
    """Each accepted change's review rationale, read by path from one notes commit."""

    return {
        sequence: note.rationale
        for sequence, note in change_notes(instance, generations, notes_oid).items()
    }


def change_notes(
    instance: PlaybillInstance,
    generations: Iterable[AcceptedGenerationLocation],
    notes_oid: str | None,
) -> dict[int, ChangeNote]:
    """Each accepted change's review notes, read by path from one notes commit.

    A change's rationale is what the proposals that published its exact
    candidate, by its own actor, recorded. A missing note is missing rationale,
    never invented. ``notes_oid`` is one immutable notes commit, or None for no
    notes at all.
    """

    wanted = [item for item in generations if item.candidate_digest is not None]
    result: dict[int, ChangeNote] = {item.sequence: ChangeNote((), ()) for item in generations}
    if notes_oid is None or not wanted:
        return result
    commits = _candidate_commits(instance, (str(item.candidate_digest) for item in wanted))
    pairs = sorted({("evaluation", oid) for oids in commits.values() for oid in oids})
    notes = instance.read_proposal_notes(pairs, notes_commit=notes_oid) if pairs else {}
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
        result[generation.sequence] = ChangeNote(
            commits.get(str(generation.candidate_digest), ()),
            tuple(dict.fromkeys(by_proposal[key] for key in sorted(by_proposal))),
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
    rationales: Mapping[int, tuple[str, ...]],
) -> dict[int, bytes]:
    """Render the change file of each sequence in ``rationales``, with its rationale.

    The members come from the history index and the time from the accepted
    commit (the ledger stamps it from the candidate's own timestamp), so no
    change-set record is re-read.
    """

    wanted = sorted(rationales)
    if not wanted:
        return {}
    generations = [history.generation(sequence) for sequence in wanted]
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
