"""Bound FIFO regressions and release a blocking reader if the regression returns."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypeVar

_Result = TypeVar("_Result")


def call_with_fifo_timeout(fifo: Path, call: Callable[[], _Result]) -> _Result:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call)
        try:
            return pending.result(timeout=5)
        finally:
            if not pending.done():
                # A pre-fix blocking open/read must not leave a test worker alive.
                handle = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
                os.close(handle)
