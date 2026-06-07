"""Quake BSP (version 29) reader. Pure stdlib.

Header: int32 version; lump_t lumps[15], lump_t = {int32 fileofs, int32 filelen}.
We decode only what a wireframe walker needs into flat arrays of tuples.
All little-endian.
"""

import struct

BSPVERSION = 29

# lump indices
ENTITIES, PLANES, TEXTURES, VERTEXES, VISIBILITY, NODES, TEXINFO, FACES, \
    LIGHTING, CLIPNODES, LEAFS, MARKSURFACES, EDGES, SURFEDGES, MODELS = range(15)

CONTENTS_SOLID = -2

_S_VERTEX = struct.Struct("<3f")        # x y z
_S_EDGE = struct.Struct("<2H")          # v0 v1
_S_SURFEDGE = struct.Struct("<i")       # signed edge index
_S_FACE = struct.Struct("<hhihh4Bi")    # plane side firstedge numedges texinfo styles[4] lightofs
_S_PLANE = struct.Struct("<4fi")        # nx ny nz dist type
_S_NODE = struct.Struct("<i8hHH")       # planenum child[2] mins[3] maxs[3] firstface numfaces
_S_LEAF = struct.Struct("<ii6hHH4B")    # contents visofs mins[3] maxs[3] firstmark nummark ambient[4]
_S_TEXINFO = struct.Struct("<8fii")     # vecs[2][4] miptex flags
_S_MIPTEX = struct.Struct("<16sII4I")   # name width height offsets[4]
_S_MARK = struct.Struct("<H")
_S_MODEL = struct.Struct("<9f7i")       # mins[3] maxs[3] origin[3] headnode[4] visleafs firstface numfaces
_S_CLIPNODE = struct.Struct("<i2h")     # planenum child[2]


