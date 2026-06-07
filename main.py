"""Pure-Python Quake wireframe walker. tkinter is the only non-stdlib dependency.

Loads the shareware data, parses a real Quake level, and lets you fly/walk through
it as wireframe 3D drawn with tkinter Canvas lines.

    python3 main.py [mapname]      e.g. python3 main.py e1m1

Controls:
    WASD            move          mouse        look (click window to capture)
    left / right    turn          up / down    forward / back
    Space           jump (walk) / up (noclip)  Shift   move faster
    N               toggle noclip flight        Tab    toggle mouselook
    Esc             release mouse / quit
"""

import math
import sys
import time
import tkinter as tk

from pak import Pak
from bsp import Bsp
from render import Renderer, angle_vectors
from physics import Physics, VIEW_HEIGHT, MAXSPEED

PAK_PATH = "quake-shareware/id1/pak0.pak"
NOCLIP_SPEED = 500.0       # units / second when flying
LOOK_SENS = 0.15           # degrees / pixel
YAW_SPEED = 140.0          # degrees / second (keyboard turning)


class App:
    def __init__(self, mapname):
        pak = Pak(PAK_PATH)
        path = f"maps/{mapname}.bsp"
        if path not in pak.files:
            sys.exit(f"no such map: {path}")
        self.bsp = Bsp(pak.read(path))
        self.rend = Renderer(self.bsp)
        self.phys = Physics(self.bsp)

        # player origin from the level's spawn point (eye sits VIEW_HEIGHT above)
        (sx, sy, sz), yaw = self.bsp.find_spawn()
        self.pos = [sx, sy, sz]
        self.vel = [0.0, 0.0, 0.0]
        self.onground = False
        self.noclip = False
        self.yaw = yaw
        self.pitch = 0.0

        # window
        self.root = tk.Tk()
        self.root.title(f"pq.ai — {mapname}")
        self.root.geometry("800x600")
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.lines = []            # reusable Canvas line item ids
        self.prev_used = 0
        self.hud = self.canvas.create_text(
            8, 8, anchor="nw", fill="#00ff66", font=("Menlo", 11), text="")

        # input state
        self.keys = set()
        self.mouselook = False
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
        self.canvas.bind("<Button-1>", lambda e: self._set_mouselook(True))
        r.bind("<Configure>", self._resize)

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
        self.keys.add(k)

    def _keyup(self, e):
        self.keys.discard(e.keysym.lower())

    def _set_mouselook(self, on):
        self.mouselook = on
        self.canvas.config(cursor="none" if on else "")
        if on:
            self._warp_center()

    def _warp_center(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        self.canvas.event_generate("<Motion>", warp=True,
                                   x=w // 2, y=h // 2)

    def _motion(self, e):
        if not self.mouselook:
            return
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        cx, cy = w // 2, h // 2
        dx, dy = e.x - cx, e.y - cy
        if dx == 0 and dy == 0:
            return
        self.yaw -= dx * LOOK_SENS
        self.pitch -= dy * LOOK_SENS
        self.pitch = max(-89.0, min(89.0, self.pitch))
        self._warp_center()

    def _resize(self, e):
        if e.widget is self.root:
            self.rend.resize(self.canvas.winfo_width(),
                             self.canvas.winfo_height())

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
        eye = (self.pos[0], self.pos[1], self.pos[2] + VIEW_HEIGHT)
        segs, leaf = self.rend.render(eye, self.yaw, self.pitch)
        self._draw(segs)

        spd = math.hypot(self.vel[0], self.vel[1])
        mode = "NOCLIP" if self.noclip else ("ground" if self.onground else "air")
        self.canvas.itemconfig(
            self.hud,
            text=(f"{self.fps:5.1f} fps   segs {len(segs)}   leaf {leaf}   {mode}\n"
                  f"pos {self.pos[0]:.0f} {self.pos[1]:.0f} {self.pos[2]:.0f}   "
                  f"spd {spd:.0f}   yaw {self.yaw:.0f} pitch {self.pitch:.0f}   "
                  f"{'MOUSELOOK' if self.mouselook else 'click to capture mouse'} "
                  f"[N]oclip"))
        self.canvas.tag_raise(self.hud)
        self.root.after(8, self.tick)

    def _draw(self, segs):
        c = self.canvas
        lines = self.lines
        n = len(segs)
        # grow the pool if needed
        while len(lines) < n:
            lines.append(c.create_line(0, 0, 0, 0, fill="#00ff66"))
        coords = c.coords
        for i in range(n):
            x0, y0, x1, y1 = segs[i]
            coords(lines[i], x0, y0, x1, y1)
        # hide the ones used last frame but not this frame
        if self.prev_used > n:
            itemconfig = c.itemconfig
            for i in range(n, self.prev_used):
                itemconfig(lines[i], state="hidden")
        # reveal any reused-from-hidden
        if n > self.prev_used:
            itemconfig = c.itemconfig
            for i in range(self.prev_used, n):
                itemconfig(lines[i], state="normal")
        self.prev_used = n

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    mapname = sys.argv[1] if len(sys.argv) > 1 else "e1m1"
    App(mapname).run()
