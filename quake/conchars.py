"""Conchars bitmap font + qpic helpers for compositing UI text into the 8-bit
indexed framebuffer (the same surface the status bar draws into). Ports
draw.c's Draw_Character / Draw_String / Draw_ConsoleBackground / Draw_FadeScreen
and screen.c's SCR_DrawCenterString line centering.

Pure: no OS, UI, or engine imports. The conchars lump is a 128x128 raw 8-bit
image (a 16x16 grid of 8x8 glyphs); glyph index 0 is transparent. Quake .lmp
qpics are an 8-byte width/height header followed by width*height palette
indices.

NOTE: ConFont.char duplicates quake/sbar.py's Sbar._char (the same 8x8 blit).
They should be unified onto this module eventually; sbar.py is left untouched
for now to avoid disturbing its golden tests.
"""

import struct


class ConFont:
    """Draw_Character / Draw_String from a conchars lump."""

    def __init__(self, conchars):
        self.src = conchars                  # 128*128 bytes

    def char(self, fb, fbw, x, y, num):
        """Draw_Character: 8x8 glyph from the 16x16 grid; index 0 transparent."""
        src = self.src
        sy, sx = (num >> 4) * 8, (num & 15) * 8
        for r in range(8):
            s = (sy + r) * 128 + sx
            d = (y + r) * fbw + x
            for i in range(8):
                b = src[s + i]
                if b:
                    fb[d + i] = b

    def text(self, fb, fbw, x, y, s):
        """Draw_String: left-aligned, 8px advance. High bytes wrap into the
        gold/brown half of the conchars grid, exactly like Draw_Character."""
        for ch in s:
            self.char(fb, fbw, x, y, ord(ch) & 255)
            x += 8

    def text_centered(self, fb, fbw, cx, y, s):
        """One line centered on cx (SCR_DrawCenterString: x = cx - len*8/2)."""
        self.text(fb, fbw, cx - len(s) * 4, y, s)


def load_qpic(lump):
    """Parse a .lmp qpic -> (width, height, indices)."""
    w, h = struct.unpack_from("<ii", lump, 0)
    return (w, h, lump[8:8 + w * h])


def blit_conback(fb, fbw, fbh, pic, lines):
    """Draw_ConsoleBackground: stretch the conback pic across the framebuffer
    (nearest-neighbour) and paint only the top `lines` rows -- the console
    panel over the top of the scene."""
    pw, ph, px = pic
    for dy in range(min(lines, fbh)):
        sy = (dy * ph // fbh) * pw
        d = dy * fbw
        for dx in range(fbw):
            fb[d + dx] = px[sy + dx * pw // fbw]


def fade_region(fb, fbw, x0, y0, x1, y1):
    """Draw_FadeScreen: a checkerboard of black (palette index 0) over the
    region so the scene shows through dimmed, no blend table needed."""
    for y in range(y0, y1):
        base = y * fbw
        for x in range(x0, x1):
            if (x ^ y) & 1:
                fb[base + x] = 0
