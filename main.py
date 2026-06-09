"""Pure-Python Quake wireframe walker. tkinter is the only non-stdlib dependency.

Loads the shareware data, parses a real Quake level, and lets you fly/walk through
it as wireframe 3D drawn with tkinter Canvas lines.

    python3 main.py [mapname]      e.g. python3 main.py e1m1

Controls:
    WASD            move          mouse        look (click window to capture)
    left / right    turn          Space / C    up / down
    Space           jump (walk) / up (noclip)  Shift   move faster
    N               toggle noclip flight        Tab    toggle mouselook
    F               toggle flat shading         Esc    release mouse / quit
    Z               toggle z-buffer (textured)  T      toggle texturing

This is a THIN tkinter frontend: it owns the window, the Tk canvas item pools and
the WARP-based mouselook; all game logic lives in client.Client, which returns a
RenderFrame each tick that this draws. The Client is UI-agnostic (no tkinter).
"""

import ctypes
import sys
import time
import tkinter as tk

from quake.render import ZBUF_SCALE

from client import Client, InputState

MOUSE_MARGIN = 100         # px from a window edge that triggers a recenter warp

# HUD/crosshair font: a fixed-width face that exists on each OS (Menlo ships on
# macOS, Consolas on Windows; Tk falls back to a default monospace elsewhere).
HUD_FONT = ("Menlo" if sys.platform == "darwin" else
            "Consolas" if sys.platform == "win32" else "TkFixedFont")

LINE_COLOR = "#00ff66"
PREGROW = 2048             # line items pre-created up front to avoid hitches
PREGROW_POLY = 768         # polygon items pre-created for flat-shading mode
PREGROW_PART = 256         # point-sprite items for particles


def look_delta(last, x, y, w, h, margin):
    """Per-event mouselook delta with a recenter-warp guard. `last` is the previous
    cursor (x, y) or None; returns (newlast, dx, dy) -- the pixel deltas to apply to
    yaw/pitch, or 0 when the event must not move the view.

    Mouselook recenters the cursor (a warp) when it nears a window edge. On Windows
    that warp's <Motion> arrives asynchronously and unsuppressed, so an event
    straddling the teleport reports a delta of order the window half-size -- which,
    applied, snaps the view to a random angle. A genuine move between events is
    small, so any delta at least (half - margin) px (the smallest a recenter can
    produce) is treated as a warp artifact and dropped, still re-seeding `last` so
    the stream resynchronises. (macOS masked this with post-warp event suppression.)
    """
    if last is None:
        return (x, y), 0, 0
    dx, dy = x - last[0], y - last[1]
    if abs(dx) >= w // 2 - margin or abs(dy) >= h // 2 - margin:
        return (x, y), 0, 0          # cursor teleported by a recenter -- not input
    return (x, y), dx, dy


def _make_cursor_reassociator():
    """macOS warps the cursor with CGWarpMouseCursorPosition, which suppresses
    mouse-delta events for ~0.25s afterwards — so warp-based mouselook stutters
    (the view 'clamps' at each recenter). Re-associating the mouse and cursor
    right after the warp cancels that suppression (the SDL workaround). Returns
    a no-arg callable, or None off macOS / if the framework can't be loaded."""
    if sys.platform != "darwin":
        return None
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices."
                         "framework/ApplicationServices")
        fn = cg.CGAssociateMouseAndMouseCursorPosition
        fn.argtypes = [ctypes.c_int]
        fn.restype = ctypes.c_int
        return lambda: fn(True)
    except OSError:
        return None


_reassociate_cursor = _make_cursor_reassociator()


