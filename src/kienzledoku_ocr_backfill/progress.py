"""Timestamped progress reporting and monotonic step-duration measurement."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator, MutableMapping, Optional
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")


def format_duration(seconds: float) -> str:
    return f"{max(0.0, seconds):.3f} s"


class TimestampedReporter:
    """Prefix every non-empty console line with local wall-clock time."""

    def __init__(
        self,
        sink: Callable[[str], None] = print,
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._sink = sink
        self._now = now or (lambda: datetime.now(BERLIN))

    def __call__(self, message: str) -> None:
        if not message:
            self._sink("")
            return
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=BERLIN)
        else:
            current = current.astimezone(BERLIN)
        self._sink(f"[{current:%d.%m.%Y %H:%M:%S}] {message}")


@contextmanager
def timed_step(
    label: str,
    report: Callable[[str], None],
    *,
    timings: Optional[MutableMapping[str, float]] = None,
    key: Optional[str] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Iterator[None]:
    """Always report elapsed monotonic time, including when a step fails."""

    clock_function = clock or time.monotonic
    started = clock_function()
    try:
        yield
    finally:
        elapsed = max(0.0, clock_function() - started)
        rounded = round(elapsed, 3)
        if timings is not None and key is not None:
            timings[key] = rounded
        report(f"Dauer {label}: {format_duration(elapsed)}")
