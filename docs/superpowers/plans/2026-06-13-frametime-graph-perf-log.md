# Frametime Sparkline + CSV Perf Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a scrolling frametime sparkline to the existing `prof` HUD and a `logperf <file>` console command that records raw per-frame section times to CSV.

**Architecture:** Both features extend `quake/perf.py`'s `Profiler` singleton (driven once per frame by every frontend via `frame_end()`), so all three frontends get them for free. `frame_end()` snapshots raw per-frame data (a deque of total-ms for the sparkline; a dict of section ms for the CSV row). `client.py` renders `graph()` into the HUD string and registers the `logperf` command. File writing is plain stdlib — no UI/OS calls, preserving perf.py's agnostic contract.

**Tech Stack:** Pure Python stdlib (`collections.deque`, `csv`, `io`). Tests use the existing injectable-`FakeClock` pattern in `tests/test_perf.py`; no engine boot, no real files (StringIO seam), no sleeps.

---

## File Structure

- **Modify `quake/perf.py`** — add raw-data capture in `frame_end()`, the `graph()` renderer, and `start_log()`/`stop_log()`/`_write_log_row()`. All new state initialised in `__init__`.
- **Modify `client.py`** — append `graph()` under `bars()` in the `show_prof` HUD block (`client.py:1333-1339`); add `_cmd_logperf` and register the `logperf` command (`client.py:824`).
- **Modify `tests/test_perf.py`** — add tests for history bounding, graph glyph mapping, and CSV logging.

Module constants added to `quake/perf.py`:

```python
HISTORY_LEN = 120                  # frames of total-ms kept for the sparkline
_SPARK = " ▁▂▃▄▅▆▇█"              # index 0 = blank (~0ms); 8 = full / over-budget
```

---

## Task 1: Capture raw per-frame data in the profiler

**Files:**
- Modify: `quake/perf.py` (`__init__` ~lines 43-55; `frame_end` ~lines 83-94; module top ~line 27)
- Test: `tests/test_perf.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_perf.py`:

```python
def test_history_records_raw_total_and_bounds():
    """frame_end() appends each frame's raw total-ms to history, bounded at
    HISTORY_LEN; the raw value is the latest frame (not the EMA)."""
    from quake.perf import HISTORY_LEN
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=0.1)   # heavy smoothing: EMA != raw
    with p.section("a"):
        clk.advance(10.0)
    p.frame_end()
    assert abs(p.history[-1] - 10.0) < 1e-6, p.history   # raw, not EMA
    assert p._last_raw["total"] == p.history[-1]
    assert abs(p._last_raw["a"] - 10.0) < 1e-6, p._last_raw
    for _ in range(HISTORY_LEN + 50):    # overfill
        with p.section("a"):
            clk.advance(1.0)
        p.frame_end()
    assert len(p.history) == HISTORY_LEN, len(p.history)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_perf.py`
Expected: FAIL with `AttributeError: 'Profiler' object has no attribute 'history'`

- [ ] **Step 3: Implement the capture**

In `quake/perf.py`, add the import and constants near the top (after the existing `_EIGHTHS` line ~27):

```python
import collections

HISTORY_LEN = 120                  # frames of total-ms kept for the sparkline
_SPARK = " ▁▂▃▄▅▆▇█"              # index 0 = blank (~0ms); 8 = full / over-budget
```

(Add `import collections` next to the existing `import time` at the top.)

In `__init__`, after `self.total_ms = 0.0`:

```python
        self.history = collections.deque(maxlen=HISTORY_LEN)  # raw total-ms per frame
        self._last_raw = {}         # this frame's raw section ms + "total"
        self._log = None            # csv.writer while logging, else None
        self._log_file = None       # open file handle while logging
        self._log_cols = []         # CSV column order (excludes the frame index)
        self._log_frame = 0         # rows written so far
```

In `frame_end()`, insert before `self._accum.clear()`:

```python
        raw = {n: self._accum.get(n, 0.0) * 1000.0 for n in self.ms}
        raw["total"] = self._frame_total * 1000.0
        self._last_raw = raw
        self.history.append(raw["total"])
        if self._log is not None:
            self._write_log_row(raw)
```

`_write_log_row` does not exist yet — Task 3 adds it. This frame block only runs
when `self._log is not None`, which stays `None` until Task 3, so Task 1 and
Task 2 tests pass without it.

- [ ] **Step 4: Run test to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_perf.py`
Expected: PASS (prints `OK`)

- [ ] **Step 5: Commit**

```bash
git add quake/perf.py tests/test_perf.py
git commit -m "perf: capture raw per-frame total/section ms for graph + logging

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Frametime sparkline `graph()`

