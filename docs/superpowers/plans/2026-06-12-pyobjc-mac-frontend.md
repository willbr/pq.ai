# PyObjC Cocoa macOS Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace tkinter as the default macOS frontend with a native Cocoa frontend (PyObjC), mirroring `win_gdi.py`/`win_ui.py` — owned drain-then-step loop, CoreGraphics drawing in `drawRect:`, true relative mouselook.

**Architecture:** `mac_cocoa.py` (frontend: NSApplication/NSWindow/GameView + frame loop) and `mac_ui.py` (pure helpers + CG drawing functions), both at repo root outside `quake/`. `client.py` and the engine are untouched. `main.py` dispatches darwin→cocoa, win32→gdi, `--tk`/other→tk.

**Tech Stack:** Python 3.13+, PyObjC (`pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz`), existing `client.Client` / `InputState` / `RenderFrame` contract.

**Spec:** `docs/superpowers/specs/2026-06-12-pyobjc-mac-frontend-design.md`

**Conventions that apply** (from CLAUDE.md): tests are standalone scripts importing `tests/_bootstrap.py`, named `test_*`, printing `OK`; run with `PQ_AUDIO=0`. No pytest. Code outside `quake/` uses absolute imports.

---

### Task 1: mac_ui pure helpers (keycodes, RGBA expansion, particle fit) — TDD

**Files:**
- Create: `mac_ui.py`
- Create: `tests/test_mac_ui.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_mac_ui.py`:

```python
"""Unit tests for mac_ui's pure helpers (no PyObjC, no window): the macOS
keycode->name table, the fb->RGBA palette expansion, and particle letterbox
fitting. The CG drawing helpers need a real CGContext and are exercised by
running the game (the win_ui pattern)."""

import _bootstrap  # noqa: F401  (repo root on sys.path, chdir to root)

from mac_ui import KEYCODE_NAMES, pal_channel_tables, expand_fb_rgba, fit_particles


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


if __name__ == "__main__":
    test_keycode_names()
    test_expand_fb_rgba()
    test_fit_particles()
    print("OK")
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `PQ_AUDIO=0 python3 tests/test_mac_ui.py`
Expected: `ModuleNotFoundError: No module named 'mac_ui'`

- [ ] **Step 3: Implement the pure half of mac_ui.py**

```python
"""macOS Cocoa UI helpers (outside the `quake` engine package): the pure,
unit-testable half of the Cocoa frontend, the twin of win_ui.py.

Pure helpers (no PyObjC needed, tested in tests/test_mac_ui.py):
  KEYCODE_NAMES        macOS ANSI virtual keycode -> key name ('w', 'space', ...)
  pal_channel_tables   palette -> three 256-byte translate tables (from main.py)
  expand_fb_rgba       8-bit indexed framebuffer -> packed RGBA via bytes.translate
  fit_particles        remap window-space particles into a letterbox rect
                       (pure port of win_ui.GdiBlitter._fit_particles)

CG drawing helpers (need PyObjC + a CGContext; verified by running the game,
the win_ui pattern) are added below the marker line in this file.
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `PQ_AUDIO=0 python3 tests/test_mac_ui.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add mac_ui.py tests/test_mac_ui.py
git commit -m "mac_ui: pure helpers for the Cocoa frontend (keycodes, RGBA expand, particle fit)"
```

---

### Task 2: mac_ui CG drawing helpers (framebuffer image, vectors, text, panels)

**Files:**
- Modify: `mac_ui.py` (append below the pure half)

No unit tests (needs a live CGContext — the win_ui convention: "verified by
running the game"). Verified by import + the Task 5 smoke run.

- [ ] **Step 1: Append the PyObjC drawing half to mac_ui.py**

```python
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
    """Wrap a packed-RGBA byte buffer as a CGImage (no copy of the copy: the
    provider retains the bytes)."""
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
```

Note: `cell_metrics`/`draw_*` heights are floats (AppKit metrics); the integer
arithmetic from win_ui (`dst_h * 2 // 5`) stays integer where it was integer.

- [ ] **Step 2: Verify the module imports and the pure tests still pass**

Run: `PQ_AUDIO=0 python3 -c "import mac_ui; print(mac_ui.cell_metrics())" && PQ_AUDIO=0 python3 tests/test_mac_ui.py`
Expected: a `(width, height)` tuple (Menlo metrics, roughly `(7.2..., 15...)`) then `OK`

- [ ] **Step 3: Commit**

```bash
git add mac_ui.py
git commit -m "mac_ui: CG drawing half (fb image, segs/polys/particles, text, console/menu panels)"
```

---

### Task 3: mac_cocoa.py — window, GameView, event handling, frame loop

**Files:**
- Create: `mac_cocoa.py`

No unit tests (live window; the win_gdi convention). Verified by the Task 5
smoke run. Import-check at the end of this task.

- [ ] **Step 1: Write mac_cocoa.py**

```python
"""macOS Cocoa front-end (outside the `quake` engine package): plays the REAL
game by driving the UI-agnostic `Client` core with its own drain-then-step
frame loop, drawing via CoreGraphics in an NSView's drawRect:, with true
relative-delta mouselook (no warp hack). The PyObjC twin of win_gdi.py.

