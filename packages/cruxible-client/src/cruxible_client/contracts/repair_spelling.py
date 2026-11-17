"""Every repair in the spelling of the client that shows it.

One renderer for every surface. A ``next`` row and a refusal both carry a
served operation id (``cruxible.proposal.readmit``) with wire arguments; the
CLI prints the ``cruxible ...`` command that performs it, the MCP server the
tool call with that tool's own argument names, and the SDK the ``Cruxible``
call. An operation with no form on a client says which client has it, rather
than printing another client's spelling.

``next_repair_command`` is the exact composer the ``next`` queue uses (it
returns None where a row's command cannot be composed, and the row then carries
no command). ``render_repair`` is the refusal path: it tries the same composer
first and falls back to ``REPAIR_SPELLINGS``, the declared spelling of every
other served operation, filling a required operand the repair does not name
with a placeholder to replace.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from cruxible_client.contracts.codes import normalize_code
from cruxible_client.contracts.placeholders import placeholder
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.repairs import (
    REPAIR_COMMAND_PATHS,
    HandEditRepair,
    RepairOperation,
    ServedRepair,
)

RepairSurface: TypeAlias = Literal["cli", "mcp", "sdk"]

# Each of these needs a local file. The queue knows the path only if the row
# carried it, so the placeholder is filled from the arguments when they name it
# and dropped -- with the flag that introduces it -- when they do not. A bare
# `PAYLOAD_FILE` left in the line is not a hint, it is an unrunnable command
# presented as a runnable one, which is the one thing `command` must never be.
_REPAIR_COMMAND_OPERANDS: Mapping[str, tuple[str, ...]] = {
    "cruxible.authoring.bind": ("--payload-file", "PAYLOAD_FILE"),
}
_REPAIR_COMMAND_PLACEHOLDERS: Mapping[str, str] = {
    "PAYLOAD_FILE": "payload_file",
}


def _revision_name(source: str) -> str:
    """The proposal name a modified-Document repair suggests for its source."""

    return f"revise-{source}"


def _repair_paths(values: Mapping[str, object]) -> list[str]:
    """The workspace pages a page-scoped repair names, or none."""

    paths = values.get("paths")
    if not isinstance(paths, (list, tuple)):
        return []
    return [path for path in paths if isinstance(path, str) and path]


def _repair_operands(operation: str, values: Mapping[str, object]) -> list[str]:
    """Render one operation's file operands, filling or dropping each placeholder."""

    rendered: list[str] = []
    pending_flag: str | None = None
    for operand in _REPAIR_COMMAND_OPERANDS[operation]:
        key = _REPAIR_COMMAND_PLACEHOLDERS.get(operand)
        if key is None:
            pending_flag = operand
            continue
        supplied = values.get(key)
        if not isinstance(supplied, str) or not supplied:
            pending_flag = None
            continue
        if pending_flag is not None:
            rendered.append(pending_flag)
            pending_flag = None
        rendered.append(shlex.quote(supplied))
    return rendered


def _restated_set(values: Mapping[str, object]) -> dict[str, object] | None:
    """The set call a `cruxible.set` repair names, from its arguments, or None."""

    subject, field, value = values.get("subject"), values.get("field"), values.get("value")
    if not isinstance(subject, str) or not isinstance(field, str):
        return None
    if not isinstance(value, str | int | bool):
        return None
    restated: dict[str, object] = {"subject": subject, "field": field, "value": value}
    role = values.get("role")
    if isinstance(role, str):
        restated["role"] = role
    capture = values.get("capture_digest")
    contract = values.get("evidence_contract")
    if isinstance(capture, str):
        restated["evidence"] = {"kind": "capture", "capture": capture}
    elif isinstance(contract, str):
        restated["evidence"] = {"kind": "contract", "contract": contract}
    return restated


def _write_changes(values: Mapping[str, object]) -> list[dict[str, object]]:
    changes = values.get("changes")
    if not isinstance(changes, list | tuple):
        return []
    return [dict(item) for item in changes if isinstance(item, Mapping)]


def _submitted_payload(values: Mapping[str, object]) -> Mapping[str, object] | None:
    """The authoring input a `cruxible.authoring.submit` repair carries in full, or None."""

    payload = values.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("kind"), str):
        return None
    return payload


#: SDK `ChangeSetDraft.line` keywords for a Line input's fields, with each default.
_SDK_LINE_KEYWORDS: tuple[tuple[str, str, object], ...] = (
    ("name", "name", None),
    ("procedure_name", "procedure", None),
    ("bindings", "bindings", {}),
    ("acquisition", "acquisition", {}),
    ("max_authority", "max_authority", None),
    ("trigger_input", "trigger_input", None),
    ("parameters", "parameters", {}),
    ("budgets", "budgets", None),
    ("occurrence_epoch", "occurrence_epoch", 1),
    ("retire", "retire", False),
)


def _rebind_section(values: Mapping[str, object]) -> str | None:
    """A section citation's ``FILE#Heading/Path``, when the repair names it."""

    file, section = values.get("section_file"), values.get("section")
    if not isinstance(file, str) or not file or not isinstance(section, list | tuple):
        return None
    if not all(isinstance(item, str) for item in section):
        return None
    return f"{file}#{'/'.join(section)}"


def _rebind_change(values: Mapping[str, object]) -> dict[str, object] | None:
    """The typed ``rebind`` change a re-bind repair names, as far as the row knows it."""

    claim_id = values.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id:
        return None
    change: dict[str, object] = {"op": "rebind", "target": claim_id}
    citation = values.get("citation")
    if isinstance(citation, str) and citation:
        change["citation"] = citation
    file, section = values.get("section_file"), values.get("section")
    if isinstance(file, str) and isinstance(section, list | tuple):
        change["evidence"] = {"kind": "section", "file": file, "section": list(section)}
    return change


def _cli_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


#: What each operand the caller adds reads as in a command, for it to replace.
#: Each is refused if sent unchanged (``placeholders``); the CLI quotes it, so
#: the shell passes it through to that check.
_OPERAND_PLACEHOLDERS: Mapping[str, str] = {
    "because": placeholder("TEXT"),
    "file": placeholder("FILE#ANCHOR"),
    "capture": placeholder("CAP"),
    # The Runbook or internal action a missed-instants repair runs.
    "runbook": placeholder("NAME"),
    "action": placeholder("NAME"),
}


#: The observation that settles a prediction is the settler's to choose.
_OBSERVATION_PLACEHOLDER = placeholder("CLAIM_ID")


def _quoted(name: str) -> str:
    """One operand placeholder as a CLI word."""

    return shlex.quote(_OPERAND_PLACEHOLDERS[name])


def _because_flag(operands: tuple[str, ...]) -> list[str]:
    return ["--because", _quoted("because")] if "because" in operands else []


def _because(operands: tuple[str, ...]) -> dict[str, object]:
    return {"because": _OPERAND_PLACEHOLDERS["because"]} if "because" in operands else {}


def _mcp_call(tool: str, **arguments: object) -> str:
    rendered = ", ".join(f"{key}={json.dumps(value)}" for key, value in arguments.items())
    return f"{tool}({rendered})"


