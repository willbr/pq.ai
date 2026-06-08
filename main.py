"""Pure-Python Quake wireframe walker. tkinter is the only non-stdlib dependency.

Loads the shareware data, parses a real Quake level, and lets you fly/walk through
it as wireframe 3D drawn with tkinter Canvas lines.

    python3 main.py [mapname]      e.g. python3 main.py e1m1

Controls:
    WASD            move          mouse        look (click window to capture)
    left / right    turn          up / down    forward / back
    Space           jump (walk) / up (noclip)  Shift   move faster
    N               toggle noclip flight        Tab    toggle mouselook
    F               toggle flat shading         Esc    release mouse / quit
"""

import ctypes
import math
import sys
import time
import tkinter as tk

from pak import Pak
from bsp import Bsp
from render import Renderer, angle_vectors
from physics import Physics, VIEW_HEIGHT, MAXSPEED
from progs import Progs
from sv import Server
from mdl import Mdl

PAK_PATH = "quake-shareware/id1/pak0.pak"
SV_TICK = 0.1              # server runs the QC at a fixed 10 Hz (like Quake)
NOCLIP_SPEED = 500.0       # units / second when flying
LOOK_SENS = 0.15           # degrees / pixel
YAW_SPEED = 140.0          # degrees / second (keyboard turning)

