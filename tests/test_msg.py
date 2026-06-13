"""Codec parity tests for quake/msg.py against WinQuake common.c:510-725.
Run muted: PQ_AUDIO=0 python tests/test_msg.py  -> prints OK."""
import _bootstrap  # noqa: F401
from quake.msg import MsgWriter, MsgReader


def test_writer_primitive_bytes():
    w = MsgWriter()
    w.byte(0x12)
    w.short(-2)               # LE signed: ff ff... -2 -> fe ff
    w.long(0x04030201)
    assert bytes(w.data) == bytes([0x12, 0xfe, 0xff, 0x01, 0x02, 0x03, 0x04])


def test_writer_coord_and_angle():
    w = MsgWriter()
    w.coord(8.0)              # 8*8 = 64 -> short 64 -> 40 00
    w.angle(180.0)            # int(180)*256//360 = 128 -> 80
    assert bytes(w.data) == bytes([0x40, 0x00, 0x80])


def test_writer_string_nul_terminated():
    w = MsgWriter()
    w.string("hi")
    assert bytes(w.data) == b"hi\x00"


def test_reader_roundtrips_writer():
    w = MsgWriter()
    w.byte(200); w.char(-5); w.short(-1234); w.long(123456); w.float(1.5)
    w.string("quake"); w.coord(73.25); w.angle(270.0)
    r = MsgReader(bytes(w.data))
    assert r.byte() == 200
    assert r.char() == -5
    assert r.short() == -1234
    assert r.long() == 123456
    assert abs(r.float() - 1.5) < 1e-6
    assert r.string() == "quake"
    assert abs(r.coord() - 73.25) < 1e-6          # 73.25*8 = 586 exact
    ang = r.angle()
    assert abs(ang - 270.0) < 1.5 or abs(ang + 90.0) < 1.5  # 270 -> 192 -> -90 (sign-extend)
    assert r.at_end


def test_reader_past_end_raises():
    r = MsgReader(b"")
    try:
        r.byte()
    except EOFError:
        return
    raise AssertionError("expected EOFError past end")


if __name__ == "__main__":
    test_writer_primitive_bytes()
    test_writer_coord_and_angle()
    test_writer_string_nul_terminated()
    test_reader_roundtrips_writer()
    test_reader_past_end_raises()
    print("OK")
