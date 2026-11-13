from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from cruxible_core.consumers.state import DisposableState

_SCHEMA = "CREATE TABLE rows (id INTEGER PRIMARY KEY);"


def test_concurrent_first_opens_of_a_fresh_state_do_not_race(tmp_path: Path) -> None:
    """Orient and next opening one consumer's state for the first time must not collide."""
    instance = SimpleNamespace(
        root=str(tmp_path), descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def open_once(index: int) -> None:
        state = DisposableState(f"next/race-{index % 2}", _SCHEMA)
        try:
            start.wait()
            with state.open(instance):
                pass
        except BaseException as exc:  # noqa: BLE001 - collect for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=open_once, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert (tmp_path / "exhaust" / "next" / "race-0" / "state.sqlite3").is_file()
