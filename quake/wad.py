"""Quake WAD2 archive reader (gfx.wad: the 2D/HUD art). Pure stdlib.
Ports the lump directory of WinQuake's wad.c (W_LoadWadFile/W_GetLumpName).

WAD2 layout (little-endian):
  header: char id[4] = "WAD2"; int32 numlumps; int32 infotableofs
  directory at infotableofs: numlumps entries, each 32 bytes:
      int32 filepos, disksize, size; char type, compression, pad1, pad2;
      char name[16]
  qpic lump (type 0x42): int32 width, height; byte pixels[width*height]
      (palette indices; 255 is transparent by convention -- the drawer's
      business, not ours). Other types (0x44 CONCHARS) are raw bytes.

Lump names are stored upper-case but looked up case-insensitively, as
W_CleanupName does.
"""

import struct

HEADER = struct.Struct("<4sii")          # id, numlumps, infotableofs
ENTRY = struct.Struct("<iiibbbb16s")     # filepos, disksize, size, type,
                                         # compression, pad1, pad2, name
TYP_QPIC = 0x42


class Wad:
    def __init__(self, data):
        self.data = data
        magic, numlumps, ofs = HEADER.unpack_from(data, 0)
        if magic != b"WAD2":
            raise ValueError(f"not a WAD2 file (magic {magic!r})")
        self.lumps = {}                  # lowercase name -> (filepos, size, type)
        for i in range(numlumps):
            (pos, _disk, size, typ, _comp, _p1, _p2,
             name) = ENTRY.unpack_from(data, ofs + i * ENTRY.size)
            name = name.split(b"\0", 1)[0].decode("ascii", "replace").lower()
            self.lumps[name] = (pos, size, typ)

    def lump(self, name):
        """Raw lump bytes (e.g. CONCHARS, a headerless 128x128 font sheet)."""
        pos, size, _typ = self.lumps[name.lower()]
        return self.data[pos:pos + size]

    def qpic(self, name):
        """A type-0x42 picture lump as (width, height, pixels): pixels is
        width*height palette indices, row-major."""
        pos, _size, typ = self.lumps[name.lower()]
        if typ != TYP_QPIC:
            raise ValueError(f"{name}: lump type {typ:#x} is not a qpic")
        w, h = struct.unpack_from("<ii", self.data, pos)
        return w, h, self.data[pos + 8:pos + 8 + w * h]

    def names(self):
        return sorted(self.lumps)


if __name__ == "__main__":
    from .pak import Pak
    pak = Pak("quake-shareware/id1/pak0.pak")
    wad = Wad(pak.read("gfx.wad"))
    print(f"gfx.wad: {len(wad.lumps)} lumps")
    for n in wad.names():
        pos, size, typ = wad.lumps[n]
        dims = "%dx%d" % wad.qpic(n)[:2] if typ == TYP_QPIC else f"{size}B"
        print(f"  {n:16s} type={typ:#04x} {dims}")