class App:
    def __init__(self, mapname):
        # all game logic + the engine stack lives in the UI-agnostic Client
        self.client = Client(mapname)

        # window
        self.root = tk.Tk()
        self.root.title(f"pq.ai — {mapname}")
        self.root.geometry("800x600")
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        # z-buffer mode blits a software framebuffer here. Created first so it
        # sits at the bottom of the stack (lines/polys/particles/HUD draw above);
        # hidden until the mode is on. self.fb_photo holds the live PhotoImage.
        self.fb_photo = None
        self.fb_item = self.canvas.create_image(0, 0, anchor="nw", state="hidden")
        # reusable line-item pool; unused items are parked off-screen with a
        # cheap coords() call (no itemconfig state churn, no extra item count)
        self.pool = [self.canvas.create_line(-10, -10, -10, -10, fill=LINE_COLOR)
                     for _ in range(PREGROW)]
        self.prev_n = 0
        # filled-polygon pool for flat-shading mode (drawn back-to-front)
        self.polypool = [self.canvas.create_polygon(
            -10, -10, -10, -10, -10, -10, outline="", fill="#000000")
            for _ in range(PREGROW_POLY)]
        self.polyfill = [None] * PREGROW_POLY
        self.poly_prev = 0
        # point-sprite pool for particles (teleport fog, fireball trails)
        self.partpool = [self.canvas.create_rectangle(
            -10, -10, -8, -8, outline="", fill="#ffffff")
            for _ in range(PREGROW_PART)]
        self.partfill = [None] * PREGROW_PART
        self.part_prev = 0
        self.hud = self.canvas.create_text(
            8, 8, anchor="nw", fill="#00ff66", font=(HUD_FONT, 11), text="")
        self.crosshair = self.canvas.create_text(
            0, 0, fill="#00ff66", font=(HUD_FONT, 18), text="+")
        self.center_text = self.canvas.create_text(
            0, 0, fill="#ffff00", font=(HUD_FONT, 16, "bold"), text="",
            justify="center")
        # bottom status bar: health / armor / ammo (Quake-style readout)
        self.statusbar = self.canvas.create_text(
            0, 0, anchor="sw", fill="#ffcc00", font=(HUD_FONT, 16, "bold"), text="")

        # frontend input state
        self.keys = set()
        self.mouselook = False
        self._last_mouse = None
        self._look_accum = (0.0, 0.0)       # mouse deltas since the last frame
        self.last_t = time.perf_counter()
        # fire (button0) comes from two inputs -- the mouse and the Ctrl key --
        # OR'd together so releasing one doesn't cancel the other.
        self.fire_mouse = False
        self.fire_key = False
        # one-shot queues drained into the next InputState
        self._cmd_queue = set()             # mode toggles: noclip/flat/zbuf/texture
        self._pending_impulse = 0           # weapon-select keypress

        self._bind()
        self.canvas.focus_set()
        self.root.after(16, self.tick)

    # ---- input ----
    def _bind(self):
        r = self.root
        r.bind("<KeyPress>", self._keydown)
        r.bind("<KeyRelease>", self._keyup)
        r.bind("<Motion>", self._motion)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<ButtonRelease-1>", self._release)

    def _input(self):
        """Build one frame of InputState from held keys + accumulators."""
        keys = self.keys
        fwd = (("w" in keys or "up" in keys) -
               ("s" in keys or "down" in keys))
        strafe = ("d" in keys) - ("a" in keys)
        rise = ("space" in keys) - ("c" in keys)
        turn = ("right" in keys) - ("left" in keys)
        run = "shift_l" in keys or "shift_r" in keys

        look_dx, look_dy = self._look_accum
        self._look_accum = (0.0, 0.0)

        fire = self.fire_mouse or self.fire_key
        impulse = self._pending_impulse
        self._pending_impulse = 0
        commands = frozenset(self._cmd_queue)
        self._cmd_queue.clear()

        return InputState(
            move_forward=fwd, move_strafe=strafe, move_up=rise, turn=turn,
            look_dx=look_dx, look_dy=look_dy, run=run, fire=fire,
            impulse=impulse, commands=commands, mouselook=self.mouselook)

    def _click(self, e):
        # first click captures the mouse; while captured, hold to fire (button0).
        # The QC weapon frame handles per-weapon cadence, ammo and animation.
        if not self.mouselook:
            self._set_mouselook(True)
        else:
            self.fire_mouse = True

    def _release(self, e):
        self.fire_mouse = False

    def _keydown(self, e):
        k = e.keysym.lower()
        if k == "escape":
            if self.mouselook:
                self._set_mouselook(False)
            else:
                self.root.destroy()
            return
        if k == "tab":
            self._set_mouselook(not self.mouselook)
            return
        if k == "n":
            self._cmd_queue.add("noclip")
            return
        if k == "f":
            self._cmd_queue.add("flat")
            return
        if k == "z":
            self._cmd_queue.add("zbuf")
            return
        if k == "t":
            self._cmd_queue.add("texture")
            return
        if len(k) == 1 and "1" <= k <= "8":   # select a weapon (Quake impulse 1-8)
            self._pending_impulse = int(k)
            return
        if k == "control_l" or k == "control_r":   # Ctrl fires (Quake +attack)
            self.fire_key = True
            return
        self.keys.add(k)

    def _keyup(self, e):
        k = e.keysym.lower()
        if k == "control_l" or k == "control_r":
            self.fire_key = False
            return
        self.keys.discard(k)

    def _set_mouselook(self, on):
        self.mouselook = on
        if not on:
            self.fire_mouse = False       # releasing the mouse stops mouse-firing
        self.canvas.config(cursor="none" if on else "")
        if on:
            self._last_mouse = None
            self._warp_center()

    def _warp_center(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        # Seed _last_mouse to the centre before generating the event. On macOS the
        # warp's <Motion> is delivered synchronously (re-entering _motion), so this
        # makes it a zero-delta no-op. On Windows the warp event arrives async and
        # the cursor teleports under us; look_delta's jump guard is what actually
        # absorbs the straddle there -- this seed just keeps the macOS path clean.
        self._last_mouse = (w // 2, h // 2)
        self.canvas.event_generate("<Motion>", warp=True,
                                   x=w // 2, y=h // 2)
        # cancel macOS's post-warp event suppression so turning stays smooth
        if _reassociate_cursor is not None:
            _reassociate_cursor()

    def _motion(self, e):
        if not self.mouselook:
            return
        # Accumulate deltas from the previous cursor position rather than from the
        # centre: we only recenter near a window edge (rarely), so motion stays
        # smooth in between. look_delta drops the window-scale delta a recenter
        # warp injects (async + unsuppressed on Windows) so the view can't snap.
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        self._last_mouse, dx, dy = look_delta(self._last_mouse, e.x, e.y, w, h,
                                              MOUSE_MARGIN)
        if dx or dy:
            ax, ay = self._look_accum
            self._look_accum = (ax + dx, ay + dy)
        if (e.x < MOUSE_MARGIN or e.x > w - MOUSE_MARGIN or
                e.y < MOUSE_MARGIN or e.y > h - MOUSE_MARGIN):
            self._warp_center()

    # ---- main loop ----
    def tick(self):
        now = time.perf_counter()
        dt = now - self.last_t
        self.last_t = now
        self.client.resize(self.canvas.winfo_width(), self.canvas.winfo_height())
        rf = self.client.frame(dt, self._input())
        self._draw_frame(rf)
        work_ms = (time.perf_counter() - now) * 1000
        self.root.after(max(1, int(16 - work_ms)), self.tick)

    def _draw_frame(self, rf):
        """Draw one RenderFrame: dispatch the geometry by mode, then particles and
        the text overlays / crosshair."""
        if rf.mode == "wire":
            self._draw(rf.segs)
            self._park(self.polypool, self.poly_prev, 6); self.poly_prev = 0
            self.canvas.itemconfig(self.fb_item, state="hidden")
        elif rf.mode == "flat":
            self._draw_polys(rf.polys)
            self._park(self.pool, self.prev_n, 4); self.prev_n = 0
            self.canvas.itemconfig(self.fb_item, state="hidden")
        else:                                # 'zbuf'
            self._draw_fb(rf.framebuffer)
            self._park(self.pool, self.prev_n, 4); self.prev_n = 0
            self._park(self.polypool, self.poly_prev, 6); self.poly_prev = 0
            self.canvas.itemconfig(self.fb_item, state="normal")

        self._draw_particles(rf.particles)

        # route overlays to the three Tk text items by anchor; any item with no
        # overlay this frame is cleared to "".
        by_anchor = {"nw": self.hud, "sw": self.statusbar, "center": self.center_text}
        seen = set()
        for x, y, text, rgb, anchor in rf.overlays:
            item = by_anchor.get(anchor)
            if item is None:
                continue
            self.canvas.coords(item, x, y)
            self.canvas.itemconfig(item, text=text, fill="#%02x%02x%02x" % rgb)
            seen.add(anchor)
        for anchor, item in by_anchor.items():
            if anchor not in seen:
                self.canvas.itemconfig(item, text="")

        self.canvas.coords(self.crosshair, *rf.crosshair)
        self.canvas.tag_raise(self.hud)
        self.canvas.tag_raise(self.crosshair)
        self.canvas.tag_raise(self.center_text)
        self.canvas.tag_raise(self.statusbar)

    def _draw(self, segs):
        c = self.canvas
        pool = self.pool
        coords = c.coords
        n = len(segs)
        while len(pool) < n:
            pool.append(c.create_line(-10, -10, -10, -10, fill=LINE_COLOR))
        for i in range(n):
            x0, y0, x1, y1 = segs[i]
            coords(pool[i], x0, y0, x1, y1)
        for i in range(n, self.prev_n):      # park last frame's surplus off-screen
            coords(pool[i], -10, -10, -10, -10)
        self.prev_n = n

    def _draw_particles(self, particles):
        """Draw the precomputed particle sprites (Client projected/sized/occluded
        them already): a list of (x, y, half, (r,g,b)). Sizes the reusable rect
        pool to the list and parks last frame's surplus off-screen."""
        c = self.canvas
        pool = self.partpool
        fillc = self.partfill
        coords = c.coords
        itemconfig = c.itemconfig
        n = 0
        for x, y, half, rgb in particles:
            if n >= len(pool):
                pool.append(c.create_rectangle(-10, -10, -8, -8,
                                               outline="", fill="#ffffff"))
                fillc.append(None)
            coords(pool[n], x - half, y - half, x + half, y + half)
            r, g, b = rgb
            col = f"#{r:02x}{g:02x}{b:02x}"
            if fillc[n] != col:
                itemconfig(pool[n], fill=col)
                fillc[n] = col
            n += 1
        for i in range(n, self.part_prev):       # park last frame's surplus
            coords(pool[i], -10, -10, -8, -8)
        self.part_prev = n

    def _draw_polys(self, polys):
        c = self.canvas
        pool = self.polypool
        fillc = self.polyfill
        coords = c.coords
        itemconfig = c.itemconfig
        n = len(polys)
        while len(pool) < n:
            pool.append(c.create_polygon(-10, -10, -10, -10, -10, -10,
                                         outline="", fill="#000000"))
            fillc.append(None)
        for i in range(n):
            flat, col = polys[i]
            coords(pool[i], *flat)
            if fillc[i] != col:              # only re-set fill when it changes
                itemconfig(pool[i], fill=col)
                fillc[i] = col
        for i in range(n, self.poly_prev):   # park surplus (degenerate triangle)
            coords(pool[i], -10, -10, -10, -10, -10, -10)
        self.poly_prev = n

    def _draw_fb(self, fbdata):
        """Wrap the renderer's raw RGB framebuffer in a PPM PhotoImage, scale it
        up to fill the window (chunky pixels), and show it on the canvas image
        item. A fresh PhotoImage per frame -- cheap; the costly part is the
        per-pixel fill the renderer already did."""
        fb, w, h = fbdata
        ppm = b"P6 %d %d 255 " % (w, h) + bytes(fb)
        photo = tk.PhotoImage(data=ppm, format="ppm")
        if ZBUF_SCALE > 1:
            photo = photo.zoom(ZBUF_SCALE)
        self.canvas.itemconfig(self.fb_item, image=photo)
        self.fb_photo = photo            # keep a ref so Tk doesn't GC the pixels
        self.canvas.tag_lower(self.fb_item)

    def _park(self, pool, used, ncoords):
        """Move the first `used` items of a pool off-screen (on mode switch)."""
        coords = self.canvas.coords
        off = (-10,) * ncoords
        for i in range(used):
            coords(pool[i], *off)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    mapname = sys.argv[1] if len(sys.argv) > 1 else "e1m1"
    App(mapname).run()
