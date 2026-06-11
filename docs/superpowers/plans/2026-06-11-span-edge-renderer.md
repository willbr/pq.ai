# Span/edge (scanline) world renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the textured mode's per-pixel z-buffer world fill with a faithful port of WinQuake's edge-list / surface-stack / span renderer (`r_edge.c` + `d_scan.c`), structurally fixing the lift z-fighting.

**Architecture:** A new pure-2D occlusion engine `quake/r_edge.py` (the `r_edge.c` role) turns screen-space convex polygons into edges, sweeps scanlines maintaining a surface stack sorted by BSP key (with a 1/z fudge tie-break for coplanar brush models), and emits spans grouped by surface. `render.py` keeps the camera→screen "emit" half and the per-span pixel "fill" half (the `d_scan.c` role): it projects faces, computes 1/z + texture gradients, feeds screen polygons to the engine, then fills the returned spans from the blocky surface cache / sky / turb — writing 1/z to the existing z-buffer (no depth *test*) so alias models and particles drawn afterward still occlude.

**Tech Stack:** Python 3.13 stdlib only. Standalone `test_*.py` scripts (no pytest). Real Quake shareware data via the `_boot()` helpers. Run muted with `PQ_AUDIO=0`.

---

## File structure

- **Create `quake/r_edge.py`** — the occlusion engine. `Surf`, `Edge`, constants `NORMAL/SKY/TURB`, class `EdgeRaster` (`begin_frame`, `add_surface`, `scan`), and an `if __name__ == "__main__"` smoke self-test. Pure 2D, relative imports only.
- **Create `test_r_edge.py`** — pure-2D unit tests for the engine (no pak).
- **Modify `quake/render.py`** — restructure the world/brush face path inside `render_zbuffer` to emit into an `EdgeRaster` and fill its spans; remove the `BMODEL_ZSCALE` bias; adapt the surface cache to blocky lightmap sampling.
- **Modify `test_zbuffer_raster.py`** — regenerate goldens for the new textured output.
- **Modify `test_lift_zfight.py`** — assert the structural fix (no reliance on the bias constant).
- **Modify `faults.md`** — drop the span-renderer item once landed.

The engine is the only genuinely new, self-contained unit; it gets full code below. The render.py integration is described against the real symbols already in the file.

---

## Task 1: Engine skeleton — `Surf`, `Edge`, `EdgeRaster.begin_frame`, `add_surface`

**Files:**
- Create: `quake/r_edge.py`
- Test: `test_r_edge.py`

- [ ] **Step 1: Write the failing test**

```python
# test_r_edge.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python test_r_edge.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'quake.r_edge'`.

- [ ] **Step 3: Write minimal implementation**

