"""Pure-Python Quake wireframe walker. tkinter is the only non-stdlib dependency.

Loads the shareware data, parses a real Quake level, and lets you fly/walk through
it as wireframe 3D drawn with tkinter Canvas lines.

    python3 main.py [mapname]      e.g. python3 main.py e1m1

Controls:
    WASD            move          mouse        look (click window to capture)
    left / right    turn          Space / C    up / down
    Space           jump (walk) / up (noclip)  Shift   move faster
    N               toggle noclip flight        Tab    toggle mouselook
    F               toggle flat shading         1-8    select weapon
    Z               toggle z-buffer (textured)  T      toggle texturing
    P               toggle profiler HUD (per-frame section ms)
    F1 or `         drop-down console           Esc    overlay menu (video / quit)

This is a THIN tkinter frontend: it owns the window, the Tk canvas item pools and
the WARP-based mouselook; all game logic lives in client.Client, which returns a
RenderFrame each tick that this draws. The Client is UI-agnostic (no tkinter).
"""

import ctypes
import sys
import time
import tkinter as tk
import tkinter.font as tkfont

from quake.perf import PROFILER

from client import Client, InputState

MOUSE_MARGIN = 100         # px from a window edge that triggers a recenter warp

# HUD/crosshair font: a fixed-width face that exists on each OS. Cascadia Mono
# (Windows 11) and Menlo (macOS) carry the 1/8-block glyphs the profiler bar
# chart draws; Tk falls back to a default monospace elsewhere.
HUD_FONT = ("Menlo" if sys.platform == "darwin" else
            "Cascadia Mono" if sys.platform == "win32" else "TkFixedFont")

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


