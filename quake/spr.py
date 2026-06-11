""".spr sprite model parser (spritegn.h; rendered like r_sprite.c R_DrawSprite).

A sprite is a sequence of palette-indexed images billboarded at an entity's
origin -- explosions (progs/s_explod.spr, set by QC's BecomeExplosion),
drowning bubbles (s_bubble.spr), torch flames. Index 255 is transparent.

Layout: dsprite_t header (ident "IDSP", version 1, type, boundingradius,
maxwidth, maxheight, numframes, beamlength, synctype), then per frame a
spriteframetype int: SPR_SINGLE (0) is dspriteframe_t {origin[2], width,
height} + width*height bytes; SPR_GROUP is a count, that many float
intervals, then that many dspriteframe_t. Groups are flattened to their
first image here -- id1's sprites are all singles.

Spr.frame(i) -> (origin_x, origin_y, width, height, pixels): origin is the
top-left offset from the entity origin in sprite space (x right, y up), so a
centred frame has origin_x = -width/2, origin_y = +height/2.
"""

import struct

_S_HEADER = struct.Struct("<4siifiiifi")
_S_FRAME = struct.Struct("<4i")          # origin[2] width height

SPR_SINGLE = 0


class Spr:
    def __init__(self, data):
        (ident, version, self.type, self.boundingradius, self.maxwidth,
         self.maxheight, self.numframes, self.beamlength,
         self.synctype) = _S_HEADER.unpack_from(data, 0)
        if ident != b"IDSP" or version != 1:
            raise ValueError(f"not a v1 IDSP sprite ({ident!r} v{version})")
        self.frames = []
        ofs = _S_HEADER.size
        for _ in range(self.numframes):
            frametype, = struct.unpack_from("<i", data, ofs)
            ofs += 4
            if frametype == SPR_SINGLE:
                ofs = self._read_frame(data, ofs)
            else:                        # group: intervals then sub-frames
                count, = struct.unpack_from("<i", data, ofs)
                ofs += 4 + 4 * count     # skip the float intervals
                first = len(self.frames)
                for _ in range(count):
                    ofs = self._read_frame(data, ofs)
                # flatten: a group counts as one frame (its first image)
                del self.frames[first + 1:]

    def _read_frame(self, data, ofs):
        ox, oy, w, h = _S_FRAME.unpack_from(data, ofs)
        ofs += _S_FRAME.size
        self.frames.append((ox, oy, w, h, bytes(data[ofs:ofs + w * h])))
        return ofs + w * h

    def frame(self, i):
        """(origin_x, origin_y, width, height, pixels) for frame i, clamped."""
        if not self.frames:
            raise ValueError("sprite has no frames")
        return self.frames[max(0, min(len(self.frames) - 1, int(i)))]


if __name__ == "__main__":
    import sys
    from .pak import Pak
    pak = Pak(sys.argv[1] if len(sys.argv) > 1 else
              "quake-shareware/id1/pak0.pak")
    for name in sorted(f for f in pak.files if f.endswith(".spr")):
        s = Spr(pak.read(name))
        ox, oy, w, h, _ = s.frame(0)
        print(f"{name}: type {s.type}, {s.numframes} frames, "
              f"{w}x{h} @ ({ox},{oy})")