Why this exists: tkinter's after() loop owns the event pump and the ~13ms
software render blocks it (win_gdi.py's diagnosis applies on every platform).
This front-end inverts ownership the same way:
    drain ALL pending NSEvents -> step Client -> view.display() -> repeat
Mouse deltas accumulate in the view between frames and are read ONCE per frame,
so input bursts coalesce structurally. Mouselook uses
CGAssociateMouseAndMouseCursorPosition(False) + NSEvent deltaX/deltaY -- real
relative input, so none of main.py's warp/recenter machinery exists here.

Run: python main.py [map] on macOS (this is the darwin default; --tk forces
tkinter). Requires PyObjC: pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz
"""

import sys
import time

import AppKit
import Quartz
import objc

from client import Client, InputState
from quake.console import TeeStdout
from quake.perf import PROFILER
import mac_ui
from win_ui import letterbox_rect      # pure, platform-free helper

# one-shot toggle keys -> the Client command they fire (edge-triggered)
COMMAND_KEYS = {"n": "noclip", "f": "flat", "z": "zbuf", "t": "texture",
                "p": "prof"}
FRAME_S = 1 / 60                       # target frame cadence (sleep floor)


class GameView(AppKit.NSView):
    """The game's content view: accumulates input state (held keys, mouse
    deltas, buttons) from the responder methods and draws the current
    RenderFrame in drawRect:. The frame loop owns stepping and presentation;
    this class is deliberately dumb storage + drawing."""

    def initWithFrame_(self, frame):
        self = objc.super(GameView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.keys = set()              # held key names (mac_ui.KEYCODE_NAMES)
        self.dx = 0.0                  # mouse deltas accumulated since last read
        self.dy = 0.0
        self.lbutton = False           # left button held
        self.mouselook = False         # cursor grabbed?
        self.console = None            # wired to client.con in run()
        self.menu = None               # wired to client.menu in run()
        self.rf = None                 # RenderFrame to draw
        self.client = None             # wired in run() (palette for the LUT)
        self._pal_luts = None          # (lr, lg, lb) translate tables
        self._pal_version = -1
        return self

    # ---- view behaviour ----
    def isFlipped(self):
        return True                    # y-down, matching RenderFrame coords

    def acceptsFirstResponder(self):
        return True

    # ---- mouse grab / ungrab ----
    def grab(self):
        if self.mouselook:
            return
        AppKit.NSCursor.hide()
        Quartz.CGAssociateMouseAndMouseCursorPosition(False)
        self.dx = self.dy = 0.0
        self.mouselook = True

    def ungrab(self):
        if not self.mouselook:
            return
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        AppKit.NSCursor.unhide()
        self.lbutton = False
        self.mouselook = False

    # ---- keyboard ----
    def keyDown_(self, event):
        name = mac_ui.KEYCODE_NAMES.get(event.keyCode())
        # F1 (or backtick) toggles the console open AND closed -- checked first
        if name in ("f1", "grave"):
            self._toggle_console()
            return
        if self.console is not None and self.console.active:
            self._console_key(name, event)
            return
        if self.menu is not None and self.menu.active:
            self._menu_key(name)
            return
        if name == "escape":
            self._open_menu()
            return
        if name is not None and not event.isARepeat():
            self.keys.add(name)

    def keyUp_(self, event):
        name = mac_ui.KEYCODE_NAMES.get(event.keyCode())
        self.keys.discard(name)

    def flagsChanged_(self, event):
        """Shift (run) and Ctrl (fire) arrive as modifier-flag changes, not
        keyDown/keyUp; mirror them into the held-keys set."""
        flags = event.modifierFlags()
        for flag, name in ((AppKit.NSEventModifierFlagShift, "shift"),
                           (AppKit.NSEventModifierFlagControl, "control")):
            if flags & flag:
                self.keys.add(name)
            else:
                self.keys.discard(name)

    def _toggle_console(self):
        """F1/backtick: open or close the console. Opening clears held keys,
        ungrabs the mouse, and closes the menu (panels never stack). Mirrors
        win_gdi._toggle_console."""
        con = self.console
        if con is None:
            return
        con.active = not con.active
        if con.active:
            if self.menu is not None:
                self.menu.active = False
            self.keys.clear()
            self.ungrab()

    def _open_menu(self):
        """Esc with the console closed: open the overlay menu, clear held keys,
        ungrab. Mirrors win_gdi._open_menu."""
        if self.menu is None:
            return
        self.menu.active = True
        self.keys.clear()
        self.ungrab()

    def _menu_key(self, name):
        m = self.menu
        if name == "escape":
            m.key_escape()
        elif name == "up":
            m.key_up()
        elif name == "down":
            m.key_down()
        elif name == "left":
            m.key_left()
        elif name == "right":
            m.key_right()
        elif name in ("return", "kp_enter"):
            m.key_enter()

    def _console_key(self, name, event):
        con = self.console
        if name == "escape":
            con.active = False
        elif name in ("return", "kp_enter"):
            con.key_enter()
        elif name == "backspace":
            con.key_backspace()
        elif name == "delete":
            con.key_delete()
        elif name == "tab":
            con.key_tab()
        elif name == "left":
            con.key_left()
        elif name == "right":
            con.key_right()
        elif name == "home":
            con.key_home()
        elif name == "end":
            con.key_end()
        elif name == "up":
            con.key_up()
        elif name == "down":
            con.key_down()
        elif name == "pageup":
            con.key_pageup()
        elif name == "pagedown":
            con.key_pagedown()
        else:
            chars = event.charactersIgnoringModifiers()
            if chars:
                ch = chars[0]
                if ch >= " " and ch != "\x7f":
                    con.key_char(ch)

    # ---- mouse ----
    def mouseDown_(self, event):
        self.lbutton = True

    def mouseUp_(self, event):
        self.lbutton = False

    def mouseMoved_(self, event):
        if self.mouselook:
            self.dx += event.deltaX()
            self.dy += event.deltaY()

    def mouseDragged_(self, event):
        self.mouseMoved_(event)

    def read_mouse(self):
        dx, dy = self.dx, self.dy
        self.dx = self.dy = 0.0
        return dx, dy

    # ---- drawing ----
    def drawRect_(self, rect):
        rf = self.rf
        b = self.bounds()
        w, h = int(b.size.width), int(b.size.height)
        ctx = AppKit.NSGraphicsContext.currentContext().CGContext()
        mac_ui.fill_rect(ctx, 0, 0, w, h, (0, 0, 0))      # clear to black
        if rf is None:
            return
        texts = list(rf.overlays) + [
            (rf.crosshair[0], rf.crosshair[1], "+", (0, 255, 102), "center")]
        particles = rf.particles
        if rf.mode == "zbuf":
            fb, fw, fh = rf.framebuffer
            if self._pal_luts is None or rf.palette_version != self._pal_version:
                pal = rf.palette or self.client.palette
                self._pal_luts = mac_ui.pal_channel_tables(pal)
                self._pal_version = rf.palette_version
            rgba = mac_ui.expand_fb_rgba(fb, fw, fh, *self._pal_luts)
            img = mac_ui.fb_cgimage(rgba, fw, fh)
            ox, oy, ow, oh = letterbox_rect(fw, fh, w, h)
            mac_ui.draw_fb(ctx, img, ox, oy, ow, oh, h)
            if ox or oy:
                particles = mac_ui.fit_particles(particles, ox, oy, ow, oh, w, h)
        elif rf.mode == "wire":
            mac_ui.draw_segs(ctx, rf.segs)
        elif rf.mode == "wire_hidden":
            mac_ui.draw_wire_hidden(ctx, rf.polys)
        else:                                            # "flat"
            mac_ui.draw_polys(ctx, rf.polys)
        mac_ui.draw_particles(ctx, particles)
        mac_ui.draw_texts(ctx, texts)
        if rf.console is not None:
            lines, input_line, cursor_col = rf.console
            mac_ui.draw_console(ctx, lines, input_line, cursor_col, w, h)
        if rf.menu is not None:
            mac_ui.draw_menu(ctx, rf.menu, w, h)


class _Delegate(AppKit.NSObject):
    """Window + application delegate: turns 'the user closed the window' or
    Cmd-Q into a clean loop exit (running=False) instead of process death, so
    run()'s finally block can shut the Client down."""

    def initWithState_(self, state):
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self.state = state
        return self

    def windowWillClose_(self, note):
        self.state["running"] = False

    def applicationShouldTerminate_(self, app):
        self.state["running"] = False
        return AppKit.NSTerminateCancel


