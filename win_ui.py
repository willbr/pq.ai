"""Windows GDI helpers (outside the `quake` engine package): GdiBlitter and the
raw-input ctypes structs/helpers used by win_gdi.py for its own WndProc.

  GdiBlitter   StretchDIBits a raw framebuffer straight to a window DC, doing the
               1/ZBUF_SCALE upscale in GDI (no PPM, no PhotoImage, no .zoom()).
               Also handles double-buffered vector (wireframe/flat) frames and GDI
               HUD text, so both render paths share one presenter.

  Raw-input    RAWINPUT / RAWINPUTHEADER / RAWMOUSE / RAWINPUTDEVICE structs plus
  structs &    the WNDPROC / LRESULT types and the WM_INPUT / RID_INPUT / RIDEV_*
  helpers      / HID_USAGE_* constants. win_gdi.py's own WndProc uses these to
               decode WM_INPUT packets for acceleration-free mouselook.

The pure, OS-independent helpers (bgr_swap, raw_mouse_delta, apply_left_button,
the RAWINPUT struct layout) are unit-tested in tests/test_win_ui.py. DLL loading is
deferred into GdiBlitter.__init__ so importing this module is side-effect free.
"""

import ctypes
from ctypes import wintypes

# ---- raw-input flags (winuser.h) --------------------------------------------
# RI_MOUSE / RAWMOUSE.usFlags: bit 0 distinguishes relative from absolute motion.
MOUSE_MOVE_RELATIVE = 0x00       # lLastX/Y are deltas (a normal mouse)
MOUSE_MOVE_ABSOLUTE = 0x01       # lLastX/Y are screen coords (touchpad / RDP)
# RAWMOUSE button transition flags (low word of the button union)
RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002


# ---- RAWINPUT structures (winuser.h), pinned by test_win_ui ------------------
class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD),
                ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE),
                ("wParam", wintypes.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    # ctypes inserts the 2-byte pad after usFlags to 4-align the button union;
    # the union itself we only need as a single ULONG (we read motion, not buttons).
    _fields_ = [("usFlags", wintypes.USHORT),
                ("ulButtons", wintypes.ULONG),
                ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG),
                ("lLastY", wintypes.LONG),
                ("ulExtraInformation", wintypes.ULONG)]


class RAWINPUT(ctypes.Structure):
    # The real RAWINPUT.data is a union of RAWMOUSE/RAWKEYBOARD/RAWHID; we only
    # ever ask for mouse input, so modelling just the mouse arm is correct and
    # keeps the struct the right size (header + the largest arm we use).
    _fields_ = [("header", RAWINPUTHEADER),
                ("mouse", RAWMOUSE)]


def bgr_swap(buf):
    """Swap the R and B channels of a packed 24bpp RGB buffer in place-of-a-copy,
    returning a bytearray in the B,G,R order a GDI BI_RGB DIB expects. Green is
    untouched; length is preserved. Strided slice assignment runs in C, so this
    is far cheaper than the PPM-encode + PhotoImage.zoom() it replaces."""
    out = bytearray(buf)
    out[0::3], out[2::3] = out[2::3], out[0::3]
    return out