**Files:**
- Modify: `quake/perf.py` (new method after `bars()` ~line 130)
- Test: `tests/test_perf.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_perf.py`:

```python
def test_graph_maps_totals_to_glyph_heights():
    """graph() renders one block glyph per frame: ~0ms blank, target ~mid,
    2x target full, over-budget capped at full. Empty history -> ''."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    assert p.graph() == ""                       # nothing logged yet
    # push known totals by spending that long in a section each frame
    for ms in (0.0, 16.7, 33.4, 100.0):
        with p.section("a"):
            clk.advance(ms)
        p.frame_end()
    g = p.graph(target_ms=16.7, width=120)
    assert len(g) == 4, repr(g)
    assert g[0] == " "                           # ~0ms -> blank
    assert g[1] == "▄"                           # target -> level 4
    assert g[2] == "█"                           # 2x target -> full
    assert g[3] == "█"                           # over-budget -> capped full

def test_graph_shows_only_last_width_frames():
    """graph(width=N) shows the most recent N frames."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    for _ in range(10):
        with p.section("a"):
            clk.advance(33.4)
        p.frame_end()
    assert len(p.graph(width=3)) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_perf.py`
Expected: FAIL with `AttributeError: 'Profiler' object has no attribute 'graph'`

- [ ] **Step 3: Implement `graph()`**

In `quake/perf.py`, add after `bars()`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_perf.py`
Expected: PASS (prints `OK`)

- [ ] **Step 5: Commit**

```bash
git add quake/perf.py tests/test_perf.py
git commit -m "perf: add frametime sparkline graph()

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: CSV perf logging on the profiler

**Files:**
- Modify: `quake/perf.py` (new methods after `graph()`; `import csv` at top)
- Test: `tests/test_perf.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_perf.py`:

```python
import io

def test_logging_writes_header_and_raw_rows():
    """start_log writes a header of frame + seen sections + total; each
    frame_end appends one raw (non-EMA) row; stop_log returns (path, n)."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=0.1)        # heavy smoothing: prove rows are raw
    with p.section("a"):                       # one frame so "a" is a known section
        clk.advance(5.0)
    p.frame_end()
    buf = io.StringIO()
    p.start_log("run.csv", open_fn=lambda path, mode, **kw: buf)
    for ms in (8.0, 12.0):
        with p.section("a"):
            clk.advance(ms)
        p.frame_end()
    result = p.stop_log()
    assert result == ("run.csv", 2), result
    assert p._log is None
    lines = buf.getvalue().splitlines()
    assert lines[0] == "frame,total,a", lines[0]   # frame + total + seen section
    assert lines[1] == "0,8.000,8.000", lines[1]   # raw ms, not EMA
    assert lines[2] == "1,12.000,12.000", lines[2]

def test_stop_log_without_start_returns_none():
    p = Profiler()
    assert p.stop_log() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_perf.py`
Expected: FAIL with `AttributeError: 'Profiler' object has no attribute 'start_log'`

- [ ] **Step 3: Implement the logging methods**

Add `import csv` next to `import collections` at the top of `quake/perf.py`.

Add after `graph()`:

```python
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
```

Note: `stop_log()` reads `self._log_file.name`; `io.StringIO` has no `.name`, so
the test sets the expected path explicitly. To make `.name` available, the test's
`open_fn` returns a `StringIO`; add `buf.name = "run.csv"` in the test before
`start_log`, OR have the test assert on the path it passed. **Use the
`buf.name` approach** — update the test's setup:

```python
    buf = io.StringIO()
    buf.name = "run.csv"
    p.start_log("run.csv", open_fn=lambda path, mode, **kw: buf)
```

