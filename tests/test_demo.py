"""Demo file framing tests (quake/demo.py) against WinQuake cl_demo.c and the
real shareware demo1.dem. Run muted: PQ_AUDIO=0 python tests/test_demo.py."""
import _bootstrap  # noqa: F401
import struct
from quake.demo import DemoReader, DemoWriter, write_demo_frame
from quake.pak import Pak

PAK = "quake-shareware/id1/pak0.pak"


def test_synthetic_frame_roundtrip():
    body = bytes(range(20))
    blob = b"0\n" + struct.pack("<i", len(body)) + struct.pack("<3f", 1.0, 2.0, 3.0) + body
    r = DemoReader(blob)
    assert r.cdtrack == "0"
    ang, msg = r.next_frame()
    assert msg == body
    assert abs(ang[0] - 1.0) < 1e-6 and abs(ang[2] - 3.0) < 1e-6
    assert r.next_frame() is None          # EOF


def test_real_demo1_header_and_first_frame():
    blob = Pak(PAK).read("demo1.dem")
    r = DemoReader(blob)
    assert r.cdtrack == "2"                 # demo1.dem CD track
    ang, msg = r.next_frame()
    assert len(msg) > 1000                  # the big signon message
    assert msg[0] == 11                     # svc_serverinfo is the first byte


def test_write_demo_frame_matches_reader():
    out = bytearray(b"3\n")
    out += write_demo_frame((0.0, 90.0, 0.0), b"\x01\x02\x03")
    r = DemoReader(bytes(out))
    assert r.cdtrack == "3"
    ang, msg = r.next_frame()
    assert msg == b"\x01\x02\x03" and abs(ang[1] - 90.0) < 1e-6


def test_demowriter_roundtrips_through_reader(tmp_path=None):
    import io
    buf = io.BytesIO()
    w = DemoWriter(buf, cdtrack="3")
    w.write_frame((0.0, 90.0, 0.0), b"\x07\x01")
    w.write_frame((1.0, 2.0, 3.0), b"\x08hello\x00")
    blob = buf.getvalue()                   # snapshot before close() closes buf
    w.close()
    r = DemoReader(blob)
    assert r.cdtrack == "3"
    a0, m0 = r.next_frame()
    assert m0 == b"\x07\x01" and abs(a0[1] - 90.0) < 1e-6
    a1, m1 = r.next_frame()
    assert m1 == b"\x08hello\x00" and abs(a1[2] - 3.0) < 1e-6
    assert r.next_frame() is None


if __name__ == "__main__":
    test_synthetic_frame_roundtrip()
    test_real_demo1_header_and_first_frame()
    test_write_demo_frame_matches_reader()
    test_demowriter_roundtrips_through_reader()
    print("OK")