```python
# quake/r_edge.py
"""Span/edge (scanline) occlusion engine -- a faithful, algorithm-level port of
WinQuake's r_edge.c. Pure 2D, no OS/UI: it consumes screen-space convex polygons
plus their 1/z plane gradients and produces, per surface, the horizontal spans
that survive occlusion. render.py owns camera->screen projection and the per-span
pixel fill (the d_scan.c role).

Faithful to the structure of r_edge.c (newedges/removeedges scanline buckets, the
u-sorted active edge list, the surface stack toggled by leading/trailing edges,
the 1/z tie-break with id's ~1% fudge for coplanar brush models -- r_edge.c:488)
but uses idiomatic float math, not 16.16 fixed-point.

Run `python -m quake.r_edge` for a smoke self-test.
"""
import math

NORMAL = 0
SKY = 1
TURB = 2

# id's leading-edge depth tie-break fudge (r_edge.c:493): a surface must be ~1%
# nearer to displace the current top of stack, giving hysteresis so coplanar
# brush-model/world surfaces don't flicker. Replaces render.py's BMODEL_ZSCALE.
NEARZI_FUDGE = 1.0 / 1.01


class Surf:
    __slots__ = ("key", "flags", "zi", "fill", "next", "prev",
                 "spanstate", "last_u", "spans")

    def __init__(self, key=0, flags=NORMAL, zi=(0.0, 0.0, 0.0)):
        self.key = key
        self.flags = flags
        self.zi = zi              # (z00, zdx, zdy): 1/z at (x=0,y=0) and per-px steps
        self.fill = None          # opaque handle render.py attaches
        self.next = self.prev = None
        self.spanstate = 0
        self.last_u = 0
        self.spans = []


class Edge:
    __slots__ = ("u", "u_step", "surf_lead", "surf_trail", "next", "prev")

    def __init__(self):
        self.u = 0.0
        self.u_step = 0.0
        self.surf_lead = None     # Surf revealed when the sweep crosses this edge
        self.surf_trail = None    # Surf concealed
        self.next = self.prev = None


class EdgeRaster:
    """Accumulate surfaces (add_surface), then scan() to get spans per surface."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.newedges = [None] * height   # newedges[y] = u-sorted list head (list)
        self.removeedges = [None] * height

    def begin_frame(self):
        # background sentinel: always bottom of the stack, key larger than any
        # real surface so nothing sorts under it.
        self.surfaces = []
        self.bg = Surf(key=0x7FFFFFFF, flags=NORMAL)
        for y in range(self.height):
            self.newedges[y] = None
            self.removeedges[y] = None

    def add_surface(self, key, flags, zi_plane, screen_poly):
        surf = Surf(key=key, flags=flags, zi=zi_plane)
        self.surfaces.append(surf)
        self._emit_edges(surf, screen_poly)
        return surf

    def _emit_edges(self, surf, poly):
        # Turn a convex screen polygon into left/right edges bucketed by the
        # scanline they activate on. Each polygon edge spanning rows [v0,v1)
        # becomes one Edge stepping u by du/dv per row. WinQuake R_EmitEdge.
        h = self.height
        n = len(poly)
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            if ay == by:
                continue                      # horizontal edge: no scanlines
            leading = ay > by                 # downward in screen-space reveals surf
            if ay > by:
                ax, ay, bx, by = bx, by, ax, ay   # order top (small y) -> bottom
            ytop = int(math.ceil(ay - 0.5))
            ybot = int(math.ceil(by - 0.5))
            if ytop < 0:
                ytop = 0
            if ybot > h:
                ybot = h
            if ytop >= ybot:
                continue
            u_step = (bx - ax) / (by - ay)
            e = Edge()
            e.u_step = u_step
            e.u = ax + (ytop + 0.5 - ay) * u_step     # u at the first sampled row
            if leading:
                e.surf_lead = surf
            else:
                e.surf_trail = surf
            self._bucket_insert(self.newedges, ytop, e)
            self._chain_remove(ybot - 1, e)

    def _bucket_insert(self, buckets, y, e):
        # keep newedges[y] as a u-sorted Python list
        lst = buckets[y]
        if lst is None:
            buckets[y] = [e]
            return
        lo, hi = 0, len(lst)
        while lo < hi:
            mid = (lo + hi) >> 1
            if lst[mid].u < e.u:
                lo = mid + 1
            else:
                hi = mid
        lst.insert(lo, e)

    def _chain_remove(self, y, e):
        if y < 0:
            return
        lst = self.removeedges[y]
        if lst is None:
            self.removeedges[y] = [e]
        else:
            lst.append(e)

    def scan(self):
        raise NotImplementedError   # Task 2
```

- [ ] **Step 4: Run test to verify it still fails (scan not implemented)**

Run: `PQ_AUDIO=0 python test_r_edge.py`
Expected: FAIL — `NotImplementedError` from `scan()`. (Confirms the skeleton imports and `add_surface` runs; `scan` is Task 2.)

- [ ] **Step 5: Commit**

```bash
git add quake/r_edge.py test_r_edge.py
git commit -m "r_edge: span/edge engine skeleton (Surf/Edge/add_surface)"
```

---

## Task 2: The scanline sweep — `EdgeRaster.scan`

**Files:**
- Modify: `quake/r_edge.py`
- Test: `test_r_edge.py` (the Task 1 test now must pass)

- [ ] **Step 1: Implement `scan` (replace the `NotImplementedError` body)**

