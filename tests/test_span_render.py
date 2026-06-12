"""Boot the full stack against real shareware data and render e1m1 frames
through the span/edge textured path (quake/r_edge.py). Asserts no crash, that the
engine emitted a sane number of world spans (the world is visible), that the
framebuffer isn't blank, and that the result is deterministic frame-to-frame
(the property the old per-pixel z-buffer lost on coplanar surfaces -- z-fighting).
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)
from test_zbuffer_raster import _renderer, _views


def test_e1m1_frame_emits_spans_and_paints():
    b, r = _renderer()
    views = _views(b)
    _name, eye, yaw, pitch, textured, styles = views[0]   # tex_yaw0
    (fb, w, h), _leaf = r.render_zbuffer(eye, yaw, pitch, textured=True,
                                         lightstyles=styles, time=0.5)
    assert len(fb) == w * h, (len(fb), w, h)
    total = sum(len(s.spans) for s in r.edges.surfaces)
    assert total > 50, ("too few spans -- world not rendering?", total)
    assert len(set(fb)) > 8, "framebuffer looks blank"


def test_span_render_is_deterministic():
    # Two identical renders must produce byte-identical framebuffers. The old
    # z-buffer flickered on coplanar lift/wall seams; the surface-stack tie-break
    # makes the choice deterministic, so repeats are stable.
    b, r = _renderer()
    _name, eye, yaw, pitch, textured, styles = _views(b)[0]
    f1 = bytes(r.render_zbuffer(eye, yaw, pitch, textured=True,
                                lightstyles=styles, time=0.5)[0][0])
    f2 = bytes(r.render_zbuffer(eye, yaw, pitch, textured=True,
                                lightstyles=styles, time=0.5)[0][0])
    assert f1 == f2, "span render not deterministic across identical frames"


if __name__ == "__main__":
    test_e1m1_frame_emits_spans_and_paints()
    test_span_render_is_deterministic()
    print("OK")
