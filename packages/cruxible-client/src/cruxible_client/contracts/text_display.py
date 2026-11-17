"""One text rendering per result, shared by the CLI and the SDK objects' ``str()``.

The same request gives the same text on every surface: ``cruxible orient``
prints ``render_orient``, and ``print(cx.orient())`` shows it too; a write
outcome, an authoring landing and an accepted coordinate render here once.
``repr()`` stays the full model. Each renderer reads only the result it is
given: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cruxible_client._error_base import printable
from cruxible_client.contracts.claim_type_inputs import CLAIM_TYPE_LINT_TEXT
from cruxible_client.contracts.get_display import get_value_display
from cruxible_client.contracts.get_text import file_shown
from cruxible_client.contracts.public_names import public_name as _public_name

if TYPE_CHECKING:
    from cruxible_client.contracts import (
        NextHealth,
        NextItem,
        NextRepair,
        NextRepairRequirement,
        NextStatus,
    )
    from cruxible_client.contracts.authoring_landing import (
        AuthoringLanded,
        AuthoringProposed,
        AuthoringRefused,
    )
    from cruxible_client.contracts.proposal_summary import (
        ProposalReviewSummary,
        ReviewChange,
        ReviewDefinition,
    )
    from cruxible_client.contracts.vocabulary_file import (
        VocabularyApplyResult,
        VocabularyExportResult,
        VocabularyPlan,
    )
    from cruxible_client.contracts.write import ApprovalNeeded, ChangeOutcome, WriteOutcome

#: How many hex digits of a Git OID name a generation in text, as ``World`` prints it.
SHORT_OID = 12


def short_oid(git_oid: str) -> str:
    """A Git OID as text shows it: its first ``SHORT_OID`` hex digits."""

    return git_oid[:SHORT_OID]


def one_line_repr(result: object) -> str:
    """An SDK result's ``repr()``: the first line of the text the CLI prints for it.

    Ruling S1: a REPL echo is one short line, never a model dump; ``print()``
    shows the whole text and ``model_dump()`` (or the result's fields) the model.
    """

    first = str(result).split("\n", 1)[0].strip()
    return first or type(result).__name__


def short_provider_build(name: str) -> str:
    """A provider build as text shows it: ``NAME/<64 hex>`` (or ``Provider:NAME/...``)
    as ``NAME/<12 hex>``, as coordinates show a Git OID."""

    bare = name.removeprefix("Provider:")
    head, separator, build = bare.rpartition("/")
    if not separator or len(build) <= SHORT_OID:
        return bare
    return f"{head}/{build[:SHORT_OID]}"


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
        f"at {result['coordinate']['git_oid'][:12]} proposed {result['proposed_at']}"
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
            + (
                ""
                if state == "current"
                else "; refresh: cruxible vocabulary export"
                if state == "behind"
                else "; restore its header, or replace it: cruxible vocabulary export --force"
            )
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
                f"seq {row['sequence']} at {row['git_oid'][:12]}  proposed {row['proposed_at']}  "
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
    if result.get("attention_scope") == "head_only":
        lines.append("attention: head only")
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
        if (
            vocabulary_file is not None
            and vocabulary_file["state"] != "current"
            and not attention.get("workspace_counted")
        ):
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


def change_outcome_line(
    change: ChangeOutcome,
    *,
    extra: Sequence[str] = (),
    file_paths: Mapping[str, str] | None = None,
) -> str:
    """One change as a write outcome (and a proposal review) prints it, unindented.

    ``extra`` are further bracketed details, after the outcome's own. A note's
    file reads as its workspace path when ``file_paths`` maps it (``file_shown``).
    """

    target = " ".join(part for part in (change.subject, change.field) if part)
    if change.op == "create":
        line = f"create {change.subject or '(id assigned on submit)'}: {change.resolved}"
    elif change.op == "retire" and change.field is None:
        line = f"retire Subject {change.subject}"
    elif change.op == "retire":
        line = f"retire {target}: {_value_text(change.before)}"
    elif change.op == "add":
        line = f"add {target}: {_value_text(change.after)}"
    elif change.op == "rebind":
        line = f"rebind {target}: {_value_text(change.after)} (value unchanged)"
    elif change.op == "about":
        file = "-" if change.file is None else file_shown(change.file, file_paths)
        where = f"{file}#{'/'.join(change.section or ())}"
        about = ", ".join(change.about or ()) or "(unlinked: retired)"
        line = f"note {where} about {about}  [{change.note}]"
    elif change.before is None:
        line = f"set {target}: {_value_text(change.after)}"
    else:
        line = f"set {target}: {_value_text(change.before)} -> {_value_text(change.after)}"
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
    details.extend(extra)
    return line + (f"  [{'; '.join(details)}]" if details else "")


def write_outcome_lines(
    outcome: WriteOutcome, *, file_paths: Mapping[str, str] | None = None
) -> list[tuple[str, bool]]:
    """A write outcome's lines, each with whether the CLI prints it to stderr.

    A file reads as its workspace path: ``file_paths`` (by default the catalog
    the workspace write read, ``WriteOutcome.file_paths``) maps a catalog name
    to it; the name is shown only where nothing maps it.
    """

    if file_paths is None:
        file_paths = outcome.file_paths
    lines: list[tuple[str, bool]] = []

    def out(text: str, *, err: bool = False) -> None:
        lines.append((text, err))

    head = outcome.status.replace("_", " ")
    out(f"{head} (generation {outcome.coordinate.generation}, {outcome.coordinate.git_oid})")
    if outcome.hint is not None:
        out(outcome.hint)
    for doc in outcome.docs:
        out(
            f"  doc {doc.file or file_shown(doc.source_id or '-', file_paths)} "
            f"{list(doc.section)}: "
            f"{'written' if doc.written else 'not written'} ({doc.after_digest})"
        )
        if doc.refusal:
            out(f"{doc.refusal.code}: {doc.refusal.message}. {doc.refusal.repair}", err=True)
    for item in outcome.catalogued:
        done = "catalogued" if item.written else "would catalogue"
        out(f"  {done} {item.file} (as {item.name})")
    for change in outcome.changes:
        out(f"  {change_outcome_line(change, file_paths=file_paths)}")
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


# -- proposal reviews ----------------------------------------------------------


def _json_text(value: object) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _plain(value: object) -> str:
    """A stored value in a text cell: a string as written, anything else as compact JSON."""

    return printable(value) if isinstance(value, str) else printable(_json_text(value))


def _summary_value_text(value: object) -> str:
    """A bounded review value (``ReviewSummaryValue``) as one cell."""

    if value is None:
        return "-"
    text = _plain(getattr(value, "value", None))
    return text + ("..." if getattr(value, "truncated", False) else "")


def _instant(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _table(rows: Sequence[Sequence[str]], indent: str) -> list[str]:
    """Rows as left-aligned columns, two spaces apart."""

    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        (indent + "  ".join(cell.ljust(width) for cell, width in zip(row, widths))).rstrip()
        for row in rows
    ]


def review_change_lines(
    item: ReviewChange,
    *,
    why: str | None = None,
    file_paths: Mapping[str, str] | None = None,
) -> list[str]:
    """One proposed value change: the write outcome's line, what it cites, and why.

    Cited text that is exactly ``why`` (the proposal's own rationale, already
    printed) is named, not repeated. A cited file reads as its workspace path
    when ``file_paths`` maps its catalog name (``file_shown``).
    """

    extra = [] if item.role in (None, "normative") else [f"role {item.role}"]
    lines = [f"  {change_outcome_line(item.change, extra=extra, file_paths=file_paths)}"]
    for citation in item.citations:
        where = file_shown(citation.source, file_paths, public=True) if citation.source else "-"
        if citation.section:
            where += "#" + "/".join(citation.section)
        elif citation.anchor:
            where += "#" + citation.anchor
        origin = " (own words)" if citation.origin == "self_source" else ""
        lines.append(f"    cites {citation.capture} {printable(where)}{origin}")
        if citation.erased is not None:
            erased = citation.erased
            lines.append(
                f"      erased by {printable(erased.by)} at {_instant(erased.at)}: "
                f"{printable(erased.because)}"
            )
        elif citation.unavailable:
            lines.append("      unavailable: the cited body is missing or damaged here")
        elif citation.text_withheld:
            lines.append("      (cited text withheld: needs body read)")
        elif citation.text is not None and why is not None and citation.text == why.strip():
            lines[-1] += ": the Why text above"
        elif citation.text is not None:
            text_lines = [line for line in citation.text.splitlines() if line.strip()]
            if citation.text_truncated:
                text_lines[-1:] = [(text_lines[-1] if text_lines else "") + " ..."]
            lines.extend(f"      | {printable(line)}" for line in text_lines)
    if item.citations_more:
        lines.append(f"    cites {item.citations_more} more")
    if item.rationale:
        lines.append(f"    because: {printable(item.rationale)}")
    return lines


def review_definition_lines(item: ReviewDefinition) -> list[str]:
    """One proposed definition: a heading, then its typed fields as terse rows."""

    from cruxible_client.contracts.mandate_scope import MANDATE_CHANGE_KIND_ORDER
    from cruxible_client.contracts.proposal_summary import (
        ReviewLineDefinition,
        ReviewMandateDefinition,
        ReviewRunbookDefinition,
        ReviewTriggerDefinition,
    )

    # Names read in their public spelling (`playbill.` -> `cruxible.`); JSON keeps them.
    lines = [f"  {item.kind} {_public_name(item.name)}: {item.action}"]
    rows: list[tuple[str, str]] = []
    detail = item.detail
    if isinstance(detail, ReviewLineDefinition):
        rows.append(("procedure", detail.procedure))
        rows.append(("authority", detail.authority))
        rows.extend(
            ("binding", f"{binding.slot} -> {binding.provider}#{binding.operation}")
            for binding in detail.bindings
        )
        rows.extend(
            ("parameter", f"{name} = {_summary_value_text(value)}")
            for name, value in detail.parameters.items()
        )
        rows.extend(
            ("acquisition", f"{output}: {', '.join(origins) or '(no origins)'}")
            for output, origins in detail.acquisition.items()
        )
        if detail.budgets:
            rows.append(
                ("budget", "  ".join(f"{name}={value}" for name, value in detail.budgets.items()))
            )
        if detail.inbox:
            rows.append(("inbox", ", ".join(detail.inbox)))
        if detail.trigger_input:
            rows.append(("trigger input", detail.trigger_input))
    elif isinstance(detail, ReviewMandateDefinition):
        rows.append(("line", detail.line))
        rows.append(("grants", detail.grants))
        rows.append(("valid", f"{_instant(detail.valid_from)} .. {_instant(detail.expires_at)}"))
        if detail.namespace:
            rows.append(("namespace", ", ".join(detail.namespace)))
        if detail.subjects:
            rows.append(("subjects", detail.subjects))
        if detail.condition:
            rows.append(("condition", _public_name(detail.condition)))
        if detail.subject_creation:
            rows.append(("may create", ", ".join(detail.subject_creation)))
        if detail.suspended:
            rows.append(("suspended", "yes"))
    elif isinstance(detail, ReviewRunbookDefinition):
        if detail.description:
            lines.append(f"    {printable(detail.description)}")
        rows.extend(
            (str(index), _public_name(step)) for index, step in enumerate(detail.steps, start=1)
        )
    elif isinstance(detail, ReviewTriggerDefinition):
        rows.append(("schedule", detail.schedule))
        rows.append(("target", _public_name(detail.target)))
    if rows:
        lines.extend(_table([(label, printable(value)) for label, value in rows], "    "))
    if isinstance(detail, ReviewMandateDefinition) and detail.scope:
        moves = any(scope.value is not None for scope in detail.scope)
        binds = any(scope.binds != "subject" for scope in detail.scope)
        header = ["ClaimType", "create", "revise", "retire"]
        header += ["value"] if moves else []
        header += ["binds"] if binds else []
        table: list[list[str]] = [header]
        for scope in detail.scope:
            row = [_public_name(scope.claim_type).removeprefix("ClaimType:")]
            for kind in MANDATE_CHANGE_KIND_ORDER:
                covered = scope.changes.of(kind)
                row.append("-" if covered is None else "|".join(covered.basis))
            if moves:
                value = scope.value
                row.append(
                    "-"
                    if value is None
                    else " ".join(
                        part
                        for part in (
                            None
                            if value.from_ is None
                            else "from " + "|".join(map(str, value.from_)),
                            None if value.to is None else "to " + "|".join(map(str, value.to)),
                        )
                        if part
                    )
                )
            if binds:
                row.append(scope.binds)
            table.append(row)
        lines.extend(_table(table, "    "))
    for change in item.changed:
        lines.append(
            f"    changed {change.field}: {_summary_value_text(change.before)} -> "
            f"{_summary_value_text(change.after)}"
        )
    return lines


def _why_lines(rationale: object) -> list[str]:
    """The proposal's rationale: its own line breaks kept, continuation lines indented.

    Each line still goes through ``printable``, so authored text cannot forge a
    line of output; a non-text rationale stays one compact cell.
    """

    value = getattr(rationale, "value", None)
    if not isinstance(value, str) or "\n" not in value.strip():
        return [f"Why: {_summary_value_text(rationale)}"]
    text_lines = value.strip().splitlines()
    if getattr(rationale, "truncated", False):
        text_lines[-1] += "..."
    return [
        f"Why: {printable(text_lines[0])}",
        *(f"  {printable(line)}".rstrip() for line in text_lines[1:]),
    ]


def render_review_summary(
    review: ProposalReviewSummary,
    *,
    command: str | None = None,
    file_paths: Mapping[str, str] | None = None,
) -> str:
    """A proposal review summary as ``cruxible proposal review`` prints it.

    ``command`` is the review command a paging hint runs, when the text is shown
    by another verb (``proposal approve``); by default a hint names only flags.
    ``file_paths`` is the workspace file catalog the caller read: a cited file
    reads as its path (``file_shown``).
    """

    run = "" if command is None else f"{command} "
    lines = [f"Proposal: {review.proposal_id}", f"Candidate: {review.candidate_digest}"]
    if review.originating_line is not None:
        lines.append(
            f"Originating Line: {review.originating_line.identity.qualified} "
            f"{review.originating_line.artifact_digest}"
        )
    if review.rationale is not None:
        lines.extend(_why_lines(review.rationale))
    lines.append(f"Required tier: {review.required_tier}; activation: {review.activation_policy}")
    for requirement in review.approval_requirements:
        lines.append(
            f"Approval: {requirement.role}, "
            f"{requirement.minimum_distinct_signers} distinct signer(s)"
        )
    if review.withheld is not None:
        lines.append(
            "Withheld: a proposed Claim cites a Capture this reader may not read; "
            "proposed values, definition fields and cited text are left out"
        )
    if review.prior_refusal_member_count:
        lines.append(
            f"Earlier refusals: {review.prior_refusal_member_count} member(s); --detail for all"
        )
    if review.prior_refusal_coverage is not None:
        coverage = review.prior_refusal_coverage
        lines.append(
            f"Earlier refusal coverage: generation {coverage.indexed_through_generation} "
            f"of {coverage.accepted_generation} at summary build; --detail to refresh"
        )
    for guide in review.guidance:
        title = "" if guide.title is None else f" ({guide.title})"
        lines.append(
            f"Guidance for {_public_name(guide.definition)}: {_public_name(guide.document)}{title}"
        )
    for finding in review.findings:
        value = finding.value
        if isinstance(value, Mapping) and "code" in value:
            where = value.get("offending_element")
            lines.append(
                f"Finding: {value['code']}"
                + (f" at {where}" if where else "")
                + (f": {printable(str(value['message']))}" if value.get("message") else "")
            )
        else:
            lines.append(f"Finding: {_summary_value_text(finding)}")
    if review.findings_next_cursor:
        lines.append(f"More findings: {run}--findings-after {review.findings_next_cursor}")
    lines.append(
        f"Changes: {review.counts.created} created / {review.counts.revised} revised / "
        f"{review.counts.retired} retired"
    )
    for kind, counts in review.subject_kinds.items():
        lines.append(
            f"  {kind}: {counts.created} created / {counts.revised} revised / "
            f"{counts.retired} retired"
        )
    if review.subject_kinds_truncated:
        lines.append(
            f"  ({len(review.subject_kinds)} of {review.subject_kind_count} kinds shown; "
            "--json for all)"
        )
    if review.change_count:
        shown = len(review.changes)
        lines.append(
            f"Values ({review.change_count}):"
            if shown == review.change_count
            else f"Values ({review.change_count}; showing {shown}):"
        )
        why = review.rationale.value if review.rationale is not None else None
        for item in review.changes:
            lines.extend(
                review_change_lines(
                    item, why=why if isinstance(why, str) else None, file_paths=file_paths
                )
            )
        if review.changes_next is not None:
            lines.append(f"More values: {run}--changes-from {review.changes_next}")
        if shown < review.change_count and review.change_counts:
            lines.append("Values by kind and field:")
            lines.extend(
                _table(
                    [
                        (row.subject_kind, row.field or "-", row.op, str(row.count))
                        for row in review.change_counts
                    ],
                    "  ",
                )
            )
            if review.change_counts_truncated:
                lines.append("  (more kinds and fields: --json)")
    if review.definition_count:
        shown = len(review.definitions)
        lines.append(
            f"Definitions ({review.definition_count}):"
            if shown == review.definition_count
            else f"Definitions ({review.definition_count}; showing {shown}):"
        )
        for definition in review.definitions:
            lines.extend(review_definition_lines(definition))
        if review.definitions_next is not None:
            lines.append(f"More definitions: {run}--definitions-from {review.definitions_next}")
    if review.item_reason_count:
        lines.append(f"Item reasons: {review.item_reason_count} (--detail to page them)")
    for group in review.groups:
        lines.append(
            f"Group {group.subject_kind} {group.disposition} "
            f"({sum(group.counts.model_dump().values())}) group={group.group}"
        )
        for row in group.rows:
            lines.append(f"  {row.path}: {_json_text(row.model_dump(mode='json'))}")
        if group.next_cursor:
            lines.append(
                f"  More rows: {run}--summary-group {group.group} --rows-after {group.next_cursor}"
            )
    if review.next_cursor:
        lines.append(f"More groups: {run}--summary-after {review.next_cursor}")
    lines.append(
        f"Stored fields grouped by kind: {run}--json. "
        f"Every member's artifact and delta: {run}--detail"
    )
    return "\n".join(lines)


# -- authoring landings and intents --------------------------------------------


def landing_lines(outcome: AuthoringLanded | AuthoringProposed | AuthoringRefused) -> list[str]:
    """Whether an authored change landed, or why it stopped and what to run."""

    if outcome.status == "landed":
        at = short_oid(outcome.accepted_coordinate.git_oid)
        if outcome.unchanged:
            return [f"unchanged: identical to accepted state at {at}; nothing proposed"]
        return [f"landed: accepted at {at}"]
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
    outcome: AuthoringLanded | AuthoringProposed | AuthoringRefused | None,
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


def vocabulary_plan_lines(plan: VocabularyPlan) -> list[str]:
    """A vocabulary plan as ``cruxible vocabulary plan`` prints it."""

    lines = [f"Vocabulary plan: {plan.path} (based on {plan.based_on.git_oid[:12]})"]
    if plan.claims:
        lines.append("Claims:")
        for claim in plan.claims:
            change = (
                f"becomes {claim.becomes}"
                if claim.action == "re_author"
                else f"retires ({claim.reason})"
            )
            lines.append(f"  {claim.claim} {claim.subject} {claim.field}: {claim.value} {change}")
    changes = plan.changes
    lines.append("Changes:" if changes else "Changes: none")
    for entry in changes:
        lines.append(
            f"  {entry.action} {entry.definition}"
            + (f" ({', '.join(entry.changed)})" if entry.changed else "")
            + (
                f" ({entry.live_claims} live Claim(s) retire with it)"
                if entry.live_claims is not None
                else ""
            )
            + (f" kit={entry.kit}" if entry.kit else "")
        )
    left = [entry for entry in plan.entries if entry.action == "not_in_file"]
    if left:
        lines.append(f"Not in file (left as they are): {len(left)}")
        for entry in left:
            lines.append(f"  {entry.definition}" + (f" kit={entry.kit}" if entry.kit else ""))
    preflight = plan.preflight
    verdict = preflight.verdict.replace("_", " ")
    lines.append(f"Preflight: {verdict}" + (f": {preflight.note}" if preflight.note else ""))
    for item in () if preflight.frontier is None else preflight.frontier.diagnostics:
        lines.append(f"  {item.code} at {item.offending_element}: {item.message}")
    for warning in () if preflight.lint is None else preflight.lint.warnings:
        meaning = CLAIM_TYPE_LINT_TEXT.get(warning.code)
        lines.append(
            f"  lint {warning.code} ({warning.artifact})"
            + ("" if meaning is None else f": {meaning}")
        )
    return lines


def render_vocabulary_export(result: VocabularyExportResult) -> str:
    """What ``cruxible vocabulary export`` prints for one export."""

    state = "Wrote" if result.status == "written" else "Already current:"
    return (
        f"{state} {result.path} ({result.kinds} kind(s), {result.fields} field(s)) "
        f"at {result.based_on.git_oid[:SHORT_OID]}"
    )


def render_vocabulary_apply(result: VocabularyApplyResult) -> str:
    """What ``cruxible vocabulary apply`` prints: the plan, then how it landed."""

    lines = vocabulary_plan_lines(result.plan)
    if result.status == "unchanged":
        lines.append("Nothing to apply.")
    elif result.status == "refused":
        lines.append("Refused: nothing was submitted.")
        if result.moved:
            lines.append(f"Changed since the file's export: {', '.join(result.moved)}")
    elif result.outcome is not None:
        lines.append(str(result.outcome))
    if result.next:
        lines.append(f"Next: {result.next}")
    return "\n".join(lines)


# -- next -----------------------------------------------------------------------------

#: What each placeholder a repair command leaves (`operands_to_add`) stands for.
NEXT_OPERAND_PLACEHOLDER_TEXT: dict[str, str] = {
    "because": "<TEXT> (why, in your words)",
    "file": "<FILE#ANCHOR> (the file and anchor holding the new text)",
    "capture": "<CAP> (the fresh Capture's handle)",
}


def next_input_lines(item: NextItem) -> list[str]:
    """A missing Line input's row: the file, its parameter, its description as written."""

    detail = item.detail if isinstance(item.detail, dict) else {}
    if item.reason != "line_input_missing" or not isinstance(detail.get("relative_path"), str):
        return []
    parameter = detail.get("parameter")
    lines = [
        f"needs file {detail['relative_path']}"
        + (f" (parameter {parameter})" if isinstance(parameter, str) else "")
    ]
    if isinstance(detail.get("description"), str):
        lines.append(f"  {detail['description']}")
    return lines


def next_run_failure_lines(item: NextItem) -> list[str]:
    """A failed Line run's row: the run, its failure code and its cause, if recorded."""

    detail = item.detail if isinstance(item.detail, dict) else {}
    if item.reason != "line_run_failed" or not isinstance(detail.get("run_id"), str):
        return []
    lines = [
        f"run {detail['run_id']} failed"
        + (f" ({detail['failure_code']})" if detail.get("failure_code") else "")
        + (f" at {detail['finalized_at']}" if detail.get("finalized_at") else "")
    ]
    cause = detail.get("cause")
    if isinstance(cause, dict):
        lines.append(
            f"Cause: {cause.get('node_id')}: "
            f"{cause.get('kind') or cause.get('provider_code')}: {cause.get('message')}"
        )
    return lines


def next_claim_about(item: NextItem) -> str | None:
    """What a Claim row is about, as ``kind/id field``: its Subject and predicate.

    A proposal row names every ``kind/id field`` its Claims would write.
    """

    touches = item.detail.get("touches") if isinstance(item.detail, dict) else None
    if isinstance(touches, list) and touches:
        return "; ".join(str(touch) for touch in touches)
    predicate = item.detail.get("predicate") if isinstance(item.detail, dict) else None
    subject = next((ref for ref in item.related_identities if ref.startswith("subjects/")), None)
    if not isinstance(predicate, str) or subject is None:
        return None
    return f"{subject.removeprefix('subjects/').removesuffix('.json')} {predicate}"


def next_requirement_hint(
    requires: NextRepairRequirement | None, *, author: str | None = None
) -> str:
    """What running a withheld repair needs, in one phrase.

    ``author`` names the actor an ``author``-gated repair (a stale proposal's
    readmit) waits on, from the row's ``actor_id``.
    """

    if requires is None:
        return "this caller cannot run it"
    from cruxible_client.contracts.repairs import REPAIR_COMMAND_PATHS

    path = REPAIR_COMMAND_PATHS.get(requires.operation)
    # Name the verb a CLI caller runs; the MCP tool name only when no verb maps.
    label = requires.tool if path is None else f"cruxible {path}"
    needs = []
    if "tier" in requires.because:
        needs.append(f"the {requires.tier} tier")
    if "profile" in requires.because:
        needs.append(f"the {requires.profile} MCP tool profile")
    if "author" in requires.because:
        needs.append(f"the proposal's author ({author or 'unknown actor'})")
    refusal = requires.authoring_refusal
    if "authoring" in requires.because and refusal is not None:
        # The identity repair comes first: no tier helps a caller that cannot author.
        needs.insert(0, f"a caller that can author ({refusal.code}: {refusal.detail})")
    return f"{label} needs " + " and ".join(needs)


def next_repair_hint(repair: NextRepair) -> str:
    """The runnable command, or else the operation and the change it must make."""

    if repair.command is not None:
        return repair.command
    operation = "hand edit" if repair.operation == "hand_edit" else repair.operation
    return f"{operation} {repair.target}: {repair.required_change}"


#: Facet states the status header shows; healthy and unobserved facets stay quiet.
NEXT_STATUS_ATTENTION = {
    "instance": {"decommissioned"},
    "floor": {"missing", "stale"},
    "ledger_mirror": {"behind", "never_published"},
    "provider_lane": {"unavailable"},
    "procedure_catalog": {"missing"},
    "compiler": {"upgrade_available"},
    "line_dispatch": {"due"},
    "consumers": {"lagging"},
    "triggers": {"unscheduled"},
}


def next_status_lines(status: NextStatus) -> list[str]:
    """The environment facets that need attention, above the work rows."""

    lines: list[str] = []
    if status.blocking:
        lines.append("BLOCKING: this instance refuses every write.")
    for facet, states in NEXT_STATUS_ATTENTION.items():
        health: NextHealth = getattr(status, facet)
        if health.state not in states:
            continue
        repair = health.repair
        hint = None if repair is None else repair.command or repair.required_change
        if health.repair_hidden:
            hint = f"(repair withheld: {next_requirement_hint(health.repair_requires)})"
        label = facet.replace("_", " ")
        lines.append(f"Status: {label} {health.state}" + (f"  next={hint}" if hint else ""))
    arms = (
        status.consumers.detail.get("line_enablements")
        if isinstance(status.consumers.detail, dict)
        else None
    )
    if isinstance(arms, dict) and (arms.get("stalled") or arms.get("stopped")):
        lines.append(
            f"Status: line enablements stalled={arms.get('stalled', 0)} "
            f"stopped={arms.get('stopped', 0)}"
            "  next=cruxible orient --section lines"
        )
    return lines


def next_lines(
    *,
    status: NextStatus,
    items: Sequence[NextItem],
    delta_since: str | None = None,
    removed_item_ids: Iterable[str] = (),
    unobserved: Sequence[str] = (),
    brief: bool = False,
    total_items: int | None = None,
    next_cursor: str | None = None,
) -> list[str]:
    """A ``next`` queue as ``cruxible next`` prints it.

    ``unobserved`` are the workspace-derived reasons for each unobserved domain
    (the caller reads the workspace; this reads only what it is given).
    """

    lines = next_status_lines(status)
    if not items:
        lines.append(
            "No changes since the requested queue digest."
            if delta_since is not None
            else "No repair work in the observed domains."
        )
    removed_ids = frozenset(removed_item_ids)
    for item in items:
        change = (
            "removed  "
            if delta_since is not None and item.item_id in removed_ids
            else "added  "
            if delta_since is not None
            else ""
        )
        row = f"{change}{item.severity}  {item.reason}  {item.subject_identity}"
        about = next_claim_about(item)
        if about is not None:
            row += f"  ({about})"
        if item.repair is None:
            author = item.detail.get("actor_id") if isinstance(item.detail, dict) else None
            needs = next_requirement_hint(
                item.repair_requires, author=author if isinstance(author, str) else None
            )
            lines.append(row + ("" if brief else f"  repair withheld: {needs}"))
            if not brief:
                for finding in item.findings:
                    lines.append(
                        f"  also: {finding.severity}  {finding.reason}  {finding.subject_identity}"
                    )
            continue
        if brief:
            lines.append(row + (f"  next={item.repair.command}" if item.repair.command else ""))
            continue
        lines.append(f"{row}  next={item.repair.operation}")
        for line in (*next_run_failure_lines(item), *next_input_lines(item)):
            lines.append(f"  {line}")
        lines.append(f"  repair: {next_repair_hint(item.repair)}")
        for alternative in item.repair.alternatives:
            lines.append(f"  or: {next_repair_hint(alternative)}")
        operands = dict.fromkeys(
            (
                *item.repair.operands_to_add,
                *(op for alt in item.repair.alternatives for op in alt.operands_to_add),
            )
        )
        if operands:
            lines.append(
                "  fill in: " + ", ".join(NEXT_OPERAND_PLACEHOLDER_TEXT[op] for op in operands)
            )
        for finding in item.findings:
            lines.append(
                f"  also: {finding.severity}  {finding.reason}  {finding.subject_identity}"
            )
    if unobserved and not brief:
        lines.append("Unobserved: " + "; ".join(unobserved))
    if next_cursor is not None:
        lines.append(f"Showing {len(items)} of {total_items} rows. Next: --cursor {next_cursor}")
    return lines
