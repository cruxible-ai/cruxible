"""The text ``cruxible get`` prints for one answer, shared with the SDK's ``str()``.

``print(cx.get(ref))`` shows what ``cruxible get REF`` prints for the same
request, at every detail; ``repr()`` stays the model. Each renderer reads only
the result it is given: no request, no read.
"""

from __future__ import annotations

import json
import shlex
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from cruxible_client._error_base import printable
from cruxible_client.contracts.captures import COORDINATOR_SELF_SOURCE_IDENTITY
from cruxible_client.contracts.get_display import (
    GET_CLI_HISTORY_VALUE_WIDTH,
    GET_CLI_VALUE_WIDTH,
    get_value_display,
)
from cruxible_client.contracts.public_names import public_name
from cruxible_client.contracts.write import capture_handle


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def file_shown(name: str, file_paths: Mapping[str, str] | None, *, public: bool = False) -> str:
    """A file citation's catalog name as a person reads it: the file's workspace path.

    The catalog name is the stored identity (signed data and JSON keep it; it
    survives the file moving). ``file_paths`` is the workspace catalog the
    caller read once (``catalog_file_paths``): text shows the path it maps the
    name to now. No mapping (no workspace, an entry since removed, a reader
    over HTTP) shows the name as stored, by its public spelling with
    ``public``. Reads nothing.
    """

    path = file_paths.get(name) if file_paths else None
    if path:
        return path
    return public_name(name) if public else name


#: A one-file-per-name definition family and the kind its names are qualified by.
_NAMED_FAMILIES = {
    "capture-contracts/": "CaptureContract",
    "subject-kinds/": "SubjectKind",
    "query-definitions/": "QueryDefinition",
    "procedures/": "Procedure",
    "procedure-mandates/": "Mandate",
    "lines/": "Line",
    "triggers/": "Trigger",
    "runbooks/": "Runbook",
    "documents/": "Document",
    "notes/": "Note",
    "resolution-contracts/": "ResolutionContract",
}


def definition_path_name(path: str) -> str:
    """A ledger path as the kind-qualified public name a reader types (``ClaimType:p``,
    ``CaptureContract:cruxible.…``, ``Contract:name@N``, ``kind/id``); any other path
    unchanged. Display only: structured answers keep the exact path."""

    if not path.endswith(".json"):
        return path
    stem = path[: -len(".json")]
    family, _, rest = stem.partition("/")
    if not rest:
        return path
    if family == "claim-types" and "/" in rest:
        namespace, _, name = rest.rpartition("/")
        return f"ClaimType:{namespace}.{name}"
    if family in ("contracts", "capabilities") and "/v" in rest:
        name, _, version = rest.rpartition("/v")
        if version.isdigit():
            if family == "capabilities":
                return f"Capability:{name}@{version}"
            return public_name(f"Contract:{name}") + f"@{version}"
    if family == "subjects" and "/" in rest:
        return rest
    if family == "claims" and "/" in rest:
        return rest.rpartition("/")[2]
    named = _NAMED_FAMILIES.get(family + "/")
    if named is None or "/" in rest:
        return path
    # Only the kinds that own reserved names are mapped: a Document may lawfully
    # carry a name a reserved CaptureContract also has.
    return public_name(f"{named}:{rest}") if named == "CaptureContract" else f"{named}:{rest}"


def file_ref_shown(ref: str, file_paths: Mapping[str, str] | None) -> str:
    """``NAME#Heading/Path`` (or a bare NAME) with NAME shown as its workspace path."""

    name, mark, rest = ref.partition("#")
    return file_shown(name, file_paths) + mark + rest


