import json
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

from tests.test_playbill.test_authoring_history_reuse import _intent, _operation

from cruxible_core.playbill.authoring import store as after

baseline = types.ModuleType("cruxible_core.playbill.authoring._fingerprint_baseline")
sys.modules[baseline.__name__] = baseline
baseline.__file__ = "store-before-fingerprint-proof.py"
source = subprocess.check_output(
    ["git", "show", "534da597:src/cruxible_core/playbill/authoring/store.py"], text=True
)
exec(compile(source, baseline.__file__, "exec"), baseline.__dict__)
root = Path(tempfile.mkdtemp(prefix="fingerprint-comparison-"))
rows = []
for count in (32, 160):
    exhaust = root / f"streams-{count}" / "exhaust"
    exhaust.mkdir(parents=True)
    writer = after.AuthoringIntentStore(exhaust)
    for index in range(count):
        intent = _intent(value=str(index)).model_copy(update={"intent_id": f"AIT-{index:032x}"})
        previous = None
        for sequence in range(3):
            event = after.build_authoring_intent_event(
                sequence=sequence,
                previous_event_digest=previous,
                operation_key=_operation(sequence),
                intent=intent.model_copy(update={"intent_revision": sequence}),
            )
            path = writer.root / intent.intent_id / "events" / f"{sequence:020d}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(writer._render_event(event))
            previous = event.event_digest
    expected = event.intent
    for condition in ("cold", "warm"):
        readers = {
            name: module.AuthoringIntentStore(exhaust)
            for name, module in [("before", baseline), ("after", after)]
        }
        if condition == "warm":
            for name, module in [("before", baseline), ("after", after)]:
                module._reset_authoring_history_memo()
                assert (
                    readers[name]._active_by_fingerprint(
                        expected.create_fingerprint, actor_id="owner"
                    )
                    == expected
                )
        for repeat in range(3):
            for name, module in [("before", baseline), ("after", after)]:
                if condition == "cold":
                    module._reset_authoring_history_memo()
                counter = [0]
                original = module._decode_authoring_intent_event

                def counted(raw):
                    counter[0] += 1
                    return original(raw)

                module._decode_authoring_intent_event = counted
                start = time.perf_counter()
                found = readers[name]._active_by_fingerprint(
                    expected.create_fingerprint, actor_id="owner"
                )
                elapsed = time.perf_counter() - start
                module._decode_authoring_intent_event = original
                assert found == expected
                rows.append(
                    dict(
                        streams=count,
                        events=count * 3,
                        condition=condition,
                        mode=name,
                        repeat=repeat,
                        elapsed_s=elapsed,
                        decoded_events=counter[0],
                    )
                )
    entries = list(after._FINGERPRINT_MEMO.items())
    logical_bytes = sum(
        len(str(path).encode())
        + len(entry.stream_digest)
        + 8
        + len(entry.state.actor_id.encode())
        + len(entry.state.fingerprint.encode())
        + 1
        for path, entry in entries
    )
    print(
        json.dumps(
            {
                "streams": count,
                "logical_compact_proof_bytes_including_path": logical_bytes,
                "warning": "logical field bytes, not Python heap measurement",
            }
        ),
        flush=True,
    )
summary = {
    "baseline_commit": "534da597",
    "fixture": (
        "3 events per stream, self-source payload, same exact filesystem fixture per "
        "before/after pair; full-model memo defaults 128streams/256MiB; no live daemon"
    ),
    "rows": rows,
    "medians": [
        {
            "streams": count,
            "condition": condition,
            **{
                mode: statistics.median(
                    r["elapsed_s"]
                    for r in rows
                    if r["streams"] == count and r["condition"] == condition and r["mode"] == mode
                )
                for mode in ("before", "after")
            },
        }
        for count in (32, 160)
        for condition in ("cold", "warm")
    ],
}
(root / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
Path("/private/tmp/authoring-fingerprint-comparison-root.txt").write_text(str(root))
print(json.dumps({"root": str(root), "medians": summary["medians"]}, indent=2))
