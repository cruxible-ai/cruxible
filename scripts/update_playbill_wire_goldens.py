"""Rebuild Playbill wire goldens whose produced policy bytes changed in place."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_core.playbill.settlement import (
    ChangeSetRecordV3,
    change_set_digest,
    parse_change_set_record,
    render_change_set,
)

ROOT = Path(__file__).resolve().parents[1]
CHANGESET_V3 = ROOT / "tests/goldens/playbill/changeset-v3.json"


def _update_changeset() -> None:
    payload = json.loads(CHANGESET_V3.read_bytes())
    record = parse_change_set_record(
        json.dumps(payload["record"], separators=(",", ":"), sort_keys=True).encode() + b"\n",
        path="changesets/golden.json",
    )
    if not isinstance(record, ChangeSetRecordV3):
        raise TypeError("the v3 ChangeSet golden did not parse as ChangeSetRecordV3")
    # The fixture deliberately retains an explicit nondefault requirement to
    # prove the dormant matcher and wire remain readable. Newly compiled
    # candidates emit no requirements by default.
    updated = record.model_copy(update={"changeset_digest": change_set_digest(record).tagged})
    canonical = render_change_set(updated)
    reparsed = parse_change_set_record(canonical, path="changesets/golden.json")
    if reparsed != updated:
        raise AssertionError("rebuilt v3 ChangeSet golden did not round-trip")

    payload["canonical_bytes"] = canonical.decode("utf-8")
    payload["changeset_digest"] = updated.changeset_digest
    payload["recomputed_changeset_digest"] = change_set_digest(updated).tagged
    payload["record"] = updated.model_dump(mode="json")
    CHANGESET_V3.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _update_changeset()


if __name__ == "__main__":
    main()