def _mcp_repair_call(
    operation: str,
    *,
    arguments: object,
    operands: tuple[str, ...] = (),
) -> str | None:
    """Render one repair as the MCP tool call that performs it, or None.

    A repair whose operands are local files or structured bases the queue does
    not hold renders no call rather than a call that cannot be made.
    """

    values = arguments if isinstance(arguments, Mapping) else {}

    def text(key: str) -> str | None:
        value = values.get(key)
        return value if isinstance(value, str) and value else None

    if operation == "cruxible.compiler.upgrade":
        return _mcp_call("cruxible_compiler_upgrade")
    if operation == "cruxible.line.enable" and text("line"):
        return _mcp_call("cruxible_line_enable", line=text("line"))
    if operation == "cruxible.line.evaluate" and text("line") and text("since") and text("until"):
        return _mcp_call(
            "cruxible_line_evaluate",
            line=text("line"),
            request={"since": text("since"), "until": text("until")},
        )
    if operation == "cruxible.line.dispatch" and text("line"):
        limit = values.get("limit")
        request = {"limit": limit} if isinstance(limit, int) and limit > 1 else {}
        return _mcp_call("cruxible_line_dispatch", line=text("line"), request=request)
    if operation == "cruxible.inbox.list" and text("line"):
        return _mcp_call("cruxible_inbox_list", line=text("line"), status="unread")
    if operation == "cruxible.runbook.run" and text("runbook"):
        return _mcp_call("cruxible_runbook_run", runbook=text("runbook"))
    if operation == "cruxible.kit.add" and text("reference"):
        return _mcp_call("cruxible_kit_add", reference=text("reference"))
    if operation == "cruxible.prediction.settle" and text("prediction_id"):
        # The observation is the settler's to choose: its Claim ID is the one
        # argument left to add.
        return _mcp_call(
            "cruxible_prediction_settle",
            prediction_id=text("prediction_id"),
            observation=_OBSERVATION_PLACEHOLDER,
        )
    if operation == "cruxible.authoring.example" and text("example"):
        return _mcp_call("cruxible_authoring_example", name=text("example"))
    if operation == "cruxible.authoring.submit" and (payload := _submitted_payload(values)):
        return _mcp_call("cruxible_authoring_submit", payload=dict(payload))
    if operation == "cruxible.proposal.readmit" and text("proposal_id"):
        return _mcp_call("cruxible_proposal_readmit", proposal_id=text("proposal_id"))
    if operation == "cruxible.proposal.activate" and text("proposal_id"):
        return _mcp_call("cruxible_proposal_activate", proposal_id=text("proposal_id"))
    if operation == "cruxible.proposal.approve" and text("proposal_id") and text("signer_id"):
        return _mcp_call(
            "cruxible_proposal_approve",
            proposal_id=text("proposal_id"),
            signer_id=text("signer_id"),
        )
    if operation == "cruxible.block.depublish" and text("source_id") and text("block_id"):
        return _mcp_call(
            "cruxible_block_depublish",
            file_id=text("source_id"),
            block_id=text("block_id"),
        )
    if operation == "cruxible.block.repin" and text("source_id") and text("block_id"):
        return _mcp_call("cruxible_block_repin", file=text("source_id"), block=text("block_id"))
    if operation == "cruxible.block.sync" and values.get("all") is True:
        return _mcp_call("cruxible_block_sync", all_files=True)
    if operation == "cruxible.block.detach" and _repair_paths(values):
        return _mcp_call("cruxible_block_detach", files=_repair_paths(values))
    if operation == "cruxible.floor.export":
        return _mcp_call("cruxible_floor_export", mode="write")
    if operation == "cruxible.vocabulary.export":
        if values.get("force") is True:
            return _mcp_call("cruxible_vocabulary_export", force=True)
        return _mcp_call("cruxible_vocabulary_export")
    if operation == "cruxible.files.propose" and (source := text("source_id")):
        return _mcp_call(
            "cruxible_files_propose", file=source, proposal_name=_revision_name(source)
        )
    if operation == "cruxible.claim.retire" and text("claim_id"):
        # Why it ends is the retirer's to say: `because` is the argument left to add.
        return _mcp_call("cruxible_retire", target=text("claim_id"), **_because(operands))
    if operation == "cruxible.set" and (restated := _restated_set(values)) is not None:
        # `because` (and the file evidence a recapture needs) is the caller's to add.
        return _mcp_call("cruxible_set", **restated, **_because(operands))
    if operation == "cruxible.write" and (changes := _write_changes(values)):
        return _mcp_call("cruxible_write", changes=changes, **_because(operands))
    if operation == "cruxible.get" and text("ref"):
        return _mcp_call("cruxible_get", ref=text("ref"))
    if operation == "cruxible.rebind" and (change := _rebind_change(values)) is not None:
        # A re-bind is one change of the typed write; `because` (and a file
        # citation's new text, or a fresh Capture) is the caller's to add.
        if "file" in operands:
            change["evidence"] = {"kind": "file", "file": _OPERAND_PLACEHOLDERS["file"]}
        elif "capture" in operands:
            change["evidence"] = {"kind": "capture", "capture": _OPERAND_PLACEHOLDERS["capture"]}
        return _mcp_call("cruxible_write", changes=[change], **_because(operands))
    return None


def _python_literal(value: object) -> str:
    """Render a str, bool, None, int, or list/dict of those as Python source.

    A JSON string literal is a valid Python one (every JSON escape is a Python
    escape), so strings keep their double quotes; JSON's `true`, `false` and
    `null` are not Python, so they are rendered here.
    """

    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return (
            "{"
            + ", ".join(
                f"{_python_literal(key)}: {_python_literal(item)}" for key, item in value.items()
            )
            + "}"
        )
    raise TypeError(f"no Python literal for an SDK repair operand of type {type(value).__name__}")


#: ``authoring_example`` is a module function, not a ``Cruxible`` method: its line
#: imports it, so it runs as printed beside a connected ``cx``.
_AUTHORING_EXAMPLE_IMPORT = "from cruxible_client.authoring.examples import authoring_example; "


def _sdk_example(example: object, **keywords: object) -> str:
    return _AUTHORING_EXAMPLE_IMPORT + _sdk_call("authoring_example", example, **keywords)


def _sdk_call(target: str, *positional: object, **keywords: object) -> str:
    rendered = [_python_literal(value) for value in positional]
    rendered.extend(f"{key}={_python_literal(value)}" for key, value in keywords.items())
    return f"{target}({', '.join(rendered)})"


def _sdk_repair_call(
    operation: str,
    *,
    arguments: object,
    operands: tuple[str, ...] = (),
) -> str | None:
    """Render one repair as the `Cruxible` SDK call that performs it, or None.

    Like the CLI and MCP renderings, an operand only the caller holds -- the
    signer, the observation, the retirement reason -- is left for it to add. A
    repair the SDK facade has no method for, or whose operands are local files,
    renders no call rather than another surface's command.
    """

    values = arguments if isinstance(arguments, Mapping) else {}

    def text(key: str) -> str | None:
        value = values.get(key)
        return value if isinstance(value, str) and value else None

    if operation == "cruxible.line.enable" and (line := text("line")):
        return _sdk_call("cx.line", line) + ".enable()"
    if operation == "cruxible.runbook.run" and (runbook := text("runbook")):
        return _sdk_call("cx.runbook", runbook) + ".run()"
    if operation == "cruxible.vocabulary.export":
        return (
            _sdk_call("cx.vocabulary.export", force=True)
            if values.get("force") is True
            else ("cx.vocabulary.export()")
        )
    if operation == "cruxible.line.evaluate" and (line := text("line")):
        since, until = text("since"), text("until")
        if since is None or until is None:
            return None
        return _sdk_call("cx.line", line) + _sdk_call(".evaluate", since=since, until=until)
    if operation == "cruxible.line.dispatch" and (line := text("line")):
        limit = values.get("limit")
        if isinstance(limit, int) and limit > 1:
            return _sdk_call("cx.line", line) + _sdk_call(".dispatch", limit=limit)
        return _sdk_call("cx.line", line) + ".dispatch()"
    if operation == "cruxible.inbox.list" and (line := text("line")):
        return "cx.inbox()" + _sdk_call(".list", line=line, status="unread")
    if operation == "cruxible.prediction.settle" and (prediction := text("prediction_id")):
        return _sdk_call("cx.settle", prediction, observation=_OBSERVATION_PLACEHOLDER)
    if operation == "cruxible.authoring.example":
        example = text("example")
        return None if example is None else _sdk_example(example)
    if operation == "cruxible.authoring.submit":
        # The facade authors a Line as a changeset member; other kinds have
        # their own builders, which no repair names yet.
        payload = _submitted_payload(values)
        if payload is None or payload.get("kind") != "line":
            return None
        keywords = {
            keyword: payload[field]
            for field, keyword, default in _SDK_LINE_KEYWORDS
            if field in payload and payload[field] != default
        }
        return _sdk_call("cx.changes().line", **keywords) + ".submit()"
    if operation == "cruxible.proposal.activate" and (proposal := text("proposal_id")):
        return _sdk_call("cx.activate", proposal)
    if operation == "cruxible.proposal.approve" and (proposal := text("proposal_id")):
        return _sdk_approve(values)
    if operation == "cruxible.claim.retire" and (claim := text("claim_id")):
        return _sdk_call("cx.retire", claim, **_because(operands))
    if operation == "cruxible.set" and (restated := _restated_set(values)) is not None:
        subject, field, value = (restated.pop(key) for key in ("subject", "field", "value"))
        return _sdk_call("cx.set", subject, field, value, **restated, **_because(operands))
    if operation == "cruxible.write":
        changes = _write_changes(values)
        if len(changes) == 1 and changes[0].get("op") == "retire":
            return _sdk_call("cx.retire", changes[0]["target"], **_because(operands))
        return None
    if operation == "cruxible.get" and (ref := text("ref")):
        return _sdk_call("cx.get", ref)
    if operation == "cruxible.rebind" and (claim := text("claim_id")):
        options: dict[str, object] = {}
        if citation := text("citation"):
            options["citation"] = citation
        if (section := _rebind_section(values)) is not None:
            options["section"] = section
        for operand in ("file", "capture"):
            if operand in operands:
                options[operand] = _OPERAND_PLACEHOLDERS[operand]
        return _sdk_call("cx.rebind", claim, **options, **_because(operands))
    if operation == "cruxible.block.repin" and (source := text("source_id")):
        block = text("block_id")
        if block is None:
            return None
        listed = values.get("claim")
        named = [values.get("claim_id"), *(listed if isinstance(listed, (list, tuple)) else ())]
        claims = [
            value.removeprefix("Claim:") for value in named if isinstance(value, str) and value
        ]
        if claims:
            return _sdk_call("cx.block.repin", source, block, claims=claims)
        return _sdk_call("cx.block.repin", source, block)
    if operation == "cruxible.block.sync" and values.get("all") is True:
        return _sdk_call("cx.block.sync", all=True)
    if operation == "cruxible.block.detach" and _repair_paths(values):
        return _sdk_call("cx.block.detach", *_repair_paths(values))
    if operation == "cruxible.block.depublish" and (source := text("source_id")):
        block = text("block_id")
        return (
            None
            if block is None
            else _sdk_call("cx.block.depublish", file_id=source, block_id=block)
        )
    return None