def node_summary_text(summary: Mapping[str, Any]) -> str:
    """One node's run summary as a compact phrase: counts only, never values."""

    kind = summary.get("kind")
    if kind == "extract":
        parts = [plural(summary["records"], "record")]
        for name, noun in (
            ("invalid_records", "invalid record"),
            ("duplicates", "duplicate"),
            ("invalid_fields", "invalid field"),
        ):
            if summary.get(name):
                parts.append(plural(summary[name], noun))
        check = summary.get("complete_when")
        if isinstance(check, Mapping):
            parts.append(
                f"complete_when {check.get('observed')}/{check.get('expected')}"
                + ("" if check.get("held") else " not held")
            )
        if not summary.get("complete"):
            parts.append("incomplete")
        return ", ".join(parts)
    if kind == "transform":
        rows_in, rows_out = summary.get("rows_in"), summary.get("rows_out")
        return (
            f"{summary.get('transform_kind')} "
            f"{'-' if rows_in is None else rows_in} -> "
            f"{'1 value' if rows_out is None else plural(rows_out, 'row')}"
        )
    if kind == "read":
        return plural(summary["rows"], "row")
    if kind == "call":
        digests = summary.get("capture_digests", ())
        shown = " ".join(capture_handle(digest) for digest in digests)
        return plural(summary["captures"], "capture") + (f" {shown}" if shown else "")
    if kind == "guard":
        operands = []
        for operand in summary.get("operands", ()):
            # A side that is not a number or a boolean is never carried: "?".
            left, right = (
                json.dumps(operand[side]) if side in operand else "?" for side in ("left", "right")
            )
            operator = operand["operator"]
            operands.append(
                f"{operator}={left}"
                if operator in {"present", "absent"}
                else (f"{left} {operator} {right}")
            )
        more = summary.get("operands_truncated", 0)
        shown = "; ".join(operands) + (f"; +{more} more" if more else "")
        return f"took {summary['branch']}" + (f" ({shown})" if shown else "")
    if kind == "claims":
        parts = [
            f"{summary[name]} {label}"
            for name, label in (
                ("written", "written"),
                ("revised", "revised"),
                ("inverse_written", "inverse"),
                ("subjects_created", "subjects created"),
                ("re_observed", "re-observed"),
                ("no_op", "no-op"),
                ("retired", "retired"),
                ("surfaced", "surfaced"),
                ("downgraded", "downgraded"),
            )
            if summary.get(name)
        ]
        refused = summary.get("reobservations_refused") or {}
        if refused:
            parts.append(
                "re-observation not recorded "
                + ", ".join(f"{reason}={count}" for reason, count in sorted(refused.items()))
            )
        skipped = summary.get("skipped") or {}
        if skipped:
            parts.append(
                "skipped "
                + ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items()))
            )
        return (", ".join(parts) or "nothing") + f"; {summary.get('delivery')}"
    if kind in {"emit_capture", "post_inbox"}:
        unposted = summary.get("unposted", 0)
        return f"{summary['emitted']} emitted" + (f", {unposted} unposted" if unposted else "")
    if kind == "halt":
        return f"halted ({summary['reason']})" if summary.get("reason") else "halted"
    if kind == "repeat":
        return plural(summary["attempts"], "attempt")
    return str(kind)


def run_node_line(node: Mapping[str, Any]) -> str:
    """``<node> <kind> <verdict>: <summary>`` for one finished node."""

    head = f"{node.get('node')} {node.get('kind') or '?'} {node.get('verdict') or '?'}"
    summary = node.get("summary")
    return head + (f": {node_summary_text(summary)}" if isinstance(summary, Mapping) else "")


def run_egress_line(egress: Mapping[str, Any]) -> str:
    """One terminal of a run: ``<node> <kind> <verdict>`` and the one handle it produced."""

    parts = [str(egress.get("node_id")), str(egress.get("kind")), str(egress.get("verdict"))]
    if egress.get("settle_outcome") == "settled" and egress.get("accepted_git_oid"):
        parts.append(f"settled {str(egress['accepted_git_oid'])[:12]}")
    elif egress.get("proposal_id"):
        parts.append(f"proposal {egress['proposal_id']}")
    if egress.get("refusal_code"):
        parts.append(str(egress["refusal_code"]))
    children = egress.get("children") or ()
    if children:
        parts.append(f"children {len(children)}")
    if egress.get("claims_same_count"):
        parts.append(f"{egress['claims_same_count']} unchanged")
    if egress.get("claims_diagnostics_count"):
        parts.append(plural(int(egress["claims_diagnostics_count"]), "diagnostic"))
    return "  ".join(parts)


def run_outcomes_text(outcomes: Sequence[Mapping[str, Any]]) -> str:
    """A finished run's outcomes as counts by kind; each one is in the run's proof."""

    counts = Counter(str(item.get("event_kind")) for item in outcomes)
    return f"{len(outcomes)} (" + ", ".join(f"{k} {n}" for k, n in sorted(counts.items())) + ")"


def query_definition_lines(definition: Any) -> list[str] | None:
    """An authored QueryDefinition as its clauses, one per line, as its author wrote them.

    ``from``, ``where`` (with any ``now - 3d``), ``hops``, ``fields`` and the
    rest, by their authored names; a default left at its default is not shown,
    and ``query`` and ``description`` are the card's own lines. ``None`` for a
    definition with no authored form (the card shows it as stored).
    """

    if not hasattr(definition, "from_"):
        return None
    spec = definition.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    lines: list[str] = []
    for name, part in spec.items():
        if name in {"query", "description"} or part in (None, [], {}):
            continue
        if isinstance(part, dict):
            text = ", ".join(f"{key}={_card_cell(item)}" for key, item in part.items())
        elif isinstance(part, list):
            text = ", ".join(_card_cell(item) for item in part)
        else:
            text = _card_cell(part)
        lines.append(f"{name}: {printable(text)}")
    return lines


