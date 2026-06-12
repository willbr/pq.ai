"""macOS Cocoa UI helpers (outside the `quake` engine package): the pure,
unit-testable half of the Cocoa frontend, the twin of win_ui.py.

Pure helpers (no PyObjC needed, tested in tests/test_mac_ui.py):
  KEYCODE_NAMES        macOS ANSI virtual keycode -> key name ('w', 'space', ...)
  pal_channel_tables   palette -> three 256-byte translate tables (from main.py)
  expand_fb_rgba       8-bit indexed framebuffer -> packed RGBA via bytes.translate
  fit_particles        remap window-space particles into a letterbox rect
                       (pure port of win_ui.GdiBlitter._fit_particles)

CG drawing helpers (need PyObjC + a CGContext; verified by running the game,
the win_ui pattern) live below the marker line.
"""

# ---- macOS virtual keycodes (Carbon kVK_ANSI_*, layout-independent) ---------
KEYCODE_NAMES = {
    0x00: "a", 0x01: "s", 0x02: "d", 0x03: "f", 0x04: "h", 0x05: "g",
    0x06: "z", 0x07: "x", 0x08: "c", 0x09: "v", 0x0B: "b", 0x0C: "q",
    0x0D: "w", 0x0E: "e", 0x0F: "r", 0x10: "y", 0x11: "t",
    0x12: "1", 0x13: "2", 0x14: "3", 0x15: "4", 0x16: "6", 0x17: "5",
    0x19: "9", 0x1A: "7", 0x1C: "8", 0x1D: "0",
    0x1F: "o", 0x20: "u", 0x22: "i", 0x23: "p", 0x25: "l", 0x26: "j",
    0x28: "k", 0x2D: "n", 0x2E: "m",
    0x30: "tab", 0x31: "space", 0x32: "grave", 0x33: "backspace",
    0x35: "escape", 0x24: "return", 0x4C: "kp_enter", 0x75: "delete",
    0x73: "home", 0x77: "end", 0x74: "pageup", 0x79: "pagedown",
    0x7A: "f1", 0x7B: "left", 0x7C: "right", 0x7D: "down", 0x7E: "up",
}


def pal_channel_tables(pal):
    """Split a 256-entry (r,g,b) palette into three 256-byte translate tables
    (R, G, B), padded to 256 so bytes.translate always has a full table.
    Same helper as main.py's (duplicated so the Cocoa frontend never imports
    the tkinter module)."""
    r = bytearray(256); g = bytearray(256); b = bytearray(256)
    for i, c in enumerate(pal[:256]):
        r[i], g[i], b[i] = c[0], c[1], c[2]
    return bytes(r), bytes(g), bytes(b)


def expand_fb_rgba(fb, w, h, pal_r, pal_g, pal_b):
    """Expand an 8-bit palette-indexed framebuffer to packed RGBA. The alpha
    byte is ignored by kCGImageAlphaNoneSkipLast but written as 255 anyway.
    Three C-level bytes.translate passes interleaved by strided slice
    assignment, as in main.py's expand_fb_to_ppm -- no per-pixel Python."""
    n = w * h
    buf = bytearray(4 * n)
    buf[0::4] = fb.translate(pal_r)
    buf[1::4] = fb.translate(pal_g)
    buf[2::4] = fb.translate(pal_b)
    buf[3::4] = b"\xff" * n
    return bytes(buf)


def fit_particles(particles, ox, oy, ow, oh, dst_w, dst_h):
    """Remap window-space particle sprites into the letterbox image rect so the
    sprites stay aligned with the (smaller) world image. Pure port of
    win_ui.GdiBlitter._fit_particles."""
    if not particles:
        return particles
    sx, sy = ow / dst_w, oh / dst_h
    s = min(sx, sy)
    return [(ox + x * sx, oy + y * sy, max(1.0, half * s), rgb)
            for (x, y, half, rgb) in particles]
