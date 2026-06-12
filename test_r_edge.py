"""Pure-2D unit tests for the span/edge occlusion engine (quake/r_edge.py).
No pak, no camera -- feed screen-space convex polygons and 1/z plane gradients,
assert the spans the scanline sweep emits. Mirrors WinQuake R_ScanEdges.
"""
from quake.r_edge import EdgeRaster, NORMAL


def _spans_by_key(surfs):
    """Flatten scan() output to {key: sorted [(v, u, count)]} for assertions."""
    out = {}
    for s in surfs:
        out.setdefault(s.key, []).extend((v, u, n) for (u, v, n) in s.spans)
    for k in out:
        out[k].sort()
    return out


def test_single_rect_fills_its_rows():
    er = EdgeRaster(64, 64)
    er.begin_frame()
    # a 10..30 x 5..15 axis-aligned rectangle; flat depth 1/z = 0.5 everywhere
    er.add_surface(key=10, flags=NORMAL, zi_plane=(0.5, 0.0, 0.0),
                   screen_poly=[(10.0, 5.0), (30.0, 5.0), (30.0, 15.0), (10.0, 15.0)])
    surfs = er.scan()
    spans = _spans_by_key(surfs)
    assert 10 in spans, "the surface emitted no spans"
    # every covered row is a single span starting at u=10 with width 20
    rows = {v for (v, u, n) in spans[10]}
    assert rows == set(range(5, 15)), rows
    for (v, u, n) in spans[10]:
        assert (u, n) == (10, 20), (v, u, n)


