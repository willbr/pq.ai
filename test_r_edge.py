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


if __name__ == "__main__":
    test_single_rect_fills_its_rows()
    print("OK")
