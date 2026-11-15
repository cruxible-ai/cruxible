"""One text rendering per result, shared by the CLI and the SDK objects' ``str()``.

The same request gives the same text on every surface: ``cruxible orient``
prints ``render_orient``, and ``print(cx.orient())`` shows it too; a write
outcome, an authoring landing and an accepted coordinate render here once.
``repr()`` stays the full model. Each renderer reads only the result it is
given: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from cruxible_client._error_base import printable
from cruxible_client.contracts.get_display import get_value_display
from cruxible_client.contracts.public_names import public_name as _public_name

if TYPE_CHECKING:
    from cruxible_client.contracts.authoring_landing import AuthoringLanded, AuthoringProposed
    from cruxible_client.contracts.write import ApprovalNeeded, WriteOutcome

#: How many hex digits of a Git OID name a generation in text, as ``World`` prints it.
SHORT_OID = 12


def short_oid(git_oid: str) -> str:
    """A Git OID as text shows it: its first ``SHORT_OID`` hex digits."""

    return git_oid[:SHORT_OID]


# -- orient --------------------------------------------------------------------


def _orient_predicate_line(item: Mapping[str, Any]) -> str:
    kind = item["type"]
    if item.get("members"):
        kind = "enum[" + "|".join(str(member) for member in item["members"]) + "]"
    parts = [str(item["name"]), str(item["cardinality"]), kind]
    if item.get("live_claims") is not None:
        parts.append(f"claims={item['live_claims']}")
    if item.get("subjects_with_value") is not None:
        parts.append(f"subjects={item['subjects_with_value']}")
    if item.get("stale_after"):
        parts.append(f"stale_after={item['stale_after']}")
    if item.get("evidence") is not None:
        parts.append("evidence=" + (",".join(map(_public_name, item["evidence"])) or "(none)"))
    return "  ".join(parts)


#: A query's description in the default map: its first sentence, at most this long.
_ORIENT_QUERY_TEXT_LIMIT = 100


def _orient_kind_summary(kind: Mapping[str, Any]) -> str:
    """One line per kind in the default map (`OrientKindSummary`): its first
    fields by name and a count of the rest; `orient --kind` prints every field."""

    names = [str(name) for name in kind.get("fields", ())]
    more = int(kind.get("more_fields", 0))
    fields = f"  fields={','.join(names)}" + (f" (+{more})" if more > 0 else "") if names else ""
    return f"{kind['kind']}  subjects={kind['subjects']}{fields}"


def _flattened_counts(counts: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Non-zero counts as `name=N`, a nested family as `family.name=N`."""

    flat: list[str] = []
    for name, value in counts.items():
        if isinstance(value, Mapping):
            flat.extend(_flattened_counts(value, f"{prefix}{name}."))
        elif value:
            flat.append(f"{prefix}{name}={value}")
    return flat


def _first_sentence(text: str) -> str:
    """A description's first sentence, cut to the default map's width."""

    first = text.strip().split("\n", 1)[0]
    end = first.find(". ")
    if end != -1:
        first = first[: end + 1]
    if len(first) > _ORIENT_QUERY_TEXT_LIMIT:
        first = first[: _ORIENT_QUERY_TEXT_LIMIT - 3].rstrip() + "..."
    return first