def provider_operation_line(row: dict[str, Any]) -> str:
    """One operation of a Provider card: name, signature, what it serves, where its code is."""

    source = row["source"]
    where = source["path"]
    if source.get("repository"):
        where += f" ({source['repository']}" + (
            f" at {source['revision'][:12]})" if source.get("revision") else ")"
        )
    refusals = ", ".join(
        code + (" (absorbable)" if code in row.get("absorbable", ()) else "")
        for code in row.get("refusals", ())
    )
    return (
        f"{row['operation']}  {row['input']} -> {row['output']} ({row['effect']})  "
        f"serves {', '.join(row['serves'])}\n    {printable(row['description'])}\n"
        f"    source {where}" + (f"\n    refuses {refusals}" if refusals else "")
    )


def backlink_line(link: Any, *, file_paths: Mapping[str, str] | None = None) -> str:
    """One backlink row; a cited section names its file by workspace path (``file_shown``)."""

    where = (
        ""
        if link.file is None
        else f" {file_shown(link.file, file_paths)}#{'/'.join(link.section or ())}"
    )
    extra = [
        item
        for item in (
            link.title,
            link.subject and f"on {link.subject}",
            link.field,
            link.role,
            link.drift,
        )
        if item
    ]
    return f"{link.relation} {link.source_kind} {link.source}{where}" + (
        f"  [{'; '.join(extra)}]" if extra else ""
    )


def note_link_text(link: Any, *, file_paths: Mapping[str, str] | None = None) -> str:
    """One note pointer: its section locator (the file by workspace path) and drift."""

    if link.file is None:
        return f"{link.note}  (section withheld)"
    where = f"{file_shown(link.file, file_paths)}#{'/'.join(link.section or ())}"
    return where + (f"  [{link.drift}]" if link.drift else "")


def section_read_line(link: Any, *, file_paths: Mapping[str, str] | None = None) -> str | None:
    """``cruxible section`` for one note's section, the file named by its path."""

    if link.file is None:
        return None
    locator = f"{file_shown(link.file, file_paths)}#{'/'.join(link.section or ())}"
    return f"cruxible section {shlex.quote(locator)}"


def get_value_text(
    value: object,
    *,
    width: int = GET_CLI_VALUE_WIDTH,
    evidence_hint: bool = True,
    claim: object = None,
) -> str:
    """A value as the CLI previews it; ``claim`` (one Claim ID) makes a cut name its read."""

    return get_value_display(
        value,
        width=width,
        evidence_hint=evidence_hint,
        claim=claim if isinstance(claim, str) else None,
    ).text


_WHY_SUMMARY_MAX_ITEMS = 5


def _why_scalar_text(value: object) -> str:
    text = str(value)
    return public_name(text) if "playbill." in text else text


def why_summary_lines(value: Any, *, key: str | None = None, depth: int = 0) -> list[str]:
    """``get --detail why`` as a readable, bounded summary; ``--json`` keeps the whole payload.

    The served explanation is a deeply nested typed envelope (law evidence,
    verdict detail, admission accounts, source handles, ...); printed as raw
    JSON it runs to hundreds of lines. This walks it generically -- every key
    still appears, nothing is renamed -- but bounds each list to a handful of
    items so the common case is a short, readable summary instead of a dump.
    """

    indent = "  " * depth
    if isinstance(value, Mapping):
        items = [(k, v) for k, v in value.items() if k != "tag" and v not in (None, [], {}, ())]
        if not items:
            return []
        lines = [] if key is None else [f"{indent}{key}:"]
        for item_key, item_value in items:
            lines.extend(why_summary_lines(item_value, key=item_key, depth=depth + 1))
        return lines
    if isinstance(value, list | tuple):
        if not value:
            return []
        shown = value[:_WHY_SUMMARY_MAX_ITEMS]
        overflow = len(value) - len(shown)
        if all(isinstance(item, Mapping) for item in shown):
            header = [] if key is None else [f"{indent}{key} ({len(value)}):"]
            lines = list(header)
            flat = all(
                not isinstance(v, Mapping | list | tuple) for item in shown for v in item.values()
            )
            for item in shown:
                if flat:
                    # Every field is a scalar: one joined line per item, as
                    # get's other card rendering already does.
                    lines.append(
                        f"{indent}  " + "  ".join(_why_scalar_text(v) for v in item.values())
                    )
                else:
                    lines.extend(why_summary_lines(item, depth=depth + 1))
            if overflow:
                lines.append(f"{indent}  ... and {overflow} more")
            return lines
        rendered = ", ".join(_why_scalar_text(item) for item in shown)
        if overflow:
            rendered += f", ... and {overflow} more"
        return [f"{indent}{key}: {rendered}" if key is not None else f"{indent}{rendered}"]
    text = _why_scalar_text(value)
    return [f"{indent}{key}: {text}" if key is not None else f"{indent}{text}"]


