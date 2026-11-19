"""Unique Provider invocation ids for tests that run real processes.

The runtime's termination sweep kills every host process whose argv names the
invocation id, which is sound in production because a real invocation id is
unique. Tests that start a real Provider child, or any process carrying an
invocation id in its argv, must therefore never share an id with another test:
not with a test in another xdist worker of this run, and not with another
pytest run on the same host. A shared constant let one test's termination kill
another test's child.

Each id is derived from a per-process nonce, the running test's node id and a
label. Within one test the same label always returns the same id, so a test
that retries or recovers one invocation keeps its id; different labels give
different ids.
"""

from __future__ import annotations

import hashlib
import os
import secrets

_RUN_NONCE = secrets.token_hex(16)


def unique_invocation_id(label: str = "") -> str:
    """The current test's invocation id for ``label`` (``sha256:`` + 64 hex)."""

    current = os.environ.get("PYTEST_CURRENT_TEST")
    if not current:
        raise RuntimeError("unique_invocation_id() is only defined inside a running test")
    # "tests/x.py::test_y[param] (call)": setup, call and teardown share one id.
    node_id = current.rsplit(" (", 1)[0]
    material = "\x00".join((_RUN_NONCE, node_id, label)).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()