```python
    def scan(self):
        """R_ScanEdges: sweep top->bottom, maintain a u-sorted active edge list
        and a surface stack sorted by key (1/z tie-break on equal keys), emitting
        one span per (top-of-stack surface, run-of-u). Returns surfaces in the
        order they were added (their .spans are populated)."""
        active = []                      # u-sorted active edges this scanline
        for v in range(self.height):
            new = self.newedges[v]
            if new is not None:
                active = self._merge(active, new)
            self._generate_spans(active, v)
            rem = self.removeedges[v]
            if rem is not None:
                rset = set(id(e) for e in rem)
                active = [e for e in active if id(e) not in rset]
            for e in active:
                e.u += e.u_step
            active.sort(key=lambda e: e.u)   # re-sort after stepping (cheap; small)
        return self.surfaces

    @staticmethod
    def _merge(active, new):
        merged = active + new
        merged.sort(key=lambda e: e.u)
        return merged

    def _generate_spans(self, active, v):
        # Walk active edges left->right. The surface stack `top` is the nearest
        # surface currently covering the sweep position; the background sits
        # underneath. R_GenerateSpans + R_LeadingEdge/R_TrailingEdge.
        bg = self.bg
        bg.next = bg.prev = bg
        bg.spanstate = 1
        bg.last_u = 0
        top = bg
        for e in active:
            u = int(e.u + 0.5)
            if u < 0:
                u = 0
            elif u > self.width:
                u = self.width
            sl = e.surf_lead
            st = e.surf_trail
            if sl is not None:
                # leading edge: surface sl appears. Does it go on top?
                if self._nearer(sl, top, e.u, v):
                    self._close_span(top, u, v)
                    top.spanstate = 1
                    sl.last_u = u
                top = self._stack_insert(top, sl)
                sl.spanstate = 1
                if top is sl:
                    sl.last_u = u
            if st is not None:
                # trailing edge: surface st disappears.
                if top is st:
                    self._close_span(st, u, v)
                    top = self._stack_remove(st)
                    top.last_u = u
                else:
                    self._stack_remove(st)
                st.spanstate = 0
        # close the background span out to the right edge
        self._close_span(top, self.width, v)

    def _nearer(self, surf, top, fu, fv):
        # True if `surf` should sit above `top` at sweep x=fu, row fv.
        if top is self.bg:
            return True
        if surf.key < top.key:
            return True
        if surf.key > top.key:
            return False
        # equal key (coplanar brush vs world): 1/z compare with id's fudge
        z00, zdx, zdy = surf.zi
        t00, tdx, tdy = top.zi
        newzi = z00 + zdx * fu + zdy * fv
        topzi = t00 + tdx * fu + tdy * fv
        return newzi * NEARZI_FUDGE >= topzi

    def _stack_insert(self, top, surf):
        # surf goes above `top` if nearer, else find its slot walking down.
        if self._nearer(surf, top, surf.last_u, 0) or top is self.bg:
            surf.prev = None
            surf.next = top
            return surf
        cur = top
        while cur.next is not None and not self._nearer(surf, cur.next, surf.last_u, 0):
            cur = cur.next
        surf.next = cur.next
        surf.prev = cur
        if cur.next is not None:
            cur.next.prev = surf
        cur.next = surf
        return top

    def _stack_remove(self, surf):
        # unlink surf; return the (possibly new) top.
        p, n = surf.prev, surf.next
        if p is not None:
            p.next = n
        if n is not None:
            n.prev = p
        surf.next = surf.prev = None
        return p if p is not None else (n if n is not None else self.bg)

    def _close_span(self, surf, u, v):
        if surf.spanstate and u > surf.last_u:
            surf.spans.append((surf.last_u, v, u - surf.last_u))
        surf.last_u = u
```

> Note on the stack: WinQuake threads a real doubly-linked list with a head/tail
> sentinel. This port keeps `top` as the head and walks `.next` downward; the
> background `bg` is the permanent bottom. `_stack_insert`/`_stack_remove` keep
> the chain sorted by `_nearer`. If the synthetic tests below reveal an ordering
> bug, prefer matching r_edge.c's R_LeadingEdge structure exactly over patching
> symptoms.

- [ ] **Step 2: Run the Task 1 test to verify it passes**

Run: `PQ_AUDIO=0 python test_r_edge.py`
Expected: PASS — `OK`.

- [ ] **Step 3: Add a module self-test and run it**

Append to `quake/r_edge.py`:

```python
if __name__ == "__main__":
    er = EdgeRaster(32, 32)
    er.begin_frame()
    er.add_surface(5, NORMAL, (0.5, 0.0, 0.0),
                   [(4.0, 4.0), (20.0, 4.0), (20.0, 20.0), (4.0, 20.0)])
    surfs = er.scan()
    total = sum(len(s.spans) for s in surfs)
    assert total > 0, "no spans"
    print("OK", total, "spans")
```

Run: `PQ_AUDIO=0 python -m quake.r_edge`
Expected: PASS — `OK 16 spans`.

- [ ] **Step 4: Commit**

```bash
git add quake/r_edge.py
git commit -m "r_edge: scanline sweep + surface stack -> spans"
```

---

## Task 3: Occlusion correctness tests (overlap, stacking, tie-break, behind)

**Files:**
- Modify: `test_r_edge.py`

- [ ] **Step 1: Add the failing tests**

```python
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
    # for each row in 10..30, the near surface covers 10..30 and the far surface
    # covers 0..10 and 30..40, with nothing covering a pixel twice.
    near = next(s for s in surfs if s.key == 10)
    far = next(s for s in surfs if s.key == 20)
    near_by_row = {v: (u, n) for (u, v, n) in near.spans}
    for v in range(10, 30):
        assert near_by_row[v] == (10, 20), (v, near_by_row.get(v))
    # far surface: no span on rows 10..30 may intrude into 10..30
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
    # The fudge must let A win deterministically -- the abstracted lift case.
    er.add_surface(15, NORMAL, (0.50, 0.0, 0.0),
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    A = er.surfaces[-1]
    er.add_surface(15, NORMAL, (0.55, 0.0, 0.0),
                   [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)])
    B = er.surfaces[-1]
    surfs = er.scan()
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
```

Add all four to the `__main__` block:

```python
if __name__ == "__main__":
    test_single_rect_fills_its_rows()
    test_nearer_rect_occludes_farther_no_overlap_no_gap()
    test_surface_fully_behind_emits_nothing_in_covered_area()
    test_coplanar_equal_key_tiebreak_picks_nearer()
    test_offscreen_clamped()
    print("OK")
```

- [ ] **Step 2: Run the tests**

Run: `PQ_AUDIO=0 python test_r_edge.py`
Expected: PASS — `OK`. If occlusion ordering is wrong, fix `_generate_spans`/`_stack_insert`/`_nearer` to match `r_edge.c` R_LeadingEdge/R_TrailingEdge before moving on. Do not weaken the assertions.

- [ ] **Step 3: Commit**

```bash
git add test_r_edge.py
git commit -m "r_edge: occlusion correctness tests (overlap, behind, tie-break, clamp)"
```

---

## Task 4: Blocky surface cache — revert bilinear in the cached path

**Files:**
- Modify: `quake/render.py` (`_surface_cache`, near the `face_lm` / `raster_poly_cached` machinery)

- [ ] **Step 1: Locate and read `_surface_cache`**

Run: `grep -n "_surface_cache\|def _animate_lightmaps\|bilinear\|lerp" quake/render.py`
Read the `_surface_cache` method and confirm where it samples luxels (currently bilinear, per the `0e95606` commit).

- [ ] **Step 2: Switch luxel sampling to nearest (blocky)**

In `_surface_cache`, replace the 4-tap bilinear luxel blend with a single nearest-luxel fetch (`lr = int((t)*0.0625); lc = int((s)*0.0625)` clamped), so the cache is built at WinQuake's blocky 16-texel resolution. Keep the cache shape, keying, and the texture×colormap combination identical — only the lightmap sampling changes.

- [ ] **Step 3: Run the existing raster golden test (expect a diff, do not regen yet)**

Run: `PQ_AUDIO=0 python test_zbuffer_raster.py`
Expected: FAIL — pixels differ from the goldens (bilinear→blocky). This confirms the change took effect; goldens are regenerated in Task 7 once the whole path is in place.

- [ ] **Step 4: Commit**

```bash
git add quake/render.py
git commit -m "render: blocky (nearest-luxel) surface cache, faithful to D_CacheSurface"
```

---

## Task 5: Wire the EdgeRaster into `render_zbuffer` — emit + fill