_ATTESTATION_COUNT_WORDS = {
    "cites_admitted_capture": "yes (cites Captures this Claim admits)",
    "consequence_policy": "yes (consequence policy rule {rule})",
    "cites_no_capture": "no (cites no Capture)",
    "capture_not_admitted": "no (cites a Capture this Claim does not admit)",
    "not_accepted": "no (recorded, not accepted: the verdict reads accepted attestations)",
    "not_current": "no (not current)",
}


def attestation_counts_text(attestation: Any) -> str:
    """Whether one attestation counts toward the Claim's verdict, and why, in words."""

    return _ATTESTATION_COUNT_WORDS[attestation.counts_reason].format(rule=attestation.rule)


_UNLOCATED_OWN_WORDS_SOURCES = frozenset({"unknown", COORDINATOR_SELF_SOURCE_IDENTITY})


def citation_label(capture: Any, *, file_paths: Mapping[str, str] | None = None) -> str:
    """What a cited Capture is, in words, and whether the verdict reads it.

    Own words say how much text they hold, and own words found in a workspace
    file name it: their source is the file's logical id (shown as the file's
    workspace path when ``file_paths`` maps it), where unlocated own words name
    their producer (a qualified ``Principal:``) or nothing. A copy that only
    records where cited text sits reads as that location, never as rejected
    evidence.
    """

    status = capture.status.replace("_", " ")
    if capture.origin == "self_source":
        source = str(capture.source or "")
        located = bool(source) and ":" not in source
        located = located and source not in _UNLOCATED_OWN_WORDS_SOURCES
        where = f" in {file_shown(source, file_paths, public=True)}" if located else ""
        size = "" if capture.byte_length is None else f" ({capture.byte_length} bytes)"
        return f"own words{where}{size}: {status}"
    if capture.role == "copy":
        where = f"location in {file_shown(str(capture.source), file_paths, public=True)}"
        return where if capture.status == "not_evidence" else f"{where}: {status}"
    return f"{capture.role}: {status}"


def history_value(value: Any) -> str:
    return get_value_text(value, width=GET_CLI_HISTORY_VALUE_WIDTH)


def reason_text(reason: Any) -> str:
    """One revision's reason on one escaped line: its item's words, else its change set's."""

    if reason.kind == "procedure_item":
        text = f"{printable(reason.reason)}  (Procedure {reason.procedure}, run {reason.run})"
    else:
        text = printable(reason.text)
    return text + ("  (truncated)" if reason.truncated else "")


def effective_text(start: datetime | None, end: datetime | None) -> str:
    """A Claim's effective period as ``effective <from>..<until>``; empty when it has none."""

    if start is None and end is None:
        return ""
    return "effective " + "..".join("" if at is None else at.isoformat() for at in (start, end))


def _by(item: Any) -> str:
    """Who accepted a revision: ``by owner``, then ``, approved by alice`` when anyone signed."""

    approvers = [name for name in getattr(item, "approved_by", ()) if name]
    shown = f"by {item.actor or '-'}"
    return shown + (f", approved by {', '.join(approvers)}" if approvers else "")


