"""Compare synchronous completion against enqueue with a controlled local delay."""

import json
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from tests.test_playbill.test_ledger_mirror import _mirrored
from tests.test_playbill.test_proposal_notes import _submit

from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.ledger_mirror import read_mirror_state

DELAY = 1.0
rows = []


def wait_request(instance, sequence):
    deadline = time.monotonic() + 15
    with instance._mirror_condition:
        while time.monotonic() < deadline:
            state = read_mirror_state(instance.root)
            if state.published_sequence >= sequence:
                return state
            if state.status == "behind":
                raise AssertionError(state.detail)
            instance._mirror_condition.wait(0.05)
    raise AssertionError("publication did not finish")


for iteration in range(3):
    for mode in ("synchronous_completion", "background_publication"):
        with tempfile.TemporaryDirectory(prefix="publication-benchmark-") as directory:
            instance, remote = _mirrored(Path(directory))
            enqueue = instance.request_ledger_mirror

            def synchronously_publish():
                requested = enqueue()
                return wait_request(instance, requested.requested_sequence)

            if mode == "synchronous_completion":
                instance.request_ledger_mirror = synchronously_publish
            original = GitLedger.push_mirror
            calls = []

            def delayed(ledger, url, **kwargs):
                calls.append(dict(kwargs["snapshot"]))
                time.sleep(DELAY)
                return original(ledger, url, **kwargs)

            with patch.object(GitLedger, "push_mirror", delayed):
                start = time.perf_counter()
                result = _submit(instance)
                returned = time.perf_counter() - start
                assert result.candidate is not None
                queued = read_mirror_state(instance.root).requested_sequence
                state = wait_request(instance, queued)
                published = time.perf_counter() - start
                assert state.status == "current"
                assert state.published_refs == instance._ledger.mirror_refs()
                worker = instance._mirror_thread
                if worker is not None:
                    worker.join(10)
                assert len(calls) == 1
            rows.append(
                dict(
                    iteration=iteration,
                    mode=mode,
                    submit_seconds=returned,
                    remote_ack_seconds=published,
                    mirror_pushes=len(calls),
                )
            )
            print(json.dumps(rows[-1]), flush=True)
result = dict(
    scope="One-document candidate submission, three paired fresh fixtures per mode; "
    "real local bare Git remote, controlled 1s delay per push. Baseline waits for "
    "the same publisher's acknowledgment inside the submission callback. "
    "No live daemon, public remote, HTTP, approval, activation or setup in timers.",
    delay_seconds=DELAY,
    rows=rows,
    medians={
        mode: {
            key: statistics.median(row[key] for row in rows if row["mode"] == mode)
            for key in ("submit_seconds", "remote_ack_seconds")
        }
        for mode in ("synchronous_completion", "background_publication")
    },
)
Path("/private/tmp/ledger-publication-benchmark.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result["medians"]), flush=True)
