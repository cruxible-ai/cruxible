"""Deterministic compact-config expander.

It reads a COMPACT authoring YAML (e.g. ``kits/agent-operation/config.yaml``; a fuller
commented reference lives at ``docs/dev/agent-operation.compact.draft.yaml``) and expands
it 1:1 into the explicit ``CoreConfig``-shaped dict. The compact source is the single
source of truth: the loader (``config/loader.py``) detects it via :func:`looks_compact`
and expands it on load, so the explicit form exists only transiently in memory -- there
is no committed expanded artifact. Graph semantics stay fully explicit post-expansion.

The expander loads the source with a plain YAML parser and then interprets the
compact *string values* (the closed set of 7 string mini-languages documented in
the draft header). There is no custom pre-YAML tokenizer. The single exception is
relationship one-line descriptions, which are authored as *trailing YAML comments*
on the signature line -- ``yaml.safe_load`` discards comments, so we recover them
with a light line-level pre-scan (``ruamel.yaml`` is intentionally NOT a
dependency; see the README note in the draft's "Implementation wrinkle").

Public API
----------
``expand_compact(source_text) -> dict``
    Expand compact YAML text to a CoreConfig-shaped dict.
``expand_compact_file(path) -> dict``
    Read a file and expand it.
``ExpandResult``
    Carries the expanded config plus the stripped ``metadata`` (e.g.
    ``requires_cruxible``) that is expander-owned and never emitted as ontology.
``CompactExpansionError``
    Raised on any ambiguity or malformed compact construct (fails loudly; never
    guesses -- notably on relationship-direction ambiguity).
``dump_expanded(config_dict) -> str``
    Serialize the expanded dict to deterministic, diff-stable YAML.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Inert engine-side defaults filled into the expanded output when omitted. These
# are the "resource guards" called out in the draft -- decision-bearing knobs
# (mode / returns / relationship_state) are NEVER defaulted here; they must be
# explicit in the source.
_DEFAULT_RESULT_SHAPE = "entity"
_DEFAULT_ALLOW_REL_STATE_OVERRIDE = True
_DEFAULT_MAX_PATHS = 500
_DEFAULT_MAX_PATHS_PER_RESULT = 50
_DEFAULT_LIMIT = 100

_SCALAR_TYPES = {"string", "date", "datetime", "int", "integer", "float", "number", "bool", "json"}


class CompactExpansionError(ValueError):
    """Raised when a compact construct is ambiguous or malformed.

    The expander fails loudly rather than guessing -- most importantly on
    relationship-direction ambiguity (self-referential edges referenced without a
    ``>``/``<`` marker, or edges whose direction is not uniquely determined by the
    anchor).
    """


def _reject_unknown_keys(
    construct: str,
    data: dict[str, Any],
    allowed: set[str],
) -> None:
    """Fail closed when an authored compact mapping contains unsupported keys."""
    unknown = sorted(set(data) - allowed)
    if not unknown:
        return
    if len(unknown) == 1:
        raise CompactExpansionError(f"{construct}: unsupported key '{unknown[0]}'")
    joined = "', '".join(unknown)
    raise CompactExpansionError(f"{construct}: unsupported keys '{joined}'")


@dataclass
class ExpandResult:
    """Outcome of expanding a compact source.

    Attributes:
        config: The CoreConfig-shaped dict (validates as ``CoreConfig``).
        metadata: Expander-owned manifest fields consumed from ``metadata:`` in the
            source (e.g. ``requires_cruxible``). Recorded here, stripped from
            ``config``.
        all_adjacent_queries: Compact query bodies that used ``include: all_adjacent``.
            These are retained out-of-band so composition can rematerialize them
            against the final merged relationship set.
        warnings: Non-fatal expansion notes (e.g. a decision-bearing knob that the
            engine contract makes inapplicable for a given query shape).
    """

    config: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    all_adjacent_queries: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Comment pre-scan (relationship one-line descriptions)
# ---------------------------------------------------------------------------

# Matches a relationship signature list item and captures the relationship name
# and any trailing `# comment`. Example:
#   - work_item_owned_by_actor: WorkItem -> Actor   # Actor accountable for a work item.
_REL_SIG_LINE = re.compile(
    r"""^\s*-\s+
        (?P<name>[A-Za-z_][\w]*)\s*:\s*
        (?P<sig>[^#]*?)\s*
        (?:\#\s*(?P<comment>.*?))?\s*$
    """,
    re.VERBOSE,
)


def _scan_relationship_comments(source_text: str) -> dict[str, str]:
    """Recover trailing `# comment` descriptions on relationship signature lines.

    Returns a mapping of relationship name -> trailing comment text. Only lines
    that look like a relationship signature item (``- <name>: From -> To``) are
    considered, so other ``# comments`` in the file are ignored. ``yaml.safe_load``
    discards these comments, so this light pre-scan is how we recover them without
    pulling in a round-trip YAML parser.
    """
    comments: dict[str, str] = {}
    for raw in source_text.splitlines():
        match = _REL_SIG_LINE.match(raw)
        if match is None:
            continue
        sig = match.group("sig")
        # A relationship signature is `From -> To`; require the arrow so we don't
        # misread an arbitrary `- key: value  # comment` line elsewhere.
        if "->" not in sig:
            continue
        comment = match.group("comment")
        if comment:
            comments[match.group("name")] = comment.strip()
    return comments


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def _expand_enums(raw_enums: dict[str, Any]) -> dict[str, Any]:
    """Expand the compact ``enums:`` block.

    ``name: [a, b]``  -> ``{values: [a, b]}``
    ``name: {values: [...], ordered: low_to_high}`` passes through.
    """
    out: dict[str, Any] = {}
    for name, value in raw_enums.items():
        if isinstance(value, list):
            out[name] = {"values": list(value)}
        elif isinstance(value, dict):
            _reject_unknown_keys(f"enum '{name}'", value, {"values", "ordered", "description"})
            entry: dict[str, Any] = {"values": list(value["values"])}
            if "ordered" in value:
                entry["ordered"] = value["ordered"]
            if "description" in value:
                entry["description"] = value["description"]
            out[name] = entry
        else:
            raise CompactExpansionError(
                f"enum '{name}' must be a list or a mapping with 'values', "
                f"got {type(value).__name__}"
            )
    return out


# ---------------------------------------------------------------------------
# Entity properties (compact scalar grammar)
# ---------------------------------------------------------------------------


def _expand_property_scalar(name: str, spec: str) -> dict[str, Any]:
    """Expand one compact entity/relationship property string.

    Grammar (intentional string mini-language #2):
        ``string`` / ``int`` / ``float`` / ``number`` / ``bool`` / ``json`` /
        ``date`` / ``datetime``               -> {type: <t>}
        trailing ``?``                         -> optional: true
        ``optional``                           -> optional: true (flow-map-safe
                                                  spelling: a bare ``?`` is YAML
                                                  syntax inside ``{...}``)
        ``indexed``                            -> indexed: true
        ``enum <ref>``                         -> {type: string, enum_ref: <ref>}
        ``= <v>``                              -> default: <v>
    Tokens may combine, e.g. ``string indexed``, ``enum actor_status = active``.
    """
    tokens = spec.split()
    if not tokens:
        raise CompactExpansionError(f"property '{name}' has an empty type spec")

    result: dict[str, Any] = {}
    optional = False
    i = 0

    # Leading type / enum_ref token.
    head = tokens[0]
    if head == "enum":
        if len(tokens) < 2:
            raise CompactExpansionError(f"property '{name}': 'enum' requires a reference")
        result["type"] = "string"
        enum_ref = tokens[1]
        if enum_ref.endswith("?"):
            enum_ref = enum_ref[:-1]
            optional = True
        result["enum_ref"] = enum_ref
        i = 2
    elif head in _SCALAR_TYPES or head.rstrip("?") in _SCALAR_TYPES:
        base = head
        if base.endswith("?"):
            base = base[:-1]
            optional = True
        if base not in _SCALAR_TYPES:
            raise CompactExpansionError(f"property '{name}': unknown type token '{head}'")
        result["type"] = base
        i = 1
    else:
        raise CompactExpansionError(
            f"property '{name}': spec must start with a scalar type or 'enum <ref>', got '{spec}'"
        )

    # Modifier tokens.
    while i < len(tokens):
        token = tokens[i]
        if token == "indexed":
            result["indexed"] = True
            i += 1
        elif token in ("?", "optional"):
            optional = True
            i += 1
        elif token == "=":
            if i + 1 >= len(tokens):
                raise CompactExpansionError(f"property '{name}': '=' requires a default value")
            result["default"] = _coerce_scalar(tokens[i + 1])
            i += 2
        elif token.startswith("="):
            result["default"] = _coerce_scalar(token[1:])
            i += 1
        else:
            raise CompactExpansionError(
                f"property '{name}': unexpected token '{token}' in spec '{spec}'"
            )

    if optional:
        result["optional"] = True
    return result


def _coerce_scalar(value: str) -> Any:
    """Coerce a bare default token to a YAML-ish scalar (kept simple/explicit)."""
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _expand_entity_types(raw_entities: dict[str, Any]) -> dict[str, Any]:
    """Expand the compact ``entity_types:`` block.

    Each entity may declare an entity-level ``id: <name>`` shorthand that expands
    to a ``<name>: {type: string, primary_key: true}`` property, plus a
    ``properties:`` block whose scalar values use the compact property grammar.
    """
    out: dict[str, Any] = {}
    for type_name, body in raw_entities.items():
        if not isinstance(body, dict):
            raise CompactExpansionError(f"entity '{type_name}': body must be a mapping")
        _reject_unknown_keys(
            f"entity '{type_name}'",
            body,
            {"description", "id", "properties", "write_policy", "auth_managed", "constraints"},
        )

        entity: dict[str, Any] = {}
        if "description" in body:
            entity["description"] = body["description"]

        props: dict[str, Any] = {}

        # Entity-level `id: <name>` -> primary-key property, emitted first.
        if "id" in body:
            pk_name = body["id"]
            if not isinstance(pk_name, str):
                raise CompactExpansionError(
                    f"entity '{type_name}': id must be a property name string"
                )
            props[pk_name] = {"type": "string", "primary_key": True}

        for prop_name, prop_spec in body.get("properties", {}).items():
            if isinstance(prop_spec, str):
                props[prop_name] = _expand_property_scalar(prop_name, prop_spec)
            elif isinstance(prop_spec, dict):
                # Explicit block form passes through verbatim.
                props[prop_name] = dict(prop_spec)
            else:
                raise CompactExpansionError(
                    f"entity '{type_name}' property '{prop_name}': "
                    f"must be a compact string or explicit mapping"
                )

        entity["properties"] = props
        if "write_policy" in body:
            entity["write_policy"] = body["write_policy"]
        if "auth_managed" in body:
            entity["auth_managed"] = body["auth_managed"]
        if "constraints" in body:
            entity["constraints"] = list(body["constraints"])
        out[type_name] = entity
    return out


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

_SIG_RE = re.compile(r"^\s*(?P<from>\w+)\s*->\s*(?P<to>\w+)\s*$")


@dataclass
class RelInfo:
    """Resolved relationship topology used by query direction inference."""

    name: str
    from_entity: str
    to_entity: str

    @property
    def is_self_ref(self) -> bool:
        return self.from_entity == self.to_entity


def _expand_relationships(
    raw_rels: list[Any],
    *,
    policies: dict[str, Any],
    comments: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, RelInfo]]:
    """Expand the compact ``relationships:`` list.

    Returns the explicit relationship dicts plus an index (name -> RelInfo) used
    later for traversal direction inference and ``all_adjacent`` resolution.
    """
    out: list[dict[str, Any]] = []
    index: dict[str, RelInfo] = {}

    for item in raw_rels:
        if not isinstance(item, dict) or len(item) < 1:
            raise CompactExpansionError(f"relationship item must be a mapping, got {item!r}")

        # The signature is the single key whose value is a `From -> To` string.
        name, sig = _find_signature(item)
        _reject_unknown_keys(
            f"relationship '{name}'",
            item,
            {name, "proposal_policy", "basis", "description", "properties", "write_policy"},
        )

        match = _SIG_RE.match(sig)
        if match is None:
            raise CompactExpansionError(
                f"relationship '{name}': signature must be 'From -> To', got '{sig}'"
            )
        from_entity = match.group("from")
        to_entity = match.group("to")

        rel: dict[str, Any] = {"name": name, "from": from_entity, "to": to_entity}

        # Description: trailing `# comment` (one-line) or block `description: >`.
        description = comments.get(name)
        if "description" in item:
            description = item["description"]
        if description is not None:
            rel["description"] = description

        # Properties: explicit `properties: {...}` block, then `basis:` rationale.
        props: dict[str, Any] = {}
        if "properties" in item:
            for prop_name, prop_spec in item["properties"].items():
                if isinstance(prop_spec, str):
                    props[prop_name] = _expand_property_scalar(prop_name, prop_spec)
                elif isinstance(prop_spec, dict):
                    props[prop_name] = dict(prop_spec)
                else:
                    raise CompactExpansionError(
                        f"relationship '{name}' property '{prop_name}': "
                        f"must be a compact string or explicit mapping"
                    )
        if "basis" in item:
            basis_prop = item["basis"]
            # `basis: <prop>` adds <prop>: string? (the rationale field).
            props[basis_prop] = {"type": "string", "optional": True}

        if props:
            rel["properties"] = props

        # Proposal policy: name reference -> preset; inline map -> one-off; omitted
        # -> ungoverned (no proposal_policy key).
        if "proposal_policy" in item:
            policy_ref = item["proposal_policy"]
            if isinstance(policy_ref, str):
                if policy_ref not in policies:
                    raise CompactExpansionError(
                        f"relationship '{name}': proposal_policy '{policy_ref}' "
                        f"is not defined in presets.policies"
                    )
                rel["proposal_policy"] = _expand_proposal_policy(policies[policy_ref])
            elif isinstance(policy_ref, dict):
                rel["proposal_policy"] = _expand_proposal_policy(policy_ref)
            else:
                raise CompactExpansionError(
                    f"relationship '{name}': proposal_policy must be a preset name "
                    f"or inline mapping"
                )

        if "write_policy" in item:
            rel["write_policy"] = item["write_policy"]

        out.append(rel)
        index[name] = RelInfo(name=name, from_entity=from_entity, to_entity=to_entity)

    return out, index


def _find_signature(item: dict[str, Any]) -> tuple[str, str]:
    """Find the relationship name and its `From -> To` signature in a list item.

    The signature is the single mapping key whose value is a string containing
    ``->``. The other keys (proposal_policy, basis, description, properties) are
    structured fields and are skipped -- so a block ``description`` whose text
    contains ``->`` is not mistaken for a second signature.
    """
    structured = {"proposal_policy", "basis", "description", "properties", "write_policy"}
    candidates = [
        (key, value)
        for key, value in item.items()
        if key not in structured and isinstance(value, str) and "->" in value
    ]
    if len(candidates) != 1:
        raise CompactExpansionError(
            f"relationship item must have exactly one 'name: From -> To' signature, "
            f"found {len(candidates)} in {item!r}"
        )
    return candidates[0]


def _expand_proposal_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Expand a (possibly compact-inline) proposal policy to the explicit shape.

    Signal entries authored inline (``{role: required, always_review_on_unsure: true}``)
    pass through as explicit mappings; the engine fills the remaining inert policy
    defaults.
    """
    _reject_unknown_keys(
        "proposal policy",
        policy,
        {"signals", "auto_resolve_when", "auto_resolve_requires_prior_trust", "max_group_size"},
    )

    out: dict[str, Any] = {}
    signals = policy.get("signals", {})
    expanded_signals: dict[str, Any] = {}
    for signal_name, signal_body in signals.items():
        expanded_signals[signal_name] = dict(signal_body)
    out["signals"] = expanded_signals
    for key in ("auto_resolve_when", "auto_resolve_requires_prior_trust", "max_group_size"):
        if key in policy:
            out[key] = policy[key]
    return out


# ---------------------------------------------------------------------------
# Traversal direction inference
# ---------------------------------------------------------------------------


def _resolve_direction(
    rel_ref: str,
    anchor: str,
    rel_index: dict[str, RelInfo],
    *,
    context: str,
) -> tuple[str, str]:
    """Resolve a relationship reference + anchor entity to (canonical_name, direction).

    Direction is INFERRED from which endpoint the anchor sits on:
        anchor == from  -> outgoing
        anchor == to    -> incoming
    Self-referential edges (from == to) are ambiguous and MUST carry a ``>`` (out)
    or ``<`` (in) marker on the reference. If direction cannot be uniquely
    inferred, this FAILS LOUDLY rather than guessing.

    ``rel_ref`` may carry a trailing ``>``/``<`` marker (self-ref disambiguation).
    """
    if not isinstance(rel_ref, str):
        raise CompactExpansionError(
            f"{context}: relationship reference must be a string, got {type(rel_ref).__name__}"
        )
    marker: str | None = None
    name = rel_ref
    if name.endswith(">"):
        marker = ">"
        name = name[:-1]
    elif name.endswith("<"):
        marker = "<"
        name = name[:-1]

    info = rel_index.get(name)
    if info is None:
        raise CompactExpansionError(
            f"{context}: relationship '{name}' is not defined in this config"
        )

    if info.is_self_ref:
        if marker is None:
            raise CompactExpansionError(
                f"{context}: relationship '{name}' is self-referential "
                f"({info.from_entity} -> {info.to_entity}); direction is ambiguous and "
                f"must be disambiguated with '>' (outgoing) or '<' (incoming)"
            )
        return name, ("outgoing" if marker == ">" else "incoming")

    # Non-self-ref edge: a marker is redundant but, if present, must agree with
    # the anchor-inferred direction. Otherwise infer from the anchor.
    if anchor == info.from_entity:
        inferred = "outgoing"
    elif anchor == info.to_entity:
        inferred = "incoming"
    else:
        raise CompactExpansionError(
            f"{context}: relationship '{name}' ({info.from_entity} -> {info.to_entity}) "
            f"does not touch anchor entity '{anchor}'; direction cannot be inferred"
        )

    if marker is not None:
        marked = "outgoing" if marker == ">" else "incoming"
        if marked != inferred:
            raise CompactExpansionError(
                f"{context}: relationship '{name}' direction marker '{marker}' conflicts "
                f"with anchor-inferred direction '{inferred}'"
            )
    return name, inferred


# ---------------------------------------------------------------------------
# Where predicates
# ---------------------------------------------------------------------------


_WHERE_SCOPES = {"candidate", "edge", "input", "result", "source", "target"}


def _expand_where(raw_where: dict[str, Any], *, scope: str) -> dict[str, Any]:
    """Expand compact ``where: {field: {op: value}}`` to scoped predicate paths.

    The field targets the entity in scope: ``result.properties.<field>`` at
    collection level, ``candidate.properties.<field>`` inside a traverse step (the
    ``as:`` node being filtered). Already-scoped predicate paths pass through for
    explicit-schema parity.
    """
    if not isinstance(raw_where, dict):
        raise CompactExpansionError(
            f"where must be a mapping of property predicates, got {type(raw_where).__name__}"
        )
    out: dict[str, Any] = {}
    for field_name, predicate in raw_where.items():
        field_text = str(field_name)
        predicate_scope, sep, _path = field_text.partition(".")
        if sep and predicate_scope in _WHERE_SCOPES:
            out[field_text] = predicate
        else:
            out[f"{scope}.properties.{field_text}"] = predicate
    return out


# ---------------------------------------------------------------------------
# Order clause
# ---------------------------------------------------------------------------


def _expand_order_clause(clause: str, *, ref_base: str = "$result") -> dict[str, Any]:
    """Expand one compact order clause (intentional string mini-language #3).

    Grammar: ``<field> <asc|desc> [<type>|^<enum>]``
        - bare type token (``date``/``datetime``/...) -> value_type
        - ``^<enum>`` -> enum_ref (sort by enum order)
    """
    tokens = clause.split()
    if len(tokens) < 2:
        raise CompactExpansionError(
            f"order clause '{clause}' must be '<field> <asc|desc> [<type>|^<enum>]'"
        )
    if len(tokens) > 3:
        raise CompactExpansionError(
            f"order clause '{clause}' has unsupported extra token '{tokens[3]}'"
        )
    field_name, direction = tokens[0], tokens[1]
    if direction not in {"asc", "desc"}:
        raise CompactExpansionError(
            f"order clause '{clause}': direction must be 'asc' or 'desc', got '{direction}'"
        )
    spec: dict[str, Any] = {"by": f"{ref_base}.properties.{field_name}", "direction": direction}
    if len(tokens) >= 3:
        type_token = tokens[2]
        if type_token.startswith("^"):
            spec["enum_ref"] = type_token[1:]
        else:
            spec["value_type"] = type_token
    return spec


def _expand_order_list(value: Any, *, ref_base: str = "$result") -> list[dict[str, Any]]:
    """Expand an ``order:`` value (single clause string or list of clauses)."""
    if isinstance(value, str):
        return [_expand_order_clause(value, ref_base=ref_base)]
    if isinstance(value, list):
        return [_expand_order_clause(clause, ref_base=ref_base) for clause in value]
    raise CompactExpansionError(f"order must be a string or list of clauses, got {value!r}")


# ---------------------------------------------------------------------------
# Includes
# ---------------------------------------------------------------------------


def _include_entry(
    *,
    from_ref: str,
    relationship: str,
    direction: str,
    limit: int | None = None,
    order: Any | None = None,
    where: dict[str, Any] | None = None,
    required: bool | None = None,
) -> dict[str, Any]:
    """Build one explicit include entry in a stable field order."""
    entry: dict[str, Any] = {
        "from": from_ref,
        "relationship": relationship,
        "direction": direction,
        "many": True,
    }
    if where is not None:
        entry["where"] = _expand_where(where, scope="source")
    if limit is not None:
        entry["limit"] = limit
    if order is not None:
        entry["order_by"] = _expand_order_list(order, ref_base="$source")
    if required is not None:
        entry["required"] = required
    return entry


def _all_adjacent_includes(
    anchor: str,
    rel_index: dict[str, RelInfo],
    from_ref: str,
) -> dict[str, dict[str, Any]]:
    """Resolve depth-1 adjacency for an ``include: all_adjacent`` directive.

    Returns include entries keyed by relationship name (self-ref edges emit two
    keys: ``<name>_out`` and ``<name>_in``). Resolved from THIS config's
    relationships; structured so a composed schema (base + overlays) could be
    passed via ``rel_index`` later without changing the algorithm.

    NOTE (invariant 2): ``all_adjacent`` only populates context/include -- it never
    alters the top-level result shape, which ``select``/``returns`` own.
    """
    includes: dict[str, dict[str, Any]] = {}
    # Deterministic order: iterate relationships in declaration order.
    for name, info in rel_index.items():
        touches_anchor = anchor in (info.from_entity, info.to_entity)
        if not touches_anchor:
            continue
        if info.is_self_ref:
            includes[f"{name}_out"] = _include_entry(
                from_ref=from_ref, relationship=name, direction="outgoing"
            )
            includes[f"{name}_in"] = _include_entry(
                from_ref=from_ref, relationship=name, direction="incoming"
            )
        else:
            direction = "outgoing" if anchor == info.from_entity else "incoming"
            includes[name] = _include_entry(
                from_ref=from_ref, relationship=name, direction=direction
            )
    return includes


def _adjacent_relationship_names(anchor: str, rel_index: dict[str, RelInfo]) -> list[str]:
    """Distinct relationship names adjacent to the anchor (declaration order)."""
    return [
        name for name, info in rel_index.items() if anchor in (info.from_entity, info.to_entity)
    ]


# ---------------------------------------------------------------------------
# Select projection
# ---------------------------------------------------------------------------


def _expand_select(
    raw_select: dict[str, Any],
    *,
    anchor: str,
    primary_key: str | None,
    rel_index: dict[str, RelInfo],
    include_names: set[str],
    query_name: str,
) -> dict[str, Any]:
    """Expand a compact ``select:`` block into explicit ``$`` projection refs.

    Handles ``properties``, ``counts``, ``items`` sub-blocks and verbatim deep
    projections. ``counts``/``items`` reference an edge by relationship name; a
    self-ref ``>``/``<`` edge MUST be aliased (the bare arrow is too subtle for an
    output field -> error if unaliased).
    """
    out: dict[str, Any] = {}

    # `properties: [a, b, <pk>]` -> a: $result.properties.a ; <pk> -> $result.entity_id
    for prop in raw_select.get("properties", []):
        if primary_key is not None and prop == primary_key:
            out[prop] = "$result.entity_id"
        else:
            out[prop] = f"$result.properties.{prop}"

    # `counts` -> <field>_count: $include.<include>.count
    counts = raw_select.get("counts", {})
    for alias, rel_ref, include_name in _iter_select_edges(
        counts, anchor, rel_index, include_names, query_name, kind="counts"
    ):
        out[f"{alias}_count"] = f"$include.{include_name}.count"

    # `items` -> <field>: $include.<include>.items
    items = raw_select.get("items", {})
    for alias, rel_ref, include_name in _iter_select_edges(
        items, anchor, rel_index, include_names, query_name, kind="items"
    ):
        out[alias] = f"$include.{include_name}.items"

    # Any remaining keys are verbatim deep/custom projections (e.g.
    # latest_review_request_id: $include.latest_review.items.0.source.entity_id).
    for key, value in raw_select.items():
        if key in {"properties", "counts", "items"}:
            continue
        out[key] = value

    return out


def _iter_select_edges(
    spec: Any,
    anchor: str,
    rel_index: dict[str, RelInfo],
    include_names: set[str],
    query_name: str,
    *,
    kind: str,
) -> list[tuple[str, str, str]]:
    """Normalize a counts/items spec to (output_alias, rel_ref, include_name) tuples.

    ``[<rel>]``                -> alias derived from the rel; include key == rel.
    ``{<alias>: <rel>}``       -> explicit alias; include key derived from the rel.

    For self-ref ``>``/``<`` edges the include key is ``<name>_out``/``<name>_in``.
    A bare (unaliased) self-ref ``>``/``<`` edge in select is an error.
    """
    results: list[tuple[str, str, str]] = []

    if isinstance(spec, list):
        entries: list[tuple[str | None, str]] = [(None, ref) for ref in spec]
    elif isinstance(spec, dict):
        entries = [(alias, ref) for alias, ref in spec.items()]
    else:
        raise CompactExpansionError(
            f"query '{query_name}' select {kind} must be a list or mapping, got {spec!r}"
        )

    for alias, rel_ref in entries:
        name, include_key, marker = _resolve_select_edge(rel_ref, anchor, rel_index, query_name)
        if alias is None:
            if marker is not None:
                raise CompactExpansionError(
                    f"query '{query_name}' select {kind}: self-ref edge '{rel_ref}' must be "
                    f"aliased (use {{<alias>: {rel_ref}}}); a bare arrow is too subtle for an "
                    f"output field"
                )
            alias = name
        results.append((alias, rel_ref, include_key))
    return results


def _resolve_select_edge(
    rel_ref: str,
    anchor: str,
    rel_index: dict[str, RelInfo],
    query_name: str,
) -> tuple[str, str, str | None]:
    """Resolve a select edge reference to (canonical_name, include_key, marker).

    ``include_key`` matches the key that ``all_adjacent`` would have produced:
    relationship name for normal edges, ``<name>_out``/``<name>_in`` for self-ref.
    """
    marker: str | None = None
    name = rel_ref
    if name.endswith(">"):
        marker = ">"
        name = name[:-1]
    elif name.endswith("<"):
        marker = "<"
        name = name[:-1]

    info = rel_index.get(name)
    if info is None:
        raise CompactExpansionError(
            f"query '{query_name}' select references unknown relationship '{name}'"
        )

    if info.is_self_ref:
        if marker is None:
            raise CompactExpansionError(
                f"query '{query_name}' select: self-ref edge '{name}' must carry a "
                f"'>'/'<' direction marker"
            )
        suffix = "out" if marker == ">" else "in"
        return name, f"{name}_{suffix}", marker

    return name, name, None


# ---------------------------------------------------------------------------
# Named queries
# ---------------------------------------------------------------------------

# Knobs that carry a decision and stay explicit (no defaulting).
_DECISION_KNOBS = ("mode", "returns", "relationship_state")
# Pass-through knobs the author may set; defaulted when omitted.
_PASSTHROUGH_KNOBS = ("result_shape", "max_paths", "max_paths_per_result", "limit")

_COMPACT_QUERY_KEYS = {
    *_DECISION_KNOBS,
    *_PASSTHROUGH_KNOBS,
    "description",
    "entry_point",
    "where",
    "include",
    "bound",
    "select",
    "traverse",
    "traverse_all",
    "direction",
    "as",
    "max_depth",
    "order",
}

_EXPLICIT_QUERY_MARKER = "explicit"
_EXPLICIT_QUERY_SUGGESTION = (
    "If this query is intentionally authored in the explicit engine schema, "
    "add 'explicit: true' to its body."
)
_EXPLICIT_QUERY_FORBIDDEN_COMPACT_KEYS = (
    "traverse",
    "traverse_all",
    "bound",
    "order",
    "as",
    "max_depth",
    "direction",
)


def _reject_unknown_named_query_keys(name: str, body: dict[str, Any]) -> None:
    """Fail closed on compact query keys, with explicit-schema escape-hatch guidance."""
    try:
        _reject_unknown_keys(f"query '{name}'", body, _COMPACT_QUERY_KEYS)
    except CompactExpansionError as exc:
        raise CompactExpansionError(f"{exc}. {_EXPLICIT_QUERY_SUGGESTION}") from None


def _explicit_named_query_body(name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Return an explicit engine-schema query body only when the opt-in marker is present."""
    if body.get(_EXPLICIT_QUERY_MARKER) is not True:
        return None

    explicit_body = deepcopy(body)
    explicit_body.pop(_EXPLICIT_QUERY_MARKER, None)
    for key in _EXPLICIT_QUERY_FORBIDDEN_COMPACT_KEYS:
        if key in explicit_body:
            raise CompactExpansionError(
                f"query '{name}': explicit body contains compact-grammar key '{key}' "
                "— remove 'explicit: true' or convert the body"
            )
    return explicit_body


def _entity_primary_key(entity_types: dict[str, Any], entity_name: str) -> str | None:
    """Return the primary-key property name for an entity type, if any."""
    entity = entity_types.get(entity_name)
    if entity is None:
        return None
    for prop_name, prop in entity.get("properties", {}).items():
        if isinstance(prop, dict) and prop.get("primary_key"):
            return str(prop_name)
    return None


def _expand_named_query(
    name: str,
    body: dict[str, Any],
    *,
    rel_index: dict[str, RelInfo],
    entity_types: dict[str, Any],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Expand one compact named query to the explicit ``NamedQuerySchema`` shape."""
    if not isinstance(body, dict):
        raise CompactExpansionError(f"query '{name}': body must be a mapping")
    explicit_body = _explicit_named_query_body(name, body)
    if explicit_body is not None:
        return explicit_body
    _reject_unknown_named_query_keys(name, body)

    mode = body.get("mode")
    if mode is None:
        raise CompactExpansionError(f"query '{name}': 'mode' is required and must be explicit")
    returns = body.get("returns")
    if returns is None:
        raise CompactExpansionError(f"query '{name}': 'returns' is required and must be explicit")
    returns = str(returns)

    is_traversal = mode == "traversal"
    anchor: str | None = None
    if is_traversal and "entry_point" in body:
        anchor = str(body["entry_point"])

    out: dict[str, Any] = {"mode": mode}
    if "description" in body:
        out["description"] = body["description"]
    if is_traversal:
        if anchor is None:
            raise CompactExpansionError(f"query '{name}': traversal mode requires 'entry_point'")
        out["entry_point"] = anchor
    out["returns"] = returns

    # Result shape: explicit, else the inert default for the mode. Collection
    # queries default to `entity` (one row per collected entity); traversal queries
    # default to `path` (the engine default, required by path budgets and the
    # `reviewable` trust axis). This matches the explicit reference shapes.
    default_shape = "path" if is_traversal else _DEFAULT_RESULT_SHAPE
    result_shape = body.get("result_shape", default_shape)
    out["result_shape"] = result_shape

    # Collection-level where targets $result.
    if "where" in body:
        out["where"] = _expand_where(body["where"], scope="result")

    # Build include set: explicit named bounded sets, all_adjacent, plus auto
    # includes derived from select counts/items and bound caps.
    include_directive = body.get("include")
    bound = body.get("bound", {})
    select = body.get("select")
    traverse = body.get("traverse")

    includes: dict[str, dict[str, Any]] = {}
    # Includes/select edges anchor on the entity in scope at the include point:
    #   - all_adjacent context dump: the entry node ($entry, entry_point type)
    #   - traversal with explicit traverse: the traversed result node ($result,
    #     `returns` type)
    #   - collection: the collected result node ($result, `returns` type)
    is_all_adjacent = include_directive == "all_adjacent"
    include_anchor: str
    if is_traversal and is_all_adjacent:
        if anchor is None:
            raise CompactExpansionError(
                f"query '{name}': include all_adjacent requires a traversal entry_point"
            )
        include_anchor = anchor
        include_from_ref = "$entry"
    else:
        # Traversal-with-explicit-traverse and collection both anchor on the
        # result node (`returns` type), reached via $result.
        include_anchor = returns
        include_from_ref = "$result"

    # all_adjacent -> include every adjacent edge (depth 1) off the anchor.
    if is_all_adjacent:
        includes.update(_all_adjacent_includes(include_anchor, rel_index, "$entry"))
    elif isinstance(include_directive, dict):
        # `include: {<name>: {rel: <rel>, ...}}` defines NEW named bounded sets.
        for set_name, set_body in include_directive.items():
            includes[set_name] = _expand_named_include(
                set_name,
                set_body,
                include_anchor,
                rel_index,
                include_from_ref,
                name,
                entry_anchor=anchor if is_traversal else None,
                result_anchor=returns,
            )
    elif include_directive is not None:
        raise CompactExpansionError(
            f"query '{name}': include must be 'all_adjacent' or a mapping of named sets"
        )

    # Auto-include edges referenced by select counts/items (derive, never re-list).
    if select is not None:
        includes = _add_auto_includes_from_select(
            includes, select, include_anchor, rel_index, include_from_ref, name
        )

    # `bound: {<rel>: {limit, order, where}}` caps an auto/all_adjacent set.
    for rel_ref, cap in bound.items():
        include_key = _bound_include_key(rel_ref, include_anchor, rel_index, name)
        existing = includes.get(include_key)
        capped = _apply_bound(
            existing, rel_ref, cap, include_anchor, rel_index, include_from_ref, name
        )
        includes[include_key] = capped

    # Traverse steps (explicit traversal).
    traversal_steps: list[dict[str, Any]] = []
    if is_traversal:
        if traverse is not None:
            # Direction inference walks the chain: each hop anchors on the
            # previous hop's landing entity, so only genuinely ambiguous hops
            # (relationship lists, direction both, external relationships)
            # need an explicit direction.
            hop_anchor: str | None = anchor
            for step in traverse:
                expanded_step = _expand_traverse_step(step, hop_anchor, rel_index, name)
                traversal_steps.append(expanded_step)
                hop_anchor = _traverse_landing_entity(expanded_step, rel_index)
        elif body.get("traverse_all") is not None:
            traversal_steps.append(_expand_traverse_all_step(body, anchor, rel_index, name))
        elif is_all_adjacent:
            # all_adjacent context dump: traverse every adjacent edge, both
            # directions, as a single fan-out step. include_anchor == entry_point here.
            adjacent = _adjacent_relationship_names(include_anchor, rel_index)
            traversal_steps.append({"relationship": adjacent, "direction": "both", "as": "context"})
        else:
            raise CompactExpansionError(
                f"query '{name}': traversal mode requires 'traverse', 'traverse_all', "
                f"or 'include: all_adjacent'"
            )
        out["traversal"] = traversal_steps

    if includes:
        out["include"] = includes

    # Select projection. The projected entity is the result/scope node, so its
    # primary key and edge anchoring both use `include_anchor`.
    if select is not None:
        primary_key = _entity_primary_key(entity_types, include_anchor)
        out["select"] = _expand_select(
            select,
            anchor=include_anchor,
            primary_key=primary_key,
            rel_index=rel_index,
            include_names=set(includes),
            query_name=name,
        )

    # Order.
    if "order" in body:
        out["order_by"] = _expand_order_list(body["order"])

    # Decision-bearing trust axis (explicit only; never defaulted silently). One
    # caveat: a plain collection ENTITY query with no relationship surface (no
    # include) has no edges whose review state could matter, and the engine rejects
    # `reviewable`/`pending` there. The explicit reference config drops it in that
    # case, so we follow suit (and surface it via `warnings`) rather than emit an
    # invalid config. This is the one place the author's uniform
    # `relationship_state: reviewable` does not survive 1:1.
    if "relationship_state" in body:
        rel_state = body["relationship_state"]
        has_rel_surface = bool(includes)
        collection_entity_no_surface = (
            not is_traversal and result_shape == "entity" and not has_rel_surface
        )
        if rel_state in {"reviewable", "pending"} and collection_entity_no_surface:
            if warnings is not None:
                warnings.append(
                    f"query '{name}': dropped relationship_state '{rel_state}' -- a plain "
                    f"collection entity query has no relationship surface; the engine "
                    f"rejects it there (matches the explicit reference config)."
                )
        else:
            out["relationship_state"] = rel_state
            # allow_relationship_state_override is an inert resource guard.
            out["allow_relationship_state_override"] = _DEFAULT_ALLOW_REL_STATE_OVERRIDE

    # Inert resource guards: default when omitted; only collection mode disallows
    # path budgets, so guard those.
    if is_traversal:
        if result_shape != "entity":
            out["allow_relationship_state_override"] = out.get(
                "allow_relationship_state_override", _DEFAULT_ALLOW_REL_STATE_OVERRIDE
            )
            out["max_paths"] = body.get("max_paths", _DEFAULT_MAX_PATHS)
            out["max_paths_per_result"] = body.get(
                "max_paths_per_result", _DEFAULT_MAX_PATHS_PER_RESULT
            )
    else:
        out["limit"] = body.get("limit", _DEFAULT_LIMIT)

    return out


def _expand_named_include(
    set_name: str,
    set_body: dict[str, Any],
    anchor: str | None,
    rel_index: dict[str, RelInfo],
    from_ref: str,
    query_name: str,
    *,
    entry_anchor: str | None = None,
    result_anchor: str | None = None,
) -> dict[str, Any]:
    """Expand a NEW named bounded include set (`include: {name: {relationship:..., ...}}`)."""
    if not isinstance(set_body, dict):
        raise CompactExpansionError(
            f"query '{query_name}' include '{set_name}': body must be a mapping"
        )
    _reject_unknown_keys(
        f"query '{query_name}' include '{set_name}'",
        set_body,
        {"relationship", "from", "direction", "limit", "order", "where", "required"},
    )

    rel_ref = set_body.get("relationship")
    if rel_ref is None:
        raise CompactExpansionError(
            f"query '{query_name}' include '{set_name}': must define 'relationship'"
        )
    effective_from_ref = from_ref
    effective_anchor = anchor
    if "from" in set_body:
        authored_from = set_body["from"]
        if authored_from not in {"$entry", "$result"}:
            raise CompactExpansionError(
                f"query '{query_name}' include '{set_name}': from must be $entry or $result"
            )
        effective_from_ref = authored_from
        if authored_from == "$entry":
            if entry_anchor is None:
                raise CompactExpansionError(
                    f"query '{query_name}' include '{set_name}': from $entry requires "
                    "a traversal query"
                )
            effective_anchor = entry_anchor
        else:
            effective_anchor = result_anchor or anchor

    direction_override = set_body.get("direction")
    if direction_override is not None:
        if direction_override not in {"incoming", "outgoing"}:
            raise CompactExpansionError(
                f"query '{query_name}' include '{set_name}': direction must be incoming or outgoing"
            )
        rel_name = _canonical_relationship_name(
            rel_ref,
            rel_index,
            context=f"query '{query_name}' include '{set_name}'",
            allow_external=True,
        )
        direction = direction_override
    else:
        rel_name, direction = _resolve_direction(
            rel_ref,
            effective_anchor or "",
            rel_index,
            context=f"query '{query_name}' include '{set_name}'",
        )
    return _include_entry(
        from_ref=effective_from_ref,
        relationship=rel_name,
        direction=direction,
        limit=set_body.get("limit"),
        order=set_body.get("order"),
        where=set_body.get("where"),
        required=set_body.get("required"),
    )


def _add_auto_includes_from_select(
    includes: dict[str, dict[str, Any]],
    select: dict[str, Any],
    anchor: str | None,
    rel_index: dict[str, RelInfo],
    from_ref: str,
    query_name: str,
) -> dict[str, dict[str, Any]]:
    """Auto-create include entries for edges referenced by select counts/items."""
    for kind in ("counts", "items"):
        spec = select.get(kind)
        if spec is None:
            continue
        refs = spec if isinstance(spec, list) else list(spec.values())
        for rel_ref in refs:
            name, include_key, marker = _resolve_select_edge(
                rel_ref, anchor or "", rel_index, query_name
            )
            if include_key in includes:
                continue
            if marker is not None:
                direction = "outgoing" if marker == ">" else "incoming"
            else:
                _name, direction = _resolve_direction(
                    name, anchor or "", rel_index, context=f"query '{query_name}' select"
                )
            includes[include_key] = _include_entry(
                from_ref=from_ref, relationship=name, direction=direction
            )
    return includes


def _bound_include_key(
    rel_ref: str,
    anchor: str | None,
    rel_index: dict[str, RelInfo],
    query_name: str,
) -> str:
    """Compute the include key a `bound:` entry caps."""
    name = rel_ref.rstrip("><")
    info = rel_index.get(name)
    if info is None:
        raise CompactExpansionError(
            f"query '{query_name}' bound references unknown relationship '{name}'"
        )
    if info.is_self_ref:
        if rel_ref.endswith(">"):
            return f"{name}_out"
        if rel_ref.endswith("<"):
            return f"{name}_in"
        raise CompactExpansionError(
            f"query '{query_name}' bound: self-ref edge '{name}' needs a '>'/'<' marker"
        )
    return name


def _apply_bound(
    existing: dict[str, Any] | None,
    rel_ref: str,
    cap: dict[str, Any],
    anchor: str | None,
    rel_index: dict[str, RelInfo],
    from_ref: str,
    query_name: str,
) -> dict[str, Any]:
    """Apply a `bound:` cap (limit/order/where) to an include set."""
    if not isinstance(cap, dict):
        raise CompactExpansionError(
            f"query '{query_name}' bound '{rel_ref}': cap must be a mapping"
        )
    _reject_unknown_keys(
        f"query '{query_name}' bound '{rel_ref}'", cap, {"limit", "order", "where"}
    )
    if existing is not None:
        entry = dict(existing)
    else:
        rel_name, direction = _resolve_direction(
            rel_ref, anchor or "", rel_index, context=f"query '{query_name}' bound"
        )
        entry = {
            "from": from_ref,
            "relationship": rel_name,
            "direction": direction,
            "many": True,
        }
    if "where" in cap:
        entry["where"] = _expand_where(cap["where"], scope="source")
    if "limit" in cap:
        entry["limit"] = cap["limit"]
    if "order" in cap:
        entry["order_by"] = _expand_order_list(cap["order"], ref_base="$source")
    return entry


def _canonical_relationship_name(
    rel_ref: str,
    rel_index: dict[str, RelInfo],
    *,
    context: str,
    allow_external: bool = False,
) -> str:
    """Validate a relationship reference and strip any compact direction marker."""
    if not isinstance(rel_ref, str):
        raise CompactExpansionError(
            f"{context}: relationship reference must be a string, got {type(rel_ref).__name__}"
        )
    name = rel_ref.rstrip("><")
    if name not in rel_index and not allow_external:
        raise CompactExpansionError(
            f"{context}: relationship '{name}' is not defined in this config"
        )
    return name


def _traverse_landing_entity(
    expanded_step: dict[str, Any],
    rel_index: dict[str, RelInfo],
) -> str | None:
    """Entity type an expanded traverse step lands on, for chained inference.

    Returns None when the landing is ambiguous: relationship lists, direction
    'both', or relationships not defined in this layer (external/base refs).
    """
    rel = expanded_step.get("relationship")
    direction = expanded_step.get("direction")
    if not isinstance(rel, str):
        return None
    info = rel_index.get(rel)
    if info is None:
        return None
    if direction == "outgoing":
        return info.to_entity
    if direction == "incoming":
        return info.from_entity
    return None


def _expand_traverse_step(
    step: dict[str, Any],
    anchor: str | None,
    rel_index: dict[str, RelInfo],
    query_name: str,
) -> dict[str, Any]:
    """Expand a single explicit ``traverse:`` step."""
    if not isinstance(step, dict):
        raise CompactExpansionError(f"query '{query_name}' traverse step must be a mapping")
    _reject_unknown_keys(
        f"query '{query_name}' traverse step",
        step,
        {"relationship", "direction", "as", "where", "max_depth", "required"},
    )
    rel_ref = step.get("relationship")
    if rel_ref is None:
        raise CompactExpansionError(
            f"query '{query_name}' traverse step must define 'relationship'"
        )

    direction_override = step.get("direction")
    if direction_override is not None and direction_override not in {
        "outgoing",
        "incoming",
        "both",
    }:
        raise CompactExpansionError(
            f"query '{query_name}' traverse step: direction must be outgoing, incoming, or both"
        )

    if isinstance(rel_ref, list):
        if direction_override is None:
            raise CompactExpansionError(
                f"query '{query_name}' traverse step: relationship lists require 'direction'"
            )
        rel_name: str | list[str] = [
            _canonical_relationship_name(
                item,
                rel_index,
                context=f"query '{query_name}' traverse",
                allow_external=True,
            )
            for item in rel_ref
        ]
        direction = direction_override
    elif direction_override is not None:
        rel_name = _canonical_relationship_name(
            rel_ref,
            rel_index,
            context=f"query '{query_name}' traverse",
            allow_external=True,
        )
        direction = direction_override
    else:
        if not anchor:
            raise CompactExpansionError(
                f"query '{query_name}' traverse step: direction cannot be inferred — "
                f"the previous hop's landing entity is ambiguous (relationship list, "
                f"direction 'both', or an external relationship); add an explicit "
                f"'direction:' to this step"
            )
        rel_name, direction = _resolve_direction(
            rel_ref, anchor or "", rel_index, context=f"query '{query_name}' traverse"
        )

    out: dict[str, Any] = {"relationship": rel_name, "direction": direction}
    if "as" in step:
        out["as"] = step["as"]
    if "where" in step:
        out["where"] = _expand_where(step["where"], scope="candidate")
    if "max_depth" in step:
        out["max_depth"] = step["max_depth"]
    if "required" in step:
        out["required"] = step["required"]
    return out


def _expand_traverse_all_step(
    body: dict[str, Any],
    anchor: str | None,
    rel_index: dict[str, RelInfo],
    query_name: str,
) -> dict[str, Any]:
    """Expand a ``traverse_all: [rels]`` + ``direction:`` scoped fan-out step."""
    rels = body["traverse_all"]
    if not isinstance(rels, list):
        raise CompactExpansionError(
            f"query '{query_name}' traverse_all must be a list of relationships"
        )
    direction = body.get("direction", "both")
    # Validate each relationship exists; direction is given explicitly.
    for rel_ref in rels:
        name = rel_ref.rstrip("><")
        if name not in rel_index:
            raise CompactExpansionError(
                f"query '{query_name}' traverse_all references unknown relationship '{name}'"
            )
    out: dict[str, Any] = {
        "relationship": list(rels),
        "direction": direction,
        "as": _traverse_all_alias(body),
    }
    if "max_depth" in body:
        out["max_depth"] = body["max_depth"]
    return out


def _traverse_all_alias(body: dict[str, Any]) -> str:
    """Derive a stable alias for a traverse_all step."""
    return str(body.get("as", "lineage"))


# ---------------------------------------------------------------------------
# Query templates (`for: [...]` + `$T` substitution)
# ---------------------------------------------------------------------------


def _expand_query_template(
    template_name: str,
    body: dict[str, Any],
    *,
    rel_index: dict[str, RelInfo],
    entity_types: dict[str, Any],
    warnings: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Expand a `superseded_$T:` template with `for: [TypeA, TypeB]` to one query per type.

    ``$T`` is substituted in the query name, ``returns``, and ``description``.
    """
    if not isinstance(body, dict):
        raise CompactExpansionError(f"query template '{template_name}': body must be a mapping")
    _reject_unknown_keys(f"query template '{template_name}'", body, _COMPACT_QUERY_KEYS | {"for"})

    types = body["for"]
    out: dict[str, dict[str, Any]] = {}
    base = {key: value for key, value in body.items() if key != "for"}
    for type_name in types:
        concrete_name = template_name.replace("$T", _pluralize_snake(type_name))
        concrete_body = _substitute_template(base, type_name)
        out[concrete_name] = _expand_named_query(
            concrete_name,
            concrete_body,
            rel_index=rel_index,
            entity_types=entity_types,
            warnings=warnings,
        )
    return out


def _substitute_template(body: dict[str, Any], type_name: str) -> dict[str, Any]:
    """Substitute ``$T`` -> type_name in template string values."""
    result: dict[str, Any] = {}
    for key, value in body.items():
        if isinstance(value, str):
            result[key] = value.replace("$T", type_name)
        else:
            result[key] = value
    return result


def _pluralize_snake(type_name: str) -> str:
    """Map an entity type name to the snake_case plural used in template query names.

    ``Decision`` -> ``decisions``; ``WorkItem`` -> ``work_items``.
    """
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", type_name).lower()
    return snake + "s"


def _all_adjacent_query_intents(name: str, body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return concrete compact query bodies that need final all_adjacent expansion."""
    if not isinstance(body, dict):
        return {}
    if "for" not in body:
        if body.get("include") == "all_adjacent":
            return {name: deepcopy(body)}
        return {}

    intents: dict[str, dict[str, Any]] = {}
    types = body["for"]
    base = {key: value for key, value in body.items() if key != "for"}
    for type_name in types:
        concrete_name = name.replace("$T", _pluralize_snake(type_name))
        concrete_body = _substitute_template(base, type_name)
        if concrete_body.get("include") == "all_adjacent":
            intents[concrete_name] = deepcopy(concrete_body)
    return intents


# ---------------------------------------------------------------------------
# Mutation guards
# ---------------------------------------------------------------------------

_GUARD_TRIGGER_RE = re.compile(r"^\s*(?P<entity>\w+)\.(?P<prop>\w+)\s*->\s*(?P<value>.+?)\s*$")
_COWRITE_RE = re.compile(r"^\s*(?P<entity>\w+)\s+via\s+(?P<rel>\w+)\s*$")


def _expand_mutation_guards(raw_guards: list[Any]) -> list[dict[str, Any]]:
    """Expand compact ``mutation_guards:`` to explicit auditable guards.

    Trigger: ``when: <Entity>.<prop> -> <value|[values]>`` -> entity_type/property/new_value.
    Condition (``require:``), one of three types -- expanded 1:1 with NO identity magic:
        ``{co_write: <Entity> via <relationship>, kind: ...}`` -> type co_write
        ``{allowed_actors: [...]}``  -> type actor, allowed_actor_ids (literal passthrough)
        ``{query:..., params:..., min_count/max_count:...}`` -> type query
    Optional ``where:`` is a structured predicate map (candidate scope only) that
    scopes the trigger -- the guard fires only when the mutated entity matches. It
    passes through unchanged (same shape as ``QueryPredicateSpec``). Optional
    ``where_related:``/``where_not_related:`` are lists of related-edge predicates
    (same ``RelatedPredicateSpec`` shape as query traversal steps) that further
    scope the trigger; they pass through unchanged.
    """
    out: list[dict[str, Any]] = []
    for item in raw_guards:
        if not isinstance(item, dict) or len(item) != 1:
            raise CompactExpansionError(
                f"mutation guard must be a single-key mapping, got {item!r}"
            )
        name, body = next(iter(item.items()))
        if not isinstance(body, dict):
            raise CompactExpansionError(f"mutation guard '{name}': body must be a mapping")
        _reject_unknown_keys(
            f"mutation guard '{name}'",
            body,
            {"when", "require", "message", "where", "where_related", "where_not_related"},
        )
        guard: dict[str, Any] = {"name": name}

        when = body.get("when")
        if when is None:
            raise CompactExpansionError(f"mutation guard '{name}': 'when' is required")
        match = _GUARD_TRIGGER_RE.match(when)
        if match is None:
            raise CompactExpansionError(
                f"mutation guard '{name}': when must be '<Entity>.<prop> -> <value>', got '{when}'"
            )
        guard["entity_type"] = match.group("entity")
        guard["property"] = match.group("prop")
        guard["new_value"] = _parse_guard_value(match.group("value"))

        require = body.get("require")
        if require is None:
            raise CompactExpansionError(f"mutation guard '{name}': 'require' is required")
        guard["condition"] = _expand_guard_condition(name, require)

        if "message" in body:
            guard["message"] = body["message"]
        if "where" in body:
            guard["where"] = body["where"]
        if "where_related" in body:
            guard["where_related"] = body["where_related"]
        if "where_not_related" in body:
            guard["where_not_related"] = body["where_not_related"]
        out.append(guard)
    return out


def _parse_guard_value(value: str) -> Any:
    """Parse the guard trigger value: a single value or a `[a, b]` list."""
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [part.strip() for part in inner.split(",")]
    return text


def _expand_guard_condition(name: str, require: dict[str, Any]) -> dict[str, Any]:
    """Expand a guard ``require:`` block into one explicit condition union member."""
    if not isinstance(require, dict):
        raise CompactExpansionError(f"mutation guard '{name}': require must be a mapping")
    if "co_write" in require:
        _reject_unknown_keys(
            f"mutation guard '{name}' require co_write", require, {"co_write", "kind"}
        )
        match = _COWRITE_RE.match(require["co_write"])
        if match is None:
            raise CompactExpansionError(
                f"mutation guard '{name}': co_write must be '<Entity> via <relationship>', "
                f"got '{require['co_write']}'"
            )
        requires: dict[str, Any] = {
            "entity_type": match.group("entity"),
            "via_relationship": match.group("rel"),
        }
        if "kind" in require:
            requires["kind"] = require["kind"]
        return {"type": "co_write", "requires": requires}

    if "allowed_actors" in require:
        _reject_unknown_keys(
            f"mutation guard '{name}' require allowed_actors", require, {"allowed_actors"}
        )
        # LITERAL passthrough -- no identity resolution, no invented actors.
        return {
            "type": "actor",
            "allowed_actor_ids": list(require["allowed_actors"]),
        }

    if "query" in require:
        _reject_unknown_keys(
            f"mutation guard '{name}' require query",
            require,
            {"query", "params", "min_count", "max_count"},
        )
        condition: dict[str, Any] = {
            "type": "query",
            "query_name": require["query"],
        }
        if "params" in require:
            condition["params"] = require["params"]
        if "min_count" in require:
            condition["min_count"] = require["min_count"]
        if "max_count" in require:
            condition["max_count"] = require["max_count"]
        return condition

    raise CompactExpansionError(
        f"mutation guard '{name}': require must declare co_write, allowed_actors, or query"
    )


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------


def _expand_quality_checks(raw_checks: list[Any]) -> list[dict[str, Any]]:
    """Expand compact ``quality_checks:`` to explicit discriminated checks.

    Cardinality::

        cardinality: {entity:, relationship:, direction: out|in, min:, max:}

    Property::

        property: <relationship>.<field>   (snake_case -> relationship target)
        property: <EntityType>.<field>     (CapWords -> entity target)
        rule: non_empty|required
    """
    out: list[dict[str, Any]] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            raise CompactExpansionError(f"quality check must be a mapping, got {item!r}")
        if "name" in item and "kind" in item:
            out.append(deepcopy(item))
            continue
        if len(item) != 1:
            raise CompactExpansionError(f"quality check must be a single-key mapping, got {item!r}")

        name, body = next(iter(item.items()))

        if not isinstance(body, dict):
            raise CompactExpansionError(f"quality check '{name}': body must be a mapping")
        if "cardinality" in body:
            _reject_unknown_keys(
                f"quality check '{name}'", body, {"cardinality", "description", "severity"}
            )
            out.append(_expand_cardinality_check(name, body))
        elif "property" in body:
            _reject_unknown_keys(
                f"quality check '{name}'", body, {"property", "rule", "description", "severity"}
            )
            out.append(_expand_property_check(name, body))
        else:
            raise CompactExpansionError(
                f"quality check '{name}': must define 'cardinality' or 'property'"
            )
    return out


def _expand_cardinality_check(name: str, body: dict[str, Any]) -> dict[str, Any]:
    card = body["cardinality"]
    if not isinstance(card, dict):
        raise CompactExpansionError(f"quality check '{name}': 'cardinality' must be a mapping")
    _reject_unknown_keys(
        f"quality check '{name}' cardinality",
        card,
        {"entity", "relationship", "direction", "min", "max"},
    )
    direction = card.get("direction")
    if direction not in ("out", "in"):
        raise CompactExpansionError(
            f"quality check '{name}': cardinality.direction must be 'out' or 'in', "
            f"got {direction!r}"
        )
    check: dict[str, Any] = {
        "name": name,
        "kind": "cardinality",
        "entity_type": card["entity"],
        "relationship_type": card["relationship"],
        "direction": "outgoing" if direction == "out" else "incoming",
    }
    if "description" in body:
        check["description"] = body["description"]
    if "severity" in body:
        check["severity"] = body["severity"]
    if "min" in card:
        check["min_count"] = card["min"]
    if "max" in card:
        check["max_count"] = card["max"]
    return check


def _expand_property_check(name: str, body: dict[str, Any]) -> dict[str, Any]:
    property_path = body["property"]
    if "." not in property_path:
        raise CompactExpansionError(
            f"quality check '{name}': property must be '<relationship>.<field>' or "
            f"'<EntityType>.<field>', got '{property_path}'"
        )
    subject, field = property_path.split(".", 1)
    # Entity types are CapWords, relationship types are snake_case — the
    # casing convention is load-bearing here and everywhere in kit configs.
    if subject[:1].isupper():
        check: dict[str, Any] = {
            "name": name,
            "kind": "property",
            "target": "entity",
            "entity_type": subject,
            "property": field,
            "rule": body["rule"],
        }
    else:
        check = {
            "name": name,
            "kind": "property",
            "target": "relationship",
            "relationship_type": subject,
            "property": field,
            "rule": body["rule"],
        }
    if "description" in body:
        check["description"] = body["description"]
    if "severity" in body:
        check["severity"] = body["severity"]
    return check


# ---------------------------------------------------------------------------
# Top-level expansion
# ---------------------------------------------------------------------------

# Authoring-only top-level keys that are consumed/stripped, never emitted.
_AUTHORING_ONLY_KEYS = {"presets", "metadata"}

_PASSTHROUGH_TOP_LEVEL_KEYS = {
    "feedback_profiles",
    "outcome_profiles",
    "decision_policies",
    "contracts",
    "artifacts",
    "providers",
    "workflows",
    "runtime",
    "tests",
}

_COMPACT_TOP_LEVEL_KEYS = {
    *_AUTHORING_ONLY_KEYS,
    *_PASSTHROUGH_TOP_LEVEL_KEYS,
    "version",
    "name",
    "description",
    "cruxible_version",
    "extends",
    "enums",
    "entity_types",
    "relationships",
    "named_queries",
    "mutation_guards",
    "quality_checks",
    "constraints",
}


def looks_compact(data: Any) -> bool:
    """Heuristic: does this parsed config use the compact authoring grammar?

    True when the config carries an authoring-only top-level key (``presets`` /
    ``metadata``), expresses relationships as single-key ``name: 'From -> To'`` maps,
    or declares entity-type properties as scalar strings. Explicit (engine) ``CoreConfig``
    YAML never does any of these, so this stays False for explicit configs and their load
    path is unchanged. Used by the loader to decide whether to expand before validating.
    """
    if not isinstance(data, dict):
        return False
    if data.keys() & _AUTHORING_ONLY_KEYS:
        return True
    rels = data.get("relationships")
    if isinstance(rels, list) and rels:
        first = rels[0]
        if isinstance(first, dict) and len(first) == 1:
            (value,) = first.values()
            if isinstance(value, str) and "->" in value:
                return True
    entity_types = data.get("entity_types")
    if isinstance(entity_types, dict):
        for spec in entity_types.values():
            props = spec.get("properties") if isinstance(spec, dict) else None
            if isinstance(props, dict) and any(isinstance(v, str) for v in props.values()):
                return True
    return False


def materialize_all_adjacent_queries(
    config: dict[str, Any],
    all_adjacent_queries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Re-expand compact all_adjacent queries against a final config mapping."""
    if not all_adjacent_queries:
        return config

    named_queries = dict(config.get("named_queries", {}))
    if not named_queries:
        return config

    rel_index = _expanded_relationship_index(config.get("relationships", []))
    entity_types = config.get("entity_types", {})
    changed = False
    for query_name, query_body in all_adjacent_queries.items():
        if query_name not in named_queries:
            continue
        named_queries[query_name] = _expand_named_query(
            query_name,
            deepcopy(query_body),
            rel_index=rel_index,
            entity_types=entity_types,
        )
        changed = True

    if not changed:
        return config

    materialized = dict(config)
    materialized["named_queries"] = named_queries
    return materialized


def _expanded_relationship_index(relationships: list[Any]) -> dict[str, RelInfo]:
    index: dict[str, RelInfo] = {}
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        name = str(rel["name"])
        index[name] = RelInfo(name=name, from_entity=str(rel["from"]), to_entity=str(rel["to"]))
    return index


def expand_compact(source_text: str) -> dict[str, Any]:
    """Expand compact YAML text to a CoreConfig-shaped dict.

    Convenience wrapper returning only the config; use :func:`expand_compact_full`
    when the stripped ``metadata`` (e.g. ``requires_cruxible``) is needed.
    """
    return expand_compact_full(source_text).config


def expand_compact_full(source_text: str) -> ExpandResult:
    """Expand compact YAML text, returning both config and stripped metadata."""
    raw = yaml.safe_load(source_text)
    if not isinstance(raw, dict):
        raise CompactExpansionError("compact source must be a top-level mapping")

    _reject_unknown_keys("compact config", raw, _COMPACT_TOP_LEVEL_KEYS)

    comments = _scan_relationship_comments(source_text)
    warnings: list[str] = []
    all_adjacent_queries: dict[str, dict[str, Any]] = {}

    # presets / metadata are expander-owned: consume, record, strip.
    presets = raw.get("presets", {}) or {}
    policies = presets.get("policies", {}) or {}
    metadata = raw.get("metadata", {}) or {}

    config: dict[str, Any] = {}

    # Top-level scalar fields pass through.
    for key in ("version", "name", "description", "cruxible_version", "extends"):
        if key in raw:
            config[key] = raw[key]

    if "enums" in raw:
        config["enums"] = _expand_enums(raw["enums"])

    entity_types = _expand_entity_types(raw.get("entity_types", {}))
    config["entity_types"] = entity_types

    rel_list, rel_index = _expand_relationships(
        raw.get("relationships", []), policies=policies, comments=comments
    )
    config["relationships"] = rel_list

    # Named queries (plus templates).
    named_queries: dict[str, Any] = {}
    for q_name, q_body in raw.get("named_queries", {}).items():
        all_adjacent_queries.update(_all_adjacent_query_intents(q_name, q_body))
        if isinstance(q_body, dict) and "for" in q_body:
            named_queries.update(
                _expand_query_template(
                    q_name,
                    q_body,
                    rel_index=rel_index,
                    entity_types=entity_types,
                    warnings=warnings,
                )
            )
        else:
            named_queries[q_name] = _expand_named_query(
                q_name,
                q_body,
                rel_index=rel_index,
                entity_types=entity_types,
                warnings=warnings,
            )
    if named_queries:
        config["named_queries"] = named_queries

    if "mutation_guards" in raw:
        config["mutation_guards"] = _expand_mutation_guards(raw["mutation_guards"])

    if "quality_checks" in raw:
        config["quality_checks"] = _expand_quality_checks(raw["quality_checks"])

    if "constraints" in raw:
        config["constraints"] = list(raw["constraints"])

    # Any other recognized top-level keys that aren't authoring-only pass through
    # verbatim (forward-compatible; e.g. runtime, contracts).
    for key in _PASSTHROUGH_TOP_LEVEL_KEYS:
        if key in raw:
            config[key] = raw[key]

    return ExpandResult(
        config=config,
        metadata=metadata,
        warnings=warnings,
        all_adjacent_queries=all_adjacent_queries,
    )


def expand_compact_file(path: str | Path) -> dict[str, Any]:
    """Read a compact YAML file and expand it to a CoreConfig-shaped dict."""
    return expand_compact_file_full(path).config


def expand_compact_file_full(path: str | Path) -> ExpandResult:
    """Read a compact YAML file and expand it, returning config + metadata."""
    text = Path(path).read_text(encoding="utf-8")
    return expand_compact_full(text)


# ---------------------------------------------------------------------------
# Deterministic serialization
# ---------------------------------------------------------------------------


def dump_expanded(config: dict[str, Any]) -> str:
    """Serialize an expanded config dict to deterministic, diff-stable YAML.

    Determinism comes from a fixed top-level key order plus stable handling of
    nested mappings: insertion order is preserved (we build dicts deterministically
    during expansion), block style is forced, and flow style is disabled, so the
    same source always yields byte-identical output.
    """
    ordered = _order_top_level(config)
    return yaml.dump(
        ordered,
        Dumper=_StableDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )


# Deterministic top-level key order matching CoreConfig's logical grouping.
_TOP_LEVEL_ORDER = [
    "version",
    "name",
    "description",
    "cruxible_version",
    "extends",
    "enums",
    "entity_types",
    "relationships",
    "named_queries",
    "mutation_guards",
    "quality_checks",
    "constraints",
    "feedback_profiles",
    "outcome_profiles",
    "decision_policies",
    "contracts",
    "artifacts",
    "providers",
    "workflows",
    "runtime",
    "tests",
]


def _order_top_level(config: dict[str, Any]) -> dict[str, Any]:
    """Return config with top-level keys in a fixed, stable order."""
    ordered: dict[str, Any] = {}
    for key in _TOP_LEVEL_ORDER:
        if key in config:
            ordered[key] = config[key]
    # Any unexpected keys (forward-compat) appended in sorted order for stability.
    for key in sorted(config):
        if key not in ordered:
            ordered[key] = config[key]
    return ordered


class _StableDumper(yaml.SafeDumper):
    """SafeDumper that preserves insertion order and forces block style."""


def _represent_str(dumper: _StableDumper, data: str) -> yaml.Node:
    """Use literal block style for multi-line strings, plain otherwise."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_StableDumper.add_representer(str, _represent_str)
