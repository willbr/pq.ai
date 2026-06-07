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
    def __init__(self, bsp):
        self.bsp = bsp
        self.headnode = bsp.models[0]["headnode"]
        self.width = 800
        self.height = 600
        self.fov = 90.0
        self.backface = True
        self._update_focal()

        nfaces = len(bsp.faces)
        nedges = len(bsp.edges)
        nverts = len(bsp.vertexes)

        # precompute per-face: ordered abs edge indices + outward plane (n, dist)
        self.face_edges = []
        self.face_plane = []
        for planenum, side, firstedge, numedges in bsp.faces:
            eidx = [abs(bsp.surfedges[firstedge + k]) for k in range(numedges)]
            self.face_edges.append(eidx)
            (nx, ny, nz), dist, _ = bsp.planes[planenum]
            if side:
                nx, ny, nz, dist = -nx, -ny, -nz, -dist
            self.face_plane.append((nx, ny, nz, dist))

        # per-frame staleness markers (avoid clearing big arrays every frame)
        self.frame = 0
        self.face_frame = [0] * nfaces
        self.edge_frame = [0] * nedges
        self.vert_frame = [0] * nverts
        self.vcache = [None] * nverts

        # vis decompression scratch
        self.vis_row = (len(bsp.leafs) + 7) >> 3

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
        for li in visible_leaves:
            _, _, firstmark, nummark = leafs[li]
            for m in range(firstmark, firstmark + nummark):
                fi = marks[m]
                if face_frame[fi] == frame:
                    continue
                face_frame[fi] = frame

                if backface:
                    nx, ny, nz, dist = face_plane[fi]
                    if ox * nx + oy * ny + oz * nz - dist <= BACKFACE_EPS:
                        continue

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
                        continue            # sub-pixel: not worth a Tk line draw

                    segments.append((x0, y0, x1, y1))

        return segments, leaf