LINE_COLOR = "#00ff66"
PREGROW = 2048             # line items pre-created up front to avoid hitches
PREGROW_POLY = 768         # polygon items pre-created for flat-shading mode


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
        pak = Pak(PAK_PATH)
        path = f"maps/{mapname}.bsp"
        if path not in pak.files:
            sys.exit(f"no such map: {path}")
        self.bsp = Bsp(pak.read(path))
        pal = pak.read("gfx/palette.lmp")
        palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]
        self.rend = Renderer(self.bsp, palette)
        self.phys = Physics(self.bsp)

        # QuakeC server: spawn the level's entities and run their logic. Doors,
        # buttons and lifts are entities now; their brush models are drawn at the
        # origins the QC sets, and invisible triggers no longer render.
        self.sv = Server(Progs(pak.read("progs.dat")), bsp=self.bsp, mapname=path,
                         physics=self.phys)
        self.sv.load_level()
        self.sv_accum = 0.0

        # load the .mdl models the level precached, indexed to match modelindex
        self.models = [None] * len(self.sv.model_precache)
        for idx, name in enumerate(self.sv.model_precache):
            if name.endswith(".mdl") and name in pak.files:
                try:
                    self.models[idx] = Mdl(pak.read(name), palette)
                except Exception as e:
                    print(f"mdl load failed for {name}: {e}")

        # player origin from the level's spawn point (eye sits VIEW_HEIGHT above)
        (sx, sy, sz), yaw = self.bsp.find_spawn()
        self.pos = [sx, sy, sz]
        self.vel = [0.0, 0.0, 0.0]
        self.onground = False
        self.noclip = False
        self.yaw = yaw
        self.pitch = 0.0

        # a client edict driven by the camera: gives monsters a target and gives
        # fired shots an attacker (so QC's damage/death logic runs)
        self.sv.spawn_player(tuple(self.pos), (self.pitch, self.yaw, 0.0))
        self.last_fire = 0.0

        # window
        self.root = tk.Tk()
        self.root.title(f"pq.ai — {mapname}")
        self.root.geometry("800x600")
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        # reusable line-item pool; unused items are parked off-screen with a
        # cheap coords() call (no itemconfig state churn, no extra item count)
        self.pool = [self.canvas.create_line(-10, -10, -10, -10, fill=LINE_COLOR)
                     for _ in range(PREGROW)]
        self.prev_n = 0
        # filled-polygon pool for flat-shading mode (drawn back-to-front)
        self.flat = True
        self.polypool = [self.canvas.create_polygon(
            -10, -10, -10, -10, -10, -10, outline="", fill="#000000")
            for _ in range(PREGROW_POLY)]
        self.polyfill = [None] * PREGROW_POLY
        self.poly_prev = 0
        self.hud = self.canvas.create_text(
            8, 8, anchor="nw", fill="#00ff66", font=("Menlo", 11), text="")
        self.crosshair = self.canvas.create_text(
            0, 0, fill="#00ff66", font=("Menlo", 18), text="+")

        # input state
        self.keys = set()
        self.mouselook = False
        self._last_mouse = None
        self.last_t = time.perf_counter()
        self.fps = 0.0

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
        # bind on the canvas (not root) and use the event's own size: at startup
        # the canvas may not be laid out when the root's first <Configure> fires,
        # so winfo_width() would read 1 and the projection would collapse.
        self.canvas.bind("<Configure>", self._resize)

    def _click(self, e):
        # first click captures the mouse; clicks while captured fire the shotgun
        if not self.mouselook:
            self._set_mouselook(True)
        else:
            self._fire()

    def _fire(self):
        now = time.perf_counter()
        if now - self.last_fire < 0.4:        # shotgun cadence
            return
        self.last_fire = now
        self.sv.fire()

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
            self.noclip = not self.noclip
            self.vel = [0.0, 0.0, 0.0]
            return
        if k == "f":
            self.flat = not self.flat
            if self.flat:
                self._park(self.pool, self.prev_n, 4); self.prev_n = 0
            else:
                self._park(self.polypool, self.poly_prev, 6); self.poly_prev = 0
            return
        self.keys.add(k)

    def _keyup(self, e):
        self.keys.discard(e.keysym.lower())

    def _set_mouselook(self, on):
        self.mouselook = on
        self.canvas.config(cursor="none" if on else "")
        if on:
            self._last_mouse = None
            self._warp_center()

    def _warp_center(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        # record the centre BEFORE generating the event: Tk delivers the warp's
        # <Motion> synchronously, re-entering _motion, so _last_mouse must
        # already be the centre or that event computes a movement-cancelling
        # delta (the view snaps back toward where it started).
        self._last_mouse = (w // 2, h // 2)
        self.canvas.event_generate("<Motion>", warp=True,
                                   x=w // 2, y=h // 2)
        # cancel macOS's post-warp event suppression so turning stays smooth
        if _reassociate_cursor is not None:
            _reassociate_cursor()

    def _motion(self, e):
        if not self.mouselook:
            return
        # accumulate deltas from the previous cursor position rather than from
        # the centre. macOS suppresses mouse-motion events for ~250ms after a
        # programmatic warp, so warping every frame eats most of the movement;
        # instead we only recenter near the window edge (rarely), keeping motion
        # smooth in between.
        if self._last_mouse is None:
            self._last_mouse = (e.x, e.y)
            return
        lx, ly = self._last_mouse
        dx, dy = e.x - lx, e.y - ly
        self._last_mouse = (e.x, e.y)
        if dx == 0 and dy == 0:
            return
        self.yaw -= dx * LOOK_SENS
        self.pitch += dy * LOOK_SENS
        self.pitch = max(-89.0, min(89.0, self.pitch))
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        margin = 100
        if e.x < margin or e.x > w - margin or e.y < margin or e.y > h - margin:
            self._warp_center()

    def _resize(self, e):
        self.rend.resize(e.width, e.height)

    # ---- movement ----
    def _wishmove(self):
        """Forward/strafe intent from keys, as -1..1 each."""
        fwd = (("w" in self.keys or "up" in self.keys) -
               ("s" in self.keys or "down" in self.keys))
        strafe = ("d" in self.keys) - ("a" in self.keys)
        return fwd, strafe

    def _move(self, dt):
        # left/right arrows turn (yaw)
        turn = ("right" in self.keys) - ("left" in self.keys)
        self.yaw -= turn * YAW_SPEED * dt

        fwd, strafe = self._wishmove()
        fast = "shift_l" in self.keys or "shift_r" in self.keys

        if self.noclip:
            # free fly along the full view direction (pitch included), no gravity
            forward, right, up = angle_vectors(self.yaw, self.pitch)
            rise = (("space" in self.keys) -
                    ("control_l" in self.keys or "control_r" in self.keys))
            speed = NOCLIP_SPEED * (3.0 if fast else 1.0) * dt
            for i in range(3):
                self.pos[i] += (forward[i] * fwd + right[i] * strafe +
                                up[i] * rise) * speed
            self.vel = [0.0, 0.0, 0.0]
            return

        # walking: build a horizontal wish direction from yaw only
        forward, right, _ = angle_vectors(self.yaw, 0.0)
        wx = forward[0] * fwd + right[0] * strafe
        wy = forward[1] * fwd + right[1] * strafe
        wl = math.hypot(wx, wy)
        if wl < 1e-6:
            wishdir, wishspeed = (0.0, 0.0, 0.0), 0.0
        else:
            wishdir = (wx / wl, wy / wl, 0.0)
            wishspeed = MAXSPEED * (1.6 if fast else 1.0)

        # clamp dt so a hitch can't tunnel the player through a wall
        step = min(dt, 0.05)
        self.onground = self.phys.player_move(
            self.pos, self.vel, wishdir, wishspeed,
            self.onground, "space" in self.keys, step)

    # ---- main loop ----
    def tick(self):
        now = time.perf_counter()
        dt = now - self.last_t
        self.last_t = now
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

        self._move(dt)

        # push the camera into the client edict so monsters target the player and
        # shots originate from the current view
        self.sv.update_player((self.pos[0], self.pos[1], self.pos[2]),
                              (self.pitch, self.yaw, 0.0))

        # advance the QC server at a fixed tick (catch up real time, capped so a
        # hitch can't trigger a spiral of death), then read back entity positions
        self.sv_accum += dt
        steps = 0
        while self.sv_accum >= SV_TICK and steps < 5:
            self.sv.run_frame(SV_TICK)
            self.sv_accum -= SV_TICK
            steps += 1
        if steps:
            self._sync_from_player()      # adopt teleports / trigger-moved origin
        brush_ents = self.sv.brush_models()
        alias_ents = self._alias_ents()

        eye = (self.pos[0], self.pos[1], self.pos[2] + VIEW_HEIGHT)
        if self.flat:
            polys, leaf = self.rend.render_shaded(eye, self.yaw, self.pitch,
                                                  brush_ents, alias_ents)
            self._draw_polys(polys)
            nprim = len(polys)
        else:
            segs, leaf = self.rend.render(eye, self.yaw, self.pitch,
                                          brush_ents, alias_ents)
            self._draw(segs)
            nprim = len(segs)

        spd = math.hypot(self.vel[0], self.vel[1])
        mode = "NOCLIP" if self.noclip else ("ground" if self.onground else "air")
        hp = self.sv.player_health()
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        self.canvas.coords(self.crosshair, w // 2, h // 2)
        self.canvas.itemconfig(
            self.hud,
            text=(f"{self.fps:5.1f} fps   "
                  f"{'polys' if self.flat else 'segs'} {nprim}   "
                  f"leaf {leaf}   {mode}   health {hp:.0f}\n"
                  f"pos {self.pos[0]:.0f} {self.pos[1]:.0f} {self.pos[2]:.0f}   "
                  f"spd {spd:.0f}   yaw {self.yaw:.0f} pitch {self.pitch:.0f}   "
                  f"{'MOUSELOOK — click to fire' if self.mouselook else 'click to capture mouse'} "
                  f"[N]oclip [F]lat"))
        self.canvas.tag_raise(self.hud)
        self.canvas.tag_raise(self.crosshair)
        # target ~60 fps: cap fast maps (saves CPU), never throttle slow ones
        work_ms = (time.perf_counter() - now) * 1000
        self.root.after(max(1, int(16 - work_ms)), self.tick)

    def _sync_from_player(self):
        """A trigger (teleport) may have moved the player edict during the QC
        frame. We push the camera into the edict each tick, so any origin change
        is the game logic relocating us -- adopt it back into the camera."""
        org = self.sv.player_origin()
        if org is None:
            return
        if (abs(org[0] - self.pos[0]) > 1.0 or abs(org[1] - self.pos[1]) > 1.0 or
                abs(org[2] - self.pos[2]) > 1.0):
            self.pos = [org[0], org[1], org[2]]
            vel = self.sv.player_velocity()
            if vel is not None:
                self.vel = [vel[0], vel[1], vel[2]]
            ang = self.sv.player_angles()
            if ang is not None:                  # teleport sets fixangle -> face dest
                self.yaw = ang[1]
                self.pitch = max(-89.0, min(89.0, ang[0]))
            self.onground = False

    def _alias_ents(self):
        """Resolve live .mdl entities to (mdl, current-frame verts, origin, angles)."""
        out = []
        models = self.models
        nmodels = len(models)
        now = self.sv.time
        for mi, org, ang, frame in self.sv.alias_entities():
            m = models[mi] if mi < nmodels else None
            if m is None:
                continue
            out.append((m, m.frame_verts(frame, now), org, ang))
        return out

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
        c.tag_raise(self.hud)

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
