"""Per-frame section profiler: lightweight, always-on `perf_counter` timing of
named frame sections (server / render / raster / present), EMA-smoothed for a
steady on-screen readout.

Deliberately NOT cProfile -- its per-call overhead distorts the per-pixel
loops; a handful of `perf_counter` reads per frame costs nanoseconds. Pure
stdlib and UI/OS-agnostic, so it imports cleanly from inside the `quake`
package (`from .perf import PROFILER`) and from the root frontends
(`from quake.perf import PROFILER`).

Usage:
    with PROFILER.section("render"):
        ...                          # may nest another section() inside
    ...
    PROFILER.frame_end()             # once per frame: roll buckets into PROFILER.ms

Single-thread by design: only the frame thread touches it (the audio thread
never does), so no locking. Sections nest; `total_ms` sums only the outermost
sections, so nested time is never double-counted. A section absent for a frame
decays toward 0."""

import time
from contextlib import contextmanager


class Profiler:
    def __init__(self, clock=time.perf_counter, alpha=0.1):
        # clock() returns seconds; injectable so tests drive a deterministic
        # counter. alpha is the EMA weight on the latest frame (matches the
        # client's 0.1 fps smoothing); 1.0 reports the latest frame exactly.
        self._clock = clock
        self._alpha = alpha
        self._accum = {}            # name -> seconds accumulated this frame
        self._open = []             # (name, start) stack of open sections
        self._depth = 0             # open-section nesting depth
        self._frame_total = 0.0     # wall time under top-level sections this frame
        self.ms = {}                # name -> EMA-smoothed milliseconds
        self.total_ms = 0.0         # EMA-smoothed top-level total, milliseconds

    def begin(self, name):
        """Open a section; pair with end(name). Use this to bracket an inline
        region that can't be cleanly wrapped in a `with` block."""
        self._depth += 1
        self._open.append((name, self._clock()))

    def end(self, name):
        """Close the section begin() opened (LIFO)."""
        _name, start = self._open.pop()
        elapsed = self._clock() - start
        self._accum[name] = self._accum.get(name, 0.0) + elapsed
        self._depth -= 1
        if self._depth == 0:        # outermost block: counts toward the frame total
            self._frame_total += elapsed

    @contextmanager
    def section(self, name):
        self.begin(name)
        try:
            yield
        finally:
            self.end(name)

    def frame_end(self):
        """Blend this frame's accumulated times into the smoothed readout, then
        clear the accumulators for the next frame."""
        a = self._alpha
        for name in self._accum:                # register any first-seen section
            self.ms.setdefault(name, 0.0)
        for name in self.ms:                    # decay absent sections toward 0
            cur_ms = self._accum.get(name, 0.0) * 1000.0
            self.ms[name] = (1.0 - a) * self.ms[name] + a * cur_ms
        self.total_ms = (1.0 - a) * self.total_ms + a * (self._frame_total * 1000.0)
        self._accum.clear()
        self._frame_total = 0.0

    def report(self):
        """One-line HUD string: each section's smoothed ms (in first-seen order)
        plus the total."""
        parts = "  ".join(f"{n} {ms:.1f}" for n, ms in self.ms.items())
        return f"prof  {parts}  total {self.total_ms:.1f}"


# Module-level singleton the engine and frontends share.
PROFILER = Profiler()
