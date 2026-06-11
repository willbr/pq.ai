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

# Coplanar hysteresis for the surface-stack 1/z tie-break. WinQuake's fudge
# (r_edge.c:493) is ~1%, but it only ever compares coplanar *same-key* surfaces
# (the BSP key orders everything else). This port shares one key across all world
# and brush surfaces -- we don't port id's BSP-key + bmodel-clip machinery -- so
# the 1/z compare runs for *every* overlapping pair. The band must therefore be
# tiny, just above 1/z float-eval noise, or it hides near-but-distinct brush
# surfaces (func_walls/lifts) behind the world. A surface must be nearer by more
# than this fraction to displace the current top; exactly-coplanar surfaces fall
# below it and resolve to the incumbent (first-added) deterministically -- which
# is what kills the lift/wall z-fight, since the span sweep is already
# deterministic frame-to-frame (no per-pixel float-depth ties).
NEARZI_EPS = 1e-4


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
        self.newedges = [None] * height   # newedges[y] = u-sorted list of Edge
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
        # Normalise winding: a leading (left) edge has the polygon interior to
        # its right. Which screen direction that is depends on the polygon's
        # winding, which BSP faces don't guarantee after projection -- so derive
        # it from the signed area instead of assuming. ccw True => up-going edges
        # (start y > end y) are leading; False => down-going are leading.
        area = 0.0
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            area += ax * by - bx * ay
        ccw = area > 0.0
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            if ay == by:
                continue                      # horizontal edge: no scanlines
            going_up = ay > by
            leading = going_up if ccw else (not going_up)
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
        """R_ScanEdges: sweep top->bottom, maintain a u-sorted active edge list
        and a surface stack sorted by key (1/z tie-break on equal keys), emitting
        one span per (top-of-stack surface, run-of-u). Returns the surfaces in the
        order they were added, each with its .spans populated."""
        active = []                       # u-sorted active edges this scanline
        newedges = self.newedges
        removeedges = self.removeedges
        for v in range(self.height):
            new = newedges[v]
            if new is not None:
                active.extend(new)
                active.sort(key=_edge_u)
            self._generate_spans(active, v)
            rem = removeedges[v]
            if rem is not None:
                rset = set(map(id, rem))
                active = [e for e in active if id(e) not in rset]
            for e in active:
                e.u += e.u_step
            active.sort(key=_edge_u)       # full re-sort handles edge crossings
        return self.surfaces

    def _generate_spans(self, active, v):
        # Walk active edges left->right. `top` is the nearest surface currently
        # covering the sweep position; `bg` sits permanently underneath. A leading
        # edge inserts its surface into the depth-sorted stack (becoming the new
        # top if nearer); a trailing edge removes it. Each time the top changes,
        # the old top's span [last_u, u) is closed and the new top's opened.
        # R_GenerateSpans + R_LeadingEdge/R_TrailingEdge.
        bg = self.bg
        bg.next = bg.prev = None
        bg.spanstate = 1
        bg.last_u = 0
        top = bg
        w = self.width
        fv = float(v)
        for e in active:
            fu = e.u
            u = int(fu + 0.5)
            if u < 0:
                u = 0
            elif u > w:
                u = w
            sl = e.surf_lead
            if sl is not None:
                if self._nearer(sl, top, fu, fv):
                    self._close_span(top, u, v)     # sl becomes the new top
                    sl.prev = None
                    sl.next = top
                    top.prev = sl
                    top = sl
                    sl.last_u = u
                else:                                # insert sl below the top
                    cur = top
                    while cur.next is not None and self._nearer(cur.next, sl, fu, fv):
                        cur = cur.next
                    sl.next = cur.next
                    sl.prev = cur
                    if cur.next is not None:
                        cur.next.prev = sl
                    cur.next = sl
                sl.spanstate = 1
            st = e.surf_trail
            if st is not None:
                if top is st:                        # the visible surface ends
                    self._close_span(st, u, v)
                    top = st.next if st.next is not None else bg
                    top.prev = None
                    top.last_u = u
                else:                                # a hidden surface ends
                    p = st.prev
                    nx = st.next
                    if p is not None:
                        p.next = nx
                    if nx is not None:
                        nx.prev = p
                st.spanstate = 0
                st.next = st.prev = None
        self._close_span(top, w, v)

    def _nearer(self, surf, other, fu, fv):
        # True if `surf` should sit above `other` at sweep x=fu, row fv. Smaller
        # key = nearer in BSP order; equal keys (coplanar brush vs world) fall to
        # a 1/z compare with id's ~1% fudge for hysteresis (r_edge.c:488).
        sk = surf.key
        ok = other.key
        if sk < ok:
            return True
        if sk > ok:
            return False
        z00, zdx, zdy = surf.zi
        t00, tdx, tdy = other.zi
        newzi = z00 + zdx * fu + zdy * fv
        topzi = t00 + tdx * fu + tdy * fv
        # nearer by more than the coplanar band displaces; within it, the
        # incumbent (already on top) stays -- deterministic, no z-fight.
        return newzi > topzi * (1.0 + NEARZI_EPS)

    def _close_span(self, surf, u, v):
        if surf.spanstate and surf is not self.bg and u > surf.last_u:
            surf.spans.append((surf.last_u, v, u - surf.last_u))
        surf.last_u = u


def _edge_u(e):
    return e.u


if __name__ == "__main__":
    er = EdgeRaster(32, 32)
    er.begin_frame()
    er.add_surface(5, NORMAL, (0.5, 0.0, 0.0),
                   [(4.0, 4.0), (20.0, 4.0), (20.0, 20.0), (4.0, 20.0)])
    surfs = er.scan()
    total = sum(len(s.spans) for s in surfs)
    assert total > 0, "no spans"
    print("OK", total, "spans")
