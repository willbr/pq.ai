"""Unit tests for quake.perf.Profiler -- the per-frame section timer.

Pure logic, no engine boot: the Profiler takes an injectable clock, so these
drive it with a deterministic fake counter (no real sleeps). Run with
`python test_perf.py` (prints OK) or under pytest."""

import io

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.perf import Profiler


class FakeClock:
    """A clock the test advances by hand. .t is seconds; .advance takes ms."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000.0


def test_section_accumulates_elapsed():
    """A section's smoothed time equals the wall time spent inside it (alpha=1
    makes the EMA report the latest frame exactly)."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    with p.section("a"):
        clk.advance(5.0)
    p.frame_end()
    assert abs(p.ms["a"] - 5.0) < 1e-6, p.ms


def test_same_name_sums_within_frame():
    """Two blocks with the same name in one frame add together."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    with p.section("a"):
        clk.advance(5.0)
    with p.section("a"):
        clk.advance(3.0)
    p.frame_end()
    assert abs(p.ms["a"] - 8.0) < 1e-6, p.ms


def test_nested_sections_record_independently():
    """A nested section gets its own bucket (its time is NOT subtracted from the
    outer one); the outer bucket spans the whole block including the nested time."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    with p.section("render"):
        clk.advance(2.0)
        with p.section("raster"):
            clk.advance(3.0)
        clk.advance(1.0)
    p.frame_end()
    assert abs(p.ms["raster"] - 3.0) < 1e-6, p.ms
    assert abs(p.ms["render"] - 6.0) < 1e-6, p.ms     # 2 + 3 + 1


def test_total_counts_top_level_only():
    """The total is the sum of the outermost (top-level) sections, so nested
    time is not double-counted."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    with p.section("server"):
        clk.advance(2.0)
    with p.section("render"):              # contains raster -> still one top-level
        clk.advance(4.0)
        with p.section("raster"):
            clk.advance(3.0)
    p.frame_end()
    # server(2) + render(7, incl raster) == 9; raster NOT added again
    assert abs(p.total_ms - 9.0) < 1e-6, p.total_ms


def test_ema_smooths_and_resets_between_frames():
    """frame_end EMA-blends toward the latest frame and clears the accumulators,
    so a section absent the next frame decays toward 0 (no carry-over)."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=0.5)
    with p.section("a"):
        clk.advance(10.0)
    p.frame_end()
    assert abs(p.ms["a"] - 5.0) < 1e-6, p.ms          # 0.5*0 + 0.5*10
    # next frame: "a" not entered -> accumulator reset to 0, decays
    p.frame_end()
    assert abs(p.ms["a"] - 2.5) < 1e-6, p.ms          # 0.5*5 + 0.5*0


def test_begin_end_brackets_a_region():
    """begin()/end() time a region without a `with` block (for instrumenting an
    inline region that can't be cleanly indented), and nest like section()."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    p.begin("render")
    clk.advance(2.0)
    p.begin("raster")
    clk.advance(3.0)
    p.end("raster")
    clk.advance(1.0)
    p.end("render")
    p.frame_end()
    assert abs(p.ms["raster"] - 3.0) < 1e-6, p.ms
    assert abs(p.ms["render"] - 6.0) < 1e-6, p.ms     # 2 + 3 + 1
    assert abs(p.total_ms - 6.0) < 1e-6, p.total_ms   # render top-level only


def test_report_lists_sections_and_total():
    """report() is a one-line string naming each active section and a total."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    with p.section("server"):
        clk.advance(2.0)
    with p.section("render"):
        clk.advance(8.0)
    p.frame_end()
    r = p.report()
    assert "server" in r and "render" in r, r
    assert "total" in r, r
    assert "10.0" in r, r                             # total ms


