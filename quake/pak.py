"""Quake PAK archive reader. Pure stdlib.

PAK layout (little-endian):
  header: char id[4] = "PACK"; int32 dirofs; int32 dirlen
  directory at dirofs: dirlen/64 entries, each 64 bytes:
      char name[56]; int32 filepos; int32 filelen
"""

import struct

HEADER = struct.Struct("<4sii")        # id, dirofs, dirlen
ENTRY = struct.Struct("<56sii")        # name, filepos, filelen


class Pak:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        magic, dirofs, dirlen = HEADER.unpack_from(self.data, 0)
        if magic != b"PACK":
            raise ValueError(f"{path}: not a PAK file (magic {magic!r})")
        self.files = {}                 # name -> (filepos, filelen)
        for i in range(dirlen // ENTRY.size):
            name, pos, length = ENTRY.unpack_from(self.data, dirofs + i * ENTRY.size)
            name = name.split(b"\0", 1)[0].decode("ascii", "replace")
            self.files[name] = (pos, length)

    def read(self, name):
        pos, length = self.files[name]
        return self.data[pos:pos + length]

    def names(self):
        return sorted(self.files)


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "quake-shareware/id1/pak0.pak"
    pak = Pak(path)
    names = pak.names()
    print(f"{path}: {len(names)} files")
    maps = [n for n in names if n.startswith("maps/") and n.endswith(".bsp")]
    print(f"\nmaps ({len(maps)}):")
    for m in maps:
        print(f"  {m:24} {pak.files[m][1]:>9} bytes")
