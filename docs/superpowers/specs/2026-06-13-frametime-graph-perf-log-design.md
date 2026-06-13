# Frametime sparkline + CSV perf logging

## Goal

Two additions to the always-on profiler so a level can be played and its
per-frame performance reviewed afterward:

1. **Frametime sparkline** — a scrolling one-line graph of recent total frame
   times, drawn in the existing `prof` HUD, for spotting hitches at a glance.
2. **CSV perf logging** — a console command that records raw per-frame section
   times to a CSV file for offline review.

## Why the profiler is the right home

`quake/perf.py`'s `PROFILER` is the single shared singleton that every frontend
already drives once per frame via `frame_end()` (tk `main.py:500`, cocoa
`mac_cocoa.py:419`, gdi `win_gdi.py:465`). Putting both features there means all
three frontends get them for free; only `client.py` needs touching — to render
the graph into the HUD string and to register the console command. File writing
is plain stdlib, so it does not violate perf.py's "UI/OS-agnostic" rule (it
makes no UI or platform calls).

## Problem solved first: raw per-frame data

The profiler currently keeps only EMA-smoothed values (`self.ms`,
`self.total_ms`). A sparkline and an honest CSV both need *raw* per-frame
numbers. So `frame_end()` will, before clearing the accumulators, snapshot this
frame's raw total and raw per-section ms.

New `Profiler` state:

- `self.history` — `collections.deque(maxlen=HISTORY_LEN)` of raw total-ms per
  frame (feeds the sparkline). `HISTORY_LEN = 120`.
- `self._last_raw` — dict of this frame's raw section ms plus `total` (feeds the
  CSV row).

`frame_end()` gains, before `self._accum.clear()`:

```python
raw = {n: self._accum.get(n, 0.0) * 1000.0 for n in self.ms}
raw["total"] = self._frame_total * 1000.0
self._last_raw = raw
self.history.append(raw["total"])
if self._log is not None:
    self._write_log_row(raw)
```

## Frametime sparkline — `graph(target_ms=16.7, width=120)`

Returns a one-line string of vertical block glyphs `▁▂▃▄▅▆▇█`, one column per
recent frame taken from the tail of `history` (up to `width` columns). Height is
scaled so a frame at `2 × target_ms` fills the cell — a frame at budget sits
mid-height (~`▄`) and hitches spike visibly; an over-budget frame caps at `█`.
Empty history yields an empty string.

Glyph selection per frame total `t` (ms):

```python
_SPARK = " ▁▂▃▄▅▆▇█"   # index 0 = blank for ~0ms
level = int(round(t / (2 * target_ms) * 8))
glyph = _SPARK[min(8, max(0, level))]
```

### HUD integration

In the `show_prof` block (`client.py:1327`), append the graph as one extra line
under the existing `bars()` output:

```python
prof = PROFILER.bars()
graph = PROFILER.graph()
if graph:
    prof += "\n" + graph
```

The colour list already sizes from `prof.count("\n")`, so adding the line before
that count keeps colouring correct; the graph row is coloured `HUD_GREEN` like
the section rows. Toggled by the same `prof` / P control that exists today — no
new key or command for the graph.

## CSV perf logging — `start_log(path)` / `stop_log()`

Profiler methods:

- `start_log(path, open_fn=open)` — opens the file (the `open_fn` seam lets a
  test inject an in-memory file), records the column order as `["total"] +
  list(self.ms)` (the sections seen so far — by in-level play that is all of
  them), writes the header `frame,total,server,render,raster,present,…`, and
  resets a `self._log_frame` counter to 0. Stores the writer in `self._log`.
- `stop_log()` — closes the file, clears `self._log`, returns
  `(path, frames_written)` for the caller to report (or `None` if not logging).
- `_write_log_row(raw)` — writes `self._log_frame` then each column's value
  (`f"{raw.get(col, 0.0):.3f}"`), increments the counter. Uses the
  `csv` module's writer.

New state initialised in `__init__`: `self._log = None`, `self._log_file =
None`, `self._log_cols = []`, `self._log_frame = 0`.

### Console command

Register `logperf` near the other client commands (~`client.py:824`):

```python
con.register_command("logperf", self._cmd_logperf,
                     "start/stop per-frame CSV perf logging: logperf <file>")
```

`_cmd_logperf(args)`:

- If currently logging: `stop_log()`, print `logged N frames to <path>`.
- Else: require a filename arg (else print usage), call `start_log(path)`, print
  `logging perf to <path> (run logperf again to stop)`.

The toggle-with-optional-arg shape mirrors how a user logs just the stretch they
care about: `logperf run1.csv` … play … `logperf`.

## Testing

Extend `tests/test_perf.py`, reusing its injectable-`FakeClock` pattern:

- **history fills and bounds** — drive > `HISTORY_LEN` frames; assert
  `len(history) == HISTORY_LEN` and the oldest entries dropped.
- **graph glyph mapping** — push known totals (0, target, 2×target,
  over-budget); assert the returned string's glyphs match expected heights and
  that over-budget caps at `█`; empty history → `""`.
- **logging writes header + rows** — `start_log(path, open_fn=...)` with an
  injected `io.StringIO`; run a few frames with known section times; assert the
  header columns and that each data row holds the raw (non-EMA) ms; `stop_log()`
  returns `(path, n)` and closes the file.

All tests are pure logic — no engine boot, no real files (StringIO seam), no
sleeps.

## Out of scope (YAGNI)

- Stacked-section graph (sparkline shows total only).
- `--perflog` launch flag (console command only).
- Log rotation / auto-start / append-to-existing.