def fb_fit(win_w, win_h, fb_w, fb_h):
    """Pick how to integer-scale an fb_w x fb_h framebuffer into a win_w x win_h
    window with a Tk PhotoImage, which only scales by integer factors. Returns
    (zoom, subsample) -- apply photo.zoom(zoom).subsample(subsample); a factor of
    1 is a no-op. Chooses the largest zoom that fits while preserving aspect; if
    the framebuffer is larger than the window, subsamples to fit instead. The
    frontend centres the result, so any leftover is a letterbox border. (gdi's
    StretchDIBits fills exactly; Tk is integer-limited, so Auto resolution fills
    cleanly while a fixed low-res letterboxes.)"""
    if fb_w <= 0 or fb_h <= 0 or win_w <= 0 or win_h <= 0:
        return 1, 1
    z = min(win_w // fb_w, win_h // fb_h)
    if z >= 1:
        return z, 1
    s = max(-(-fb_w // win_w), -(-fb_h // win_h))     # ceil division -> shrink
    return 1, max(1, s)


def route_console_key(con, keysym, char):
    """Map a tkinter key event onto the console line editor while it is open.
    `keysym` is the event keysym (e.g. 'Return', 'BackSpace', 'a'); `char` is the
    event char (the printable text, or '' for named keys). Drives the matching
    con.key_* method and always returns True -- an open console swallows every
    key so it never reaches the game. The tkinter twin of win_gdi._console_key."""
    k = keysym.lower()
    if k == "escape":
        con.active = False
    elif k in ("return", "kp_enter"):
        con.key_enter()
    elif k == "backspace":
        con.key_backspace()
    elif k == "delete":
        con.key_delete()
    elif k == "tab":
        con.key_tab()
    elif k == "left":
        con.key_left()
    elif k == "right":
        con.key_right()
    elif k == "home":
        con.key_home()
    elif k == "end":
        con.key_end()
    elif k == "up":
        con.key_up()
    elif k == "down":
        con.key_down()
    elif k in ("prior", "page_up"):
        con.key_pageup()
    elif k in ("next", "page_down"):
        con.key_pagedown()
    elif char and char >= " " and char != "\x7f":
        con.key_char(char)               # printable text; con.key_char re-guards
    return True


def route_menu_key(menu, keysym):
    """Map a tkinter keysym onto the overlay menu while it is open. Drives the
    matching menu.key_* method and always returns True -- the menu swallows every
    key. The tkinter twin of win_gdi._menu_key."""
    k = keysym.lower()
    if k == "escape":
        menu.key_escape()
    elif k == "up":
        menu.key_up()
    elif k == "down":
        menu.key_down()
    elif k == "left":
        menu.key_left()
    elif k == "right":
        menu.key_right()
    elif k in ("return", "kp_enter"):
        menu.key_enter()
    return True


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
        self._pal_lut = None             # index -> 3-byte RGB, built on first use
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
        # hidden-line wireframe pool: background-filled, green-outlined polygons
        # painted back-to-front (occludes walls). Opt-in via the wire_hidden cvar,
        # so grown on demand rather than pre-allocated like the pools above.
        self.hwpool = []
        self.hw_prev = 0
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

        # drop-down console + overlay menu (the gdi32 frontend's F1/Esc panels,
        # ported to Tk canvas items). The Client owns the pure Console/Menu state
        # and hands us a draw-ready view each frame (rf.console / rf.menu); these
        # items are parked hidden until then. A monospace font object gives the
        # cell metrics the caret and panel layout need.
        self.con_font = tkfont.Font(family=HUD_FONT, size=11)
        self.con_lh = self.con_font.metrics("linespace")
        self.con_cw = self.con_font.measure("0")
        self.con_panel = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="#101018", outline="", state="hidden")
        self.con_edge = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="#00a046", outline="", state="hidden")
        self.con_scroll = self.canvas.create_text(
            0, 0, anchor="sw", fill="#c8dcc8", font=self.con_font, text="",
            state="hidden")
        self.con_input = self.canvas.create_text(
            0, 0, anchor="nw", fill="#ffffff", font=self.con_font, text="",
            state="hidden")
        self.con_caret = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="#ffffff", outline="", state="hidden")
        self.menu_panel = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="#101018", outline="", state="hidden")
        self.menu_edge = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="#00a046", outline="", state="hidden")
        self.menu_title = self.canvas.create_text(
            0, 0, anchor="nw", fill="#ffff00", font=(HUD_FONT, 11, "bold"),
            text="", state="hidden")
        self.menu_rows = []          # per-row text items, grown on demand

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
        # while a panel owns the keyboard, feed the game a do-nothing frame so the
        # world keeps ticking (console `map`, menu resolution change) but the
        # player neither moves nor fires. Mirrors win_gdi.build_input.
        if self.client.con.active or self.client.menu.active:
            return InputState(mouselook=False)
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
        # A click into an open panel does nothing (the cursor is for typing/menus).
        if self.client.con.active or self.client.menu.active:
            return
        if not self.mouselook:
            self._set_mouselook(True)
        else:
            self.fire_mouse = True

    def _release(self, e):
        self.fire_mouse = False

    def _keydown(self, e):
        k = e.keysym.lower()
        # F1 (or the Quake-style backtick, reliable where macOS eats F-keys)
        # toggles the console open AND closed -- checked first so it always wins.
        if k in ("f1", "grave"):
            self._toggle_console()
            return
        if self.client.con.active:
            route_console_key(self.client.con, e.keysym, e.char)
            return
        if self.client.menu.active:
            route_menu_key(self.client.menu, e.keysym)
            return
        if k == "escape":
            self._open_menu()         # opens the overlay menu (and releases mouse)
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
        if k == "p":
            self._cmd_queue.add("prof")
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

    def _toggle_console(self):
        """F1 / backtick: open or close the drop-down console. Opening clears held
        keys, releases the mouse so the cursor is free to type, and closes the
        overlay menu so the two panels are never up at once. Mirrors
        win_gdi._toggle_console."""
        con = self.client.con
        con.active = not con.active
        if con.active:
            self.client.menu.active = False
            self._panel_opened()

    def _open_menu(self):
        """Esc (with the console closed): open the overlay video/quit menu, clearing
        held keys and releasing the mouse. Mirrors win_gdi._open_menu."""
        self.client.menu.active = True
        self._panel_opened()

    def _panel_opened(self):
        """Shared setup when a console/menu panel takes the keyboard: drop held
        movement keys, stop firing, and ungrab the mouse so the cursor is usable."""
        self.keys.clear()
        self.fire_key = False
        self._set_mouselook(False)

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
        if self.client.quit_requested:        # console `quit`/`exit` or menu Quit
            self.root.destroy()
            return
        with PROFILER.section("present"):
            self._draw_frame(rf)
        PROFILER.frame_end()         # roll this frame's section times into the HUD readout
        work_ms = (time.perf_counter() - now) * 1000
        self.root.after(max(1, int(16 - work_ms)), self.tick)

    def _draw_frame(self, rf):
        """Draw one RenderFrame: dispatch the geometry by mode, then particles and
        the text overlays / crosshair."""
        if rf.mode == "wire":
            self._draw(rf.segs)
            self._park(self.polypool, self.poly_prev, 6); self.poly_prev = 0
            self._park(self.hwpool, self.hw_prev, 6); self.hw_prev = 0
            self.canvas.itemconfig(self.fb_item, state="hidden")
        elif rf.mode == "flat":
            self._draw_polys(rf.polys)
            self._park(self.pool, self.prev_n, 4); self.prev_n = 0
            self._park(self.hwpool, self.hw_prev, 6); self.hw_prev = 0
            self.canvas.itemconfig(self.fb_item, state="hidden")
        elif rf.mode == "wire_hidden":
            self._draw_wire_hidden(rf.polys)
            self._park(self.pool, self.prev_n, 4); self.prev_n = 0
            self._park(self.polypool, self.poly_prev, 6); self.poly_prev = 0
            self.canvas.itemconfig(self.fb_item, state="hidden")
        else:                                # 'zbuf'
            self._draw_fb(rf.framebuffer)
            self._park(self.pool, self.prev_n, 4); self.prev_n = 0
            self._park(self.polypool, self.poly_prev, 6); self.poly_prev = 0
            self._park(self.hwpool, self.hw_prev, 6); self.hw_prev = 0
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

        # console / menu panels draw on top of everything (or hide when closed)
        self._draw_console(rf.console)
        self._draw_menu(rf.menu)

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

    def _draw_wire_hidden(self, polys):
        """Hidden-line wireframe: paint each face as a background-filled (black),
        green-outlined polygon back-to-front, so near faces occlude far ones
        (painter's). polys is render_shaded's (flat_coords, color) list -- the
        fill colour is ignored; every face uses the same black fill + green edge.
        Like _draw_polys but with a fixed style and no per-poly fill caching."""
        c = self.canvas
        pool = self.hwpool
        coords = c.coords
        n = len(polys)
        while len(pool) < n:
            pool.append(c.create_polygon(-10, -10, -10, -10, -10, -10,
                                         outline=LINE_COLOR, fill="#000000"))
        for i in range(n):
            coords(pool[i], *polys[i][0])
        for i in range(n, self.hw_prev):     # park last frame's surplus
            coords(pool[i], -10, -10, -10, -10, -10, -10)
        self.hw_prev = n

    def _draw_fb(self, fbdata):
        """Expand the renderer's 8-bit palette-indexed framebuffer to RGB via a
        256-entry lookup (one C-level map+join), wrap it in a PPM PhotoImage,
        scale it to the *window* with the largest integer factor that fits
        (fb_fit -- the framebuffer is a fixed render resolution, not window//4),
        and centre it on the canvas (letterbox). A fresh PhotoImage per frame --
        cheap; the costly part is the per-pixel fill the renderer already did."""
        fb, w, h = fbdata
        lut = self._pal_lut
        if lut is None:
            lut = self._pal_lut = [bytes(c) for c in self.client.palette]
        ppm = b"P6 %d %d 255 " % (w, h) + b"".join(map(lut.__getitem__, fb))
        photo = tk.PhotoImage(data=ppm, format="ppm")
        W = self.canvas.winfo_width()
        H = self.canvas.winfo_height()
        zoom, sub = fb_fit(W, H, w, h)
        if zoom > 1:
            photo = photo.zoom(zoom)
        if sub > 1:
            photo = photo.subsample(sub)
        # centre the scaled image in the window (letterbox the remainder)
        dw, dh = photo.width(), photo.height()
        self.canvas.coords(self.fb_item, max(0, (W - dw) // 2),
                           max(0, (H - dh) // 2))
        self.canvas.itemconfig(self.fb_item, image=photo)
        self.fb_photo = photo            # keep a ref so Tk doesn't GC the pixels
        self.canvas.tag_lower(self.fb_item)

    def _draw_console(self, console):
        """Draw the drop-down console panel from the Client's view tuple
        (lines, input_line, cursor_col), or hide it when None. A dark band across
        the top ~40% of the window; scrollback bottom-aligned just above the
        `] input` line; a caret bar at the cursor column. Mirrors
        win_ui.draw_console (monospace cell metrics from self.con_font)."""
        c = self.canvas
        items = (self.con_panel, self.con_edge, self.con_scroll,
                 self.con_input, self.con_caret)
        if console is None:
            for it in items:
                c.itemconfig(it, state="hidden")
            return
        lines, input_line, cursor_col = console
        w = c.winfo_width()
        h = c.winfo_height()
        lh, cw = self.con_lh, self.con_cw
        panel_h = h * 2 // 5
        iy = panel_h - lh - 4                 # top of the input line
        c.coords(self.con_panel, 0, 0, w, panel_h)
        c.coords(self.con_edge, 0, panel_h - 2, w, panel_h)
        # scrollback: south-west anchored at the input top, so the block grows
        # upward with the newest line resting just above the input line
        c.coords(self.con_scroll, 6, iy)
        c.itemconfig(self.con_scroll, text="\n".join(lines))
        c.coords(self.con_input, 6, iy)
        c.itemconfig(self.con_input, text=input_line)
        cx = 6 + cursor_col * cw
        c.coords(self.con_caret, cx, iy, cx + 1, iy + lh)
        for it in items:
            c.itemconfig(it, state="normal")
            c.tag_raise(it)

    def _draw_menu(self, view):
        """Draw the overlay menu from the Client's view (title, rows), or hide it
        when None. A centered dark panel: yellow title, one row per item, the
        selected row '> '-prefixed and brightened. Mirrors win_ui.draw_menu; the
        per-row text items grow on demand like the geometry pools."""
        c = self.canvas
        if view is None:
            for it in (self.menu_panel, self.menu_edge, self.menu_title):
                c.itemconfig(it, state="hidden")
            for it in self.menu_rows:
                c.itemconfig(it, state="hidden")
            return
        title, rows = view
        w = c.winfo_width()
        h = c.winfo_height()
        lh = self.con_lh
        panel_w = 360
        panel_h = (len(rows) + 2) * lh + 24
        x0 = (w - panel_w) // 2
        y0 = (h - panel_h) // 2
        c.coords(self.menu_panel, x0, y0, x0 + panel_w, y0 + panel_h)
        c.coords(self.menu_edge, x0, y0 + panel_h - 2, x0 + panel_w, y0 + panel_h)
        c.coords(self.menu_title, x0 + 16, y0 + 12)
        c.itemconfig(self.menu_title, text=title, state="normal")
        y = y0 + 12 + 2 * lh                 # rows start one blank line below title
        for i, (label, value, selected) in enumerate(rows):
            while i >= len(self.menu_rows):
                self.menu_rows.append(c.create_text(
                    0, 0, anchor="nw", font=self.con_font, text=""))
            text = label if not value else f"{label}: {value}"
            text = ("> " if selected else "  ") + text
            item = self.menu_rows[i]
            c.coords(item, x0 + 16, y)
            c.itemconfig(item, text=text,
                         fill="#ffffff" if selected else "#a0c8a0", state="normal")
            y += lh
        for j in range(len(rows), len(self.menu_rows)):
            c.itemconfig(self.menu_rows[j], state="hidden")
        c.itemconfig(self.menu_panel, state="normal")
        c.itemconfig(self.menu_edge, state="normal")
        # restack panel first (lowest), then title + rows on top (tag_raise on a
        # still-hidden surplus row is harmless -- it stays hidden)
        for it in (self.menu_panel, self.menu_edge, self.menu_title,
                   *self.menu_rows):
            c.tag_raise(it)

    def _park(self, pool, used, ncoords):
        """Move the first `used` items of a pool off-screen (on mode switch)."""
        coords = self.canvas.coords
        off = (-10,) * ncoords
        for i in range(used):
            coords(pool[i], *off)

    def run(self):
        self.root.mainloop()


def select_frontend(argv, platform):
    """Pick the frontend and map from CLI args. Windows defaults to the gdi32
    frontend (win_gdi) for its own message loop + raw mouselook; `--tk` forces the
    tkinter frontend, which is also the default everywhere else."""
    args = [a for a in argv if a != "--tk"]
    mapname = args[0] if args else "e1m1"
    use_tk = "--tk" in argv or platform != "win32"
    return ("tk" if use_tk else "gdi", mapname)


if __name__ == "__main__":
    frontend, mapname = select_frontend(sys.argv[1:], sys.platform)
    if frontend == "gdi":
        import win_gdi
        win_gdi.run(mapname)
    else:
        App(mapname).run()
