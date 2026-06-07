"""Wireframe renderer for Quake BSP. Pure stdlib.

Per frame:
  1. find the leaf the camera is in (walk the BSP nodes)
  2. decompress that leaf's PVS -> set of potentially visible leaves
  3. gather those leaves' faces, backface-cull them
  4. dedup edges, transform vertices to camera space (cached per frame)
  5. clip each edge to the near plane, project to screen, cull off-screen
Returns a flat list of (x0, y0, x1, y1) line segments for the UI to draw.

Quake world space is Z-up, right-handed. Camera space: x=right, y=up, z=forward.
"""

import math
import sys

sys.setrecursionlimit(20000)   # BSP back-to-front walk can recurse deep

NEAR = 1.0
BACKFACE_EPS = 0.01
MIN_SEG_PX2 = 9.0          # drop segments shorter than 3px (Tk cost, no detail)


def angle_vectors(yaw_deg, pitch_deg):
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    forward = (cp * cy, cp * sy, -sp)
    right = (sy, -cy, 0.0)
    up = (sp * cy, sp * sy, cp)
    return forward, right, up


class Renderer:
    def __init__(self, bsp, palette=None):
        self.bsp = bsp
        self.palette = palette          # list of 256 (r,g,b) for texture colours
        self.headnode = bsp.models[0]["headnode"]
        self.width = 800
        self.height = 600
        self.fov = 90.0
        self.backface = True
        self.brushmodels = True     # draw doors/lifts/buttons (submodels 1..N)
        self._update_focal()

        nfaces = len(bsp.faces)
        nedges = len(bsp.edges)
        nverts = len(bsp.vertexes)

        # precompute per-face: ordered abs edge indices, ordered vertex indices
        # (winding), and the outward plane (n, dist)
        self.face_edges = []
        self.face_verts = []
        self.face_plane = []
        self.face_centroid = []     # world-space centroid (for sorting model faces)
        for planenum, side, firstedge, numedges, texinfo in bsp.faces:
            eidx = []
            vidx = []
            sx = sy = sz = 0.0
            for k in range(numedges):
                se = bsp.surfedges[firstedge + k]
                eidx.append(abs(se))
                vi = bsp.edges[se][0] if se >= 0 else bsp.edges[-se][1]
                vidx.append(vi)
                vx, vy, vz = bsp.vertexes[vi]
                sx += vx
                sy += vy
                sz += vz
            self.face_edges.append(eidx)
            self.face_verts.append(vidx)
            inv = 1.0 / numedges if numedges else 0.0
            self.face_centroid.append((sx * inv, sy * inv, sz * inv))
            (nx, ny, nz), dist, _ = bsp.planes[planenum]
            if side:
                nx, ny, nz, dist = -nx, -ny, -nz, -dist
            self.face_plane.append((nx, ny, nz, dist))

        # average RGB per texture (from its mip-0 palette indices)
        tex_rgb = self._texture_colors()

        # precompute a flat-shade fill colour per face: texture's average colour
        # modulated by a static directional light. Falls back to grey if a face
        # has no usable texture / no palette was supplied.
        lx, ly, lz = 0.35, 0.25, 0.90
        lm = math.sqrt(lx * lx + ly * ly + lz * lz)
        lx, ly, lz = lx / lm, ly / lm, lz / lm
        # Quake texture averages are very dark (the engine brightens with
        # lightmaps, which we don't apply) -> boost so the scene is visible.
        gain = 2.2
        texinfo = bsp.texinfo
        self.face_color = []
        for fi, (nx, ny, nz, dist) in enumerate(self.face_plane):
            inten = (0.50 + 0.50 * max(0.0, nx * lx + ny * ly + nz * lz)) * gain
            base = None
            ti = bsp.faces[fi][4]
            if 0 <= ti < len(texinfo):
                mt = texinfo[ti][0]
                if 0 <= mt < len(tex_rgb):
                    base = tex_rgb[mt]
            if base is None:
                base = (140.0, 140.0, 140.0)
            r = min(255, int(base[0] * inten))
            g = min(255, int(base[1] * inten))
            b = min(255, int(base[2] * inten))
            self.face_color.append(f"#{r:02x}{g:02x}{b:02x}")

        # per-frame staleness markers (avoid clearing big arrays every frame)
        self.frame = 0
        self.face_frame = [0] * nfaces
        self.edge_frame = [0] * nedges
        self.vert_frame = [0] * nverts
        self.vcache = [None] * nverts

        # vis decompression scratch
        self.vis_row = (len(bsp.leafs) + 7) >> 3

    def _texture_colors(self):
        """Average RGB per miptex via a palette histogram. None where unusable."""
        from collections import Counter
        pal = self.palette
        out = []
        for t in self.bsp.textures:
            if t is None or t[3] is None or pal is None:
                out.append(None)
                continue
            r = g = b = tot = 0
            for idx, c in Counter(t[3]).items():     # idx -> pixel count
                pr, pg, pb = pal[idx]
                r += pr * c
                g += pg * c
                b += pb * c
                tot += c
            out.append((r / tot, g / tot, b / tot) if tot else None)
        return out

    def _update_focal(self):
        self.focal = (self.width / 2) / math.tan(math.radians(self.fov) / 2)

    def resize(self, w, h):
        self.width, self.height = w, h
        self._update_focal()

    # ---- BSP queries ----
    def point_leaf(self, p):
        node = self.headnode
        nodes = self.bsp.nodes
        planes = self.bsp.planes
        px, py, pz = p
        while node >= 0:
            planenum, children, _, _ = nodes[node]
            (nx, ny, nz), dist, _ = planes[planenum]
            d = px * nx + py * ny + pz * nz - dist
            node = children[0] if d >= 0 else children[1]
        return -node - 1   # leaf index

    def box_in_pvs(self, mins, maxs, vis):
        """True if the AABB touches any leaf marked visible in the PVS bitset.
        Walks the world BSP, descending both sides where the box straddles a
        plane (Quake's Mod_BoxLeafnums, short-circuited on the first hit)."""
        nodes = self.bsp.nodes
        planes = self.bsp.planes
        stack = [self.headnode]
        while stack:
            num = stack.pop()
            while num >= 0:
                planenum, children, _, _ = nodes[num]
                (nx, ny, nz), dist, _ = planes[planenum]
                # project the box extents onto the plane normal
                near = (nx * (maxs[0] if nx >= 0 else mins[0]) +
                        ny * (maxs[1] if ny >= 0 else mins[1]) +
                        nz * (maxs[2] if nz >= 0 else mins[2])) - dist
                far = (nx * (mins[0] if nx >= 0 else maxs[0]) +
                       ny * (mins[1] if ny >= 0 else maxs[1]) +
                       nz * (mins[2] if nz >= 0 else maxs[2])) - dist
                if far >= 0:
                    num = children[0]          # fully in front
                elif near < 0:
                    num = children[1]          # fully behind
                else:
                    stack.append(children[1])  # straddle: visit back later
                    num = children[0]
            leafidx = -num - 1
            if leafidx > 0:
                bit = leafidx - 1
                if vis[bit >> 3] & (1 << (bit & 7)):
                    return True
        return False

    def decompress_vis(self, visofs):
        row = self.vis_row
        if visofs < 0:
            return b"\xff" * row
        out = bytearray(row)            # pre-zeroed: missing tail = "not visible"
        data = self.bsp.visdata
        n = len(data)
        o = i = 0
        while o < row:
            # the last leaf's RLE stream is truncated; the original C over-reads
            # into zeroed memory. We stop at the boundary and leave zeros.
            if visofs + i >= n:
                break
            b = data[visofs + i]
            i += 1
            if b:
                out[o] = b
                o += 1
            else:
                if visofs + i >= n:
                    break
                o += data[visofs + i]   # run of zero bytes
                i += 1
        return bytes(out)

    # ---- main entry ----
    def render(self, origin, yaw, pitch):
        bsp = self.bsp
        self.frame += 1
        frame = self.frame
        forward, right, up = angle_vectors(yaw, pitch)
        ox, oy, oz = origin
        fx, fy, fz = forward
        rx, ry, rz = right
        ux, uy, uz = up

        vertexes = bsp.vertexes
        edges = bsp.edges
        leafs = bsp.leafs
        marks = bsp.marksurfaces
        face_edges = self.face_edges
        face_plane = self.face_plane
        face_frame = self.face_frame
        edge_frame = self.edge_frame
        vert_frame = self.vert_frame
        vcache = self.vcache

        focal = self.focal
        hw = self.width / 2
        hh = self.height / 2
        W, H = self.width, self.height
        backface = self.backface

        leaf = self.point_leaf(origin)
        visofs = leafs[leaf][1]
        vis = self.decompress_vis(visofs)

        # build the list of visible leaf indices from the PVS bitset
        visible_leaves = []
        nleaf = len(leafs)
        for i in range(nleaf - 1):
            if vis[i >> 3] & (1 << (i & 7)):
                visible_leaves.append(i + 1)

        def transform(vi):
            if vert_frame[vi] == frame:
                return vcache[vi]
            vx, vy, vz = vertexes[vi]
            dx, dy, dz = vx - ox, vy - oy, vz - oz
            c = (dx * rx + dy * ry + dz * rz,    # camera x (right)
                 dx * ux + dy * uy + dz * uz,    # camera y (up)
                 dx * fx + dy * fy + dz * fz)    # camera z (forward/depth)
            vcache[vi] = c
            vert_frame[vi] = frame
            return c

        segments = []

        def emit_face(fi):
            if face_frame[fi] == frame:
                return
            face_frame[fi] = frame

            if backface:
                nx, ny, nz, dist = face_plane[fi]
                if ox * nx + oy * ny + oz * nz - dist <= BACKFACE_EPS:
                    return

            for ei in face_edges[fi]:
                if edge_frame[ei] == frame:
                    continue
                edge_frame[ei] = frame
                a, b = edges[ei]
                cax, cay, caz = transform(a)
                cbx, cby, cbz = transform(b)

                # near-plane clip (caz/cbz are depth)
                if caz < NEAR and cbz < NEAR:
                    continue
                if caz < NEAR:
                    t = (NEAR - caz) / (cbz - caz)
                    cax += (cbx - cax) * t
                    cay += (cby - cay) * t
                    caz = NEAR
                elif cbz < NEAR:
                    t = (NEAR - cbz) / (caz - cbz)
                    cbx += (cax - cbx) * t
                    cby += (cay - cby) * t
                    cbz = NEAR

                x0 = hw + cax * focal / caz
                y0 = hh - cay * focal / caz
                x1 = hw + cbx * focal / cbz
                y1 = hh - cby * focal / cbz

                # cheap off-screen reject (both ends past one edge)
                if (x0 < 0 and x1 < 0) or (x0 > W and x1 > W):
                    continue
                if (y0 < 0 and y1 < 0) or (y0 > H and y1 > H):
                    continue

                dxp = x1 - x0
                dyp = y1 - y0
                if dxp * dxp + dyp * dyp < MIN_SEG_PX2:
                    continue                # sub-pixel: not worth a Tk line draw

                segments.append((x0, y0, x1, y1))

        # world (model 0): only the PVS-visible leaves' surfaces
        for li in visible_leaves:
            _, _, firstmark, nummark = leafs[li]
            for m in range(firstmark, firstmark + nummark):
                emit_face(marks[m])

        # brush submodels (doors, lifts, buttons, secret walls). These aren't in
        # any leaf's marksurfaces, so the PVS walk never reaches them. They're few,
        # so just feed them all through backface/off-screen culling.
        if self.brushmodels:
            for md in bsp.models[1:]:
                if not self.box_in_pvs(md["mins"], md["maxs"], vis):
                    continue
                ff = md["firstface"]
                for fi in range(ff, ff + md["numfaces"]):
                    emit_face(fi)

        return segments, leaf

    def render_shaded(self, origin, yaw, pitch):
        """Flat-shaded polygons, back-to-front (painter's algorithm via the BSP).
        Returns (polys, leaf) where each poly is (flat_xy_coords, fill_color)."""
        bsp = self.bsp
        self.frame += 1
        frame = self.frame
        forward, right, up = angle_vectors(yaw, pitch)
        ox, oy, oz = origin
        fx, fy, fz = forward
        rx, ry, rz = right
        ux, uy, uz = up

        vertexes = bsp.vertexes
        leafs = bsp.leafs
        marks = bsp.marksurfaces
        nodes = bsp.nodes
        planes = bsp.planes
        face_verts = self.face_verts
        face_plane = self.face_plane
        face_color = self.face_color
        face_frame = self.face_frame
        vert_frame = self.vert_frame
        vcache = self.vcache
        focal = self.focal
        hw = self.width / 2
        hh = self.height / 2
        backface = self.backface

        leaf = self.point_leaf(origin)
        vis = self.decompress_vis(leafs[leaf][1])

        def transform(vi):
            if vert_frame[vi] == frame:
                return vcache[vi]
            vx, vy, vz = vertexes[vi]
            dx, dy, dz = vx - ox, vy - oy, vz - oz
            c = (dx * rx + dy * ry + dz * rz,
                 dx * ux + dy * uy + dz * uz,
                 dx * fx + dy * fy + dz * fz)
            vcache[vi] = c
            vert_frame[vi] = frame
            return c

        polys = []

        def emit_face_poly(fi):
            if face_frame[fi] == frame:
                return
            face_frame[fi] = frame
            nx, ny, nz, dist = face_plane[fi]
            if ox * nx + oy * ny + oz * nz - dist <= BACKFACE_EPS:
                return                                  # backface

            pts = [transform(vi) for vi in face_verts[fi]]

            # Sutherland-Hodgman clip against the near plane (cz >= NEAR)
            clipped = []
            n = len(pts)
            for i in range(n):
                ax, ay, az = pts[i]
                bx, by, bz = pts[(i + 1) % n]
                ain = az >= NEAR
                if ain:
                    clipped.append(pts[i])
                if ain != (bz >= NEAR):
                    t = (NEAR - az) / (bz - az)
                    clipped.append((ax + (bx - ax) * t,
                                    ay + (by - ay) * t, NEAR))
            if len(clipped) < 3:
                return

            flat = []
            for cx, cy, cz in clipped:
                flat.append(hw + cx * focal / cz)
                flat.append(hh - cy * focal / cz)
            polys.append((flat, face_color[fi]))

        face_centroid = self.face_centroid

        def emit_model(md):
            ff = md["firstface"]
            nf = md["numfaces"]
            if nf <= 1:
                emit_face_poly(ff)
                return
            # a brush model isn't convex/BSP-ordered, so sort its own faces
            # back-to-front by centroid depth before painting them
            order = []
            for fi in range(ff, ff + nf):
                cx, cy, cz = face_centroid[fi]
                d = (cx - ox) * fx + (cy - oy) * fy + (cz - oz) * fz
                order.append((d, fi))
            order.sort(reverse=True)        # far first
            for _, fi in order:
                emit_face_poly(fi)

        def emit_models(mlist):
            if len(mlist) > 1:              # several at one depth -> sort by centre
                def keyf(md):
                    mn, mx = md["mins"], md["maxs"]
                    cx = (mn[0] + mx[0]) * 0.5
                    cy = (mn[1] + mx[1]) * 0.5
                    cz = (mn[2] + mx[2]) * 0.5
                    return -((cx - ox) * fx + (cy - oy) * fy + (cz - oz) * fz)
                mlist = sorted(mlist, key=keyf)
            for md in mlist:
                emit_model(md)

        def box_side(mins, maxs, nx, ny, nz, dist):
            # +1 box fully in front of plane, -1 fully behind, 0 straddles
            pmin = (nx * (mins[0] if nx >= 0 else maxs[0]) +
                    ny * (mins[1] if ny >= 0 else maxs[1]) +
                    nz * (mins[2] if nz >= 0 else maxs[2])) - dist
            if pmin >= 0:
                return 1
            pmax = (nx * (maxs[0] if nx >= 0 else mins[0]) +
                    ny * (maxs[1] if ny >= 0 else mins[1]) +
                    nz * (maxs[2] if nz >= 0 else mins[2])) - dist
            return -1 if pmax <= 0 else 0

        # collect the visible brush submodels (doors, lifts, buttons, platforms)
        pending = []
        if self.brushmodels:
            for md in bsp.models[1:]:
                if self.box_in_pvs(md["mins"], md["maxs"], vis):
                    pending.append(md)

        # walk the world BSP far-child-first -> back-to-front, weaving each brush
        # model in at its own depth (partitioned by every split plane it crosses)
        def walk(num, models):
            if num < 0:
                li = -num - 1
                if li > 0:
                    bit = li - 1
                    if vis[bit >> 3] & (1 << (bit & 7)):
                        _, _, fm, nm = leafs[li]
                        for m in range(fm, fm + nm):
                            emit_face_poly(marks[m])
                emit_models(models)
                return
            planenum, children, _, _ = nodes[num]
            (nx, ny, nz), dist, _ = planes[planenum]
            if models:
                front, back, on = [], [], []
                for md in models:
                    s = box_side(md["mins"], md["maxs"], nx, ny, nz, dist)
                    (front if s > 0 else back if s < 0 else on).append(md)
            else:
                front = back = on = ()
            if ox * nx + oy * ny + oz * nz - dist >= 0:   # camera in front
                walk(children[1], back)                   # far = back side
                emit_models(on)
                walk(children[0], front)                  # near = front side
            else:
                walk(children[0], front)                  # far = front side
                emit_models(on)
                walk(children[1], back)

        walk(self.headnode, pending)
        return polys, leaf