(StringIO accepts arbitrary attribute assignment, so `buf.name = "run.csv"` works.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_perf.py`
Expected: PASS (prints `OK`)

- [ ] **Step 5: Commit**

```bash
git add quake/perf.py tests/test_perf.py
git commit -m "perf: add start_log/stop_log CSV per-frame logging

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Render the sparkline in the prof HUD

**Files:**
- Modify: `client.py:1333-1339` (the `show_prof` block)

No new unit test — this is HUD string assembly inside `client._build_render_frame`;
it is exercised by running the game (Task 6 manual check). Keep the change minimal.

- [ ] **Step 1: Modify the HUD block**

In `client.py`, replace the body of the `if self.show_prof:` block so the graph
line is appended before the colour list is sized (so colouring stays correct):

```python
        if self.show_prof:
            # previous completed frame's smoothed section ms (server/render/
            # raster/present) as a bar chart, then a sparkline of recent raw
            # frame totals. present is timed in the frontend and frame_end()
            # rolls the buckets, so the figures lag one frame uniformly. The
            # total row (top of the chart) is tinted by frame budget via a
            # per-line colour list; every other line stays green.
            prof = PROFILER.bars()
            graph = PROFILER.graph()
            if graph:
                prof += "\n" + graph
            base = hud_str.count("\n") + 1
            colors = [HUD_GREEN] * (base + prof.count("\n") + 1)
            for i, ln in enumerate(prof.split("\n")):
                if ln.startswith("total"):
                    colors[base + i] = prof_total_color(PROFILER.total_ms)
            hud_str += "\n" + prof
            hud_rgb = colors
```

- [ ] **Step 2: Verify the full test suite still passes**

Run: `PQ_AUDIO=0 for t in tests/test_*.py; do python "$t"; done`
Expected: every test prints `OK` (no new failures vs. before this plan)

- [ ] **Step 3: Commit**

```bash
git add client.py
git commit -m "client: draw frametime sparkline under the prof HUD bars

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: `logperf` console command

**Files:**
- Modify: `client.py` (new `_cmd_logperf` method near the other `_cmd_*` methods ~line 624; register at `client.py:824`)

- [ ] **Step 1: Register the command**

In `client.py`, after the `prof` registration (line 824), add:

```python
        con.register_command("logperf", self._cmd_logperf,
                             "logperf <file>: start/stop per-frame CSV perf logging")
```

- [ ] **Step 2: Add the command handler**

Add a method near the other `_cmd_*` handlers (e.g. after `_cmd_save`/`_cmd_load`
~line 640):

```python
    def _cmd_logperf(self, args):
        """Toggle per-frame CSV perf logging. `logperf <file>` starts; a bare
        `logperf` stops and reports the path + frame count."""
        result = PROFILER.stop_log()
        if result is not None:
            path, n = result
            self.con.print(f"logged {n} frames to {path}")
            return
        if not args:
            self.con.print("usage: logperf <file>  (run again to stop)")
            return
        PROFILER.start_log(args[0])
        self.con.print(f"logging perf to {args[0]} (run logperf again to stop)")
```

- [ ] **Step 3: Verify the suite still passes**

Run: `PQ_AUDIO=0 for t in tests/test_*.py; do python "$t"; done`
Expected: every test prints `OK`.

- [ ] **Step 4: Commit**

```bash
git add client.py
git commit -m "client: add logperf console command for CSV perf logging

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Manual end-to-end check

**Files:** none (verification only)

- [ ] **Step 1: Run the game and exercise both features**

Run: `python main.py e1m1`

In-game:
1. Press `P` — confirm the prof bar chart shows, with a sparkline line beneath
   it that scrolls and spikes when you move/turn (hitches read as tall glyphs).
2. Open the console (`` ` `` / F1 depending on frontend), type `logperf run1.csv`,
   confirm the `logging perf to run1.csv` message.
3. Play for ~10 seconds, then `logperf` again — confirm `logged N frames to run1.csv`.
4. Quit.

- [ ] **Step 2: Inspect the CSV**

Run: `head -3 run1.csv && wc -l run1.csv`
Expected: header `frame,total,server,render,raster,present,...`; one row per
frame; N+1 lines total. Then remove the scratch file: `rm run1.csv`.

- [ ] **Step 3: Update docs if needed**

Check whether `README.md` or `main.py`'s key-list docstring (the `P` line ~`main.py:15`)
should mention the sparkline / `logperf`. The `P` toggle behaviour is unchanged
(graph rides along with the existing prof HUD), so a one-line note that the prof
HUD now includes a frametime sparkline, plus a mention of `logperf` in any
console-command list, is sufficient. Commit any doc edits:

```bash
git add -A && git commit -m "docs: note frametime sparkline + logperf command

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **Run tests muted:** always `export PQ_AUDIO=0` before running tests — the
  CoreAudio callback thread segfaults nondeterministically in headless runs
  (often after printing `OK`).
- **Relative imports inside `quake/`:** `perf.py` already uses none beyond stdlib;
  keep it stdlib-only (no UI/OS imports) — `csv`/`collections`/`io` are fine.
- **The graph and CSV use RAW per-frame data**, deliberately not the EMA-smoothed
  `self.ms`/`self.total_ms` (those are for the steady on-screen readout). Tests
  use `alpha=0.1` specifically to prove the raw path isn't accidentally reading
  the smoothed values.