class Bsp:
    def __init__(self, data):
        version, = struct.unpack_from("<i", data, 0)
        if version != BSPVERSION:
            raise ValueError(f"BSP version {version}, expected {BSPVERSION}")
        # 15 lumps each (fileofs, filelen) right after the version int
        lumps = [struct.unpack_from("<ii", data, 4 + i * 8) for i in range(15)]

        def lump(i):
            ofs, length = lumps[i]
            return data[ofs:ofs + length]

        # --- geometry ---
        self.vertexes = [v for v in _S_VERTEX.iter_unpack(lump(VERTEXES))]
        self.edges = [e for e in _S_EDGE.iter_unpack(lump(EDGES))]
        self.surfedges = [s[0] for s in _S_SURFEDGE.iter_unpack(lump(SURFEDGES))]

        # faces: keep (planenum, side, firstedge, numedges, texinfo)
        self.faces = [(f[0], f[1], f[2], f[3], f[4])
                      for f in _S_FACE.iter_unpack(lump(FACES))]

        # texinfo: keep (miptex index, flags); the s/t vectors aren't needed here
        self.texinfo = [(t[8], t[9])
                        for t in _S_TEXINFO.iter_unpack(lump(TEXINFO))]

        # textures: decode each embedded miptex to (name, w, h, mip0 index bytes).
        # The lump is: int nummiptex; int dataofs[nummiptex]; then miptex_t blobs.
        self.textures = self._load_textures(lump(TEXTURES))

        # planes: (normal(3), dist, type)
        self.planes = [((p[0], p[1], p[2]), p[3], p[4])
                       for p in _S_PLANE.iter_unpack(lump(PLANES))]

        # --- BSP tree + visibility ---
        # node: (planenum, (child0,child1), firstface, numfaces)
        self.nodes = [(n[0], (n[1], n[2]), n[9], n[10])
                      for n in _S_NODE.iter_unpack(lump(NODES))]
        # leaf: (contents, visofs, firstmark, nummark)
        self.leafs = [(l[0], l[1], l[8], l[9])
                      for l in _S_LEAF.iter_unpack(lump(LEAFS))]
        self.marksurfaces = [m[0] for m in _S_MARK.iter_unpack(lump(MARKSURFACES))]
        self.visdata = lump(VISIBILITY)

        # models: model 0 is the world. keep headnode[0] + face range + bounds.
        self.models = []
        for m in _S_MODEL.iter_unpack(lump(MODELS)):
            self.models.append({
                "mins": m[0:3], "maxs": m[3:6], "origin": m[6:9],
                "headnode": m[9],              # hull 0 (visual BSP nodes)
                "headnodes": (m[9], m[10], m[11], m[12]),  # all 4 hulls
                "firstface": m[14], "numfaces": m[15],
            })

        # clipnodes (for collision later): (planenum, (child0, child1))
        self.clipnodes = [(c[0], (c[1], c[2]))
                          for c in _S_CLIPNODE.iter_unpack(lump(CLIPNODES))]

        self.entities = lump(ENTITIES).split(b"\0", 1)[0].decode("latin-1")

    # ---- helpers ----
    def _load_textures(self, tex):
        """Decode the TEXTURES lump -> list of (name, w, h, mip0_indices) | None.
        Layout: int nummiptex; int dataofs[nummiptex]; then miptex_t blobs."""
        out = []
        if len(tex) < 4:
            return out
        nummip, = struct.unpack_from("<i", tex, 0)
        for i in range(nummip):
            ofs, = struct.unpack_from("<i", tex, 4 + i * 4)
            if ofs < 0 or ofs + _S_MIPTEX.size > len(tex):
                out.append(None)
                continue
            name, w, h, o0, o1, o2, o3 = _S_MIPTEX.unpack_from(tex, ofs)
            name = name.split(b"\0", 1)[0].decode("latin-1")
            px = None
            if o0 and ofs + o0 + w * h <= len(tex):
                px = tex[ofs + o0: ofs + o0 + w * h]   # mip level 0, 8-bit indices
            out.append((name, w, h, px))
        return out

    def face_vertices(self, face_index):
        """Ordered vertex indices around a face (following surfedge winding)."""
        firstedge, numedges = self.faces[face_index][2], self.faces[face_index][3]
        out = []
        for i in range(firstedge, firstedge + numedges):
            se = self.surfedges[i]
            if se >= 0:
                out.append(self.edges[se][0])
            else:
                out.append(self.edges[-se][1])
        return out

    def num_visleafs(self):
        return len(self.leafs) - 1   # leaf 0 is the solid leaf

    def find_spawn(self):
        """Parse the entity string for info_player_start -> (origin, yaw)."""
        origin, yaw = (0.0, 0.0, 0.0), 0.0
        cur = {}
        for line in self.entities.splitlines():
            line = line.strip()
            if line.startswith("{"):
                cur = {}
            elif line.startswith("}"):
                if cur.get("classname") == "info_player_start":
                    o = cur.get("origin", "0 0 0").split()
                    origin = (float(o[0]), float(o[1]), float(o[2]))
                    yaw = float(cur.get("angle", "0"))
                    break
            elif line.startswith('"'):
                parts = line.split('"')
                if len(parts) >= 5:
                    cur[parts[1]] = parts[3]
        return origin, yaw


if __name__ == "__main__":
    import sys
    from pak import Pak
    pak = Pak("quake-shareware/id1/pak0.pak")
    name = sys.argv[1] if len(sys.argv) > 1 else "maps/e1m1.bsp"
    b = Bsp(pak.read(name))
    print(f"{name}:")
    print(f"  vertexes     {len(b.vertexes)}")
    print(f"  edges        {len(b.edges)}")
    print(f"  surfedges    {len(b.surfedges)}")
    print(f"  faces        {len(b.faces)}")
    print(f"  planes       {len(b.planes)}")
    print(f"  nodes        {len(b.nodes)}")
    print(f"  leafs        {len(b.leafs)} ({b.num_visleafs()} vis)")
    print(f"  marksurfaces {len(b.marksurfaces)}")
    print(f"  clipnodes    {len(b.clipnodes)}")
    print(f"  models       {len(b.models)}")
    print(f"  visdata      {len(b.visdata)} bytes")
    xs = [v[0] for v in b.vertexes]
    ys = [v[1] for v in b.vertexes]
    zs = [v[2] for v in b.vertexes]
    print(f"  bounds       x[{min(xs):.0f},{max(xs):.0f}] "
          f"y[{min(ys):.0f},{max(ys):.0f}] z[{min(zs):.0f},{max(zs):.0f}]")
    print(f"  spawn        {b.find_spawn()}")
