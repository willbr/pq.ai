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

import collections
import csv
import time
from contextlib import contextmanager

# Unicode block fractions for 1/8..7/8 of a cell; "█" is a full cell. Lets a bar
# end on an eighth-of-a-character boundary so short times still read smoothly.
_EIGHTHS = "▏▎▍▌▋▊▉"

HISTORY_LEN = 120                  # frames of total-ms kept for the sparkline
_SPARK = " ▁▂▃▄▅▆▇█"              # index 0 = blank (~0ms); 8 = full / over-budget


def _bar(frac, width):
    """A horizontal block-character bar `width` cells wide at `frac` (0..1) full,
    clamped to the width (an over-budget section just fills it)."""
    if frac <= 0.0:
        return ""
    eighths = int(round(frac * width * 8))
    full, rem = divmod(eighths, 8)
    if full >= width:
        return "█" * width
    return "█" * full + (_EIGHTHS[rem - 1] if rem else "")


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
        self._parent = {}           # name -> enclosing section name (first seen), or None
        self._frame_total = 0.0     # wall time under top-level sections this frame
        self.ms = {}                # name -> EMA-smoothed milliseconds
        self.total_ms = 0.0         # EMA-smoothed top-level total, milliseconds
        self.history = collections.deque(maxlen=HISTORY_LEN)  # raw total-ms per frame
        self._last_raw = {}         # this frame's raw section ms + "total"
        self._log = None            # csv.writer while logging, else None
        self._log_file = None       # open file handle while logging
        self._log_cols = []         # CSV column order (excludes the frame index)
        self._log_frame = 0         # rows written so far

    def begin(self, name):
        """Open a section; pair with end(name). Use this to bracket an inline
        region that can't be cleanly wrapped in a `with` block."""
        # remember the section we're nested inside (the first time we see this
        # name), so the bar chart can indent children under their parent
        self._parent.setdefault(name, self._open[-1][0] if self._open else None)
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
        raw = {n: self._accum.get(n, 0.0) * 1000.0 for n in self.ms}
        raw["total"] = self._frame_total * 1000.0
        self._last_raw = raw
        self.history.append(raw["total"])
        if self._log is not None:
            self._write_log_row(raw)
        self._accum.clear()
        self._frame_total = 0.0

    def report(self):
        """One-line HUD string: each section's smoothed ms (in first-seen order)
        plus the total."""
        parts = "  ".join(f"{n} {ms:.1f}" for n, ms in self.ms.items())
        return f"prof  {parts}  total {self.total_ms:.1f}"

    def bars(self, target_ms=16.7, width=12):
        """Multi-line block-character bar chart of the smoothed section times,
        each bar scaled so a section filling the whole frame budget (`target_ms`,
        default ~60fps) spans the full `width`. The frame total is the first
        row under the header; below it, children are listed indented directly
        under their parent, siblings hottest-first (flame-graph order, so the
        eye lands on the cost driver at every level). Monospace HUD font
        keeps the columns aligned."""
        # total first, then depth-first with siblings by descending time
        msd = self.ms
        rows = [("total", 0)]

        def add(name, depth):
            rows.append((name, depth))
            kids = [c for c in msd if self._parent.get(c) == name]
            for child in sorted(kids, key=msd.get, reverse=True):
                add(child, depth + 1)

        top = [n for n in msd if self._parent.get(n) is None]
        for name in sorted(top, key=msd.get, reverse=True):
            add(name, 0)

        lwidth = max(9, max(2 * d + len(n) for n, d in rows) + 1)
        lines = [f"prof (ms, target {target_ms:.1f})"]
        for name, depth in rows:
            ms = self.total_ms if name == "total" else self.ms[name]
            label = "  " * depth + name
            lines.append(f"{label:<{lwidth}}{ms:5.1f} {_bar(ms / target_ms, width)}")
        return "\n".join(lines)

    def graph(self, target_ms=16.7, width=120):
        """One-line sparkline of recent raw frame totals from history (up to
        `width` columns, newest last). Height is scaled so a frame at
        2*target_ms fills the cell -- a frame at budget sits mid-height and
        hitches spike visibly; an over-budget frame caps at the full block.
        Empty history returns ''."""
        if not self.history:
            return ""
        recent = list(self.history)[-width:]
        full = 2.0 * target_ms
        out = []
        for t in recent:
            level = int(round(t / full * 8)) if full > 0 else 0
            out.append(_SPARK[min(8, max(0, level))])
        return "".join(out)

    def start_log(self, path, open_fn=open):
        """Begin per-frame CSV logging to `path`. Column order is fixed now from
        the sections seen so far (`total` first, then first-seen section order) --
        by in-level play that is every section. `open_fn` is an injectable seam
        for tests. Header: frame,total,<sections...>."""
        self._log_file = open_fn(path, "w", newline="")
        self._log = csv.writer(self._log_file)
        self._log_cols = ["total"] + list(self.ms)
        self._log_frame = 0
        self._log.writerow(["frame"] + self._log_cols)

    def _write_log_row(self, raw):
        """Append one frame's raw ms as a CSV row (frame index first)."""
        row = [self._log_frame] + [f"{raw.get(c, 0.0):.3f}" for c in self._log_cols]
        self._log.writerow(row)
        self._log_frame += 1

    def stop_log(self):
        """Close the log; return (path, frames_written) or None if not logging."""
        if self._log is None:
            return None
        path = self._log_file.name
        n = self._log_frame
        self._log_file.close()
        self._log = None
        self._log_file = None
        return (path, n)


# Module-level singleton the engine and frontends share.
PROFILER = Profiler()