def test_bars_full_width_at_target():
    """A section whose time equals the target budget draws a full-width bar."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    with p.section("a"):
        clk.advance(10.0)
    p.frame_end()
    out = p.bars(target_ms=10.0, width=12)
    a_line = next(l for l in out.splitlines() if "a " in l and "total" not in l)
    assert a_line.count("█") == 12, a_line       # 12 full blocks


def test_bars_length_scales_with_time():
    """A slower section draws a longer bar than a faster one."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    with p.section("slow"):
        clk.advance(8.0)
    with p.section("fast"):
        clk.advance(2.0)
    p.frame_end()
    out = p.bars(target_ms=16.0, width=16)
    lines = out.splitlines()
    slow = next(l for l in lines if "slow" in l)
    fast = next(l for l in lines if "fast" in l)
    assert slow.count("█") > fast.count("█"), out


def test_bars_nests_child_under_parent_in_order():
    """A nested section is listed (indented) right after its parent, and the
    total row sits at the top, right under the header."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    p.begin("render")
    clk.advance(2.0)
    p.begin("raster")
    clk.advance(4.0)
    p.end("raster")           # raster finishes before render
    clk.advance(1.0)
    p.end("render")
    with p.section("present"):
        clk.advance(3.0)
    p.frame_end()
    lines = p.bars().splitlines()
    idx = lambda sub: next(i for i, l in enumerate(lines) if sub in l)
    assert idx("render") < idx("raster") < idx("present"), lines
    assert lines[idx("raster")].startswith("  "), repr(lines[idx("raster")])
    assert lines[1].startswith("total"), lines    # first row under the header


def test_prof_total_color_buckets():
    """The HUD total-row colour follows the frame-budget buckets: green within
    60fps, yellow within 30, orange within 20, red beyond."""
    from client import prof_total_color, HUD_GREEN
    assert prof_total_color(10.0) == HUD_GREEN          # > 60fps
    assert prof_total_color(25.0) == (255, 204, 0)      # > 30fps
    assert prof_total_color(45.0) == (255, 140, 0)      # > 20fps
    assert prof_total_color(80.0) == (255, 64, 64)


def test_bars_sorts_hottest_first_within_level():
    """Siblings list in descending time at every depth (flame-graph order):
    the slowest top-level section leads, and within a parent the slowest
    child leads -- regardless of the order the sections ran."""
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=1.0)
    p.begin("render")
    with p.section("cool"):
        clk.advance(1.0)
    with p.section("hot"):
        clk.advance(5.0)
    p.end("render")
    with p.section("big"):
        clk.advance(20.0)
    p.frame_end()
    lines = p.bars().splitlines()
    idx = lambda sub: next(i for i, l in enumerate(lines) if sub in l)
    assert idx("big") < idx("render"), lines      # 20ms before 6ms, ran later
    assert idx("hot") < idx("cool"), lines        # 5ms child before 1ms child


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
    assert abs(p.total_ms - p.history[-1]) > 0.5, (p.total_ms, p.history[-1])  # EMA != raw
    assert p._last_raw["total"] == p.history[-1]
    assert abs(p._last_raw["a"] - 10.0) < 1e-6, p._last_raw
    for _ in range(HISTORY_LEN + 50):    # overfill
        with p.section("a"):
            clk.advance(1.0)
        p.frame_end()
    assert len(p.history) == HISTORY_LEN, len(p.history)


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


def test_logging_writes_header_and_raw_rows():
    """start_log writes a header of frame + total + seen sections; each
    frame_end appends one raw (non-EMA) row; stop_log returns (path, n)
    and closes the file."""
    class CaptureIO(io.StringIO):
        name = "run.csv"
        def close(self):
            self.captured = self.getvalue()
            super().close()
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=0.1)        # heavy smoothing: prove rows are raw
    with p.section("a"):                       # one frame so "a" is a known section
        clk.advance(5.0)
    p.frame_end()
    buf = CaptureIO()
    p.start_log("run.csv", open_fn=lambda path, mode, **kw: buf)
    for ms in (8.0, 12.0):
        with p.section("a"):
            clk.advance(ms)
        p.frame_end()
    result = p.stop_log()
    assert result == ("run.csv", 2), result
    assert p._log is None
    assert buf.closed                          # stop_log closed the file
    lines = buf.captured.splitlines()
    assert lines[0] == "frame,total,a", lines[0]   # frame + total + seen section
    assert lines[1] == "0,8.000,8.000", lines[1]   # raw ms, not EMA
    assert lines[2] == "1,12.000,12.000", lines[2]


def test_gauge_and_count_log_as_extra_columns():
    """gauge()/count() metrics append their own CSV columns after the timing
    sections: counts accumulate across the frame and reset, gauges overwrite
    and clear, ints stay whole, floats get 3dp, strings pass through, and a
    metric absent for a frame logs blank."""
    class CaptureIO(io.StringIO):
        name = "m.csv"
        def close(self):
            self.captured = self.getvalue()
            super().close()
    clk = FakeClock()
    p = Profiler(clock=clk, alpha=0.1)
    # one frame so "a" (section) and the metric names are known before start_log
    with p.section("a"):
        clk.advance(5.0)
    p.count("builds", 3)
    p.count("builds")                 # accumulates -> 4 this frame
    p.gauge("x", 1.5)
    p.gauge("map", "e1m1")
    p.frame_end()
    buf = CaptureIO()
    p.start_log("m.csv", open_fn=lambda path, mode, **kw: buf)
    # frame 0: builds counts to 7, gauges set
    p.count("builds", 7)
    p.gauge("x", 2.0)
    p.gauge("map", "e1m1")
    with p.section("a"):
        clk.advance(8.0)
    p.frame_end()
    # frame 1: no metrics set -> all blank (counts reset, gauges cleared)
    with p.section("a"):
        clk.advance(8.0)
    p.frame_end()
    p.stop_log()
    lines = buf.captured.splitlines()
    assert lines[0] == "frame,total,a,builds,x,map", lines[0]
    assert lines[1] == "0,8.000,8.000,7,2.000,e1m1", lines[1]
    assert lines[2] == "1,8.000,8.000,,,", lines[2]   # metrics cleared each frame


def test_metrics_dont_leak_into_timing_readouts():
    """A gauge/count must not appear in self.ms or the bars/report (those are
    durations only)."""
    p = Profiler()
    with p.section("render"):
        pass
    p.count("builds", 5)
    p.gauge("x", 9.0)
    p.frame_end()
    assert "builds" not in p.ms and "x" not in p.ms, p.ms
    assert "builds" not in p.report() and "x" not in p.report()


def test_stop_log_without_start_returns_none():
    p = Profiler()
    assert p.stop_log() is None


def test_double_start_log_closes_the_prior_file():
    """A second start_log without an intervening stop closes the first file
    rather than leaking the handle."""
    p = Profiler()
    first = io.StringIO()
    first.name = "first.csv"
    p.start_log("first.csv", open_fn=lambda *a, **kw: first)
    second = io.StringIO()
    second.name = "second.csv"
    p.start_log("second.csv", open_fn=lambda *a, **kw: second)
    assert first.closed                       # prior file was closed
    assert p._log_file is second
    assert p.stop_log() == ("second.csv", 0)


if __name__ == "__main__":
    test_section_accumulates_elapsed()
    test_same_name_sums_within_frame()
    test_nested_sections_record_independently()
    test_total_counts_top_level_only()
    test_begin_end_brackets_a_region()
    test_ema_smooths_and_resets_between_frames()
    test_report_lists_sections_and_total()
    test_bars_full_width_at_target()
    test_bars_length_scales_with_time()
    test_bars_nests_child_under_parent_in_order()
    test_bars_sorts_hottest_first_within_level()
    test_prof_total_color_buckets()
    test_history_records_raw_total_and_bounds()
    test_graph_maps_totals_to_glyph_heights()
    test_graph_shows_only_last_width_frames()
    test_logging_writes_header_and_raw_rows()
    test_gauge_and_count_log_as_extra_columns()
    test_metrics_dont_leak_into_timing_readouts()
    test_stop_log_without_start_returns_none()
    test_double_start_log_closes_the_prior_file()
    print("OK")