def _make_app():
    """NSApplication with the activation dance (without Regular policy +
    activate there is no key window) and a minimal menu bar (Quit, Cmd-Q)."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    menubar = AppKit.NSMenu.alloc().init()
    appitem = AppKit.NSMenuItem.alloc().init()
    menubar.addItem_(appitem)
    appmenu = AppKit.NSMenu.alloc().init()
    quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit pq.ai", "terminate:", "q")
    appmenu.addItem_(quit_item)
    appitem.setSubmenu_(appmenu)
    app.setMainMenu_(menubar)
    return app


def _make_window(title, width, height, state):
    style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable |
             AppKit.NSWindowStyleMaskMiniaturizable |
             AppKit.NSWindowStyleMaskResizable)
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (width, height)), style, AppKit.NSBackingStoreBuffered, False)
    window.setTitle_(title)
    window.center()
    view = GameView.alloc().initWithFrame_(((0, 0), (width, height)))
    window.setContentView_(view)
    window.makeFirstResponder_(view)
    window.setAcceptsMouseMovedEvents_(True)
    delegate = _Delegate.alloc().initWithState_(state)
    window.setDelegate_(delegate)
    window.makeKeyAndOrderFront_(None)
    return window, view, delegate


def build_input(view, prev_keys):
    """Translate this frame's keyboard + mouse state into an InputState. Edge
    detection drives the one-shots (impulse, commands, Tab, click-to-grab);
    held keys drive movement. Mirrors win_gdi.GameWindow.build_input; returns
    (InputState, new_prev_keys)."""
    if (view.console is not None and view.console.active) or \
       (view.menu is not None and view.menu.active):
        return InputState(mouselook=view.mouselook), set()
    keys = view.keys
    newly = keys - prev_keys

    if "tab" in newly:
        view.ungrab() if view.mouselook else view.grab()
    if not view.mouselook and view.lbutton:
        view.grab()

    def held(name):
        return 1.0 if name in keys else 0.0

    move_forward = (1.0 if ("w" in keys or "up" in keys) else 0.0) - \
                   (1.0 if ("s" in keys or "down" in keys) else 0.0)
    move_strafe = held("d") - held("a")
    move_up = held("space") - held("c")
    turn = held("right") - held("left")
    run_held = "shift" in keys

    look_dx, look_dy = view.read_mouse() if view.mouselook else (0.0, 0.0)
    fire = view.lbutton or ("control" in keys)

    impulse = 0
    for i in range(8):
        if str(i + 1) in newly:
            impulse = i + 1
            break

    commands = frozenset(cmd for key, cmd in COMMAND_KEYS.items()
                         if key in newly)
    return InputState(move_forward=move_forward, move_strafe=move_strafe,
                      move_up=move_up, turn=turn, look_dx=look_dx,
                      look_dy=look_dy, run=run_held, fire=fire,
                      impulse=impulse, commands=commands,
                      mouselook=view.mouselook), set(keys)


def run(mapname):
    state = {"running": True}
    app = _make_app()
    window, view, delegate = _make_window(f"pq.ai cocoa — {mapname}",
                                          800, 600, state)
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    app.finishLaunching()

    real_stdout = sys.stdout
    client = Client(mapname)
    view.client = client
    view.console = client.con
    view.menu = client.menu
    sys.stdout = TeeStdout(real_stdout, client.con.print)

    distant_past = AppKit.NSDate.distantPast()
    prev_keys = set()
    last = time.perf_counter()
    last_wh = (0, 0)
    try:
        while state["running"]:
            # drain ALL pending events first (the crux, as in win_gdi.pump)
            while True:
                event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    AppKit.NSEventMaskAny, distant_past,
                    AppKit.NSDefaultRunLoopMode, True)
                if event is None:
                    break
                app.sendEvent_(event)
            if not state["running"]:
                break

            now = time.perf_counter()
            dt = now - last
            last = now

            b = view.bounds()
            cw, ch = max(1, int(b.size.width)), max(1, int(b.size.height))
            if (cw, ch) != last_wh:
                client.resize(cw, ch)
                last_wh = (cw, ch)

            inp, prev_keys = build_input(view, prev_keys)
            rf = client.frame(dt, inp)
            if client.quit_requested:
                break
            if client.mapname != mapname:        # changelevel / `map`: retitle
                mapname = client.mapname
                window.setTitle_(f"pq.ai cocoa — {mapname}")

            view.rf = rf
            with PROFILER.section("present"):
                view.setNeedsDisplay_(True)
                view.displayIfNeeded()
            PROFILER.frame_end()

            work = time.perf_counter() - now
            if work < FRAME_S:
                time.sleep(FRAME_S - work)
    finally:
        sys.stdout = real_stdout
        client.shutdown()            # stop+dispose audio while healthy
        view.ungrab()                # restore cursor + re-associate the mouse
        window.close()


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("mac_cocoa is macOS-only")
    run(sys.argv[1] if len(sys.argv) > 1 else "e1m1")
```

- [ ] **Step 2: Import-check (no window)**

Run: `PQ_AUDIO=0 python3 -c "import mac_cocoa; print('import ok')"`
Expected: `import ok`

- [ ] **Step 3: Commit**

```bash
git add mac_cocoa.py
git commit -m "mac_cocoa: native Cocoa frontend (PyObjC) -- owned loop, CG drawing, relative mouselook"
```

---

### Task 4: main.py dispatch (darwin -> cocoa) + lazy tkinter import

**Files:**
- Modify: `main.py` (top-of-file imports; `select_frontend`; `__main__` block)
- Test: `tests/test_fb_scale.py` (check whether `select_frontend` is asserted anywhere; update expectations for darwin)

- [ ] **Step 1: Check for existing select_frontend tests**

Run: `grep -rn "select_frontend" tests/`
If a test asserts `("tk", ...)` for darwin, update it to expect `("cocoa", ...)` and add a case asserting `--tk` still forces `"tk"` on darwin.

- [ ] **Step 2: Make the tkinter import survivable**

In `main.py`, replace:

```python
import tkinter as tk
import tkinter.font as tkfont
```

with:

```python
try:
    import tkinter as tk
    import tkinter.font as tkfont
except ImportError:                  # Homebrew python without python-tk: the
    tk = tkfont = None               # cocoa frontend runs without tkinter
```

and add a guard at the top of `App.__init__`:

```python
        if tk is None:
            sys.exit("tkinter is not available; install python-tk, or run "
                     "the default Cocoa frontend (drop --tk)")
```

- [ ] **Step 3: Extend select_frontend**

Replace the existing `select_frontend` body:

```python
def select_frontend(argv, platform):
    """Pick the frontend and map from CLI args. Windows defaults to the gdi32
    frontend (win_gdi), macOS to the Cocoa frontend (mac_cocoa); `--tk` forces
    the tkinter frontend, which is also the default everywhere else."""
    args = [a for a in argv if a != "--tk"]
    mapname = args[0] if args else "e1m1"
    if "--tk" in argv:
        return "tk", mapname
    if platform == "win32":
        return "gdi", mapname
    if platform == "darwin":
        return "cocoa", mapname
    return "tk", mapname
```

- [ ] **Step 4: Dispatch in __main__**

Replace the `__main__` block:

```python
if __name__ == "__main__":
    frontend, mapname = select_frontend(sys.argv[1:], sys.platform)
    if frontend == "gdi":
        import win_gdi
        win_gdi.run(mapname)
    elif frontend == "cocoa":
        try:
            import mac_cocoa
        except ImportError:
            sys.exit("the macOS frontend needs PyObjC:\n"
                     "    pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz\n"
                     "(or run the tkinter fallback: python main.py --tk)")
        mac_cocoa.run(mapname)
    else:
        App(mapname).run()
```

- [ ] **Step 5: Update the main.py module docstring**

The docstring's frontend description ("tkinter frontend (all platforms;
default off-Windows...)") must say: tkinter is the fallback (`--tk` anywhere,
default on Linux); Windows defaults to gdi32 (`win_gdi.py`), macOS to Cocoa
(`mac_cocoa.py`).

- [ ] **Step 6: Run the full test suite**

Run: `export PQ_AUDIO=0; for t in tests/test_*.py; do python3 "$t" || echo "FAIL: $t"; done`
Expected: every test prints `OK` (no FAIL lines). `tests/test_zbuffer_raster.py` needs goldens; if it fails on missing goldens only, run it once with `--regen` per CLAUDE.md.

- [ ] **Step 7: Commit**

```bash
git add main.py tests/
git commit -m "main: darwin defaults to the Cocoa frontend; tkinter import survives absence"
```

---

### Task 5: Manual smoke run + fix fallout

**Files:**
- Possibly modify: `mac_cocoa.py`, `mac_ui.py` (whatever the smoke shakes out)

- [ ] **Step 1: Launch the game**

Run: `python3 main.py e1m1` (foreground; this opens a window and grabs the screen — run it for ~20s, then quit via Esc menu or the console `quit`).

Verify, in order:
1. Window appears, becomes key, title is "pq.ai cocoa — e1m1".
2. Textured (zbuf) view renders correctly (not vertically mirrored — the
   draw_fb unflip), aspect-correct with bars on an off-ratio window size.
3. WASD moves, click grabs the mouse (cursor hides, look works smoothly,
   cursor stays pinned), Tab releases.
4. Z/T/F toggle render modes: wireframe segs, flat polys draw y-down correctly.
5. F1 console opens/closes; typing, tab-completion, `map e1m2` works; Esc menu
   opens, arrows navigate, resolution change works.
6. 1-8 weapon select, Ctrl/click fires, P shows profiler bars (Menlo block glyphs).
7. Window close button quits cleanly (audio stops, no traceback); Cmd-Q too.
8. Sound plays.

- [ ] **Step 2: Fix anything broken, re-run, commit fixes**

```bash
git add -A
git commit -m "mac_cocoa: smoke-run fixes"
```

---

### Task 6: Documentation (README.md, CLAUDE.md)

**Files:**
- Modify: `README.md` (identity line, run instructions, architecture table)
- Modify: `CLAUDE.md` ("What this is", Commands, Architecture diagram)

- [ ] **Step 1: Update CLAUDE.md**

- "What this is": change "`tkinter` is the only non-stdlib dependency" to
  "pure Python standard library engine; the only non-stdlib dependencies are
  UI frontends — PyObjC for the native macOS frontend, tkinter for the
  fallback/Linux frontend".
- Commands: `python main.py e1m1   # run the game (gdi32 on Windows, Cocoa on
  macOS, tkinter elsewhere)`; note `--tk` forces tkinter on Windows AND macOS;
  note the PyObjC install line for macOS.
- Architecture diagram: add two rows mirroring the win_gdi/win_ui rows:

```
mac_cocoa.py        Cocoa macOS frontend (default on macOS): NSEvent pump loop, relative-delta
                      mouselook + cursor grab, CoreGraphics drawing in drawRect (fb image blit,
                      segs/polys, AppKit text)
mac_ui.py           macOS UI helpers: pure half (keycode map, RGBA expansion, particle fit;
                      unit-tested in tests/test_mac_ui.py) + CG drawing half (fb CGImage,
                      vector/text/console/menu drawing) used by mac_cocoa.py
```

- main.py row: "tkinter frontend (fallback: --tk anywhere, default on Linux)".

- [ ] **Step 2: Update README.md**

Same identity + run-instruction changes, wherever README states "tkinter is the
only non-stdlib dependency" and in its frontend/architecture sections (read it
first; keep its voice).

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: record the Cocoa macOS frontend (PyObjC) and tkinter's fallback role"
```

---

### Task 7: Final verification

- [ ] **Step 1: Full test suite**

Run: `export PQ_AUDIO=0; for t in tests/test_*.py; do python3 "$t" || echo "FAIL: $t"; done`
Expected: all `OK`.

- [ ] **Step 2: Frontend dispatch sanity**

Run: `python3 -c "from main import select_frontend; print(select_frontend(['e1m1'], 'darwin'), select_frontend(['--tk','e1m1'], 'darwin'), select_frontend(['e1m1'], 'win32'), select_frontend(['e1m1'], 'linux'))"`
Expected: `('cocoa', 'e1m1') ('tk', 'e1m1') ('gdi', 'e1m1') ('tk', 'e1m1')`

- [ ] **Step 3: One more live launch**

Run: `python3 main.py e1m1` briefly; quit via console `quit`.

- [ ] **Step 4: Commit any stragglers**
