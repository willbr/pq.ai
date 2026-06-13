"""Unit tests for quake/conchars.py: the conchars bitmap-font blitter and the
qpic/console-background/fade helpers that the zbuf UI compositing uses. Uses
synthetic lumps so it needs no shareware data."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.conchars import ConFont, load_qpic, blit_conback, fade_region


def _conchars_with(glyphs):
    """128x128 lump with each (char_num -> fill_index) glyph cell filled."""
    src = bytearray(128 * 128)
    for num, val in glyphs.items():
        sy, sx = (num >> 4) * 8, (num & 15) * 8
        for r in range(8):
            for i in range(8):
                src[(sy + r) * 128 + sx + i] = val
    return bytes(src)


def test_char_blits_8x8_glyph_at_offset():
    cf = ConFont(_conchars_with({65: 7}))   # 'A'
    fb = bytearray(16 * 16)
    cf.char(fb, 16, 2, 3, 65)
    assert fb[3 * 16 + 2] == 7              # top-left of the glyph
    assert fb[(3 + 7) * 16 + (2 + 7)] == 7  # bottom-right of the glyph
    assert fb[0] == 0                       # outside the glyph: untouched


def test_char_index_zero_is_transparent():
    cf = ConFont(_conchars_with({65: 0}))   # all-zero glyph
    fb = bytearray(16 * 16)
    for i in range(len(fb)):
        fb[i] = 5
    cf.char(fb, 16, 0, 0, 65)
    assert all(b == 5 for b in fb)          # nothing overwritten


def test_text_advances_8px_per_char():
    cf = ConFont(_conchars_with({ord('X'): 9}))
    fb = bytearray(80 * 8)
    cf.text(fb, 80, 0, 0, "XX")
    assert fb[0] == 9 and fb[8] == 9        # two glyphs, 8px apart
    assert fb[16] == 0


def test_text_centered_offsets_by_half_width():
    cf = ConFont(_conchars_with({ord('X'): 9}))
    fb = bytearray(80 * 8)
    cf.text_centered(fb, 80, 40, 0, "XX")   # 2 chars -> start at 40 - 8 = 32
    assert fb[32] == 9
    assert fb[31] == 0


def test_load_qpic_parses_header():
    lump = bytes([3, 0, 0, 0, 2, 0, 0, 0]) + bytes(range(6))  # 3x2
    w, h, px = load_qpic(lump)
    assert (w, h) == (3, 2)
    assert px == bytes(range(6))


def test_blit_conback_fills_top_rows_only():
    pic = (2, 2, bytes([1, 1, 1, 1]))       # solid 2x2 of index 1
    fb = bytearray(4 * 4)
    blit_conback(fb, 4, 4, pic, 2)          # only top 2 rows
    assert all(fb[y * 4 + x] for y in range(2) for x in range(4))
    assert all(fb[y * 4 + x] == 0 for y in range(2, 4) for x in range(4))


def test_fade_region_dithers_to_black():
    fb = bytearray([5] * (4 * 4))
    fade_region(fb, 4, 0, 0, 4, 4)
    # checkerboard: (x ^ y) & 1 cleared to 0, the rest left at 5
    assert fb[0 * 4 + 1] == 0 and fb[1 * 4 + 0] == 0
    assert fb[0 * 4 + 0] == 5 and fb[1 * 4 + 1] == 5


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")