def render_orient(result: Mapping[str, Any]) -> str:
    """An orient result (its JSON form) as the lines the CLI prints and ``str()`` shows."""

    lines = [
        f"Cruxible {result['instance']} generation={result['generation']} "
        f"at {result['coordinate']['git_oid'][:12]} accepted {result['accepted_at']}"
    ]
    if result.get("mirror_url"):
        lines.append(f"Ledger mirror: {result['mirror_url']}")
    floor = result.get("floor")
    if floor is not None:
        behind = floor["generations_behind"]
        lines.append(
            f"Floor: .cruxible/floor at {floor['at'][:12]}, "
            + (
                "current"
                if behind == 0
                else "generations behind unknown"
                if behind is None
                else f"{behind} generation(s) behind"
            )
            + ("" if behind == 0 else "; refresh: cruxible floor export --force")
        )
    vocabulary = result.get("vocabulary_file")
    if vocabulary is not None:
        at = vocabulary.get("at")
        state = vocabulary["state"]
        lines.append(
            "Vocabulary file: .cruxible/vocabulary.yaml"
            + ("" if at is None else f" at {at[:12]}")
            + ", "
            + (
                "current"
                if state == "current"
                else f"behind by {vocabulary.get('definitions_behind', 0)} definition(s)"
                if state == "behind"
                else "baseline invalid"
            )
            + ("" if state == "current" else "; refresh: cruxible vocabulary export")
        )
    you = result.get("you")
    if you is not None:
        refusal = you.get("authoring_refusal") or {}
        verdict = (
            "can author"
            if you["can_author"]
            else f"cannot author ({refusal.get('code')}): {refusal.get('detail')}"
        )
        lines.append(f"You: {you['actor'] or '(no actor)'}, {verdict}")
    kinds = result.get("kinds")
    if kinds is not None:
        lines.append(f"Kinds ({len(kinds)}):")
        empty = [str(kind["kind"]) for kind in kinds if not kind["subjects"]]
        for kind in kinds:
            if kind["subjects"]:
                lines.append(f"  {_orient_kind_summary(kind)}")
        if empty:
            lines.append(f"  no subjects yet ({len(empty)}): {', '.join(empty)}")
    artifacts = result.get("artifacts")
    if artifacts is not None:
        counts = _flattened_counts(artifacts)
        lines.append("Artifacts: " + (" ".join(counts) if counts else "none"))
    detail = result.get("kind_detail")
    if detail is not None:
        shared = detail.get("evidence")
        lines.append(
            f"Kind {detail['kind']}  subjects={detail['subjects']}"
            + (f"  evidence={','.join(map(_public_name, shared))}" if shared else "")
        )
        lines.extend(f"  {_orient_predicate_line(item)}" for item in detail["predicates"])
        if detail.get("incoming"):
            lines.append("Incoming (--follow-in): " + ", ".join(detail["incoming"]))
        if detail["sample_subject_ids"]:
            lines.append("Sample subjects: " + ", ".join(detail["sample_subject_ids"]))
    section = result.get("section")
    if section == "graph":
        graph = result["graph"]
        lines.append(
            f"Graph: subjects={graph['totals']['subjects']}  "
            f"this page: nodes={len(graph['nodes'])} edges={len(graph['edges'])}"
        )
        lines.extend(f"  {node['subject']}" for node in graph["nodes"])
        for edge in graph["edges"]:
            qualifier = f"[{edge['qualifier']}]" if edge.get("qualifier") else ""
            lines.append(
                f"  {edge['source']} -{edge['predicate']}{qualifier}-> {edge['target']}  "
                f"{edge['claim']}"
            )
    elif section is not None and not result[section]:
        lines.append(f"(no {section.replace('_', ' ')})")
    if section in {
        "documents",
        "procedures",
        "runs",
        "lines",
        "captures",
        "capture_contracts",
        "predictions",
    }:
        for row in result[section]:
            # Cells read stored names (a contract, a source) by their public names.
            lines.append(
                "  ".join(
                    _public_name(value) if isinstance(value, str) else str(value)
                    for value in row.values()
                )
            )
    if section == "sources":
        for row in result["sources"]:
            latest = row["latest"]
            state = (
                "  (unavailable)"
                if latest.get("unavailable")
                else "  (erased)"
                if latest.get("erased")
                else "  (unavailable)"
                if latest.get("status") == "unavailable"
                else ""
            )
            contracts_line = ", ".join(
                _public_name(item["contract"])
                + (f" v{item['version']}" if item.get("version") else "")
                + f" ({item['landings']})"
                for item in row["contracts"]
            )
            lines.append(
                f"{row['logical_source']}  landings={row['landings']}  "
                f"last landed {latest['landed_at']} {latest.get('capture') or '-'}{state}  "
                f"by {latest.get('line') or latest['run']}  under {contracts_line}"
            )
        if result.get("sources_unattributed"):
            lines.append(
                f"({result['sources_unattributed']} landings whose record or Capture can no "
                "longer be read are attributed to no source)"
            )
    if section == "pins":
        for row in result["pins"]:
            stale = "" if row.get("current", True) else "  (not the current version)"
            lines.append(f"{row['source']}  -{row['role']}->  {row['target']}{stale}")
    if section == "mandates":
        for row in result["mandates"]:
            lines.append(
                f"{row['mandate']}  {row['line']}  {row['grants']}  {row['state']}  "
                f"expires {row['expires_at']}" + ("  suspended" if row.get("suspended") else "")
            )
            for scope in row.get("scope", ()):
                changes = ",".join(
                    f"{kind}[{'|'.join(change['basis'])}]"
                    for kind, change in scope["changes"].items()
                )
                lines.append(f"  settles {scope['claim_type']}  {changes}  binds {scope['binds']}")
            if row.get("condition_query"):
                lines.append(f"  condition {row['condition_query']}")
            if row.get("subject_creation"):
                lines.append(f"  may create {', '.join(row['subject_creation'])}")
    if section == "operations":
        for row in result["operations"]:
            build = str(row["build"]).removeprefix("Provider:")
            lines.append(
                f"{build}#{row['operation']}  {row['input']} -> {row['output']}  ({row['effect']})"
            )
            if row.get("refusals"):
                lines.append(f"  refuses {', '.join(row['refusals'])}")
    if section == "generations":
        for row in result["generations"]:
            approvals = ",".join(row.get("approvals", ())) or "-"
            lines.append(
                f"seq {row['sequence']} at {row['git_oid'][:12]}  {row['accepted_at']}  "
                f"by {row.get('proposer') or '-'}  approved {approvals}  "
                f"members={row['members']}"
                + (f"  {row['proposal']}" if row.get("proposal") else "")
            )
            if row.get("because"):
                lines.append(f"  because: {printable(row['because'])}")
    if section == "activity":
        for row in result["activity"]:
            # One line per event: when, what, what it opens, then its own fields.
            fields = (
                f"{key}={','.join(value) if isinstance(value, list) else printable(str(value))}"
                for key, value in row.items()
                if key not in {"at", "event", "ref", "because"}
            )
            lines.append(
                "  ".join((row["at"], row["event"], row.get("ref") or "-", *fields)).rstrip()
            )
            if row.get("because"):
                lines.append(f"  because: {printable(row['because'])}")
    if section == "contracts":
        for row in result["contracts"]:
            pins = row.get("pins", ())
            lines.append(
                f"{row['contract']}  fields={row['fields']}"
                + (f"  pins={','.join(pins)}" if pins else "")
            )
            if row.get("description"):
                lines.append(f"  {row['description']}")
    if section == "capabilities":
        for row in result["capabilities"]:
            lines.append(f"{row['capability']}  {row['effect']}  {row['in']} -> {row['out']}")
            lines.append(f"  {row['description']}")
    if section == "subject_kinds":
        for row in result["subject_kinds"]:
            rule = (
                "minted"
                if row["rule"] == "minted"
                else f"keys: {', '.join(row.get('keys', ()))} ({row.get('uniqueness')})"
            )
            lines.append(
                f"{row['kind']}  {rule}" + (f"  kit={row['kit']}" if row.get("kit") else "")
            )
    if section == "claim_types":
        for row in result["claim_types"]:
            lines.append(
                f"{row['predicate']}  kinds={','.join(row['subject_kinds'])}  "
                + _orient_predicate_line(row).split("  ", 1)[1]
                + (f"  kit={row['kit']}" if row.get("kit") else "")
            )
    queries = result.get("queries")
    if queries:
        if section is None:
            lines.append("Queries:")
        for query in queries:
            params = ", ".join(query["params"])
            description = query.get("description") or ""
            if section is None:
                description = _first_sentence(description)
            lines.append(
                f"  {query['name']}({params})" + (f"  {description}" if description else "")
            )
    attention = result.get("attention")
    if attention is not None:
        setup = attention.get("setup_items")
        lines.append(
            f"Attention: next={attention['next_items']} "
            + (f"setup={setup} " if setup else "")
            + f"open_proposals={attention['open_proposals']}"
        )
        lines.extend(f"  {line}" for line in attention["top"])
        lines.extend(f"  note: {line}" for line in attention.get("notes", ()))
        vocabulary_file = result.get("vocabulary_file")
        if vocabulary_file is not None and vocabulary_file["state"] != "current":
            # next= counts accepted state; the workspace's own rows are next's.
            lines.append(
                "  note: next= counts accepted state; the vocabulary file row is in `cruxible next`"
            )
        enablements = attention.get("enablements")
        if enablements is not None:
            lines.append(
                f"  Line enablements: running={enablements['running']} "
                f"stalled={enablements['stalled']} stopped={enablements['stopped']}"
            )
            lines.extend(f"    {line}" for line in enablements.get("needs_attention", ()))
    if result.get("next"):
        lines.append("Next:")
        lines.extend(f"  {line}" for line in result["next"])
    return "\n".join(lines)


