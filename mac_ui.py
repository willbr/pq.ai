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


def letterbox_rect(src_w, src_h, dst_w, dst_h):
    """Largest (ox, oy, w, h) rect inside the dst window that preserves the src
    framebuffer's aspect ratio, centered, with the leftover margin left for
    black bars. Port of win_ui.letterbox_rect (win_ui itself does not import on
    macOS: its module-level WNDPROC needs ctypes.WINFUNCTYPE)."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return (0, 0, max(0, dst_w), max(0, dst_h))
    scale = min(dst_w / src_w, dst_h / src_h)
    out_w = max(1, round(src_w * scale))
    out_h = max(1, round(src_h * scale))
    return ((dst_w - out_w) // 2, (dst_h - out_h) // 2, out_w, out_h)


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


# ============================================================================
#  Live CG drawing below: needs PyObjC and a CGContext (drawRect:), so it is
#  verified by running the game, not by unit tests -- the win_ui convention.
# ============================================================================

import Quartz
import AppKit

WIRE_RGB = (0, 255, 102)            # "#00ff66", matching main.py / win_ui
HUD_FONT_NAME = "Menlo"             # carries the 1/8-block profiler bar glyphs
HUD_FONT_SIZE = 12.0

_RGB_CS = None                       # lazy singleton CGColorSpace


def _colorspace():
    global _RGB_CS
    if _RGB_CS is None:
        _RGB_CS = Quartz.CGColorSpaceCreateDeviceRGB()
    return _RGB_CS


def fb_cgimage(rgba, w, h):
    """Wrap a packed-RGBA byte buffer as a CGImage (the provider retains the
    bytes, so no further copy)."""
    provider = Quartz.CGDataProviderCreateWithCFData(rgba)
    return Quartz.CGImageCreate(
        w, h, 8, 32, 4 * w, _colorspace(),
        Quartz.kCGImageAlphaNoneSkipLast | Quartz.kCGBitmapByteOrderDefault,
        provider, None, False, Quartz.kCGRenderingIntentDefault)


def draw_fb(ctx, img, ox, oy, ow, oh, view_h):
    """Draw the framebuffer CGImage into the letterbox rect of a FLIPPED view's
    context, nearest-neighbour. CGContextDrawImage composes the image y-up, so
    in a flipped (y-down) context it would mirror vertically: unflip the CTM
    around the view for the draw, mapping the y-down rect into y-up space."""
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationNone)
    Quartz.CGContextTranslateCTM(ctx, 0, view_h)
    Quartz.CGContextScaleCTM(ctx, 1.0, -1.0)
    rect = Quartz.CGRectMake(ox, view_h - oy - oh, ow, oh)
    Quartz.CGContextDrawImage(ctx, rect, img)
    Quartz.CGContextRestoreGState(ctx)


def _set_fill(ctx, rgb):
    Quartz.CGContextSetRGBFillColor(ctx, rgb[0] / 255.0, rgb[1] / 255.0,
                                    rgb[2] / 255.0, 1.0)


def fill_rect(ctx, x, y, w, h, rgb):
    _set_fill(ctx, rgb)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(x, y, w, h))


def draw_segs(ctx, segs):
    """Stroke the wireframe segments (flat (x0, y0, x1, y1) tuples) in green in
    one CGContextStrokeLineSegments batch."""
    if not segs:
        return
    pts = []
    for x0, y0, x1, y1 in segs:
        pts.append((x0, y0))
        pts.append((x1, y1))
    Quartz.CGContextSetRGBStrokeColor(ctx, WIRE_RGB[0] / 255.0,
                                      WIRE_RGB[1] / 255.0, WIRE_RGB[2] / 255.0, 1.0)
    Quartz.CGContextSetLineWidth(ctx, 1.0)
    Quartz.CGContextStrokeLineSegments(ctx, pts, len(pts))


def _hex_to_rgb(color):
    """'#rrggbb' (as render_shaded emits) -> (r, g, b) ints."""
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


def _add_poly_path(ctx, flat):
    Quartz.CGContextBeginPath(ctx)
    Quartz.CGContextMoveToPoint(ctx, flat[0], flat[1])
    for i in range(1, len(flat) // 2):
        Quartz.CGContextAddLineToPoint(ctx, flat[2 * i], flat[2 * i + 1])
    Quartz.CGContextClosePath(ctx)


def draw_polys(ctx, polys):
    """Fill flat-shaded polygons back-to-front. Each poly is (flat, '#rrggbb')
    as render_shaded emits; no outlines (the Tk version used outline='')."""
    for flat, color in polys:
        if len(flat) < 6:
            continue
        _set_fill(ctx, _hex_to_rgb(color))
        _add_poly_path(ctx, flat)
        Quartz.CGContextFillPath(ctx)


def draw_wire_hidden(ctx, polys):
    """Hidden-line wireframe: black-filled, green-outlined polygons painted
    back-to-front (near faces occlude far ones). Mirrors win_ui's version; the
    per-poly fill colour is ignored."""
    Quartz.CGContextSetRGBFillColor(ctx, 0, 0, 0, 1.0)
    Quartz.CGContextSetRGBStrokeColor(ctx, WIRE_RGB[0] / 255.0,
                                      WIRE_RGB[1] / 255.0, WIRE_RGB[2] / 255.0, 1.0)
    Quartz.CGContextSetLineWidth(ctx, 1.0)
    for flat, _color in polys:
        if len(flat) < 6:
            continue
        _add_poly_path(ctx, flat)
        Quartz.CGContextDrawPath(ctx, Quartz.kCGPathFillStroke)


def draw_particles(ctx, particles):
    """Fill each particle (x, y, half, (r,g,b)) as a small square."""
    for x, y, half, rgb in particles:
        fill_rect(ctx, x - half, y - half, 2 * half, 2 * half, rgb)


# ---- text (AppKit string drawing: flipped-view aware, Menlo) ----------------

_FONT = None
_CELL = None                         # (char_width, line_height) of '0'


def _font():
    global _FONT, _CELL
    if _FONT is None:
        _FONT = AppKit.NSFont.fontWithName_size_(HUD_FONT_NAME, HUD_FONT_SIZE) \
            or AppKit.NSFont.userFixedPitchFontOfSize_(HUD_FONT_SIZE)
        size = AppKit.NSString.stringWithString_("0").sizeWithAttributes_(
            {AppKit.NSFontAttributeName: _FONT})
        _CELL = (size.width, size.height)
    return _FONT


def cell_metrics():
    """(char_width, line_height) of the monospace HUD font."""
    _font()
    return _CELL


def _attrs(rgb):
    return {AppKit.NSFontAttributeName: _font(),
            AppKit.NSForegroundColorAttributeName:
                AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                    rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, 1.0)}


def draw_string(ctx, x, y, s, rgb):
    """Draw one line of text with its top-left at (x, y). drawAtPoint: uses the
    current NSGraphicsContext, which inside drawRect: is the same CGContext and
    flipped-aware; `ctx` is accepted for signature symmetry."""
    AppKit.NSString.stringWithString_(s).drawAtPoint_withAttributes_(
        (x, y), _attrs(rgb))


def draw_texts(ctx, texts):
    """Draw the HUD/overlay text list: (x, y, string, (r,g,b), anchor) with
    anchor 'nw' (top-left), 'sw' (bottom-left) or 'center'. Multi-line via
    embedded newlines, mirroring win_ui._text_block."""
    cw, lh = cell_metrics()
    for x, y, s, rgb, anchor in texts:
        lines = s.split("\n")
        top = y if anchor == "nw" else (y - lh * len(lines) / 2 if anchor ==
                                        "center" else y - lh * len(lines))
        for i, line in enumerate(lines):
            lx = x - len(line) * cw / 2 if anchor == "center" else x
            draw_string(ctx, lx, top + i * lh, line, rgb)


def draw_console(ctx, lines, input_line, cursor_col, dst_w, dst_h):
    """Drop-down console panel across the top ~40% of the window: dark band,
    green bottom edge, scrollback bottom-aligned above the `] input` line, a
    caret bar at the cursor column. Layout mirrors win_ui.draw_console."""
    cw, lh = cell_metrics()
    panel_h = dst_h * 2 // 5
    fill_rect(ctx, 0, 0, dst_w, panel_h, (16, 16, 24))
    fill_rect(ctx, 0, panel_h - 1, dst_w, 1, (0, 160, 70))
    iy = panel_h - lh - 4
    y = iy - lh
    for line in reversed(lines):              # newest just above the input line
        if y < 4:
            break
        draw_string(ctx, 6, y, line, (200, 220, 200))
        y -= lh
    draw_string(ctx, 6, iy, input_line, (255, 255, 255))
    fill_rect(ctx, 6 + cursor_col * cw, iy, 1, lh, (255, 255, 255))


def draw_menu(ctx, view, dst_w, dst_h):
    """Escape overlay menu: centered dark panel, yellow title, one row per item,
    the selected row '> '-prefixed and brightened. view is
    (title, [(label, value, selected), ...]). Mirrors win_ui.draw_menu."""
    title, rows = view
    cw, lh = cell_metrics()
    panel_w = 360
    panel_h = (len(rows) + 2) * lh + 24
    x0 = (dst_w - panel_w) // 2
    y0 = (dst_h - panel_h) // 2
    fill_rect(ctx, x0, y0, panel_w, panel_h, (16, 16, 24))
    fill_rect(ctx, x0, y0 + panel_h - 1, panel_w, 1, (0, 160, 70))
    draw_string(ctx, x0 + 16, y0 + 12, title, (255, 255, 0))
    y = y0 + 12 + 2 * lh
    for label, value, selected in rows:
        text = label if not value else f"{label}: {value}"
        text = ("> " if selected else "  ") + text
        draw_string(ctx, x0 + 16, y, text,
                    (255, 255, 255) if selected else (160, 200, 160))
        y += lh
