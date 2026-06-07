"""Pure-Python Quake wireframe walker. tkinter is the only non-stdlib dependency.

Loads the shareware data, parses a real Quake level, and lets you fly/walk through
it as wireframe 3D drawn with tkinter Canvas lines.

    python3 main.py [mapname]      e.g. python3 main.py e1m1

Controls:
    WASD / arrows   move          mouse        look (click window to capture)
    Space / Ctrl    up / down     Shift        move faster
    Tab             toggle mouselook            Esc   release mouse / quit
"""

import sys
import time
import tkinter as tk

from pak import Pak
from bsp import Bsp
from render import Renderer

PAK_PATH = "quake-shareware/id1/pak0.pak"
EYE_HEIGHT = 22.0          # Quake viewheight
MOVE_SPEED = 400.0         # units / second
LOOK_SENS = 0.15           # degrees / pixel


class App:
    def __init__(self, mapname):
        pak = Pak(PAK_PATH)
        path = f"maps/{mapname}.bsp"
        if path not in pak.files:
            sys.exit(f"no such map: {path}")
        self.bsp = Bsp(pak.read(path))
        self.rend = Renderer(self.bsp)

        # camera from the level's spawn point
        (sx, sy, sz), yaw = self.bsp.find_spawn()
        self.pos = [sx, sy, sz + EYE_HEIGHT]
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
    def _move(self, dt):
        from render import angle_vectors
        forward, right, up = angle_vectors(self.yaw, self.pitch)
        speed = MOVE_SPEED * (3.0 if "shift_l" in self.keys or
                              "shift_r" in self.keys else 1.0) * dt
        fwd = (("w" in self.keys or "up" in self.keys) -
               ("s" in self.keys or "down" in self.keys))
        strafe = (("d" in self.keys or "right" in self.keys) -
                  ("a" in self.keys or "left" in self.keys))
        rise = (("space" in self.keys) - ("control_l" in self.keys or
                                          "control_r" in self.keys))
        for i in range(3):
            self.pos[i] += (forward[i] * fwd + right[i] * strafe +
                            up[i] * rise) * speed

    # ---- main loop ----
    def tick(self):
        now = time.perf_counter()
        dt = now - self.last_t
        self.last_t = now
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

        self._move(dt)
        segs, leaf = self.rend.render(self.pos, self.yaw, self.pitch)
        self._draw(segs)

        self.canvas.itemconfig(
            self.hud,
            text=(f"{self.fps:5.1f} fps   segs {len(segs)}   leaf {leaf}\n"
                  f"pos {self.pos[0]:.0f} {self.pos[1]:.0f} {self.pos[2]:.0f}   "
                  f"yaw {self.yaw:.0f} pitch {self.pitch:.0f}   "
                  f"{'MOUSELOOK' if self.mouselook else 'click to capture mouse'}"))
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
