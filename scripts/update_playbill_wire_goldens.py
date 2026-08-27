"""Rebuild Playbill wire goldens whose produced policy bytes changed in place."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.indexes import (
    DISCOVERY_JSONL_NAME,
    INDEX_MARKDOWN_NAME,
    discovery_index_manifest,
    render_discovery_index,
)
from cruxible_core.playbill.settlement import (
    ChangeSetRecordV3,
    change_set_digest,
    parse_change_set_record,
    render_change_set,
)

ROOT = Path(__file__).resolve().parents[1]
CHANGESET_V3 = ROOT / "tests/goldens/playbill/changeset-v3.json"
DISCOVERY_V1 = ROOT / "tests/goldens/playbill/discovery-index-v1.json"


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
    updated = record
    updated = updated.model_copy(update={"changeset_digest": change_set_digest(updated).tagged})
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


def _update_discovery_index() -> None:
    sys.path.insert(0, str(ROOT))
    from tests.test_playbill.test_discovery import (  # noqa: PLC0415
        _facts,
        _vocabulary,
        subject_query_view,
    )

    rows = _facts()
    view = subject_query_view(rows)
    files = render_discovery_index(view=view, vocabulary=_vocabulary(rows))
    manifest = discovery_index_manifest(
        files,
        at=AcceptedCoordinate.from_internal(view.coordinate),
    )
    DISCOVERY_V1.write_text(
        json.dumps(
            {
                "discovery_jsonl": files[DISCOVERY_JSONL_NAME].decode("utf-8"),
                "index_manifest": manifest.model_dump(mode="json"),
                "index_markdown": files[INDEX_MARKDOWN_NAME].decode("utf-8"),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _update_changeset()
    _update_discovery_index()


if __name__ == "__main__":
    main()
