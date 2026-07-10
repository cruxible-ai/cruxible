"""Generate the structured kit manifest (kits.json) from kits/*/config.yaml.

One JSON document describing every shipped kit: identity, stats, entity
types, relationships (with governance flags), named queries, workflows,
and mutation guards — all straight from the config, no hand-written
copy. Consumers today: the cruxible.ai /kits pages. The same shape is
the seed for a future marketplace index / registry feed, so keep it
config-derived and additive.

Usage:
    uv run python scripts/generate_kit_docs.py [--out dist/kits.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KITS_DIR = REPO_ROOT / "kits"
GITHUB_BASE = "https://github.com/cruxible-ai/cruxible/tree/main/kits"

# Listing kind — the catalog's three tiers: the operating BASE every
# domain composes over (agent-operation), installable domain KITS, and
# published REFERENCE STATES consumed as read-only overlay bases
# (kev-reference — future reference worlds extend this map).
KIND_OVERRIDES = {"kev-reference": "reference_state", "agent-operation": "base"}

# Composition + install semantics (kits/README.md: standalone kits use
# `init --kit`; overlay kits use `state create-overlay --kit`). Hardcoded
# until kit configs carry a machine-readable extends/composes field —
# sources: each kit's own config description + kits/README.md table.
COMPOSES_OVER = {
    "kev-triage": ["kev-reference"],
    "project-domain": ["agent-operation"],
    "agent-release": ["agent-operation"],
    "case-law-monitoring": ["agent-operation"],
    "supply-chain-blast-radius": ["agent-operation"],
}
INSTALL_OVERRIDES = {
    "kev-triage": "cruxible state create-overlay --state-ref kev-reference --kit kev-triage",
    "kev-reference": "cruxible state create-overlay --state-ref kev-reference",
    "project-domain": "cruxible init --kit agent-operation --kit project-domain",
    "agent-release": "cruxible init --kit agent-operation --kit agent-release",
    "case-law-monitoring": "cruxible init --kit agent-operation --kit case-law-monitoring",
    "supply-chain-blast-radius": "cruxible init --kit agent-operation --kit supply-chain-blast-radius",
}

# Sibling keys on a relationship list item that are metadata, not the name.
_REL_META_KEYS = {
    "properties",
    "description",
    "write_policy",
    "proposal_policy",
    "proposal_identity",
    "constraints",
    "cardinality",
}


def _rel_entry(item: dict, canonical_writes: set[str]) -> dict:
    name, span = next(
        (k, v) for k, v in item.items() if k not in _REL_META_KEYS and isinstance(v, str)
    )
    from_type, _, to_type = (part.strip() for part in span.partition("->"))
    policy = item.get("write_policy")
    return {
        "name": name,
        "from": from_type,
        "to": to_type,
        "governed": policy == "proposal_only",
        # Written by a type: canonical workflow — the deterministic ingest
        # lane. Verified disjoint from governed edges across all kits.
        "deterministic": name in canonical_writes and policy != "proposal_only",
        "write_policy": policy,
        "description": item.get("description"),
        "property_names": sorted((item.get("properties") or {}).keys()),
    }


def _canonical_relationship_writes(workflows: dict) -> set[str]:
    """Relationship types written by canonical (deterministic) workflows."""
    acc: set[str] = set()

    def walk(obj) -> None:
        if isinstance(obj, dict):
            rt = obj.get("relationship_type")
            if isinstance(rt, str):
                acc.add(rt)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for spec in workflows.values():
        if (spec or {}).get("type") == "canonical":
            walk((spec or {}).get("steps"))
    return acc


def _entity_entry(name: str, spec: dict) -> dict:
    props = spec.get("properties") or {}
    return {
        "name": name,
        "description": (spec.get("description") or "").strip(),
        "id_property": spec.get("id"),
        "property_names": list(props.keys()),
    }


def _query_entry(name: str, spec: dict) -> dict:
    return {
        "name": name,
        "description": (spec.get("description") or "").strip(),
        "mode": spec.get("mode"),
        "entry_point": spec.get("entry_point"),
        "returns": spec.get("returns"),
    }


def _guard_entry(item: dict) -> dict:
    name, spec = next(iter(item.items()))
    return {
        "name": name,
        "when": spec.get("when"),
        "message": (spec.get("message") or "").strip(),
    }


def _workflow_entry(name: str, spec: dict) -> dict:
    return {
        "name": name,
        "type": spec.get("type"),
        "description": (spec.get("description") or "").strip(),
        "step_count": len(spec.get("steps") or []),
    }


def build_kit(slug: str, config: dict) -> dict:
    entity_types = config.get("entity_types") or {}
    relationships = config.get("relationships") or []
    queries = config.get("named_queries") or {}
    workflows = config.get("workflows") or {}
    guards = config.get("mutation_guards") or []

    return {
        "slug": slug,
        "name": config.get("name", slug),
        "kind": KIND_OVERRIDES.get(slug, "kit"),
        "version": str(config.get("version", "")),
        # First paragraph only: kit descriptions carry dev-facing
        # composition notes after the summary block — those belong in the
        # config on GitHub, not on consumer surfaces.
        "description": (config.get("description") or "").strip().split("\n")[0].strip(),
        "composes_over": COMPOSES_OVER.get(slug, []),
        "install": INSTALL_OVERRIDES.get(slug, f"cruxible init --kit {slug}"),
        "github": f"{GITHUB_BASE}/{slug}",
        "stats": {
            "entity_types": len(entity_types),
            "relationships": len(relationships),
            "named_queries": len([n for n in queries if "$" not in n]),
            "workflows": len(workflows),
            "guards": len(guards),
        },
        "entity_types": [_entity_entry(n, s or {}) for n, s in entity_types.items()],
        "relationships": [
            _rel_entry(r, _canonical_relationship_writes(workflows)) for r in relationships
        ],
        "named_queries": [
            _query_entry(n, s or {}) for n, s in queries.items() if "$" not in n
        ],
        "workflows": [_workflow_entry(n, s or {}) for n, s in workflows.items()],
        "guards": [_guard_entry(g) for g in guards],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO_ROOT / "dist" / "kits.json"))
    args = parser.parse_args()

    kits = []
    for kit_dir in sorted(KITS_DIR.iterdir()):
        config_path = kit_dir / "config.yaml"
        if not config_path.is_file():
            continue
        config = yaml.safe_load(config_path.read_text())
        kits.append(build_kit(kit_dir.name, config))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"kits": kits}, indent=2) + "\n")
    for kit in kits:
        stats = " · ".join(f"{v} {k}" for k, v in kit["stats"].items() if v)
        print(f"{kit['slug']:<28} {kit['kind']:<16} {stats}")
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes, {len(kits)} kits)")


if __name__ == "__main__":
    main()