def history_lines(history: Any) -> list[str]:
    """Revisions newest first; a Subject's field events grouped by field, each its timeline."""

    lines: list[str] = []
    for revision in history.revisions:
        value = "" if revision.value is None else f"  = {history_value(revision.value)}"
        change = "" if revision.change is None else f"  {revision.change}"
        if revision.retirement is not None:
            change += f" ({revision.retirement})"
        period = effective_text(revision.effective_from, revision.effective_until)
        lines.append(
            f"rev {revision.revision}  seq {revision.sequence} at {revision.git_oid}  "
            f"proposed {revision.proposed_at}  {_by(revision)}{change}{value}"
            + (f"  {period}" if period else "")
        )
        if revision.reason is not None:
            lines.append(f"    reason: {reason_text(revision.reason)}")
        for step in revision.next:
            lines.append(f"next: {step}")
    for review in getattr(history, "measurement_reviews", ()):
        lines.append(
            f"{review.measurement_name}: standing={review.standing_resolution_id or 'none'}"
        )
        for judgment in review.history:
            lines.append(
                f"  {judgment.resolution_id}: {judgment.verdict} by {judgment.reviewer_id} "
                f"at {judgment.recorded_at.isoformat()}  reason: {judgment.reason}"
            )
        if review.history_truncated:
            lines.append(f"  showing {len(review.history)} of {review.history_total} judgments")
    if history.kind != "subject":
        return lines
    by_field: dict[str, list[Any]] = {}
    for event in history.events:
        by_field.setdefault(event.field, []).append(event)
    for name, events in by_field.items():
        lines.append(printable(name))
        for event in events:
            if event.change == "changed":
                what = f"changed  {history_value(event.before)} -> {history_value(event.value)}"
            elif event.change == "retired":
                ended = "" if event.retirement is None else f" ({event.retirement})"
                what = f"retired{ended}  {history_value(event.value)}"
            elif event.change == "rebound":
                what = (
                    f"rebound  {history_value(event.value)}  "
                    f"({', '.join(event.replaced) or '-'} -> {', '.join(event.by) or '-'})"
                )
            else:
                what = f"{event.change}  {history_value(event.value)}"
                if getattr(event, "contended", False):
                    what += "  (contends with a live value)"
            lines.append(
                f"  seq {event.sequence}  {event.git_oid}  proposed {event.proposed_at}  "
                f"{_by(event)}  {what}  [{event.claim}]"
            )
            if event.reason is not None:
                lines.append(f"      reason: {reason_text(event.reason)}")
            for step in event.next:
                lines.append(f"      next: {step}")
    return lines


def proposed_reason_lines(reason: Any) -> list[str]:
    """The Procedure item's own reason for the accepted revision, and who proposed it.

    Authored text goes through ``printable``: a reason cannot forge a line of
    daemon output, and a multi-line reason stays one escaped line.
    """

    return [
        f"reason: {printable(reason.reason)}" + ("  (truncated)" if reason.truncated else ""),
        f"proposed by {printable(reason.procedure)} run {printable(reason.run)} "
        f"terminal {printable(reason.terminal)} in proposal {printable(reason.proposal)}",
    ]