#: Served operations only the CLI performs: no MCP tool, no SDK method.
CLI_ONLY_OPERATIONS: frozenset[str] = frozenset({"cruxible.action.run"})


def next_repair_command(
    operation: str,
    *,
    arguments: object,
    surface: RepairSurface | None = None,
    operands: tuple[str, ...] = (),
) -> str | None:
    """Compose the invocation for one repair operation on this surface.

    ``operands`` the caller must add render as their placeholders
    (``_OPERAND_PLACEHOLDERS``), so the command shows every required operand.
    """

    if operation == "hand_edit":
        return None
    if surface in {"mcp", "sdk"} and operation in CLI_ONLY_OPERATIONS:
        # Only the CLI runs it: the line is the CLI command, saying so.
        values = arguments if isinstance(arguments, Mapping) else {}
        return render_repair(
            RepairOperation(operation=operation, arguments=dict(values)),
            surface=surface,
        )
    if surface == "mcp":
        return _mcp_repair_call(operation, arguments=arguments, operands=operands)
    if surface == "sdk":
        return _sdk_repair_call(operation, arguments=arguments, operands=operands)
    parts = ["cruxible", REPAIR_COMMAND_PATHS[operation]]
    values = arguments if isinstance(arguments, Mapping) else {}
    if operation == "cruxible.block.repin":
        source_id = values.get("source_id")
        block_id = values.get("block_id")
        if isinstance(source_id, str) and isinstance(block_id, str):
            parts.extend([shlex.quote(source_id), shlex.quote(block_id)])
        else:
            return None
        claim_id = values.get("claim_id")
        if isinstance(claim_id, str) and claim_id:
            parts.extend(["--claim", shlex.quote(claim_id)])
        claims = values.get("claim")
        if isinstance(claims, (list, tuple)):
            for value in claims:
                if isinstance(value, str) and value:
                    parts.extend(["--claim", shlex.quote(value.removeprefix("Claim:"))])
    elif operation == "cruxible.block.depublish":
        source_id = values.get("source_id")
        block_id = values.get("block_id")
        if isinstance(source_id, str) and isinstance(block_id, str):
            parts.extend([shlex.quote(source_id), shlex.quote(block_id)])
        else:
            return None
    elif operation == "cruxible.block.sync":
        if values.get("all") is True:
            parts.append("--all")
        else:
            return None
    elif operation == "cruxible.block.detach":
        paths = _repair_paths(values)
        if not paths:
            return None
        parts.extend(shlex.quote(path) for path in paths)
    elif operation == "cruxible.authoring.example":
        # The runnable step is the template that starts the payload; with no
        # example named, a bare `authoring example` lists every name.
        example = values.get("example")
        if isinstance(example, str) and example:
            parts.append(shlex.quote(example))
    elif operation == "cruxible.authoring.submit":
        # The payload is complete, so the command carries it on stdin rather
        # than naming a file the caller would have to write first.
        payload = _submitted_payload(values)
        if payload is None:
            return None
        parts.extend(["-", "<<<", shlex.quote(canonical_json(payload))])
    elif operation == "cruxible.compiler.upgrade":
        # No operands: the command targets the compiler the daemon runs, which
        # is the one this repair names in its facet's detail.
        pass
    elif operation == "cruxible.vocabulary.export":
        # It writes the workspace's own vocabulary file; --force replaces a file
        # whose header cannot establish what it meant.
        if values.get("force") is True:
            parts.append("--force")
    elif operation == "cruxible.line.evaluate":
        line, since, until = values.get("line"), values.get("since"), values.get("until")
        if not all(isinstance(value, str) and value for value in (line, since, until)):
            return None
        parts.extend(
            [
                shlex.quote(str(line)),
                "--since",
                shlex.quote(str(since)),
                "--until",
                shlex.quote(str(until)),
                # One command: evaluate the gap, then dispatch what it enqueued.
                "--dispatch",
            ]
        )
    elif operation in {"cruxible.line.enable", "cruxible.line.dispatch"}:
        line = values.get("line")
        limit = values.get("limit")
        if not isinstance(line, str) or not line:
            return None
        parts.append(shlex.quote(line))
        if operation == "cruxible.line.dispatch" and isinstance(limit, int) and limit > 1:
            parts.extend(["--limit", str(limit)])
    elif operation in {"cruxible.runbook.run", "cruxible.action.run"}:
        name = values.get("runbook" if operation == "cruxible.runbook.run" else "action")
        if not isinstance(name, str) or not name:
            return None
        parts.append(shlex.quote(name))
    elif operation == "cruxible.inbox.list":
        line = values.get("line")
        if not isinstance(line, str) or not line:
            return None
        parts.extend(["--line", shlex.quote(line), "--status", "unread"])
    elif operation == "cruxible.kit.add":
        # A preview by default: it shows what the add would do at the head now.
        reference = values.get("reference")
        if not isinstance(reference, str) or not reference:
            return None
        parts.append(shlex.quote(reference))
    elif operation == "cruxible.prediction.settle":
        # The daemon resolves the exact contract and window from the bound
        # window id; the observation is the settler's to choose, so its Claim
        # ID is the one operand left to add (`--observation CLM-...`).
        prediction_id = values.get("prediction_id")
        if not isinstance(prediction_id, str) or not prediction_id:
            return None
        parts.extend(
            [shlex.quote(prediction_id), "--observation", shlex.quote(_OBSERVATION_PLACEHOLDER)]
        )
    elif operation in {"cruxible.proposal.readmit", "cruxible.proposal.activate"}:
        proposal_id = values.get("proposal_id")
        if not isinstance(proposal_id, str):
            return None
        parts.append(shlex.quote(proposal_id))
    elif operation == "cruxible.proposal.approve":
        # The signing key stays in the signer's client custody; the daemon
        # never learns its path. The signer's client finds it (its remembered
        # settings) or refuses naming the repair, so no `--key` is spelled.
        proposal_id = values.get("proposal_id")
        signer_id = values.get("signer_id")
        if not isinstance(proposal_id, str) or not isinstance(signer_id, str):
            return None
        parts.extend([shlex.quote(proposal_id), "--signer-id", shlex.quote(signer_id)])
    elif operation == "cruxible.set":
        restated = _restated_set(values)
        if restated is None:
            return None
        parts.extend(
            shlex.quote(_cli_scalar(restated[key])) for key in ("subject", "field", "value")
        )
        role = restated.get("role")
        if isinstance(role, str):
            parts.extend(["--role", shlex.quote(role)])
        evidence = restated.get("evidence")
        if isinstance(evidence, Mapping) and isinstance(evidence.get("capture"), str):
            parts.extend(["--capture", shlex.quote(str(evidence["capture"]))])
        elif isinstance(evidence, Mapping) and isinstance(evidence.get("contract"), str):
            parts.extend(["--evidence-contract", shlex.quote(str(evidence["contract"]))])
        parts.extend(_because_flag(operands))
    elif operation == "cruxible.write":
        changes = _write_changes(values)
        if not changes or any(item.get("op") != "retire" for item in changes):
            return None
        because = "".join(f" {part}" for part in _because_flag(operands))
        if len(changes) == 1:
            return f"cruxible retire {shlex.quote(str(changes[0]['target']))}{because}"
        # Several retirements are one change set; `--because` is the operand left.
        return (
            "cruxible write"
            + because
            + " - <<< "
            + shlex.quote(canonical_json({"changes": changes}))
        )
    elif operation == "cruxible.get":
        ref = values.get("ref")
        if not isinstance(ref, str) or not ref:
            return None
        parts.append(shlex.quote(ref))
    elif operation == "cruxible.rebind":
        # The new text is the re-binder's to choose: a file citation leaves
        # `--file FILE#ANCHOR` to add (as retire leaves `--because`); a section
        # citation names its section, which locates the text on its own.
        claim_id = values.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            return None
        parts.append(shlex.quote(claim_id))
        citation = values.get("citation")
        if isinstance(citation, str) and citation:
            parts.extend(["--citation", shlex.quote(citation)])
        section = _rebind_section(values)
        if section is not None:
            parts.extend(["--section", shlex.quote(section)])
        if "file" in operands:
            parts.extend(["--file", _quoted("file")])
        if "capture" in operands:
            parts.extend(["--capture", _quoted("capture")])
        if "because" in operands:
            parts.extend(["--because", _quoted("because")])
    elif operation == "cruxible.files.propose":
        # Without a named file the operator adds --file and --name.
        source = values.get("source_id")
        if isinstance(source, str) and source:
            parts.extend(
                ["--file", shlex.quote(source), "--name", shlex.quote(_revision_name(source))]
            )
    elif operation == "cruxible.claim.retire":
        # Why it ends is the retirer's to say: `--because` is the operand left to add.
        claim_id = values.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            return None
        parts.append(shlex.quote(claim_id))
        if "because" in operands:
            parts.extend(["--because", _quoted("because")])
    elif operation in _REPAIR_COMMAND_OPERANDS:
        parts.extend(_repair_operands(operation, values))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The refusal path: every other served operation, declared once per surface.
