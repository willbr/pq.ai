"""Quake network message codec -- the MSG_Write*/MSG_Read* primitives from
WinQuake common.c:510-725, used by the server datagram builder (sv_send.py) and
the client parser (cl_parse.py). Little-endian throughout. Coord is a 1/8-unit
fixed-point short; angle is a 1/256-of-360 byte. Pure stdlib."""
import struct


class MsgWriter:
    """Accumulates protocol bytes into a bytearray (`data`). Mirrors the
    MSG_Write* family; no SizeBuf overflow model -- Python grows the buffer."""

    def __init__(self):
        self.data = bytearray()

    def byte(self, c):                       # MSG_WriteByte, common.c:523
        self.data.append(c & 0xff)

    def char(self, c):                       # MSG_WriteChar, common.c:510
        self.data.append(c & 0xff)           # stored as a byte; read sign-extends

    def short(self, c):                      # MSG_WriteShort, common.c:536
        self.data += struct.pack("<h", c)

    def long(self, c):                       # MSG_WriteLong, common.c:550
        self.data += struct.pack("<i", c)

    def float(self, f):                      # MSG_WriteFloat, common.c:561
        self.data += struct.pack("<f", f)

    def string(self, s):                     # MSG_WriteString, common.c:576
        if s:
            self.data += s.encode("latin-1", "replace")
        self.data.append(0)

    def coord(self, f):                      # MSG_WriteCoord, common.c:584
        self.short(int(f * 8))

    def angle(self, f):                      # MSG_WriteAngle, common.c:589
        # C truncates toward zero (not floor), which matters for negative
        # angles -- int(x/y) matches C integer division, // does not.
        self.byte(int(int(f) * 256 / 360) & 255)


class MsgReader:
    """Reads protocol bytes from a bytes buffer. Mirrors the MSG_Read* family.
    Raises EOFError past the end (the parser treats that as end-of-message,
    matching cl_parse.c's `cmd == -1` return)."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    @property
    def at_end(self):
        return self.pos >= len(self.data)

    def _take(self, n):
        if self.pos + n > len(self.data):
            raise EOFError("read past end of message")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def byte(self):                          # MSG_ReadByte, common.c:623
        return self._take(1)[0]

    def char(self):                          # MSG_ReadChar, common.c:607
        c = self._take(1)[0]
        return c - 256 if c >= 128 else c

    def short(self):                         # MSG_ReadShort, common.c:639
        return struct.unpack("<h", self._take(2))[0]

    def long(self):                          # MSG_ReadLong, common.c:657
        return struct.unpack("<i", self._take(4))[0]

    def float(self):                         # MSG_ReadFloat, common.c:677
        return struct.unpack("<f", self._take(4))[0]

    def string(self):                        # MSG_ReadString, common.c:697
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            end = len(self.data)
        s = self.data[self.pos:end].decode("latin-1")
        self.pos = end + 1
        return s

    def coord(self):                         # MSG_ReadCoord, common.c:717
        return self.short() * (1.0 / 8)

    def angle(self):                         # MSG_ReadAngle, common.c:722
        return self.char() * (360.0 / 256)


if __name__ == "__main__":                   # python -m quake.msg
    w = MsgWriter(); w.coord(8.0); w.angle(180.0)
    r = MsgReader(bytes(w.data))
    c = r.coord(); ang = r.angle()   # 180 -> byte 128 -> char -128 -> -180.0
    assert abs(c - 8.0) < 1e-6 and abs(ang + 180.0) < 1.5   # 180 -> byte 128 -> char -128 -> -180.0
    print("quake.msg OK")