# -- write outcomes ------------------------------------------------------------


def _value_text(value: object) -> str:
    return get_value_display(value).text


def awaiting_text(approval: ApprovalNeeded) -> str:
    """What the write waits for, worded by why it stopped (never naming the caller as approver).

    Only an independent approval has approvers; a write stopped by its caller's
    tier or by ``accept=never`` waits on an activation, which ``next`` names.
    """

    if approval.reason == "activation_not_permitted":
        return "activation; your tier cannot activate"
    if approval.reason == "accept_never":
        return "kept (accept=never)"
    if not approval.eligible_approvers:
        return (
            "independent approval; no eligible approver; add one: "
            "cruxible principal add PRINCIPAL_ID --key-dir DIR"
        )
    return f"independent approval; approvers: {', '.join(approval.eligible_approvers)}"


def write_outcome_lines(outcome: WriteOutcome) -> list[tuple[str, bool]]:
    """A write outcome's lines, each with whether the CLI prints it to stderr."""

    lines: list[tuple[str, bool]] = []

    def out(text: str, *, err: bool = False) -> None:
        lines.append((text, err))

    head = outcome.status.replace("_", " ")
    out(f"{head} (generation {outcome.coordinate.generation}, {outcome.coordinate.git_oid})")
    if outcome.hint is not None:
        out(outcome.hint)
    for doc in outcome.docs:
        out(
            f"  doc {doc.file or doc.source_id} {list(doc.section)}: "
            f"{'written' if doc.written else 'not written'} ({doc.after_digest})"
        )
        if doc.refusal:
            out(f"{doc.refusal.code}: {doc.refusal.message}. {doc.refusal.repair}", err=True)
    for change in outcome.changes:
        target = " ".join(part for part in (change.subject, change.field) if part)
        if change.op == "create":
            line = f"  create {change.subject or '(id assigned on submit)'}: {change.resolved}"
        elif change.op == "retire" and change.field is None:
            line = f"  retire Subject {change.subject}"
        elif change.op == "retire":
            line = f"  retire {target}: {_value_text(change.before)}"
        elif change.op == "add":
            line = f"  add {target}: {_value_text(change.after)}"
        elif change.op == "rebind":
            line = f"  rebind {target}: {_value_text(change.after)} (value unchanged)"
        elif change.op == "about":
            where = f"{change.file}#{'/'.join(change.section or ())}"
            about = ", ".join(change.about or ()) or "(unlinked: retired)"
            line = f"  note {where} about {about}  [{change.note}]"
        elif change.before is None:
            line = f"  set {target}: {_value_text(change.after)}"
        else:
            line = f"  set {target}: {_value_text(change.before)} -> {_value_text(change.after)}"
        details = [item for item in (change.claim,) if item]
        if change.already_live:
            details.append("already live")
        if change.verdict is not None:
            details.append(f"verdict {change.verdict}")
        if change.capture is not None:
            details.append(f"evidence {change.capture}")
        if change.retired:
            details.append(f"also retires {', '.join(change.retired)}")
        if change.contenders_created:
            details.append(f"contends with {', '.join(change.contenders_created)}")
        out(line + (f"  [{'; '.join(details)}]" if details else ""))
    for subject in outcome.subjects_added:
        out(f"  + subject {subject}")
    if outcome.proposal is not None and outcome.status != "accepted":
        out(f"proposal: {outcome.proposal.proposal_id} ({outcome.proposal.state})")
    if outcome.approval is not None:
        out(f"awaiting: {awaiting_text(outcome.approval)}")
    for warning in outcome.warnings:
        out(f"warning {warning.code}: {warning.message}", err=True)
    if outcome.refusal is not None:
        refusal = outcome.refusal
        where = "" if refusal.change is None else f" (change {refusal.change})"
        out(f"{refusal.code}{where}: {refusal.message}", err=True)
        if refusal.candidates:
            out(f"  nearest: {', '.join(refusal.candidates)}", err=True)
        if refusal.repair:
            out(f"  repair: {refusal.repair}", err=True)
    if outcome.next is not None:
        out(f"next: {outcome.next}")
    return lines