# ---------------------------------------------------------------------------

SdkSpelling: TypeAlias = Callable[[Mapping[str, object]], str]


@dataclass(frozen=True)
class RepairSpelling:
    """How one served operation is spelled on each client.

    Every surface renders only the wire arguments it declares here: a repair's
    other arguments are context for the reader (the refused operation, the
    accepted credential kinds), not operands. ``required`` operands the repair
    does not carry render as a placeholder (``PROPOSAL_ID``) to replace, so the
    line names everything the command needs. A ``None`` surface has no form for
    the operation and says which client has it.

    ``cli_flags`` maps a wire key to its flag. A boolean renders the first flag
    of ``--a/--b`` when true and the second (if any) when false; a list repeats
    the flag. ``mcp_args`` maps a wire key to the tool's own argument name; a
    ``request.<field>`` name nests it in the tool's ``request`` model.
    """

    cli: str | None
    cli_args: tuple[str, ...] = ()
    cli_flags: Mapping[str, str] = field(default_factory=dict)
    mcp: str | None = None
    mcp_args: Mapping[str, str] = field(default_factory=dict)
    sdk: SdkSpelling | None = None
    required: tuple[str, ...] = ()
    #: The ``next`` composer spells this operation's structured operands (a
    #: write's changes, a kit reference, a dispatch limit): try it first.
    composed_first: bool = False
    #: The arguments that composer represents. A repair carrying any other
    #: argument this spelling declares (a dry run, cleared Claims) is spelled
    #: here instead, so no control the repair carries is dropped.
    composed_keys: frozenset[str] = frozenset()
    #: An argument that, when true, sends a fixed MCP value: ``clear_claims``
    #: is ``claims=[]``.
    mcp_constants: Mapping[str, tuple[str, object]] = field(default_factory=dict)
    #: Arguments a producer carries for the reader (the refused operation, the
    #: revoked principal), never operands of the command. Any other argument
    #: a client's form does not declare leaves that client with no form, so a
    #: carried control is never dropped silently.
    context: frozenset[str] = frozenset()
    #: Required operands that are content no check refuses (a Claim's value, a
    #: Procedure's input, a directory the command creates): their placeholder
    #: would be stored or used as is. A line missing one, with nothing else left
    #: to refuse, renders as its client's preview, or not at all.
    opaque: frozenset[str] = frozenset()
    #: Tool arguments every MCP form of this operation sends (``mode="write"``).
    mcp_fixed: Mapping[str, object] = field(default_factory=dict)
    #: A CLI operand that travels on stdin as ``{key: value}`` JSON
    #: (``- <<< '...'``); missing, the line names the payload file to write.
    cli_stdin: str | None = None


#: The R12 change control every change verb takes on the CLI and as a tool.
_CHANGE_FLAGS: Mapping[str, str] = {"dry_run": "--dry-run/--commit", "at": "--at"}
_CHANGE_ARGS: Mapping[str, str] = {"dry_run": "dry_run", "at": "at"}
#: The same, for the write verbs, which preview only on request.
_WRITE_FLAGS: Mapping[str, str] = {"dry_run": "--dry-run", "at": "--at"}


