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
    # same key (coplanar brush-vs-world), same rect; A is 1% nearer than B.
    # The fudge must let the nearer one win deterministically -- the lift case.
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
    test_offscreen_clamped()
    print("OK")
