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
