"""Unit tests for quake.perf.Profiler -- the per-frame section timer.

Pure logic, no engine boot: the Profiler takes an injectable clock, so these
drive it with a deterministic fake counter (no real sleeps). Run with
`python test_perf.py` (prints OK) or under pytest."""

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
    total row comes last -- even though it finished before the parent."""
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
    assert "total" in lines[-1], lines


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
    print("OK")
