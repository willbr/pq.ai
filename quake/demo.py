"""Quake .dem demo file framing -- the read/write halves of WinQuake cl_demo.c.
A demo is an ASCII CD-track number terminated by '\\n', then repeated frames:
  [int32 LE message length][3 x float32 LE viewangles][length bytes of svc_* msg]
The message bytes are exactly what quake.cl_parse.ClientState.parse_message
consumes; DemoReader yields them one frame at a time. write_demo_frame is the
recording counterpart (used in Phase 3). Pure stdlib."""
import struct


class DemoReader:
    """Reads a .dem blob frame by frame. `cdtrack` is the header track string;
    next_frame() returns (viewangles, message_bytes) or None at end."""

    def __init__(self, data):
        self.data = data
        nl = data.find(b"\n")
        if nl < 0:
            raise ValueError("demo: no CD-track header line")
        self.cdtrack = data[:nl].decode("latin-1").strip()
        self.pos = nl + 1

    def next_frame(self):
        d = self.data
        if self.pos + 16 > len(d):
            return None                        # no room for len+angles header
        (length,) = struct.unpack_from("<i", d, self.pos)
        angles = struct.unpack_from("<3f", d, self.pos + 4)
        start = self.pos + 16
        end = start + length
        if end > len(d):
            return None                        # truncated final frame
        self.pos = end
        return angles, d[start:end]


def write_demo_frame(viewangles, message):
    """Frame a single message for a .dem file (CL_WriteDemoMessage): the length,
    the 3 viewangles, then the message bytes. Returns the bytes to append."""
    out = struct.pack("<i", len(message))
    out += struct.pack("<3f", viewangles[0], viewangles[1], viewangles[2])
    return out + bytes(message)


if __name__ == "__main__":                     # python -m quake.demo
    blob = b"1\n" + write_demo_frame((0.0, 0.0, 0.0), b"\x07")
    r = DemoReader(blob)
    assert r.cdtrack == "1" and r.next_frame()[1] == b"\x07"
    print("quake.demo OK")
