"""The pilot harness: transcripts, cost numbers and the scratch roots each scenario runs in.

Every scenario writes a transcript a person reads (``$PILOT_OUT/<scenario>.txt``)
and appends rows to ``$PILOT_OUT/results.tsv``::

    commit  scenario  status  measure  value

One row per scenario (measure ``-``) and one per cost measure. ``status`` is the
pytest outcome: ``passed``, ``xfailed`` (a strict expected failure that still
fails), ``failed`` or ``skipped``. ``PILOT_OUT`` defaults to a fresh temporary
directory; ``PILOT_COMMIT`` overrides the commit read from Git.

Nothing here reads or writes ``~/.cruxible*``: each scenario runs with ``HOME``
and ``CRUXIBLE_STATE_ROOT`` under its own temporary directory, as
``scripts/regen_pins.sh`` isolates them.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import statistics
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_ENV = "PILOT_OUT"
COMMIT_ENV = "PILOT_COMMIT"
#: A cruxible-kits checkout (its ``kits/security``); the baseline and scenario 6 read it.
KITS_ENV = "CRUXIBLE_KITS_CHECKOUT"
#: Set by the coordinator's runs: a missing checkout fails instead of skipping.
REQUIRED_ENV = "PILOT_REQUIRED"
#: Runs per cost measure; the median is recorded.
RUNS = 3
TSV_HEADER = "commit\tscenario\tstatus\tmeasure\tvalue\n"

_DEFAULT_OUT: Path | None = None


def out_dir() -> Path:
    """``$PILOT_OUT``, or one temporary directory for this process."""

    global _DEFAULT_OUT
    configured = os.environ.get(OUT_ENV)
    if configured:
        path = Path(configured).expanduser()
    else:
        if _DEFAULT_OUT is None:
            _DEFAULT_OUT = Path(tempfile.mkdtemp(prefix="pilot-out-"))
            os.environ[OUT_ENV] = str(_DEFAULT_OUT)
        path = _DEFAULT_OUT
    path.mkdir(parents=True, exist_ok=True)
    return path


def commit() -> str:
    configured = os.environ.get(COMMIT_ENV)
    if configured:
        return configured
    try:
        found = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    os.environ[COMMIT_ENV] = found
    return found


# -- transcript and measures ----------------------------------------------------------


@dataclass
class Scenario:
    """One scenario's transcript and cost measures, written when its test ends."""

    name: str
    owner: str
    title: str
    lines: list[str] = field(default_factory=list)
    measures: list[tuple[str, str]] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        """A line of narration."""

        self.lines.append(text)

    def call(self, what: str) -> None:
        """A command or call made, as a person would type or read it."""

        self.lines.append(f"$ {what}")

    def got(self, text: str) -> None:
        """What came back, indented under the call."""

        for line in str(text).splitlines() or [""]:
            self.lines.append(f"  {line}")

    def show(self, label: str, value: Any) -> None:
        """A JSON value, pretty-printed under a label."""

        self.lines.append(f"{label}:")
        rendered = json.dumps(value, indent=2, sort_keys=True, default=str)
        self.lines.extend(f"  {line}" for line in rendered.splitlines())

    def measure(self, name: str, value: float | int | str, unit: str = "") -> None:
        """A cost number: one TSV row. Seconds are recorded to the millisecond."""

        if isinstance(value, float):
            text = f"{value:.3f}"
        else:
            text = str(value)
        key = f"{name}_{unit}" if unit else name
        self.measures.append((key, text))
        self.lines.append(f"  [cost] {key} = {text}")

    def median(self, name: str, samples: list[float]) -> float:
        """Record the median of ``samples`` (seconds), and the samples in the transcript."""

        median = statistics.median(samples)
        self.measure(name, median, "s")
        self.lines.append(f"  [cost] {name} samples: " + ", ".join(f"{s:.3f}" for s in samples))
        return median

    def transcript(self, status: str, stopped: str | None) -> str:
        head = [
            f"# {self.name}: {self.title}",
            f"# owner: {self.owner}",
            f"# commit: {commit()}   status: {status}",
            "",
        ]
        tail = ["", f"STOPPED: {stopped}"] if stopped else []
        return "\n".join([*head, *self.lines, *tail]) + "\n"

    def write(self, status: str, stopped: str | None = None) -> None:
        out = out_dir()
        (out / f"{self.name}.txt").write_text(self.transcript(status, stopped))
        rows = [
            (self.name, status, "-", "-"),
            *((self.name, status, m, v) for m, v in self.measures),
        ]
        results = out / "results.tsv"
        # O_APPEND keeps rows from parallel workers whole.
        fd = os.open(results, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, TSV_HEADER.encode())
            for row in rows:
                os.write(fd, ("\t".join((commit(), *row)) + "\n").encode())
        finally:
            os.close(fd)