def test_nearer_rect_occludes_farther_no_overlap_no_gap():
    er = EdgeRaster(64, 64)
    er.begin_frame()
    # FAR: big rect, key 20, 1/z = 0.2 (far); NEAR: smaller rect on top of it,
    # key 10, 1/z = 0.8 (near). The near rect must claim its area; the far rect
    # must yield exactly that area and keep the rest -- zero overlap, zero gap.
    er.add_surface(20, NORMAL, (0.2, 0.0, 0.0),
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    er.add_surface(10, NORMAL, (0.8, 0.0, 0.0),
                   [(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)])
    surfs = er.scan()
    near = next(s for s in surfs if s.key == 10)
    far = next(s for s in surfs if s.key == 20)
    near_by_row = {v: (u, n) for (u, v, n) in near.spans}
    for v in range(10, 30):
        assert near_by_row[v] == (10, 20), (v, near_by_row.get(v))
    # far surface: no span on rows 10..30 may intrude into the near rect's column
    for (u, v, n) in far.spans:
        if 10 <= v < 30:
            assert u + n <= 10 or u >= 30, ("overlap", v, u, n)


def test_surface_fully_behind_emits_nothing_in_covered_area():
    er = EdgeRaster(64, 64)
    er.begin_frame()
    # identical rects, BEHIND has the farther 1/z and the larger (loses) key
    er.add_surface(10, NORMAL, (0.9, 0.0, 0.0),
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    er.add_surface(20, NORMAL, (0.1, 0.0, 0.0),
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    surfs = er.scan()
    behind = next(s for s in surfs if s.key == 20)
    assert sum(n for (u, v, n) in behind.spans) == 0, behind.spans


def test_coplanar_equal_key_tiebreak_picks_nearer():
    er = EdgeRaster(64, 64)
    er.begin_frame()
    # same key (two bmodels in one leaf), same rect; B is 10% nearer than A.
    # The 1/z sort must let the nearer one win deterministically.
    er.insubmodel = True
    er.add_surface(15, NORMAL, (0.50, 0.0, 0.0),
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    A = er.surfaces[-1]
    er.add_surface(15, NORMAL, (0.55, 0.0, 0.0),
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    B = er.surfaces[-1]
    er.scan()
    a_px = sum(n for (u, v, n) in A.spans)
    b_px = sum(n for (u, v, n) in B.spans)
    assert b_px > a_px, ("nearer (B) should win the coplanar tie", a_px, b_px)


def test_near_but_distinct_brush_orders_exactly():
    # A brush surface (func_wall/lift) only ~0.5% nearer than the world behind it
    # must still win where they overlap. The original 1% fudge, applied to every
    # comparison (not just coplanar same-key pairs as in WinQuake), created a
    # depth dead-zone that hid near-but-distinct brush surfaces -- the reported
    # "func walls and lifts z-order all broken" bug.
    er = EdgeRaster(64, 64)
    er.begin_frame()
    er.insubmodel = True
    er.add_surface(0, NORMAL, (0.0100, 0.0, 0.0),       # bmodel wall, added first
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    er.add_surface(0, NORMAL, (0.01005, 0.0, 0.0),      # func_wall ~0.5% nearer
                   [(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)])
    F = er.surfaces[-1]
    er.scan()
    f_by_row = {v: (u, n) for (u, v, n) in F.spans}
    for v in range(10, 30):
        assert f_by_row.get(v) == (10, 20), \
            (v, f_by_row.get(v), "nearer brush surface hidden by farther world")


def test_coplanar_is_stable_and_deterministic():
    # Two *exactly* coplanar surfaces (equal 1/z) must resolve deterministically
    # to one of them with no overlap -- the property that kills z-fighting. A
    # repeat scan must be identical.
    def run():
        er = EdgeRaster(64, 64)
        er.begin_frame()
        er.add_surface(0, NORMAL, (0.02, 0.0, 0.0),
                       [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
        a = er.surfaces[-1]
        er.add_surface(0, NORMAL, (0.02, 0.0, 0.0),
                       [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
        b = er.surfaces[-1]
        er.scan()
        return (sum(n for (_u, _v, n) in a.spans),
                sum(n for (_u, _v, n) in b.spans))
    r1 = run()
    r2 = run()
    assert r1 == r2, ("coplanar resolution not deterministic", r1, r2)
    # exactly one of them owns the overlap (no double-paint, no gap)
    assert (r1[0] == 0) != (r1[1] == 0), ("coplanar overlap not cleanly owned", r1)


def test_coplanar_bmodel_overlap_new_surface_wins():
    # Two *exactly* coplanar brush-model surfaces with the same key, partially
    # overlapping -- the e1m1 door halves, whose interlocking-teeth front faces
    # share one plane and one world leaf. WinQuake sorts same-key bmodels on 1/z
    # with a 1% fudge; an exact tie falls to `d_zistepu >=` (r_edge.c:506), which
    # puts the surface whose leading edge the sweep meets LATER on top. The old
    # incumbent-wins epsilon gave the overlap to the earlier surface, hiding the
    # later door half's tooth border.
    er = EdgeRaster(64, 64)
    er.begin_frame()
    er.insubmodel = True
    er.add_surface(7, NORMAL, (0.02, 0.0, 0.0),
                   [(0.0, 0.0), (30.0, 0.0), (30.0, 40.0), (0.0, 40.0)])
    a = er.surfaces[-1]
    er.add_surface(7, NORMAL, (0.02, 0.0, 0.0),
                   [(20.0, 0.0), (50.0, 0.0), (50.0, 40.0), (20.0, 40.0)])
    b = er.surfaces[-1]
    er.insubmodel = False
    er.scan()
    a_by_row = {v: (u, n) for (u, v, n) in a.spans}
    b_by_row = {v: (u, n) for (u, v, n) in b.spans}
    for v in range(0, 40):
        assert b_by_row.get(v) == (20, 30), \
            (v, b_by_row.get(v), "later bmodel must own the coplanar overlap")
        assert a_by_row.get(v) == (0, 20), (v, a_by_row.get(v))


def test_inverted_span_does_not_stick():
    # A near-clipped brush face can project to a huge, badly-warped screen
    # polygon (vertices at x ~ -30000 after a vertex lands close to the near
    # plane). Its leading and trailing edges then cross, so on some scanlines
    # the trailing edge precedes the leading edge in u order -- id's "inverted
    # span". WinQuake guards both handlers (`if (++surf->spanstate == 1)` /
    # `if (--surf->spanstate == 0)`, r_edge.c:348/424) so the pair is a no-op;
    # without the guard the surface is inserted but never removed and floods
    # the rest of every such scanline (the e1m1 black-band flicker at the
    # extended bridge, faults.md #4). The polygon below is the real offender
    # captured from that scene; it is entirely left of the screen, so it must
    # emit nothing at all.
    # Both polygons captured verbatim from the failing frame. The bmodel one is
    # a zero-area sliver with duplicated vertices (near-clip artifact): its
    # leading and trailing edges share identical u on every row, so the sort's
    # tie order -- perturbed by the other surface's edges -- decides which
    # fires first. It lies entirely left of the screen and must emit nothing.
    er = EdgeRaster(240, 160)
    er.begin_frame()
    er.add_surface(0, NORMAL,
                   (-0.0017112112259404233, 3.191942337183981e-06,
                    -4.7608032225730936e-05),
                   [(9183.188732653827, -20425.10385031782),
                    (5870.307086453869, -12972.707322050277),
                    (9138.231312318423, -20428.118078939133)])
    er.insubmodel = True
    er.add_surface(0, NORMAL,
                   (0.006303843117536179, -5.436509020650132e-05, 0.0),
                   [(-18278.203036323324, 6171.058931986609),
                    (-89.21352554152105, 40.97637748362866),
                    (-89.21352554152105, 40.97637748362866),
                    (-18278.203036323324, 6171.058931986611)])
    bad = er.surfaces[-1]
    er.insubmodel = False
    er.scan()
    bad_px = sum(n for (_u, _v, n) in bad.spans)
    assert bad_px == 0, ("fully off-screen sliver drew pixels", bad_px,
                         bad.spans[:4])


def test_offscreen_clamped():
    er = EdgeRaster(32, 32)
    er.begin_frame()
    # rect hanging off the left and top edges -- spans clamp to [0,w)/[0,h)
    er.add_surface(5, NORMAL, (0.5, 0.0, 0.0),
                   [(-20.0, -20.0), (10.0, -20.0), (10.0, 10.0), (-20.0, 10.0)])
    surfs = er.scan()
    for (u, v, n) in surfs[0].spans:
        assert u >= 0 and u + n <= 32 and 0 <= v < 32, (u, v, n)


if __name__ == "__main__":
    test_single_rect_fills_its_rows()
    test_nearer_rect_occludes_farther_no_overlap_no_gap()
    test_surface_fully_behind_emits_nothing_in_covered_area()
    test_coplanar_equal_key_tiebreak_picks_nearer()
    test_near_but_distinct_brush_orders_exactly()
    test_coplanar_is_stable_and_deterministic()
    test_coplanar_bmodel_overlap_new_surface_wins()
    test_inverted_span_does_not_stick()
    test_offscreen_clamped()
    print("OK")