def dib_stride(w):
    """Bytes per scanline GDI assumes for a 24bpp DIB of width `w`: the packed
    w*3 rounded up to a 4-byte (DWORD) boundary. Feeding a tighter stride shears
    every row diagonally."""
    return ((w * 3 + 3) // 4) * 4


def to_dib_bgr(rgb, w, h):
    """Turn the renderer's packed 24bpp RGB framebuffer into the B,G,R,
    DWORD-row-aligned byte layout a top-down BI_RGB DIB expects. Fast path when
    rows are already aligned (w*3 % 4 == 0, e.g. zw=200): a single in-C channel
    swap. Otherwise pad each row -- a short per-row loop at 1/4 resolution."""
    stride = dib_stride(w)
    if stride == w * 3:                       # already aligned: no per-row padding
        return bgr_swap(rgb)
    out = bytearray(stride * h)
    for y in range(h):
        row = bgr_swap(rgb[y * w * 3:(y + 1) * w * 3])
        out[y * stride:y * stride + len(row)] = row
    return out


def fb8_to_dib(fb, w, h):
    """Pad the renderer's 8-bit palette-indexed framebuffer rows out to the
    DWORD-aligned stride a top-down 8bpp DIB expects. No channel work at all
    -- the palette rides in the BITMAPINFO colour table, so an aligned width
    needs nothing but a writable copy."""
    stride = (w + 3) & ~3
    if stride == w:
        return bytearray(fb)
    out = bytearray(stride * h)
    for y in range(h):
        out[y * stride:y * stride + w] = fb[y * w:(y + 1) * w]
    return out


def letterbox_rect(src_w, src_h, dst_w, dst_h):
    """Largest (ox, oy, w, h) rect inside the dst window that preserves the src
    framebuffer's aspect ratio, centered -- the destination for an aspect-correct
    StretchDIBits, with the leftover margin left for black bars. Uniform (float)
    scale, so a src whose aspect matches dst fills it edge-to-edge (ox=oy=0),
    while an off-ratio src is letterboxed (bars top/bottom) or pillarboxed (bars
    left/right). Degenerate sizes fall back to filling the window."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return (0, 0, max(0, dst_w), max(0, dst_h))
    scale = min(dst_w / src_w, dst_h / src_h)
    out_w = max(1, round(src_w * scale))
    out_h = max(1, round(src_h * scale))
    return ((dst_w - out_w) // 2, (dst_h - out_h) // 2, out_w, out_h)


def raw_mouse_delta(usflags, last_x, last_y):
    """Relative mouse motion (dx, dy) from a RAWMOUSE's fields. Absolute-mode
    events (touchpad / remote desktop set MOUSE_MOVE_ABSOLUTE) carry screen
    coordinates rather than deltas; applying them would snap the view, so they
    yield no motion -- the raw-input analogue of look_delta's warp-straddle guard."""
    if usflags & MOUSE_MOVE_ABSOLUTE:
        return 0, 0
    return last_x, last_y


def apply_left_button(held, usbuttonflags):
    """Updated left-button held state from a RAWMOUSE button-flag word. Flags only
    report transitions, so no flag leaves `held` unchanged (a held button across a
    motion packet); a coalesced down+up ends released (up wins -> no stuck fire).
    Needed because RIDEV_NOLEGACY suppresses the WM_LBUTTONDOWN Tk fires on."""
    if usbuttonflags & RI_MOUSE_LEFT_BUTTON_UP:
        return False
    if usbuttonflags & RI_MOUSE_LEFT_BUTTON_DOWN:
        return True
    return held


# ============================================================================
#  Live ctypes glue below: needs a real window + a hand at the mouse, so it is
#  verified by running the game (tests/smoke_win_gdi.py exercises the signatures), not
#  by unit tests. DLLs load in __init__ so importing this module stays cheap.
# ============================================================================

# ---- GDI blit constants / structs (wingdi.h) --------------------------------
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
TRANSPARENT = 1
SYSTEM_FIXED_FONT = 16            # stock monospace fallback (lacks block glyphs)
# HUD font: a TrueType monospace with the block-element glyphs the profiler bar
# chart draws (the stock fixed font and even Consolas lack the 1/8..7/8 blocks;
# Cascadia Mono ships with Windows 11 and has them). Fallback to stock if absent.
HUD_FONT_FACE = "Cascadia Mono"
HUD_FONT_HEIGHT = 16              # character height in logical units (negative lfHeight)
FW_NORMAL = 400
DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 4                 # prefer a TrueType face over a raster substitute
CLIP_DEFAULT_PRECIS = 0
CLEARTYPE_QUALITY = 5
FIXED_PITCH = 1
FF_MODERN = 48                    # proportional family hint: modern (monospace)
GGI_MARK_NONEXISTING = 0x0001     # GetGlyphIndices: report missing glyphs as 0xFFFF
FULL_BLOCK = "█"             # probe glyph: present => the bar chars will render
BLACK_BRUSH = 4                   # GetStockObject id: solid black brush
NULL_PEN = 8                      # GetStockObject id: no outline (for Polygon)
PS_SOLID = 0                      # CreatePen style: solid line

WIRE_RGB = (0, 255, 102)         # wireframe segment colour ("#00ff66" in main.py)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO256(ctypes.Structure):
    """BITMAPINFOHEADER plus a full 256-entry colour table for 8bpp palettised
    DIBs. Each entry is an RGBQUAD packed as a little-endian DWORD: blue in
    the low byte, then green, red, reserved -- i.e. b | g<<8 | r<<16."""
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 256)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


