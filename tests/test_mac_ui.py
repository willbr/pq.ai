"""Unit tests for mac_ui's pure helpers (no PyObjC, no window): the macOS
keycode->name table, the fb->RGBA palette expansion, and particle letterbox
fitting. The CG drawing helpers need a real CGContext and are exercised by
running the game (the win_ui pattern)."""

import _bootstrap  # noqa: F401  (repo root on sys.path, chdir to root)

from mac_ui import (KEYCODE_NAMES, pal_channel_tables, expand_fb_rgba,
                    fit_particles, letterbox_rect)


def test_keycode_names():
    # the keys the game binds must all be present, by ANSI virtual keycode
    assert KEYCODE_NAMES[0x0D] == "w"
    assert KEYCODE_NAMES[0x00] == "a"
    assert KEYCODE_NAMES[0x01] == "s"
    assert KEYCODE_NAMES[0x02] == "d"
    assert KEYCODE_NAMES[0x31] == "space"
    assert KEYCODE_NAMES[0x08] == "c"
    assert KEYCODE_NAMES[0x30] == "tab"
    assert KEYCODE_NAMES[0x35] == "escape"
    assert KEYCODE_NAMES[0x7A] == "f1"
    assert KEYCODE_NAMES[0x32] == "grave"
    # weapon digits 1..8
    for code, name in ((0x12, "1"), (0x13, "2"), (0x14, "3"), (0x15, "4"),
                       (0x17, "5"), (0x16, "6"), (0x1A, "7"), (0x1C, "8")):
        assert KEYCODE_NAMES[code] == name
    # console editing keys
    for code, name in ((0x24, "return"), (0x4C, "kp_enter"), (0x33, "backspace"),
                       (0x75, "delete"), (0x73, "home"), (0x77, "end"),
                       (0x74, "pageup"), (0x79, "pagedown"),
                       (0x7B, "left"), (0x7C, "right"), (0x7D, "down"), (0x7E, "up")):
        assert KEYCODE_NAMES[code] == name
    # command toggles
    for code, name in ((0x2D, "n"), (0x03, "f"), (0x06, "z"), (0x11, "t"), (0x23, "p")):
        assert KEYCODE_NAMES[code] == name
    # names are unique (no two keycodes alias one name)
    assert len(set(KEYCODE_NAMES.values())) == len(KEYCODE_NAMES)


def test_expand_fb_rgba():
    # 2x2 fb indexing a 3-colour palette; index 3 beyond the palette -> black
    pal = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    lr, lg, lb = pal_channel_tables(pal)
    fb = bytes([0, 1, 2, 3])
    out = expand_fb_rgba(fb, 2, 2, lr, lg, lb)
    assert len(out) == 16
    assert out[0:4] == bytes([255, 0, 0, 255])      # index 0 -> red, alpha 255
    assert out[4:8] == bytes([0, 255, 0, 255])      # index 1 -> green
    assert out[8:12] == bytes([0, 0, 255, 255])     # index 2 -> blue
    assert out[12:16] == bytes([0, 0, 0, 255])      # index 3 -> padded black


def test_fit_particles():
    # 200x100 image rect at (0, 50) inside a 200x200 window (letterboxed
    # vertically): y scales by 0.5 and offsets by 50, x is unchanged.
    parts = [(100.0, 100.0, 4.0, (255, 0, 0))]
    out = fit_particles(parts, 0, 50, 200, 100, 200, 200)
    (x, y, half, rgb), = out
    assert x == 100.0 and y == 100.0          # 50 + 100*0.5
    assert half == 2.0                        # scaled by min(1.0, 0.5), floor 1.0
    assert rgb == (255, 0, 0)
    assert fit_particles([], 0, 50, 200, 100, 200, 200) == []


def test_letterbox_rect():
    # port of win_ui.letterbox_rect: same cases as tests/test_win_ui.py's shape
    assert letterbox_rect(200, 150, 800, 600) == (0, 0, 800, 600)   # matching 4:3
    assert letterbox_rect(200, 100, 200, 200) == (0, 50, 200, 100)  # letterboxed
    assert letterbox_rect(100, 200, 200, 200) == (50, 0, 100, 200)  # pillarboxed
    assert letterbox_rect(0, 0, 800, 600) == (0, 0, 800, 600)       # degenerate


if __name__ == "__main__":
    test_keycode_names()
    test_expand_fb_rgba()
    test_fit_particles()
    test_letterbox_rect()
    print("OK")