**Files:**
- Modify: `quake/render.py` (`render_zbuffer`, `emit_face`, the world/brush face loops; add an `EdgeRaster` instance in `_setup_zbuf`)

This is the structural task. It splits the existing fills into "compute screen poly + gradients" (emit) and "fill a span run" (fill), routing occlusion through `EdgeRaster`.

- [ ] **Step 1: Construct an EdgeRaster alongside the framebuffer**

In `_setup_zbuf` (render.py:1004), after `self.zw`/`self.zh` are set, add:

```python
from .r_edge import EdgeRaster
self.edges = EdgeRaster(self.zw, self.zh)
```

(Lazy import inside the method keeps `r_edge` out of import cost for non-textured boots, matching the file's other in-function imports.)

- [ ] **Step 2: Factor the per-span pixel fills**

For each of the three world fills (`raster_poly_tex` lightmap fallback, `raster_poly_cached`, `raster_poly_tex_turb`, and the sky branch), extract the *inner* `for idx in range(...)` body into a `fill_*` routine that takes the precomputed gradient tuple and a single span `(u, v, count)`, drops the `if iz > zbl[idx]` test, and **writes** `zbl[idx] = iz` unconditionally plus the texel. Keep the gradient setup (projection, `plane_gradients`) in an `emit_*` routine that returns `(screen_poly, zi_plane, fill_handle)` where `fill_handle` carries everything the `fill_*` body needs (gradients, texture/cache refs, flags).

- [ ] **Step 3: Rewrite `emit_face` to add surfaces instead of filling**

`emit_face(fi, pts, rec, zscale)` becomes: compute the screen polygon and gradients via the matching `emit_*`, then:

```python
surf = self.edges.add_surface(key=face_key, flags=flag, zi_plane=zi_plane,
                              screen_poly=screen_poly)
surf.fill = fill_handle
```

`face_key` is the BSP draw-order index for world faces; brush-model faces pass the entity's own key so coplanar lift/world surfaces collide and hit the tie-break. `flag` is `SKY`/`TURB`/`NORMAL`.

- [ ] **Step 4: Drive scan + fill after all faces are emitted**

Where `render_zbuffer` currently finishes the world/brush loops (before alias models/particles), call `begin_frame` before the loops and after them:

```python
for surf in self.edges.scan():
    fh = surf.fill
    for (u, v, count) in surf.spans:
        fh.fill(u, v, count)        # writes fb + zb, no depth test
```

Add `self.edges.begin_frame()` immediately before the world/brush emission loops.

- [ ] **Step 5: Leave alias models, particles, sprites, view model unchanged**

They already run after the world section and test+write `zb`. The z-buffer is now seeded by the world spans (write-only), so their occlusion is correct without changes.

- [ ] **Step 6: Run the full boot smoke (Task 6 test) and the raster test**

Run: `PQ_AUDIO=0 python test_zbuffer_raster.py`
Expected: FAIL on golden comparison (output changed) but **no crash/exception**. A crash here means the emit/fill split is wrong — debug before regenerating goldens.

- [ ] **Step 7: Commit**

```bash
git add quake/render.py
git commit -m "render: route textured world/brush fill through the span/edge engine"
```

---

## Task 6: Full-stack boot test for the span path

**Files:**
- Create: `test_span_render.py`

- [ ] **Step 1: Write the test**

```python
# test_span_render.py
"""Boot the full stack against real shareware data and render one e1m1 frame
through the span/edge textured path. Asserts no crash and that the engine
emitted a sane, non-trivial number of world spans (the world is visible)."""
import os
from test_zbuffer_raster import _boot   # reuse the existing boot helper


def test_e1m1_frame_emits_spans():
    r, origin, yaw, pitch = _boot()      # adapt to the helper's actual return
    (fb, w, h), leaf = r.render_zbuffer(origin, yaw, pitch, textured=True)
    assert len(fb) == w * h, (len(fb), w, h)
    total = sum(len(s.spans) for s in r.edges.surfaces)
    assert total > 50, ("too few spans -- world not rendering?", total)
    # framebuffer isn't entirely background
    assert len(set(fb)) > 4, "framebuffer looks blank"


if __name__ == "__main__":
    test_e1m1_frame_emits_spans()
    print("OK")
```

- [ ] **Step 2: Inspect `test_zbuffer_raster.py` for the real `_boot` signature**

Run: `grep -n "def _boot\|render_zbuffer\|return" test_zbuffer_raster.py | head`
Adjust the unpacking in Step 1 to match (origin/yaw/pitch source). If `_boot` isn't reusable, copy its body inline.

- [ ] **Step 3: Run the test**

Run: `PQ_AUDIO=0 python test_span_render.py`
Expected: PASS — `OK`.

- [ ] **Step 4: Commit**

```bash
git add test_span_render.py
git commit -m "test: full-stack span/edge render smoke for e1m1"
```

---

## Task 7: Regenerate goldens; verify the lift fix; remove the bias

**Files:**
- Modify: `quake/render.py` (delete `BMODEL_ZSCALE` and its use), `test_lift_zfight.py`, `test_zbuffer_raster.py` goldens, `faults.md`

- [ ] **Step 1: Remove the `BMODEL_ZSCALE` bias**

Delete the `BMODEL_ZSCALE = 1.001` constant (render.py:226) and its comment block (lines 219-226), and replace its use (the `zscale` passed for brush models) with `1.0`. The engine's `NEARZI_FUDGE` tie-break now handles coplanar lifts.

Run: `grep -n "BMODEL_ZSCALE" quake/render.py` → expect no matches.

- [ ] **Step 2: Regenerate the raster goldens and eyeball**

Run: `PQ_AUDIO=0 python test_zbuffer_raster.py --regen`
Then open the regenerated golden image(s) and confirm the world renders correctly (walls textured, sky/water present, no gaps/garbage). Re-run without `--regen`:

Run: `PQ_AUDIO=0 python test_zbuffer_raster.py`
Expected: PASS.

- [ ] **Step 3: Verify the lift z-fight is fixed structurally**

Read `test_lift_zfight.py`. It currently exercises the coplanar lift/wall case. Confirm it passes with the bias gone:

Run: `PQ_AUDIO=0 python test_lift_zfight.py`
Expected: PASS. If it asserted on the bias constant or a specific bias value, rewrite the assertion to check the rendered lift surface is stable/visible across two adjacent frames (no flicker) rather than referencing `BMODEL_ZSCALE`.

- [ ] **Step 4: Run the whole suite muted**

Run: `PQ_AUDIO=0 for t in test_*.py; do echo "== $t"; python "$t" || break; done`
Expected: every test prints `OK`. Fix any regression before proceeding.

- [ ] **Step 5: Update `faults.md`**

Remove the "Span/edge renderer port" item from `faults.md` (it's now done). If that leaves the "Remaining / future work" section empty, state that there is no remaining tracked work.

- [ ] **Step 6: Commit**

```bash
git add quake/render.py test_lift_zfight.py test_zbuffer_raster.py faults.md
git commit -m "render: span/edge renderer fixes lift z-fighting; drop the depth-bias stopgap"
```

---

## Self-review notes

- **Spec coverage:** module boundary (Task 1/5), data structures (Task 1), per-frame flow emit/scan/fill (Task 5), surface stack + fudge tie-break (Task 2/3), blocky surface cache (Task 4), z-buffer write-only for world + test for models (Task 5 steps 4-5), sky/turb fills re-hosted (Task 5 step 2), tests incl. lift fix + golden regen (Tasks 3/6/7), faults.md (Task 7). All spec sections map to a task.
- **Type consistency:** `EdgeRaster(width, height)`, `begin_frame()`, `add_surface(key, flags, zi_plane, screen_poly) -> Surf`, `scan() -> list[Surf]`, `Surf.spans` = list of `(u, v, count)`, `Surf.fill` opaque handle, constants `NORMAL/SKY/TURB` — used consistently across Tasks 1-7.
- **Risk flagged in plan:** the surface-stack linked-list logic (`_generate_spans`/`_stack_insert`) is the subtle part; Task 3's assertions are the guard, and the note says match `r_edge.c` structure rather than patch symptoms. The per-frame `active.sort` after stepping is a Python simplification of WinQuake's incremental re-sort — correct (edges rarely cross between scanlines), and replaceable with an insertion-repair pass if profiling demands.