def _flags(*maps: Mapping[str, str], **flags: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for item in maps:
        merged.update(item)
    merged.update(flags)
    return merged


def _given(values: Mapping[str, object], *keys: str) -> dict[str, object]:
    """The keyword arguments the repair names, in the declared order."""

    return {key: values[key] for key in keys if values.get(key) not in (None, "", (), [])}


def _placeholder(key: str) -> str:
    if key == "observation":
        return _OBSERVATION_PLACEHOLDER
    return _OPERAND_PLACEHOLDERS.get(key) or placeholder(key)


def _listed(values: Mapping[str, object], key: str) -> list[object]:
    """A list operand as the separate values a variadic parameter takes."""

    value = values.get(key)
    if isinstance(value, (list, tuple)) and value:
        return list(value)
    return [_placeholder(key)] if value in (None, "", (), []) else [value]


def _operand(values: Mapping[str, object], key: str) -> object:
    value = values.get(key)
    return _placeholder(key) if value in (None, "", (), []) else value


def _sdk_proposal(method: str, *keywords: str) -> SdkSpelling:
    def spell(values: Mapping[str, object]) -> str:
        handle = _sdk_call("cx.proposal", _operand(values, "proposal_id"))
        return handle + _sdk_call(f".{method}", **_given(values, *keywords))

    return spell


def _sdk_line(method: str, *keywords: str) -> SdkSpelling:
    def spell(values: Mapping[str, object]) -> str:
        return _sdk_call("cx.line", _operand(values, "line")) + _sdk_call(
            f".{method}", **_given(values, *keywords)
        )

    return spell


def _sdk_procedure(method: str) -> SdkSpelling:
    def spell(values: Mapping[str, object]) -> str:
        return _sdk_call("cx.accepted_procedure", _operand(values, "procedure")) + _sdk_call(
            f".{method}"
        )

    return spell


def _sdk_approve(values: Mapping[str, object]) -> str:
    """The approval call; ``cx.signer(ID)`` opens that principal's key by the shared lookup."""

    handle = _sdk_call("cx.proposal", _operand(values, "proposal_id"))
    signer = values.get("signer_id")
    who = _sdk_call("cx.signer", signer) if isinstance(signer, str) and signer else "cx.signer()"
    return f"{handle}.approve(signer={who}, reviewed={handle}.review())"


def _proposal_verb(
    verb: str, *, reason: bool = False, sdk: SdkSpelling | None = None
) -> RepairSpelling:
    extra_flags = {"reason": "--reason"} if reason else {}
    return RepairSpelling(
        cli=f"proposal {verb}",
        cli_args=("proposal_id",),
        cli_flags=_flags(_CHANGE_FLAGS, **extra_flags),
        mcp=f"cruxible_proposal_{verb}",
        mcp_args=_flags(
            _CHANGE_ARGS, proposal_id="proposal_id", **({"reason": "reason"} if reason else {})
        ),
        sdk=sdk,
        required=("proposal_id", "reason") if reason else ("proposal_id",),
    )


#: The declared spelling of every served refusal operation the ``next``
#: composer does not cover with the arguments a refusal carries. The guardrail
#: parses each CLI line against the live command and each tool call against
#: the tool's own parameters.
REPAIR_SPELLINGS: Mapping[str, RepairSpelling] = {
    # Reads.
    "cruxible.orient": RepairSpelling(
        cli="orient",
        cli_flags={"section": "--section", "kind": "--kind"},
        mcp="cruxible_orient",
        mcp_args={"section": "section", "kind": "kind"},
        sdk=lambda values: _sdk_call("cx.orient", **_given(values, "kind", "section")),
        context=frozenset({"configure", "revoked_principal_id"}),
    ),
    "cruxible.get": RepairSpelling(
        cli="get",
        cli_args=("ref",),
        cli_flags={"detail": "--detail", "range": "--range", "field": "--field"},
        mcp="cruxible_get",
        mcp_args={"ref": "ref", "detail": "detail", "range": "range", "field": "field"},
        sdk=lambda values: _sdk_call(
            "cx.get", _operand(values, "ref"), **_given(values, "detail", "range", "field")
        ),
        required=("ref",),
        context=frozenset({"claim_id"}),
    ),
    "cruxible.next": RepairSpelling(
        cli="next", mcp="cruxible_next", sdk=lambda _values: "cx.next()"
    ),
    "cruxible.whoami": RepairSpelling(cli="whoami", mcp="cruxible_whoami"),
    "cruxible.capture.read": RepairSpelling(
        cli="capture read",
        cli_args=("capture_digest",),
        mcp="cruxible_capture_read",
        mcp_args={"capture_digest": "request.capture_digest"},
        sdk=lambda values: _sdk_call("cx.capture", _operand(values, "capture_digest")),
        required=("capture_digest",),
    ),
    "cruxible.coverage.resolve": RepairSpelling(
        cli="coverage resolve", mcp="cruxible_coverage_resolve"
    ),
    "cruxible.proposal.list": RepairSpelling(
        cli="proposal list",
        cli_flags={"status": "--status"},
        mcp="cruxible_proposal_list",
        mcp_args={"status": "status"},
        sdk=lambda values: _sdk_call("cx.proposals", **_given(values, "status")),
    ),
    "cruxible.inbox.list": RepairSpelling(
        cli="inbox list",
        cli_flags={"line": "--line", "status": "--status"},
        mcp="cruxible_inbox_list",
        mcp_args={"line": "line", "status": "status"},
        sdk=lambda values: "cx.inbox()" + _sdk_call(".list", **_given(values, "line", "status")),
    ),
    "cruxible.authoring.list": RepairSpelling(cli="authoring list", mcp="cruxible_authoring_list"),
    "cruxible.kit.status": RepairSpelling(cli="kit status", mcp="cruxible_kit_status"),
    "cruxible.provider.list": RepairSpelling(cli="provider list", mcp="cruxible_provider_list"),
    "cruxible.procedure.readings": RepairSpelling(
        cli="procedure readings",
        cli_args=("procedure",),
        cli_flags={"run_id": "--run-id"},
        mcp="cruxible_procedure_readings",
        mcp_args={"procedure": "name", "run_id": "request.run_id"},
        sdk=_sdk_procedure("readings"),
        required=("procedure",),
    ),
    # Authoring.
    "cruxible.authoring.example": RepairSpelling(
        cli="authoring example",
        cli_args=("example",),
        cli_flags={"claim_id": "--attestation-claim-id", "capture_digest": "--capture-digest"},
        mcp="cruxible_authoring_example",
        mcp_args={"example": "name", "claim_id": "claim_id", "capture_digest": "capture_digest"},
        sdk=lambda values: _sdk_example(
            _operand(values, "example"), **_given(values, "claim_id", "capture_digest")
        ),
        required=("example",),
        context=frozenset({"mandate_name"}),
    ),
    "cruxible.authoring.compile": RepairSpelling(
        cli="authoring compile",
        cli_args=("payload",),
        mcp="cruxible_authoring_compile",
        mcp_args={"payload": "payload"},
        required=("payload",),
    ),
    "cruxible.write": RepairSpelling(
        cli="write",
        composed_first=True,
        composed_keys=frozenset({"changes", "claim_ids"}),
        cli_flags=_flags(_WRITE_FLAGS, because="--because"),
        cli_stdin="changes",
        mcp="cruxible_write",
        mcp_args=_flags(_CHANGE_ARGS, changes="changes", because="because"),
        # The SDK writes a batch change by change (cx.changes(because=...).set(...)):
        # no one call states it, so the SDK points at another client's form.
        required=("changes", "because"),
    ),
    "cruxible.set": RepairSpelling(
        cli="set",
        composed_first=True,
        composed_keys=frozenset(
            {"subject", "field", "value", "role", "capture_digest", "evidence_contract", "claim_id"}
        ),
        cli_args=("subject", "field", "value"),
        cli_flags=_flags(_WRITE_FLAGS, because="--because"),
        mcp="cruxible_set",
        mcp_args=_flags(
            _CHANGE_ARGS, subject="subject", field="field", value="value", because="because"
        ),
        sdk=lambda values: _sdk_call(
            "cx.set",
            _operand(values, "subject"),
            _operand(values, "field"),
            _operand(values, "value"),
            because=_operand(values, "because"),
            **_given(values, "dry_run"),
        ),
        required=("subject", "field", "value", "because"),
        opaque=frozenset({"value"}),
    ),
    "cruxible.claim.retire": RepairSpelling(
        cli="retire",
        composed_first=True,
        composed_keys=frozenset({"claim_id"}),
        cli_args=("claim_id",),
        cli_flags=_flags(_WRITE_FLAGS, because="--because"),
        mcp="cruxible_retire",
        mcp_args=_flags(_CHANGE_ARGS, claim_id="target", because="because"),
        sdk=lambda values: _sdk_call(
            "cx.retire",
            _operand(values, "claim_id"),
            because=_operand(values, "because"),
            **_given(values, "dry_run"),
        ),
        required=("claim_id", "because"),
        context=frozenset({"expected_coordinate", "rule_id"}),
    ),
    "cruxible.rebind": RepairSpelling(
        cli="rebind",
        composed_first=True,
        composed_keys=frozenset({"claim_id", "citation", "section_file", "section"}),
        cli_args=("claim_id",),
        cli_flags=_flags(_WRITE_FLAGS, because="--because", file="--file"),
        sdk=lambda values: _sdk_call(
            "cx.rebind",
            _operand(values, "claim_id"),
            file=_operand(values, "file"),
            because=_operand(values, "because"),
            **_given(values, "dry_run"),
        ),
        # The new text is the re-binder's: a file citation's anchor, by default.
        required=("claim_id", "file", "because"),
    ),
    "cruxible.authoring.submit": RepairSpelling(
        cli="authoring submit",
        composed_first=True,
        composed_keys=frozenset({"payload"}),
        cli_args=("payload",),
        cli_flags={"intent_id": "--intent-id", "dry_run": "--dry-run"},
        mcp="cruxible_authoring_submit",
        mcp_args={"payload": "payload", "intent_id": "intent_id"},
        required=("payload",),
        context=frozenset({"line"}),
    ),
    "cruxible.authoring.preflight": RepairSpelling(
        cli="authoring preflight",
        cli_args=("intent_id",),
        mcp="cruxible_authoring_preflight",
        mcp_args={"intent_id": "intent_id"},
        sdk=lambda values: (
            # prepare() is the SDK's preflight: it runs the checks again at the head.
            _sdk_call("cx.resume_intent", _operand(values, "intent_id")) + ".prepare()"
        ),
        required=("intent_id",),
    ),
    "cruxible.vocabulary.plan": RepairSpelling(
        cli="vocabulary plan",
        mcp="cruxible_vocabulary_plan",
        sdk=lambda _values: "cx.vocabulary.plan()",
    ),
    "cruxible.vocabulary.apply": RepairSpelling(
        cli="vocabulary apply",
        mcp="cruxible_vocabulary_apply",
        sdk=lambda _values: "cx.vocabulary.apply()",
    ),
    "cruxible.compiler.upgrade": RepairSpelling(
        cli="compiler upgrade",
        composed_first=True,
        cli_flags=_flags(_CHANGE_FLAGS, to="--to", name="--name"),
        mcp="cruxible_compiler_upgrade",
        mcp_args=_flags(_CHANGE_ARGS, to="target_compiler_digest", name="proposal_name"),
    ),
    "cruxible.files.propose": RepairSpelling(
        cli="files propose",
        composed_first=True,
        composed_keys=frozenset({"source_id"}),
        cli_flags=_flags(_CHANGE_FLAGS, source_id="--file", name="--name"),
        mcp="cruxible_files_propose",
        mcp_args=_flags(_CHANGE_ARGS, source_id="file", name="proposal_name"),
        required=("source_id", "name"),
        context=frozenset({"document_id"}),
    ),
    "cruxible.authoring.bind": RepairSpelling(
        cli="authoring bind",
        composed_first=True,
        composed_keys=frozenset({"payload_file"}),
        cli_flags={"file": "--file", "anchor": "--anchor", "payload_file": "--payload-file"},
        mcp="cruxible_authoring_bind",
        mcp_args={"file": "source_path", "anchor": "anchor", "payload": "payload"},
        required=("file", "anchor", "payload_file", "payload"),
        # The row names the Claim and the citation to observe again, for the reader.
        context=frozenset({"claim_id", "citation_id", "source_id"}),
    ),
    "cruxible.floor.export": RepairSpelling(
        cli="floor export",
        composed_first=True,
        cli_flags={"force": "--force"},
        mcp="cruxible_floor_export",
        mcp_args={"force": "force"},
        mcp_fixed={"mode": "write"},
        # A consumer's repair says which mode; writing is the only one a repair runs.
        context=frozenset({"mode"}),
    ),
    "cruxible.vocabulary.export": RepairSpelling(
        cli="vocabulary export",
        composed_first=True,
        composed_keys=frozenset({"force"}),
        cli_flags={"force": "--force"},
        mcp="cruxible_vocabulary_export",
        mcp_args={"force": "force"},
        sdk=lambda values: _sdk_call("cx.vocabulary.export", **_given(values, "force")),
    ),
    "cruxible.claim-type.migrate": RepairSpelling(
        cli="claim-type migrate",
        cli_args=("request_file",),
        mcp="cruxible_claim_type_migrate",
        mcp_args={"request": "request"},
        required=("request_file", "request"),
    ),
    "cruxible.claim-type.propose": RepairSpelling(
        cli="claim-type propose",
        cli_flags=_flags(_CHANGE_FLAGS, input="--input", builtin="--builtin", name="--name"),
        mcp="cruxible_claim_type_propose",
        mcp_args=_flags(_CHANGE_ARGS, input="input", builtin="builtin", name="proposal_name"),
        required=("input", "name"),
    ),
    "cruxible.document.propose": RepairSpelling(
        cli="document propose",
        cli_flags=_flags(_CHANGE_FLAGS, envelope="--envelope", name="--name"),
        mcp="cruxible_document_propose",
        mcp_args=_flags(_CHANGE_ARGS, envelope="shell", name="proposal_name"),
        required=("envelope", "name"),
    ),
    "cruxible.capture-contract.propose": RepairSpelling(
        cli="capture-contract propose",
        cli_flags=_flags(_CHANGE_FLAGS, input="--input", name="--name"),
        mcp="cruxible_capture_contract_propose",
        mcp_args=_flags(_CHANGE_ARGS, input="input", name="proposal_name"),
        required=("input", "name"),
    ),
    # Proposals.
    "cruxible.proposal.readmit": _proposal_verb(
        "readmit", sdk=_sdk_proposal("readmit", "dry_run", "at")
    ),
    "cruxible.proposal.reject": _proposal_verb("reject", reason=True),
    "cruxible.proposal.withdraw": _proposal_verb("withdraw", reason=True),
    "cruxible.proposal.activate": RepairSpelling(
        cli="proposal activate",
        cli_args=("proposal_id",),
        mcp="cruxible_proposal_activate",
        mcp_args={"proposal_id": "proposal_id"},
        sdk=lambda values: _sdk_call("cx.activate", _operand(values, "proposal_id")),
        required=("proposal_id",),
    ),
    "cruxible.proposal.approve": RepairSpelling(
        cli="proposal approve",
        cli_args=("proposal_id",),
        cli_flags={"signer_id": "--signer-id"},
        mcp="cruxible_proposal_approve",
        mcp_args={"proposal_id": "proposal_id", "signer_id": "signer_id"},
        sdk=_sdk_approve,
        required=("proposal_id", "signer_id"),
    ),
    # Projection blocks (the composer spells a repair that names its block).
    "cruxible.block.repin": RepairSpelling(
        cli="block repin",
        composed_first=True,
        composed_keys=frozenset({"source_id", "block_id", "claim_id", "claim"}),
        cli_args=("source_id", "block_id"),
        cli_flags={"claim": "--claim", "clear_claims": "--clear-claims", "dry_run": "--dry-run"},
        mcp="cruxible_block_repin",
        mcp_args={
            "source_id": "file",
            "block_id": "block",
            "claim": "claims",
            "dry_run": "dry_run",
        },
        mcp_constants={"clear_claims": ("claims", [])},
        context=frozenset({"backing_candidates"}),
        sdk=lambda values: _sdk_call(
            "cx.block.repin",
            _operand(values, "source_id"),
            _operand(values, "block_id"),
            **(
                {"claims": []}
                if values.get("clear_claims") is True
                else {"claims": values["claim"]}
                if values.get("claim")
                else {}
            ),
            **_given(values, "dry_run"),
        ),
        required=("source_id", "block_id"),
    ),
    "cruxible.block.depublish": RepairSpelling(
        cli="block depublish",
        composed_first=True,
        composed_keys=frozenset({"source_id", "block_id"}),
        cli_args=("source_id", "block_id"),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_block_depublish",
        mcp_args=_flags(_CHANGE_ARGS, source_id="file_id", block_id="block_id"),
        sdk=lambda values: _sdk_call(
            "cx.block.depublish",
            file_id=_operand(values, "source_id"),
            block_id=_operand(values, "block_id"),
            **_given(values, "dry_run", "at"),
        ),
        required=("source_id", "block_id"),
    ),
    "cruxible.block.detach": RepairSpelling(
        cli="block detach",
        composed_first=True,
        composed_keys=frozenset({"paths"}),
        cli_args=("paths",),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_block_detach",
        mcp_args=_flags(_CHANGE_ARGS, paths="files"),
        sdk=lambda values: _sdk_call(
            "cx.block.detach", *_listed(values, "paths"), **_given(values, "dry_run")
        ),
        required=("paths",),
        context=frozenset({"source_id", "block_id"}),
    ),
    # Recorded by `block sync` and `block repin` for the pages they read:
    # syncing those pages again records them again.
    "cruxible.block.observe": RepairSpelling(
        cli="block sync",
        cli_args=("paths",),
        mcp="cruxible_block_sync",
        mcp_args={"paths": "files"},
        sdk=lambda values: _sdk_call("cx.block.sync", *_listed(values, "paths")),
        required=("paths",),
    ),
    "cruxible.block.sync": RepairSpelling(
        cli="block sync",
        composed_first=True,
        composed_keys=frozenset({"all"}),
        cli_args=("paths",),
        mcp="cruxible_block_sync",
        mcp_args={"paths": "files"},
        sdk=lambda values: _sdk_call("cx.block.sync", *_listed(values, "paths")),
        required=("paths",),
    ),
    # Captures.
    "cruxible.capture.erase": RepairSpelling(
        cli="capture erase",
        cli_args=("capture_digest",),
        cli_flags=_flags(_CHANGE_FLAGS, because="--because"),
        mcp="cruxible_capture_erase",
        mcp_args=_flags(_CHANGE_ARGS, capture_digest="capture", because="because"),
        sdk=lambda values: _sdk_call(
            "cx.erase_capture",
            _operand(values, "capture_digest"),
            because=_operand(values, "because"),
            **_given(values, "dry_run", "at"),
        ),
        required=("capture_digest", "because"),
    ),
    # Lines and Procedures.
    "cruxible.line.enable": RepairSpelling(
        cli="line enable",
        cli_args=("line",),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_line_enable",
        mcp_args=_flags(_CHANGE_ARGS, line="line"),
        sdk=_sdk_line("enable", "dry_run", "at"),
        required=("line",),
    ),
    "cruxible.line.disable": RepairSpelling(
        cli="line disable",
        cli_args=("line",),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_line_disable",
        mcp_args=_flags(_CHANGE_ARGS, line="line"),
        sdk=_sdk_line("disable", "dry_run", "at"),
        required=("line",),
    ),
    "cruxible.line.run": RepairSpelling(
        cli="line run",
        cli_args=("line",),
        mcp="cruxible_line_run",
        mcp_args={"line": "line"},
        sdk=_sdk_line("run"),
        required=("line",),
    ),
    "cruxible.line.dispatch": RepairSpelling(
        cli="line dispatch",
        composed_first=True,
        composed_keys=frozenset({"line", "limit"}),
        cli_args=("line",),
        cli_flags={"occurrence_id": "--occurrence-id", "limit": "--limit"},
        mcp="cruxible_line_dispatch",
        mcp_args={
            "line": "line",
            "occurrence_id": "request.occurrence_id",
            "limit": "request.limit",
        },
        sdk=_sdk_line("dispatch", "occurrence_id", "limit"),
        required=("line",),
    ),
    # A Runbook runs on every client; an action runs on the CLI alone.
    "cruxible.runbook.run": RepairSpelling(
        cli="runbook run",
        cli_args=("runbook",),
        # Its --dry-run is the static preview: definitions only, nothing runs.
        cli_flags={"dry_run": "--dry-run"},
        mcp="cruxible_runbook_run",
        mcp_args={"runbook": "runbook"},
        sdk=lambda values: _sdk_call("cx.runbook", _operand(values, "runbook")) + ".run()",
        required=("runbook",),
    ),
    "cruxible.action.run": RepairSpelling(
        cli="action run",
        cli_args=("action",),
        required=("action",),
    ),
    "cruxible.procedure.run": RepairSpelling(
        cli="procedure run",
        cli_args=("procedure", "input_file"),
        mcp="cruxible_procedure_run",
        mcp_args={"procedure": "name", "input": "input"},
        sdk=lambda values: (
            _sdk_call("cx.accepted_procedure", _operand(values, "procedure")) + ".run(input=INPUT)"
        ),
        required=("procedure", "input_file", "input"),
        opaque=frozenset({"input"}),
    ),
    "cruxible.procedure.measure": RepairSpelling(
        cli="procedure measure",
        cli_args=("procedure",),
        cli_flags={"run_id": "--run-id"},
        mcp="cruxible_procedure_measure",
        mcp_args={"procedure": "name", "run_id": "request.run_id"},
        sdk=_sdk_procedure("measure"),
        required=("procedure",),
    ),
    "cruxible.line.evaluate": RepairSpelling(
        cli="line evaluate",
        composed_first=True,
        composed_keys=frozenset({"line", "since", "until"}),
        cli_args=("line",),
        cli_flags={"since": "--since", "until": "--until", "dry_run": "--dry-run"},
        mcp="cruxible_line_evaluate",
        mcp_args={
            "line": "line",
            "since": "request.since",
            "until": "request.until",
            "dry_run": "request.dry_run",
        },
        sdk=lambda values: (
            _sdk_call("cx.line", _operand(values, "line"))
            + _sdk_call(
                ".evaluate",
                since=_operand(values, "since"),
                until=_operand(values, "until"),
                **_given(values, "dry_run"),
            )
        ),
        required=("line", "since", "until"),
    ),
    "cruxible.prediction.settle": RepairSpelling(
        cli="prediction settle",
        composed_first=True,
        composed_keys=frozenset({"prediction_id"}),
        cli_args=("prediction_id",),
        cli_flags={"observation": "--observation"},
        mcp="cruxible_prediction_settle",
        mcp_args={"prediction_id": "prediction_id", "observation": "observation"},
        sdk=lambda values: _sdk_call(
            "cx.settle",
            _operand(values, "prediction_id"),
            observation=_OBSERVATION_PLACEHOLDER,
        ),
        required=("prediction_id", "observation"),
    ),
    # Kits and providers.
    "cruxible.kit.add": RepairSpelling(
        cli="kit add",
        composed_first=True,
        composed_keys=frozenset({"reference"}),
        cli_args=("kit",),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_kit_add",
        mcp_args=_flags(_CHANGE_ARGS, kit="reference"),
        required=("kit",),
    ),
    "cruxible.kit.remove": RepairSpelling(
        cli="kit remove",
        cli_args=("kit_id",),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_kit_remove",
        mcp_args={"kit_id": "request.kit_id", "dry_run": "request.dry_run"},
        required=("kit_id",),
    ),
    "cruxible.kit.build": RepairSpelling(
        cli="kit build",
        cli_flags={"id": "--id", "version": "--version", "owns": "--owns", "out": "--out"},
        mcp="cruxible_kit_build",
        mcp_args={"id": "request.kit_id", "version": "request.version", "owns": "request.owns"},
        required=("id", "version", "owns", "out"),
        opaque=frozenset({"out"}),
    ),
    "cruxible.provider.install": RepairSpelling(
        cli="provider install",
        cli_args=("package",),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_provider_install",
        mcp_args={"package": "request.package", "dry_run": "request.dry_run"},
        required=("package",),
    ),
    "cruxible.provider.move": RepairSpelling(
        cli="provider move",
        cli_args=("provider",),
        cli_flags=_flags(_CHANGE_FLAGS, to="--to"),
        mcp="cruxible_provider_move",
        mcp_args={"provider": "request.provider", "to": "request.to", "dry_run": "request.dry_run"},
        required=("provider", "to"),
    ),
    "cruxible.provider.retire": RepairSpelling(
        cli="provider retire",
        cli_args=("build",),
        cli_flags=_CHANGE_FLAGS,
        mcp="cruxible_provider_retire",
        mcp_args={"build": "request.build", "dry_run": "request.dry_run"},
        required=("build",),
    ),
    # Governance, credentials and the daemon: served on the CLI only.
    "cruxible.principal.add": RepairSpelling(
        cli="principal add",
        cli_args=("principal_id",),
        cli_flags=_flags(_CHANGE_FLAGS, key_dir="--key-dir", mode="--mode"),
        required=("principal_id", "key_dir"),
        opaque=frozenset({"key_dir"}),
    ),
    "credential.mint": RepairSpelling(
        cli="credential mint",
        cli_flags=_flags(
            _CHANGE_FLAGS,
            principal_id="--principal-id",
            permission_mode="--mode",
            key_dir="--key-dir",
        ),
        required=("principal_id", "permission_mode"),
        context=frozenset(
            {
                "refused_operation",
                "credential_env",
                "accepted_credentials",
                "credential_options",
                "unbound_credential_id",
            }
        ),
    ),
    "credential.rotate": RepairSpelling(
        cli="credential rotate",
        cli_args=("credential_id",),
        cli_flags=_flags(_CHANGE_FLAGS, key_dir="--key-dir"),
        required=("credential_id",),
    ),
    "credential.revoke": RepairSpelling(
        cli="credential revoke",
        cli_args=("credential_id",),
        cli_flags=_CHANGE_FLAGS,
        required=("credential_id",),
    ),
    "credential.recover-admin": RepairSpelling(
        cli="credential recover-admin",
        cli_flags=_flags(_CHANGE_FLAGS, instance_id="--instance-id"),
    ),
    "credential.claim-bootstrap": RepairSpelling(
        cli="credential claim-bootstrap",
        cli_flags=_flags(_CHANGE_FLAGS, secret_file="--secret-file"),
        required=("secret_file",),
        context=frozenset({"instance_id"}),
    ),
    "cruxible.host.create": RepairSpelling(
        cli="host create",
        cli_flags=_flags(
            _CHANGE_FLAGS,
            instance_id="--instance-id",
            workspace="--workspace",
            replace="--replace",
        ),
    ),
    "cruxible.workspace.attach": RepairSpelling(
        cli="workspace attach", cli_flags=_flags(_CHANGE_FLAGS, instance_id="--instance-id")
    ),
    "cruxible.workspace.detach": RepairSpelling(
        cli="workspace detach", cli_flags=_flags(_CHANGE_FLAGS, instance_id="--instance-id")
    ),
    "cruxible.floor.delivery": RepairSpelling(
        cli="floor delivery",
        cli_args=("state",),
        cli_flags={"instance_id": "--instance-id"},
        required=("state",),
    ),
    "cruxible.init": RepairSpelling(cli="init", cli_flags={"principal_id": "--principal-id"}),
    "cruxible.ledger.publish": RepairSpelling(cli="ledger publish", cli_flags=_CHANGE_FLAGS),
    "cruxible.ledger.set-mirror": RepairSpelling(
        cli="ledger set-mirror", cli_args=("url",), cli_flags=_CHANGE_FLAGS
    ),
    "cruxible.instance.decommission": RepairSpelling(
        cli="instance decommission",
        cli_flags=_flags(_CHANGE_FLAGS, reason="--reason"),
        required=("reason",),
    ),
    "server.start": RepairSpelling(cli="server start", cli_flags={"auth": "--auth"}),
    "server.status": RepairSpelling(cli="server status"),
}

_SURFACES: tuple[RepairSurface, ...] = ("cli", "mcp", "sdk")
_SURFACE_NAMES: Mapping[RepairSurface, str] = {"cli": "CLI", "mcp": "MCP", "sdk": "SDK"}


def _cli_line(spelling: RepairSpelling, values: Mapping[str, object]) -> str:
    assert spelling.cli is not None
    parts = ["cruxible", spelling.cli]
    for key in spelling.cli_args:
        value = values.get(key)
        if value in (None, "", (), []):
            if key in spelling.required:
                parts.append(shlex.quote(_placeholder(key)))
            continue
        if isinstance(value, (list, tuple)):
            parts.extend(shlex.quote(_cli_scalar(item)) for item in value)
            continue
        parts.append(shlex.quote(_cli_scalar(value)))
    for key, flag in spelling.cli_flags.items():
        if key not in values and key not in spelling.required:
            continue
        on, _, off = flag.partition("/")
        value = values.get(key)
        if value is None and key not in spelling.required:
            continue
        if value is None:
            parts.extend([on, shlex.quote(_placeholder(key))])
        elif isinstance(value, bool):
            if value:
                parts.append(on)
            elif off:
                parts.append(off)
        elif isinstance(value, (list, tuple)):
            for item in value:
                parts.extend([on, shlex.quote(_cli_scalar(item))])
        else:
            parts.extend([on, shlex.quote(_cli_scalar(value))])
    if spelling.cli_stdin is not None:
        payload = values.get(spelling.cli_stdin)
        if payload in (None, "", (), []):
            parts.append(shlex.quote(placeholder("PAYLOAD_FILE")))
        else:
            parts.extend(["-", "<<<", shlex.quote(canonical_json({spelling.cli_stdin: payload}))])
    return " ".join(parts)


def _mcp_line(spelling: RepairSpelling, values: Mapping[str, object]) -> str:
    assert spelling.mcp is not None
    top: dict[str, object] = {}
    request: dict[str, object] = {}
    for key, name in spelling.mcp_args.items():
        if key in values and values[key] is not None:
            value = values[key]
        elif key in spelling.required:
            value = _placeholder(key)
        else:
            continue
        if name.startswith("request."):
            request[name.removeprefix("request.")] = value
        else:
            top[name] = value
    top.update(spelling.mcp_fixed)
    for key, (name, constant) in spelling.mcp_constants.items():
        if values.get(key) is True:
            top[name] = constant
    if request or any(name.startswith("request.") for name in spelling.mcp_args.values()):
        top["request"] = request
    return _mcp_call(spelling.mcp, **top)


def _carried(spelling: RepairSpelling, values: Mapping[str, object]) -> set[str]:
    """The repair's operands: every argument it carries but the reader's context."""

    return {key for key, value in values.items() if value is not None} - spelling.context


_SDK_KEYS: dict[int, frozenset[str]] = {}


def _sdk_keys(spelling: RepairSpelling) -> frozenset[str]:
    """The arguments the SDK form spells: those whose presence changes its line."""

    cached = _SDK_KEYS.get(id(spelling))
    if cached is not None or spelling.sdk is None:
        return cached or frozenset()
    candidates = {
        *spelling.cli_args,
        *spelling.cli_flags,
        *spelling.mcp_args,
        *spelling.mcp_constants,
        *spelling.required,
        *spelling.composed_keys,
    }
    base = spelling.sdk({})
    found = frozenset(
        key
        for key in candidates
        if any(spelling.sdk({key: probe}) != base for probe in ("probe-value", True, ["a", "b"]))
    )
    _SDK_KEYS[id(spelling)] = found
    return found


def declared_keys(spelling: RepairSpelling, surface: RepairSurface) -> frozenset[str]:
    """The arguments one client's form of this operation spells."""

    if surface == "cli":
        if spelling.cli is None:
            return frozenset()
        stdin = {spelling.cli_stdin} if spelling.cli_stdin else set()
        return frozenset({*spelling.cli_args, *spelling.cli_flags, *stdin})
    if surface == "mcp":
        if spelling.mcp is None:
            return frozenset()
        return frozenset({*spelling.mcp_args, *spelling.mcp_constants})
    return _sdk_keys(spelling)


def _spelled(
    spelling: RepairSpelling, surface: RepairSurface, values: Mapping[str, object]
) -> str | None:
    declared = declared_keys(spelling, surface)
    missing = {key for key in spelling.required if key in declared and values.get(key) in _ABSENT}
    if missing and missing <= spelling.opaque:
        # Only content placeholders are left, and no check refuses those: the
        # line may run only as a preview, which stores nothing.
        if "dry_run" not in declared:
            return None
        values = {**values, "dry_run": True}
    carried = _carried(spelling, values)
    if values.get("dry_run") is False and "dry_run" not in declared:
        # Committing is the only thing a form with no preview does.
        carried.discard("dry_run")
    if not carried <= declared:
        # This client's form cannot carry one of the repair's operands or
        # controls: it has no form for this repair, rather than a lossy one.
        return None
    if surface == "cli":
        line = None if spelling.cli is None else _cli_line(spelling, values)
    elif surface == "mcp":
        line = None if spelling.mcp is None else _mcp_line(spelling, values)
    else:
        line = None if spelling.sdk is None else spelling.sdk(values)
    # A surface that cannot carry the repair's preview has no form for it: a
    # typed preview never renders as the change itself.
    return line if line is None or _keeps_preview(line, values, surface, spelling) else None


_ABSENT: tuple[object, ...] = (None, "", (), [])


#: How each surface spells a preview, as a rendered line shows it.
_PREVIEW_MARKERS: Mapping[RepairSurface, tuple[str, ...]] = {
    "cli": ("--dry-run",),
    "mcp": ("dry_run=true", '"dry_run": true'),
    "sdk": ("dry_run=True",),
}


def _keeps_preview(
    line: str,
    values: Mapping[str, object],
    surface: RepairSurface,
    spelling: RepairSpelling | None,
) -> bool:
    """Whether the line keeps the repair's preview; an operation that never previews
    (a read, an action with no dry run) has none to keep."""

    if values.get("dry_run") is not True or spelling is None:
        return True
    if "dry_run" not in {*spelling.cli_flags, *spelling.mcp_args}:
        return True
    if surface == "cli":
        return "--dry-run" in line.split()
    return any(marker in line for marker in _PREVIEW_MARKERS[surface])


def repair_spelling(operation: str) -> RepairSpelling | None:
    """The declared spelling of one served operation id, or None."""

    return REPAIR_SPELLINGS.get(normalize_code(operation))


def render_repair(repair: ServedRepair, *, surface: RepairSurface) -> str:
    """Render one served repair in the spelling of the client that shows it.

    A hand edit prints its target and the change it requires. A runnable repair
    prints the command (CLI), the tool call (MCP) or the ``Cruxible`` call
    (SDK) that performs it; where the operation has no form on this client, the
    line says so and gives the form of a client that has it.
    """

    if isinstance(repair, HandEditRepair):
        return f"hand edit {repair.hand_edit.target}: {repair.hand_edit.required_change}"
    operation = normalize_code(repair.operation)
    values: Mapping[str, object] = repair.arguments
    line = _line(operation, values, surface)
    if line is not None:
        return line
    others: tuple[RepairSurface, ...] = tuple(other for other in _SURFACES if other != surface)
    for other in others:
        line = _line(operation, values, other)
        if line is not None:
            return (
                f"{line}  (on the {_SURFACE_NAMES[other]}; "
                f"the {_SURFACE_NAMES[surface]} has no form for {operation})"
            )
    return _operation_and_arguments(repair)


def _line(operation: str, values: Mapping[str, object], surface: RepairSurface) -> str | None:
    spelling = REPAIR_SPELLINGS.get(operation)
    if spelling is None or (spelling.composed_first and _composer_holds(spelling, values)):
        composed = _composed(operation, values, surface)
        if composed is not None and _keeps_preview(composed, values, surface, spelling):
            return composed
    return None if spelling is None else _spelled(spelling, surface, values)


def _composer_holds(spelling: RepairSpelling, values: Mapping[str, object]) -> bool:
    """Whether the composer represents every operand and control this repair carries."""

    carried = _carried(spelling, values)
    if values.get("dry_run") is False and "dry_run" not in {
        *spelling.cli_flags,
        *spelling.mcp_args,
    }:
        carried.discard("dry_run")  # an operation with no preview only commits
    # A required operand the composer would spell, missing: the spelling shows
    # its placeholder instead of a command without it.
    missing = (set(spelling.required) & spelling.composed_keys) - carried
    return not missing and carried <= spelling.composed_keys


def _composed(operation: str, values: Mapping[str, object], surface: RepairSurface) -> str | None:
    """The ``next`` composer's line for this operation, when it composes one."""

    if operation not in REPAIR_COMMAND_PATHS:
        return None
    # A refusal names no operands to add; the ones its operation always needs
    # from the caller (why, for every write) render as their placeholders.
    spelling = REPAIR_SPELLINGS.get(operation)
    operands = tuple(
        key
        for key in (() if spelling is None else spelling.required)
        if key in _OPERAND_PLACEHOLDERS
    )
    return next_repair_command(operation, arguments=values, surface=surface, operands=operands)


def _operation_and_arguments(repair: RepairOperation) -> str:
    if not repair.arguments:
        return repair.operation
    arguments = json.dumps(repair.arguments, sort_keys=True, separators=(",", ":"))
    return f"{repair.operation} {arguments}"


__all__ = [
    "REPAIR_SPELLINGS",
    "RepairSpelling",
    "RepairSurface",
    "next_repair_command",
    "render_repair",
    "repair_spelling",
]