def render_write_outcome(outcome: WriteOutcome) -> str:
    """A write outcome as the CLI prints it, stdout and stderr lines in order."""

    return "\n".join(text for text, _err in write_outcome_lines(outcome))


# -- authoring landings and intents --------------------------------------------


def landing_lines(outcome: AuthoringLanded | AuthoringProposed) -> list[str]:
    """Whether an authored change landed, or why it stopped and what to run."""

    if outcome.status == "landed":
        return [f"landed: accepted at {short_oid(outcome.accepted_coordinate.git_oid)}"]
    lines = [outcome.message]
    if outcome.next:
        lines.append(f"next: {outcome.next}")
    return lines


def diagnostic_lines(diagnostics: Iterable[Any]) -> list[str]:
    """A refusal's diagnostics: code, where, the message, then each repair."""

    lines: list[str] = []
    for item in diagnostics:
        where = getattr(item, "offending_element", None)
        lines.append(f"  {item.code}" + (f" at {where}" if where else "") + f": {item.message}")
        for repair in getattr(item, "repair", None) or getattr(item, "repairs", None) or ():
            text = (
                repair.get("description")
                if isinstance(repair, Mapping)
                else getattr(repair, "description", None)
            )
            if text:
                lines.append(f"    repair: {text}")
    return lines


def render_intent(
    *,
    intent_id: object,
    revision: object,
    state: str,
    proposal_id: str | None,
    outcome: AuthoringLanded | AuthoringProposed | None,
    diagnostics: Sequence[Any],
) -> str:
    """An authoring intent: its state, its proposal and outcome, and why it refused."""

    lines = [f"intent {intent_id} revision {revision}: {state}"]
    if proposal_id is not None:
        lines.append(f"proposal: {proposal_id}")
    if outcome is not None:
        lines.extend(landing_lines(outcome))
    lines.extend(diagnostic_lines(diagnostics))
    return "\n".join(lines)