def colorref(rgb):
    """Pack an (r, g, b) tuple into GDI's 0x00BBGGRR COLORREF."""
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


def _hex_to_rgb(color):
    """Convert render_shaded's fill colour to an (r, g, b) int tuple. It emits a
    Tk hex string '#rrggbb'; tolerate an already-(r,g,b) tuple too, so the same
    helper serves either source without guessing wrong."""
    if isinstance(color, str):
        s = color.lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    return tuple(color)


class GdiBlitter:
    """StretchDIBits a packed RGB framebuffer straight to a window's DC, scaling
    it up to the window in GDI (no PPM, no PhotoImage, no .zoom()). Because the
    blit owns the whole client area, HUD overlays are drawn with GDI text on top
    of the same DC each frame -- otherwise Tk would repaint black boxes through it.

    present(fb, w, h, dst_w, dst_h, texts) is one frame: texts is a list of
    (x, y, string, (r,g,b), anchor) with anchor in {'nw','center','sw'}; a string
    may contain '\\n'."""

    def __init__(self, hwnd):
        self.hwnd = wintypes.HWND(hwnd)
        u = self.user32 = ctypes.WinDLL("user32")
        g = self.gdi32 = ctypes.WinDLL("gdi32")
        u.GetDC.argtypes = [wintypes.HWND]; u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.ReleaseDC.restype = ctypes.c_int
        g.StretchDIBits.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER),
            wintypes.UINT, wintypes.DWORD]
        g.StretchDIBits.restype = ctypes.c_int
        g.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        g.SetBkMode.restype = ctypes.c_int
        g.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        g.SetTextColor.restype = wintypes.DWORD
        g.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                               wintypes.LPCWSTR, ctypes.c_int]
        g.TextOutW.restype = wintypes.BOOL
        g.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR,
                                            ctypes.c_int, ctypes.POINTER(SIZE)]
        g.GetTextExtentPoint32W.restype = wintypes.BOOL
        g.GetStockObject.argtypes = [ctypes.c_int]
        g.GetStockObject.restype = wintypes.HANDLE
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        g.SelectObject.restype = wintypes.HANDLE
        g.CreateFontW.argtypes = ([ctypes.c_int] * 5 + [wintypes.DWORD] * 8 +
                                  [wintypes.LPCWSTR])
        g.CreateFontW.restype = wintypes.HANDLE
        g.GetGlyphIndicesW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_ushort), wintypes.DWORD]
        g.GetGlyphIndicesW.restype = wintypes.DWORD
        # -- double-buffered vector drawing (wireframe / flat / particles) -------
        # A cached memory DC + compatible bitmap is drawn into off-screen, then
        # BitBlt'd to the window in one shot, so vector frames never flicker.
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int,
                                             ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HANDLE
        g.DeleteDC.argtypes = [wintypes.HDC]
        g.DeleteDC.restype = wintypes.BOOL
        g.DeleteObject.argtypes = [wintypes.HANDLE]
        g.DeleteObject.restype = wintypes.BOOL
        g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.BitBlt.restype = wintypes.BOOL
        g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.CreatePen.restype = wintypes.HANDLE
        g.CreateSolidBrush.argtypes = [wintypes.DWORD]
        g.CreateSolidBrush.restype = wintypes.HANDLE
        g.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p]
        g.MoveToEx.restype = wintypes.BOOL
        g.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.LineTo.restype = wintypes.BOOL
        g.Polygon.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_int]
        g.Polygon.restype = wintypes.BOOL
        u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                               wintypes.HANDLE]
        u.FillRect.restype = ctypes.c_int
        self._font_created = False        # True if _font is ours to DeleteObject
        self._font = self._hud_font()
        self._bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER),
                                     biPlanes=1, biBitCount=24,
                                     biCompression=BI_RGB)
        self._bmi8 = None            # 8bpp palettised header; set_palette fills it
        self._buf = None             # keep the live DIB bytes alive across the call
        # cached double-buffer for present_vector (recreated on size change), and
        # cached stock/created objects reused every frame (freed in close()).
        self._mem_dc = None          # memory HDC the vector frame is drawn into
        self._mem_bmp = None         # bitmap selected into _mem_dc
        self._mem_wh = (0, 0)        # current backing-store size
        self._mem_oldbmp = None      # bitmap that was in _mem_dc before ours
        self._black_brush = g.GetStockObject(BLACK_BRUSH)   # stock: never delete
        self._null_pen = g.GetStockObject(NULL_PEN)         # stock: never delete
        self._wire_pen = g.CreatePen(PS_SOLID, 1, colorref(WIRE_RGB))  # del in close()

    def set_palette(self, palette):
        """Install the 256-colour palette for 8bpp framebuffer presents.
        palette: 256 (r, g, b) tuples. Until this is called, present() treats
        framebuffers as packed 24bpp RGB (the legacy path)."""
        bmi = BITMAPINFO256()
        bmi.bmiHeader = BITMAPINFOHEADER(
            biSize=ctypes.sizeof(BITMAPINFOHEADER), biPlanes=1, biBitCount=8,
            biCompression=BI_RGB, biClrUsed=256)
        for i, (r, g, b) in enumerate(palette[:256]):
            bmi.bmiColors[i] = b | (g << 8) | (r << 16)
        self._bmi8 = bmi

    def present(self, fb, w, h, dst_w, dst_h, texts=(), particles=()):
        g, u = self.gdi32, self.user32
        if self._bmi8 is not None and len(fb) == w * h:
            # 8bpp palette-indexed framebuffer: GDI maps indices through the
            # colour table during the stretch -- no Python-side expansion.
            self._buf = fb8_to_dib(fb, w, h)
            self._bmi8.bmiHeader.biWidth = w
            self._bmi8.bmiHeader.biHeight = -h  # negative => top-down rows
            pbmi = ctypes.cast(ctypes.byref(self._bmi8),
                               ctypes.POINTER(BITMAPINFOHEADER))
        else:
            self._buf = to_dib_bgr(fb, w, h)
            self._bmi.biWidth = w
            self._bmi.biHeight = -h             # negative => top-down rows
            pbmi = ctypes.byref(self._bmi)
        cbuf = (ctypes.c_char * len(self._buf)).from_buffer(self._buf)
        # scale the framebuffer into the largest aspect-correct rect; an off-ratio
        # mode (e.g. 80x40 in a 4:3 window) gets centered with black bars rather
        # than stretched, so pixels stay square.
        ox, oy, ow, oh = letterbox_rect(w, h, dst_w, dst_h)
        hdc = u.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            g.StretchDIBits(hdc, ox, oy, ow, oh, 0, 0, w, h,
                            cbuf, pbmi, DIB_RGB_COLORS, SRCCOPY)
            if ox or oy:                        # letterboxed: fill bars + fit sprites
                self._fill_bars(hdc, ox, oy, ow, oh, dst_w, dst_h)
                particles = self._fit_particles(particles, ox, oy, ow, oh,
                                                 dst_w, dst_h)
            self._draw_particles_gdi(hdc, particles)
            self._draw_texts(hdc, texts)
        finally:
            u.ReleaseDC(self.hwnd, hdc)

    def _fill_bars(self, hdc, ox, oy, ow, oh, dst_w, dst_h):
        """Black-fill the letterbox margins around the (ox, oy, ow, oh) image rect,
        so an off-ratio framebuffer shows bars instead of stale pixels. Only the
        margins are filled (not the image area), so the world image never flashes
        black under itself on this single-buffered window DC."""
        u = self.user32
        bars = []
        if oy > 0:                              # top + bottom bands (full width)
            bars.append((0, 0, dst_w, oy))
            bars.append((0, oy + oh, dst_w, dst_h))
        if ox > 0:                              # left + right (within the image band)
            bars.append((0, oy, ox, oy + oh))
            bars.append((ox + ow, oy, dst_w, oy + oh))
        for l, t, r, b in bars:
            rect = wintypes.RECT(l, t, r, b)
            u.FillRect(hdc, ctypes.byref(rect), self._black_brush)

    def _fit_particles(self, particles, ox, oy, ow, oh, dst_w, dst_h):
        """Remap window-space particle sprites into the letterbox image rect so the
        world sprites stay aligned with the (now-smaller) world image. Sprites are
        scaled by the smaller axis ratio so each stays a square within the bars."""
        if not particles:
            return particles
        sx, sy = ow / dst_w, oh / dst_h
        s = min(sx, sy)
        return [(ox + x * sx, oy + y * sy, max(1.0, half * s), rgb)
                for (x, y, half, rgb) in particles]

    def _hud_font(self):
        """Create the Cascadia Mono HUD font, verifying it actually carries the
        block glyphs the bar chart draws; fall back to the stock fixed font if the
        face is unavailable (GDI silently substitutes a font that may lack them)."""
        g = self.gdi32
        hf = g.CreateFontW(-HUD_FONT_HEIGHT, 0, 0, 0, FW_NORMAL, 0, 0, 0,
                           DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, FIXED_PITCH | FF_MODERN, HUD_FONT_FACE)
        if hf and self._font_has_glyph(hf, FULL_BLOCK):
            self._font_created = True
            return hf
        if hf:
            g.DeleteObject(hf)
        self._font_created = False
        return g.GetStockObject(SYSTEM_FIXED_FONT)

    def _font_has_glyph(self, hf, ch):
        """Does font `hf` have a glyph for `ch`? (Selects it into a scratch DC and
        asks GetGlyphIndices, which flags a missing glyph as 0xFFFF.)"""
        g, u = self.gdi32, self.user32
        hdc = u.GetDC(self.hwnd)
        if not hdc:
            return False
        try:
            old = g.SelectObject(hdc, hf)
            idx = (ctypes.c_ushort * 1)()
            g.GetGlyphIndicesW(hdc, ch, 1, idx, GGI_MARK_NONEXISTING)
            g.SelectObject(hdc, old)
            return idx[0] != 0xFFFF
        finally:
            u.ReleaseDC(self.hwnd, hdc)

    def _draw_texts(self, hdc, texts):
        """Draw the HUD/overlay text list on `hdc` (window or memory DC). Shared by
        present (textured) and present_vector (wireframe/flat). texts is a list of
        (x, y, string, (r,g,b), anchor); transparent background, stock fixed font."""
        if not texts:
            return
        g = self.gdi32
        g.SetBkMode(hdc, TRANSPARENT)
        g.SelectObject(hdc, self._font)
        for x, y, s, rgb, anchor in texts:
            self._text_block(hdc, x, y, s, rgb, anchor)

    def _text_block(self, hdc, x, y, s, rgb, anchor):
        # rgb is one (r,g,b) for the block, or a per-line list (a short list
        # extends with its last entry) -- the profiler HUD tints its total row
        g = self.gdi32
        line_rgbs = None if isinstance(rgb[0], int) else rgb
        if line_rgbs is None:
            g.SetTextColor(hdc, colorref(rgb))
        lines = s.split("\n")
        sz = SIZE()
        # line height from the first line (stock fixed font is uniform)
        g.GetTextExtentPoint32W(hdc, lines[0], len(lines[0]), ctypes.byref(sz))
        lh = sz.cy
        top = y if anchor == "nw" else (y - lh * len(lines) // 2 if anchor ==
                                        "center" else y - lh * len(lines))
        for i, line in enumerate(lines):
            if line_rgbs is not None:
                g.SetTextColor(hdc, colorref(line_rgbs[min(i, len(line_rgbs) - 1)]))
            g.GetTextExtentPoint32W(hdc, line, len(line), ctypes.byref(sz))
            lx = x - sz.cx // 2 if anchor == "center" else x
            g.TextOutW(hdc, lx, top + i * lh, line, len(line))

    # ---- double-buffered vector drawing (wireframe / flat / particles) --------
    def _memory_dc(self, hdc, w, h):
        """Lazily create (and recreate on size change) a cached memory DC backed by
        a window-compatible bitmap, returning the memory DC ready to draw into.
        GDI objects (handles) are scarce, so this is created once and reused; it is
        freed in close(). The previously-selected (default 1x1) bitmap is remembered
        so it can be reselected before we DeleteObject ours."""
        g = self.gdi32
        if self._mem_dc and self._mem_wh == (w, h):
            return self._mem_dc
        # size changed (or first call): tear down the old backing store first.
        self._free_memory_dc()
        self._mem_dc = g.CreateCompatibleDC(hdc)
        self._mem_bmp = g.CreateCompatibleBitmap(hdc, w, h)
        self._mem_oldbmp = g.SelectObject(self._mem_dc, self._mem_bmp)
        self._mem_wh = (w, h)
        return self._mem_dc

    def _free_memory_dc(self):
        g = self.gdi32
        if self._mem_dc:
            if self._mem_oldbmp:                 # restore default bmp before delete
                g.SelectObject(self._mem_dc, self._mem_oldbmp)
            g.DeleteDC(self._mem_dc)
        if self._mem_bmp:
            g.DeleteObject(self._mem_bmp)
        self._mem_dc = self._mem_bmp = self._mem_oldbmp = None
        self._mem_wh = (0, 0)

    def present_vector(self, segs, polys, particles, dst_w, dst_h, texts=(),
                       hidden=False):
        """Draw one wireframe ('segs') or flat-shaded ('polys') frame, plus
        particles and HUD text, into an off-screen memory DC, then BitBlt it to the
        window in one shot (no flicker). Exactly one of segs/polys is non-None.
        With `hidden`, `polys` are drawn as hidden-line wireframe (black fill +
        green outline) instead of flat-shaded fills."""
        g, u = self.gdi32, self.user32
        hdc = u.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            memdc = self._memory_dc(hdc, dst_w, dst_h)
            # clear to black
            r = wintypes.RECT(0, 0, dst_w, dst_h)
            u.FillRect(memdc, ctypes.byref(r), self._black_brush)
            if segs is not None:
                self._draw_segs(memdc, segs)
            elif polys is not None and hidden:
                self._draw_wire_hidden_gdi(memdc, polys)
            elif polys is not None:
                self._draw_polys_gdi(memdc, polys)
            self._draw_particles_gdi(memdc, particles)
            self._draw_texts(memdc, texts)
            g.BitBlt(hdc, 0, 0, dst_w, dst_h, memdc, 0, 0, SRCCOPY)
        finally:
            u.ReleaseDC(self.hwnd, hdc)

    def draw_console(self, lines, input_line, cursor_col, dst_w, dst_h):
        """Draw the drop-down console panel on top of the current frame: a dark
        rectangle across the top ~40% of the window, the scrollback `lines`
        top-to-bottom, then the `] input` line with a caret, in the monospace
        HUD font. Called after the world present, straight onto the window DC."""
        g, u = self.gdi32, self.user32
        hdc = u.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            panel_h = dst_h * 2 // 5
            rect = wintypes.RECT(0, 0, dst_w, panel_h)
            brush = g.CreateSolidBrush(colorref((16, 16, 24)))   # dark panel
            u.FillRect(hdc, ctypes.byref(rect), brush)
            g.DeleteObject(brush)
            # 1px brighter bottom edge so the panel reads as an object
            edge = wintypes.RECT(0, panel_h - 1, dst_w, panel_h)
            ebrush = g.CreateSolidBrush(colorref((0, 160, 70)))
            u.FillRect(hdc, ctypes.byref(edge), ebrush)
            g.DeleteObject(ebrush)

            g.SetBkMode(hdc, TRANSPARENT)
            g.SelectObject(hdc, self._font)
            # line height from the font
            sz = SIZE()
            g.GetTextExtentPoint32W(hdc, "X", 1, ctypes.byref(sz))
            lh = sz.cy or 16
            cw = sz.cx or 9
            # input line sits at the panel bottom; scrollback fills UPWARD from
            # just above it, clamped to the panel top -- so the real font height
            # (not the Client's row estimate) decides what fits, with no overlap.
            iy = panel_h - lh - 4
            g.SetTextColor(hdc, colorref((200, 220, 200)))
            y = iy - lh
            for line in reversed(lines):          # newest just above the input line
                if y < 4:
                    break                         # don't draw above the panel top
                g.TextOutW(hdc, 6, y, line, len(line))
                y -= lh
            g.SetTextColor(hdc, colorref((255, 255, 255)))
            g.TextOutW(hdc, 6, iy, input_line, len(input_line))
            # caret: a vertical bar at the cursor column
            cx = 6 + cursor_col * cw
            caret = wintypes.RECT(cx, iy, cx + 1, iy + lh)
            cbrush = g.CreateSolidBrush(colorref((255, 255, 255)))
            u.FillRect(hdc, ctypes.byref(caret), cbrush)
            g.DeleteObject(cbrush)
        finally:
            u.ReleaseDC(self.hwnd, hdc)

    def draw_menu(self, view, dst_w, dst_h):
        """Draw the Escape overlay menu: a centered dark panel with the title and
        rows, the selected row prefixed '> ' and brightened. view is
        (title, [(label, value, selected), ...]). Drawn after the world present,
        straight onto the window DC. Mirrors draw_console's font handling."""
        title, rows = view
        g, u = self.gdi32, self.user32
        hdc = u.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            g.SetBkMode(hdc, TRANSPARENT)
            g.SelectObject(hdc, self._font)
            sz = SIZE()
            g.GetTextExtentPoint32W(hdc, "X", 1, ctypes.byref(sz))
            lh = sz.cy or 16
            # panel sized to the content: title + blank + one row per item
            nlines = len(rows) + 2
            panel_w = 360
            panel_h = nlines * lh + 24
            x0 = (dst_w - panel_w) // 2
            y0 = (dst_h - panel_h) // 2
            rect = wintypes.RECT(x0, y0, x0 + panel_w, y0 + panel_h)
            brush = g.CreateSolidBrush(colorref((16, 16, 24)))
            u.FillRect(hdc, ctypes.byref(rect), brush)
            g.DeleteObject(brush)
            # 1px green bottom edge, like the console panel
            edge = wintypes.RECT(x0, y0 + panel_h - 1, x0 + panel_w, y0 + panel_h)
            ebrush = g.CreateSolidBrush(colorref((0, 160, 70)))
            u.FillRect(hdc, ctypes.byref(edge), ebrush)
            g.DeleteObject(ebrush)
            # title in yellow
            g.SetTextColor(hdc, colorref((255, 255, 0)))
            g.TextOutW(hdc, x0 + 16, y0 + 12, title, len(title))
            # rows, starting one blank line below the title
            y = y0 + 12 + 2 * lh
            for label, value, selected in rows:
                text = label if not value else f"{label}: {value}"
                if selected:
                    text = "> " + text
                    g.SetTextColor(hdc, colorref((255, 255, 255)))
                else:
                    text = "  " + text
                    g.SetTextColor(hdc, colorref((160, 200, 160)))
                g.TextOutW(hdc, x0 + 16, y, text, len(text))
                y += lh
        finally:
            u.ReleaseDC(self.hwnd, hdc)

    def _draw_segs(self, hdc, segs):
        """Draw wireframe line segments (flat (x0, y0, x1, y1) tuples) in green.
        Selects the cached wire pen, draws each as MoveToEx + LineTo, then restores
        the previously-selected pen (the cached pen is freed in close())."""
        g = self.gdi32
        old = g.SelectObject(hdc, self._wire_pen)
        for x0, y0, x1, y1 in segs:
            g.MoveToEx(hdc, int(x0), int(y0), None)
            g.LineTo(hdc, int(x1), int(y1))
        g.SelectObject(hdc, old)

    def _draw_polys_gdi(self, hdc, polys):
        """Fill flat-shaded polygons. Each poly is (flat, color) where flat is a
        flat coord list [x0, y0, x1, y1, ...] and color is a Tk hex string
        '#rrggbb' (as render_shaded emits). A NULL pen suppresses outlines (the Tk
        version used outline=''). Each per-poly brush is selected, used, the old
        brush restored, then the brush DeleteObject'd -- no GDI leak."""
        g = self.gdi32
        oldpen = g.SelectObject(hdc, self._null_pen)
        for flat, color in polys:
            n = len(flat) // 2
            if n < 3:
                continue
            pts = (wintypes.POINT * n)()
            for i in range(n):
                pts[i].x = int(flat[2 * i])
                pts[i].y = int(flat[2 * i + 1])
            brush = g.CreateSolidBrush(colorref(_hex_to_rgb(color)))
            oldbrush = g.SelectObject(hdc, brush)
            g.Polygon(hdc, pts, n)
            g.SelectObject(hdc, oldbrush)
            g.DeleteObject(brush)
        g.SelectObject(hdc, oldpen)

    def _draw_wire_hidden_gdi(self, hdc, polys):
        """Hidden-line wireframe: each render_shaded poly (flat, color) drawn as a
        black-filled, green-outlined polygon. Painted back-to-front, near faces
        occlude far ones. Mirrors _draw_polys_gdi but with the cached wire pen and
        black brush (the fill colour is ignored)."""
        g = self.gdi32
        oldpen = g.SelectObject(hdc, self._wire_pen)
        oldbrush = g.SelectObject(hdc, self._black_brush)
        for flat, _color in polys:
            n = len(flat) // 2
            if n < 3:
                continue
            pts = (wintypes.POINT * n)()
            for i in range(n):
                pts[i].x = int(flat[2 * i])
                pts[i].y = int(flat[2 * i + 1])
            g.Polygon(hdc, pts, n)
        g.SelectObject(hdc, oldbrush)
        g.SelectObject(hdc, oldpen)

    def _draw_particles_gdi(self, hdc, particles):
        """Fill each particle as a small square: (x, y, half, (r,g,b)) ->
        FillRect [x-half, y-half, x+half, y+half] with a brush of that colour.
        Brushes are grouped by colour so a run of same-colour particles reuses one
        brush; every created brush is DeleteObject'd before returning."""
        if not particles:
            return
        u, g = self.user32, self.gdi32
        brushes = {}
        try:
            for x, y, half, rgb in particles:
                rgb = tuple(rgb)
                brush = brushes.get(rgb)
                if brush is None:
                    brush = brushes[rgb] = g.CreateSolidBrush(colorref(rgb))
                r = wintypes.RECT(int(x - half), int(y - half),
                                  int(x + half), int(y + half))
                u.FillRect(hdc, ctypes.byref(r), brush)
        finally:
            for brush in brushes.values():
                g.DeleteObject(brush)

    def close(self):
        """Free the GDI objects this blitter caches: the off-screen backing store
        and the wire pen (stock objects are never deleted). Idempotent."""
        self._free_memory_dc()
        if self._wire_pen:
            self.gdi32.DeleteObject(self._wire_pen)
            self._wire_pen = None
        if self._font_created:
            self.gdi32.DeleteObject(self._font)
            self._font_created = False


# ---- raw input constants / structs (winuser.h) ------------------------------
WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_REMOVE = 0x00000001
RIDEV_NOLEGACY = 0x00000030      # mouse emits only WM_INPUT (no WM_MOUSEMOVE/clicks)
GWLP_WNDPROC = -4
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02

LRESULT = ctypes.c_ssize_t       # LRESULT / LONG_PTR are pointer-sized signed
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

