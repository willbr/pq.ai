"""Windows UI front-end (outside the `quake` engine package): fast GDI framebuffer
blit + raw-input mouselook, both hung off the Tk window's HWND.

Why this exists: tkinter's z-buffer present path encodes a PPM and builds a fresh,
upscaled PhotoImage every frame; and tkinter offers no OS mouse grab, so mouselook
fakes relative motion by warping the cursor and measuring deltas (fragile -- see
main.look_delta's warp-straddle guard). Both are Windows pain points this module
removes with ctypes against user32/gdi32:

  GdiBlitter   StretchDIBits a raw framebuffer straight to the window DC, doing the
               1/ZBUF_SCALE upscale in GDI (no PPM, no PhotoImage, no .zoom()).
  RawMouse     RegisterRawInputDevices + a subclassed WndProc reading WM_INPUT, for
               acceleration-free relative deltas with no warp -- plus ClipCursor /
               ShowCursor for a real grab.

main.py owns the Tk window and picks this front-end on sys.platform == "win32";
elsewhere the existing tkinter warp path stays. Like win.py / mac.py, the engine
imports none of this.

The pure, OS-independent core (bgr_swap, raw_mouse_delta, the RAWINPUT structs)
is unit-tested in test_win_ui.py; the live window/grab glue is verified by running
the game. DLL loading is deferred into the classes so importing this module is
side-effect free (the tests import it on any Windows box, no window required).
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
#  verified by running the game (smoke_win_ui.py exercises the signatures), not
#  by unit tests. DLLs load in __init__ so importing this module stays cheap.
# ============================================================================

# ---- GDI blit constants / structs (wingdi.h) --------------------------------
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
TRANSPARENT = 1
SYSTEM_FIXED_FONT = 16            # a stock monospace font (no CreateFont needed)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


def colorref(rgb):
    """Pack an (r, g, b) tuple into GDI's 0x00BBGGRR COLORREF."""
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


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
        self._font = g.GetStockObject(SYSTEM_FIXED_FONT)
        self._bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER),
                                     biPlanes=1, biBitCount=24,
                                     biCompression=BI_RGB)
        self._buf = None             # keep the live DIB bytes alive across the call

    def present(self, fb, w, h, dst_w, dst_h, texts=()):
        g, u, hdc = self.gdi32, self.user32, None
        self._buf = to_dib_bgr(fb, w, h)
        self._bmi.biWidth = w
        self._bmi.biHeight = -h                 # negative => top-down (our row order)
        cbuf = (ctypes.c_char * len(self._buf)).from_buffer(self._buf)
        hdc = u.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            g.StretchDIBits(hdc, 0, 0, dst_w, dst_h, 0, 0, w, h,
                            cbuf, ctypes.byref(self._bmi),
                            DIB_RGB_COLORS, SRCCOPY)
            if texts:
                g.SetBkMode(hdc, TRANSPARENT)
                g.SelectObject(hdc, self._font)
                for x, y, s, rgb, anchor in texts:
                    self._text_block(hdc, x, y, s, rgb, anchor)
        finally:
            u.ReleaseDC(self.hwnd, hdc)

    def _text_block(self, hdc, x, y, s, rgb, anchor):
        g = self.gdi32
        g.SetTextColor(hdc, colorref(rgb))
        lines = s.split("\n")
        sz = SIZE()
        # line height from the first line (stock fixed font is uniform)
        g.GetTextExtentPoint32W(hdc, lines[0], len(lines[0]), ctypes.byref(sz))
        lh = sz.cy
        top = y if anchor == "nw" else (y - lh * len(lines) // 2 if anchor ==
                                        "center" else y - lh * len(lines))
        for i, line in enumerate(lines):
            g.GetTextExtentPoint32W(hdc, line, len(line), ctypes.byref(sz))
            lx = x - sz.cx // 2 if anchor == "center" else x
            g.TextOutW(hdc, lx, top + i * lh, line, len(line))


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