def _card_cell(part: object) -> str:
    """One card cell: a reserved stored name in its public spelling, anything else as text.

    S3: a boolean reads ``true``/``false`` and a missing value ``none``, never a
    Python repr; a nested value reads as compact JSON.
    """

    if isinstance(part, str):
        return public_name(part)
    if isinstance(part, bool):
        return "true" if part else "false"
    if part is None:
        return "none"
    if isinstance(part, dict | list):
        return json.dumps(part, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(part)


def render_get(
    result: Any,
    *,
    evidence_erased: bool = False,
    file_paths: Mapping[str, str] | None = None,
) -> str | None:
    """A ``get`` answer as ``cruxible get`` prints it, values first, per kind.

    ``evidence_erased`` marks a stale Claim's verdict as read from erased
    evidence (the CLI learns it with one more read; ``str()`` never reads).
    ``file_paths`` is the workspace file catalog the caller read: evidence
    sources and backlinked sections name a catalogued file by its path
    (``file_shown``). A body is its text, with no trailing newline added.
    ``None`` for a proof, which the CLI prints as JSON. Reads nothing.
    """

    lines: list[str] = []
    if result.card is not None:
        card = result.card.model_dump(
            mode="json", by_alias=result.kind in {"procedure", "capability"}
        )
        if isinstance(card.get("evidence"), list):
            card["evidence"] = [public_name(str(name)) for name in card["evidence"]]
        for rule in card.get("evidence_rules") or ():
            if isinstance(rule, dict) and isinstance(rule.get("contracts"), list):
                rule["contracts"] = [public_name(str(name)) for name in rule["contracts"]]
        nexts = card.pop("next", [])
        if result.kind == "claim":
            lines.append(
                f"{card['subject']}  {card['predicate']} = "
                f"{get_value_text(card['value'], claim=card.get('claim'))}"
            )
            lines.append(
                f"{card['claim']}  verdict={card['verdict']}"
                + (" (evidence erased)" if evidence_erased else "")
                + f" status={card['status']} "
                f"revision={card['revision']}"
                + (f" proposed={card['proposed_at']}" if card.get("proposed_at") else "")
            )
            period = effective_text(result.card.effective_from, result.card.effective_until)
            if period:
                lines.append(period)
            for contender in card.get("contenders", []):
                lines.append(
                    f"  contender {contender['claim']} = "
                    f"{get_value_text(contender['value'], claim=contender['claim'])} "
                    f"[{contender['verdict']}]"
                )
        elif result.kind == "subject":
            lines.append(
                f"{card['subject']}  ({card['lifecycle']}, {card['incoming_count']} incoming)"
            )
            # A pair's ends, then its fields, then the pairs it is an end of.
            ends = card.get("ends", ())
            rows = card["claims"]
            width = max(
                (len(row["predicate"]) for row in rows),
                default=0,
            )
            width = max([width, *(len(end["part"]) for end in ends)])
            for end in ends:
                flags = f"  [{', '.join(end['flags'])}]" if end.get("flags") else ""
                claim = f"  {end['claim']}" if end.get("claim") else ""
                lines.append(f"  {end['part'].ljust(width)}  -> {end['subject']}{claim}{flags}")
            for row in rows:
                flags = f"  [{', '.join(row['flags'])}]" if row["flags"] else ""
                # A field holding only nones has no value to show; each none
                # (an inverse Claim) is listed under it, never as a value.
                shown = (
                    get_value_text(row["value"], claim=row["claim"])
                    if row["claim"]
                    else "(no value)"
                )
                lines.append(f"  {row['predicate'].ljust(width)}  {shown}{flags}")
                for none in row.get("nones", ()):
                    unit = "" if none.get("of") is None else f" of {get_value_text(none['of'])}"
                    marks = f", {', '.join(none['flags'])}" if none.get("flags") else ""
                    lines.append(
                        f"  {''.ljust(width)}  none{unit}  {none['claim']} "
                        f"[{none['verdict']}{marks}]"
                    )
            for group in card.get("pairs", ()):
                more = ", ..." if group.get("more") else ""
                hidden = f"  ({group['hidden']} hidden)" if group.get("hidden") else ""
                lines.append(
                    f"pairs {group['kind']} by {group['part']}: "
                    + (", ".join(group["pairs"]) or "-")
                    + more
                    + hidden
                )
            for item in card.get("pending", ()):
                # Not accepted yet: the fields above keep their accepted values.
                lines.append(
                    f"pending {item['proposal']}: {', '.join(item['fields'])} (by {item['by']})"
                )
            # Notes about it, as pointers: the sections stay in the workspace.
            for link in getattr(result.card, "notes", ()):
                lines.append("note " + note_link_text(link, file_paths=file_paths))
            if getattr(result.card, "notes_total", None) is not None:
                lines.append(f"({result.card.notes_total} notes in all)")
        else:
            for key, value in card.items():
                if key == "nodes" and result.kind == "procedure_run" and value:
                    lines.append("nodes:")
                    for node in value:
                        lines.append("  " + run_node_line(node))
                    continue
                # A run's text stays short: outcomes as counts, each terminal on
                # one line, the result cut; its proof (named in next:) has all.
                if key == "outcomes" and result.kind == "procedure_run" and value:
                    lines.append(f"outcomes: {run_outcomes_text(value)}")
                    continue
                if key == "terminal_egress" and result.kind == "procedure_run" and value:
                    lines.append("terminals:")
                    for egress in value:
                        lines.append("  " + run_egress_line(egress))
                    continue
                if key == "result" and result.kind == "procedure_run" and value is not None:
                    lines.append(
                        "result: "
                        + get_value_text(_card_cell(value), width=200, evidence_hint=False)
                    )
                    continue
                if key == "definition" and result.kind == "query" and value is not None:
                    clauses = query_definition_lines(result.card.definition)
                    if clauses is not None:
                        lines.extend(clauses)
                        continue
                if key == "authored" and result.kind == "procedure" and value is not None:
                    from cruxible_client.contracts.refs import concise

                    # Review rendering: each typed reference shown as its $... text.
                    lines.append(json.dumps(concise(value), ensure_ascii=False, indent=2))
                    continue
                if value in (None, [], {}):
                    continue
                if key == "closed_reason" and result.kind == "proposal":
                    # A reviewer's reason is read whole: it is why the author is here.
                    lines.append(f"closed_reason: {printable(value)}")
                    continue
                if key == "schedule" and result.kind == "trigger" and card.get("sched"):
                    # S3: the compact ``sched`` line says it; JSON keeps the schedule.
                    continue
                if key == "trigger" and result.kind == "line" and card.get("sched"):
                    continue
                if key == "operations" and result.kind == "provider":
                    lines.append("operations:")
                    for row in value:
                        lines.append("  " + provider_operation_line(row))
                    continue
                # Reserved names read in their public spelling throughout the
                # card, as its next: lines do; the JSON keeps the stored ids.
                if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                    lines.append(f"{key}:")
                    for item in value:
                        lines.append("  " + "  ".join(_card_cell(part) for part in item.values()))
                    continue
                if isinstance(value, dict):
                    value = "  ".join(
                        f"{name}="
                        + (
                            ", ".join(map(_card_cell, part))
                            if isinstance(part, list)
                            else _card_cell(part)
                        )
                        for name, part in value.items()
                    )
                elif isinstance(value, str):
                    value = public_name(value)
                if isinstance(value, list):
                    # Names are never cut: a truncated name is not a usable reference.
                    lines.append(f"{key}: {printable(', '.join(map(_card_cell, value)))}")
                    continue
                lines.append(f"{key}: {get_value_text(value, width=200, evidence_hint=False)}")
        if card.get("flags"):
            lines.append(f"flags: {', '.join(card['flags'])}")
        for step in nexts:
            lines.append(f"next: {step}")
        return "\n".join(lines)
    if result.evidence is not None:
        # Evidence carries the whole value; a summary card cuts a long one.
        whole = result.evidence.model_dump(mode="json")["value"]
        if isinstance(whole, str):
            lines.append(f"value: {printable(whole)}")
        else:
            lines.append(f"value: {printable(json.dumps(whole, ensure_ascii=False))}")
        for capture in result.evidence.captures:
            erased = capture.erased
            lines.append(
                f"capture {capture.capture}  {public_name(capture.contract)} "
                f"v{capture.version}  "
                f"source={file_shown(capture.source, file_paths, public=True)}  "
                f"observed={capture.observed_at.isoformat()}  "
                f"{citation_label(capture, file_paths=file_paths)}"
                + (
                    ""
                    if erased is None
                    else f"  body erased {erased.at.isoformat()} by {printable(erased.by)}: "
                    f"{printable(erased.because)}"
                )
            )
            if capture.passage is not None:
                # The cited passage, as proposal review prints it.
                text_lines = [line for line in capture.passage.splitlines() if line.strip()]
                if capture.passage_truncated:
                    text_lines[-1:] = [(text_lines[-1] if text_lines else "") + " ..."]
                lines.extend(f"  | {printable(line)}" for line in text_lines)
        for attestation in result.evidence.attestations:
            lines.append(
                f"attestation {attestation.stance} by {attestation.principal} "
                f"at {attestation.at.isoformat()}  "
                f"{'accepted' if attestation.record == 'accepted' else 'recorded'}  "
                f"counts toward verdict: {attestation_counts_text(attestation)}"
            )
        if result.evidence.rationale:
            lines.append(f"rationale: {printable(result.evidence.rationale)}")
        return "\n".join(lines)
    if result.history is not None:
        lines.extend(history_lines(result.history))
        return "\n".join(lines)
    if result.runs is not None:
        if not result.runs.runs:
            lines.append(f"(no runs of {result.runs.line})")
        for run in result.runs.runs:
            lines.append(
                f"{run.run}  {run.status}  started {run.started_at.isoformat()}  "
                f"nodes_done={run.nodes_done}  {run.procedure}"
            )
        return "\n".join(lines)
    if result.backlinks is not None:
        if not result.backlinks.links:
            target = file_ref_shown(result.backlinks.target, file_paths)
            lines.append(f"(no backlinks into {target})")
        for link in result.backlinks.links:
            lines.append(backlink_line(link, file_paths=file_paths))
        return "\n".join(lines)
    if getattr(result, "notes", None) is not None:
        if not result.notes.notes:
            lines.append(f"(no notes about {result.notes.target})")
        reads: list[str] = []
        for link in result.notes.notes:
            lines.append(f"{link.note}  {note_link_text(link, file_paths=file_paths)}")
            read = section_read_line(link, file_paths=file_paths)
            if read is not None:
                reads.append(read)
        lines.extend(f"next: {read}" for read in reads)
        return "\n".join(lines)
    if result.pairs is not None:
        if not result.pairs.groups:
            lines.append("(no pairs name this Subject)")
        for group in result.pairs.groups:
            for pair in group.pairs:
                lines.append(f"{pair}  {group.kind}  by {group.part}")
            if group.hidden:
                lines.append(f"({group.hidden} hidden {group.kind} by {group.part})")
        return "\n".join(lines)
    if result.incoming is not None:
        if not result.incoming.claims:
            lines.append("(nothing points at this Subject)")
        for row in result.incoming.claims:
            qualifier = "" if row.qualifier is None else f"[{row.qualifier}]"
            lines.append(f"{row.subject}  {row.predicate}{qualifier}  {row.claim}")
        return "\n".join(lines)
    if result.body is not None:
        # The bytes as served; the CLI adds where a ranged read continues, on stderr.
        body = result.body
        return str(body.text if body.text is not None else body.content_base64 or "")
    if result.why is not None:
        for line in why_summary_lines(result.why) or ["(nothing to explain)"]:
            lines.append(line)
        if getattr(result, "reason", None) is not None:
            lines.extend(proposed_reason_lines(result.reason))
        return "\n".join(lines)
    return None


def claim_view_lines(view: Any) -> list[str]:
    """A Claim summary the SDK read from the Claim's proof (``ClaimView``).

    The CLI's summary is the Claim card; this view carries no card status or
    contenders, so it prints what it holds, in the card's first two lines' form.
    """

    subject = str(view.subject).removeprefix("subjects/").removesuffix(".json")
    return [
        f"{subject}  {view.predicate} = {get_value_text(view.value, claim=view.claim)}",
        f"{view.claim}  verdict={view.verdict} lifecycle={view.lifecycle_state} "
        f"revision={view.revision}",
    ]


def based_on_text(claim: str, subject: str | None, field: str | None) -> str:
    """A ``based_on`` pin as one cell: the Claim id, then its Subject and field when known."""

    about = " ".join(part for part in (subject, field) if part)
    return printable(f"{claim} {about}" if about else claim)


def claim_proof_based_on(proof: Mapping[str, Any]) -> tuple[str, ...]:
    """The Claims a Claim proof's ``based-on`` pins name, from its lifecycle fact."""

    found: list[str] = []
    for fact in proof.get("facts", ()):
        if not isinstance(fact, Mapping) or fact.get("schema_id") != "cruxible.claim.lifecycle":
            continue
        value = fact.get("value")
        pins = value.get("pins", ()) if isinstance(value, Mapping) else ()
        for pin in pins:
            if not isinstance(pin, Mapping) or pin.get("role") != "based-on":
                continue
            target = pin.get("target")
            name = target.get("name") if isinstance(target, Mapping) else None
            if isinstance(name, str) and name not in found:
                found.append(name)
    return tuple(found)


def claim_proof_summary(
    proof: Mapping[str, Any],
    *,
    based_on_context: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, Any]:
    """A Claim's proof as a summary: its verdict, each cited Capture, and its based_on pins.

    ``get CLM --detail proof`` answers this by default (S1); ``--detail
    proof-full`` (MCP ``proof_full``) answers the whole envelope and facts.
    Each Capture line is ``CAP-<12 hex>  <status>  <contract>  <role>``.
    ``based_on`` lists each pinned Claim, with its Subject and field when
    ``based_on_context`` (Claim id to (Subject, field)) knows them.
    """

    verdict = ""
    for fact in proof.get("facts", ()):
        if isinstance(fact, Mapping) and fact.get("schema_id") == "cruxible.claim.current_verdict":
            value = fact.get("value")
            verdict = str(value.get("verdict", "")) if isinstance(value, Mapping) else ""
    captures = [
        "  ".join(
            (
                "CAP-" + str(item.get("capture_digest", "")).removeprefix("sha256:")[:12],
                str(item.get("status", "")),
                str(item.get("capture_contract_identity", "")),
                str(item.get("citation_role", "")),
            )
        )
        for item in proof.get("admission_accounts", ())
        if isinstance(item, Mapping)
    ]
    summary: dict[str, Any] = {"verdict": verdict, "captures": captures}
    pinned = claim_proof_based_on(proof)
    if pinned:
        context = based_on_context or {}
        rows: list[dict[str, str]] = []
        for claim in pinned:
            subject, field = context.get(claim, (None, None))
            row = {"claim": claim}
            if subject:
                row["subject"] = subject
            if field:
                row["field"] = field
            rows.append(row)
        summary["based_on"] = rows
    summary["full"] = "detail proof_full"
    return summary


def claim_proof_summary_lines(
    proof: Mapping[str, Any],
    *,
    based_on_context: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> list[str]:
    """The summary as the CLI prints it."""

    summary = claim_proof_summary(proof, based_on_context=based_on_context)
    lines = [f"verdict: {summary['verdict'] or '-'}"]
    lines.extend(f"  {line}" for line in summary["captures"])
    if not summary["captures"]:
        lines.append("  (cites no Capture)")
    for row in summary.get("based_on", ()):
        lines.append(
            "based on: " + based_on_text(row["claim"], row.get("subject"), row.get("field"))
        )
    lines.append("(--detail proof-full prints the whole envelope and facts)")
    return lines