class Stopwatch:
    """``with Stopwatch() as watch: ...`` then ``watch.seconds``."""

    def __enter__(self) -> Stopwatch:
        self.started = time.perf_counter()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self.started


# -- scratch roots ---------------------------------------------------------------------


def isolate_home(monkeypatch: Any, root: Path) -> Path:
    """``HOME`` and ``CRUXIBLE_STATE_ROOT`` under ``root``, as regen_pins.sh sets them."""

    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(home / "state"))
    return home


# -- offline guard ---------------------------------------------------------------------


class NetworkCall(AssertionError):
    """A scenario that must stay offline opened an Internet socket."""


@dataclass
class NetworkLog:
    attempts: list[str] = field(default_factory=list)


@contextlib.contextmanager
def no_network() -> Iterator[NetworkLog]:
    """Refuse (and log) every Internet connect and name lookup inside the block.

    Unix-domain sockets stay open: they are local IPC, not network.
    """

    log = NetworkLog()
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def internet(sock: socket.socket) -> bool:
        return sock.family in (socket.AF_INET, socket.AF_INET6)

    def connect(sock: socket.socket, address: Any) -> Any:
        if internet(sock):
            log.attempts.append(f"connect {address!r}")
            raise NetworkCall(f"network connect to {address!r} in an offline scenario")
        return original_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> Any:
        if internet(sock):
            log.attempts.append(f"connect {address!r}")
            raise NetworkCall(f"network connect to {address!r} in an offline scenario")
        return original_connect_ex(sock, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        log.attempts.append(f"getaddrinfo {host!r}")
        raise NetworkCall(f"name lookup of {host!r} in an offline scenario")

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        yield log
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = original_getaddrinfo  # type: ignore[assignment]


# -- checkouts -------------------------------------------------------------------------


def kits_checkout() -> Path:
    """The cruxible-kits checkout; skip without one, unless ``PILOT_REQUIRED`` is set."""

    import pytest

    value = os.environ.get(KITS_ENV)
    if not value:
        message = (
            f"set {KITS_ENV} to a cruxible-kits checkout (tests/pilot_contract_batch/README.md)"
        )
        if os.environ.get(REQUIRED_ENV):
            pytest.fail(f"{REQUIRED_ENV} is set: {message}")
        pytest.skip(message)
    path = Path(value).expanduser().resolve()
    if not (path / "kits/security/source/pilot").is_dir():
        pytest.fail(f"{KITS_ENV}={path} has no kits/security/source/pilot")
    return path


def require_providers() -> Path:
    """The providers checkout (``CRUXIBLE_PROVIDERS_CHECKOUT``), with its own runtime loaded.

    The web adapter at the checkout imports names only its own runtime has, so the
    runtime must be the checkout's (``packages/cruxible-provider-runtime/src`` on
    ``PYTHONPATH``), not an older installed copy.
    """

    import pytest

    from tests.support.provider_checkout import require_provider_checkout

    checkout = require_provider_checkout()
    import cruxible_provider_runtime

    runtime = Path(cruxible_provider_runtime.__file__).resolve()
    expected = (checkout / "packages/cruxible-provider-runtime/src").resolve()
    if not runtime.is_relative_to(expected):
        pytest.fail(
            f"cruxible_provider_runtime is loaded from {runtime.parent}, not from the "
            f"providers checkout; put {expected} first on PYTHONPATH"
        )
    return checkout