class RawMouse:
    """Acceleration-free relative mouselook via Win32 Raw Input, with a real
    cursor grab. Subclasses the (Tk) window's WndProc to catch WM_INPUT, decodes
    each RAWMOUSE, and accumulates deltas; the game reads and clears them once per
    frame with read(). grab()/ungrab() register/unregister the raw device and
    confine + hide the cursor (ClipCursor + ShowCursor). No warp, so none of
    main.look_delta's straddle machinery is needed on this path.

    The WndProc runs on Tk's own thread during its message pump, so accumulation
    and read() share a thread -- no locking. Keep this object alive: it holds the
    WNDPROC trampoline GDI must not garbage-collect, and restores the original
    proc on shutdown()."""

    def __init__(self, hwnd):
        self.hwnd = wintypes.HWND(hwnd)
        self._dx = 0
        self._dy = 0
        self.left_down = False         # fire button, read from raw (legacy suppressed)
        self._grabbed = False
        u = self.user32 = ctypes.WinDLL("user32")
        u.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE),
                                              wintypes.UINT, wintypes.UINT]
        u.RegisterRawInputDevices.restype = wintypes.BOOL
        u.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                      ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        u.GetRawInputData.restype = wintypes.UINT
        u.ClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
        u.ClipCursor.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.ShowCursor.argtypes = [wintypes.BOOL]
        u.ShowCursor.restype = ctypes.c_int
        u.CallWindowProcW.argtypes = [LRESULT, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
        u.CallWindowProcW.restype = LRESULT
        # SetWindowLongPtrW only exists on 64-bit user32; 32-bit uses SetWindowLongW
        setwl = (u.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8
                 else u.SetWindowLongW)
        # third arg typed as c_void_p so the same function installs the callback
        # (cast from WNDPROC) and later restores the original proc (a raw int).
        setwl.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        setwl.restype = LRESULT
        self._setwl = setwl
        self._wndproc = WNDPROC(self._proc)        # keep a ref (anti-GC)
        self._old_proc = setwl(self.hwnd, GWLP_WNDPROC,
                               ctypes.cast(self._wndproc, ctypes.c_void_p))
        self._ri = RAWINPUTDEVICE(HID_USAGE_PAGE_GENERIC, HID_USAGE_GENERIC_MOUSE,
                                  0, self.hwnd)

    # -- the subclassed window procedure: pick off WM_INPUT, chain the rest -----
    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                self._read_raw(lparam)
            except Exception:
                pass                              # never break Tk's message pump
        return self.user32.CallWindowProcW(self._old_proc, hwnd, msg,
                                           wparam, lparam)

    def _read_raw(self, lparam):
        ri = RAWINPUT()
        size = wintypes.UINT(ctypes.sizeof(RAWINPUT))
        got = self.user32.GetRawInputData(lparam, RID_INPUT, ctypes.byref(ri),
                                          ctypes.byref(size),
                                          ctypes.sizeof(RAWINPUTHEADER))
        if got == 0xFFFFFFFF or ri.header.dwType != RIM_TYPEMOUSE:
            return
        dx, dy = raw_mouse_delta(ri.mouse.usFlags, ri.mouse.lLastX, ri.mouse.lLastY)
        self._dx += dx
        self._dy += dy
        # low word of the button union is usButtonFlags (the transition bits)
        self.left_down = apply_left_button(self.left_down,
                                           ri.mouse.ulButtons & 0xFFFF)

    def read(self):
        """Accumulated (dx, dy) since the last call; resets to zero."""
        dx, dy = self._dx, self._dy
        self._dx = self._dy = 0
        return dx, dy

    def grab(self):
        if self._grabbed:
            return
        # RIDEV_NOLEGACY: while grabbed the mouse emits only WM_INPUT, so the Tk
        # event loop is no longer flooded with WM_MOUSEMOVE -> <Motion> events
        # (which starved the after()-driven tick and delayed keypresses). Motion
        # AND the fire button are read from the raw stream instead.
        self._ri.dwFlags = RIDEV_NOLEGACY
        self._ri.hwndTarget = self.hwnd
        self.user32.RegisterRawInputDevices(ctypes.byref(self._ri), 1,
                                            ctypes.sizeof(RAWINPUTDEVICE))
        self._clip()
        self.user32.ShowCursor(False)
        self._dx = self._dy = 0
        self.left_down = False
        self._grabbed = True

    def ungrab(self):
        if not self._grabbed:
            return
        rm = RAWINPUTDEVICE(HID_USAGE_PAGE_GENERIC, HID_USAGE_GENERIC_MOUSE,
                            RIDEV_REMOVE, None)
        self.user32.RegisterRawInputDevices(ctypes.byref(rm), 1,
                                            ctypes.sizeof(RAWINPUTDEVICE))
        self.user32.ClipCursor(None)
        self.user32.ShowCursor(True)
        self._grabbed = False

    def _clip(self):
        r = wintypes.RECT()
        if self.user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            self.user32.ClipCursor(ctypes.byref(r))

    def shutdown(self):
        try:
            self.ungrab()
        finally:
            self._setwl(self.hwnd, GWLP_WNDPROC, self._old_proc)
