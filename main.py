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
from render import (Renderer, PickupModel, angle_vectors, ZBUF_SCALE,
                    lightstyle_values)
from physics import Physics, VIEW_HEIGHT, MAXSPEED
from progs import Progs
from sv import Server, anglemod
from mdl import Mdl, EF_ROTATE
import snd

PAK_PATH = "quake-shareware/id1/pak0.pak"
SV_TICK = 0.1              # server runs the QC at a fixed 10 Hz (like Quake)
NOCLIP_SPEED = 500.0       # units / second when flying
LOOK_SENS = 0.15           # degrees / pixel
YAW_SPEED = 140.0          # degrees / second (keyboard turning)

LINE_COLOR = "#00ff66"
PREGROW = 2048             # line items pre-created up front to avoid hitches
PREGROW_POLY = 768         # polygon items pre-created for flat-shading mode
PREGROW_PART = 256         # point-sprite items for particles
CENTER_MSG_TIME = 4.0      # seconds a centerprint message stays on screen
# weapon view-model bob (Quake's V_CalcBob: cl_bob / cl_bobcycle / cl_bobup)
CL_BOB = 0.02
CL_BOBCYCLE = 0.6
CL_BOBUP = 0.5


def view_origins(pos, view_height, forward, bob):
    """Camera eye and first-person gun origin for the current head-bob, per
    Quake's V_CalcRefdef (view.c). The bob is added to BOTH the view origin and
    the gun, so they share the vertical motion: the camera (and the whole world
    with it) bobs by `bob`, while the gun's only offset relative to the view is
    forward*bob*0.4 -- a small nudge. Returns (eye, gun_origin)."""
    eye = (pos[0], pos[1], pos[2] + view_height + bob)
    gun = (eye[0] + forward[0] * bob * 0.4,
           eye[1] + forward[1] * bob * 0.4,
           eye[2] + forward[2] * bob * 0.4)
    return eye, gun


def spin_yaw(flags, angles, t):
    """Bonus items (EF_ROTATE models -- weapons, keys, powerups) spin in place:
    the client overrides their yaw each frame with anglemod(100*time), ignoring
    the spawn angle (WinQuake cl_main.c). Non-rotating models keep their angles."""
    if not (flags & EF_ROTATE):
        return angles
    return (angles[0], anglemod(100.0 * t), angles[2])


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
        self.pak = Pak(PAK_PATH)
        self.progs_data = self.pak.read("progs.dat")
        pal = self.pak.read("gfx/palette.lmp")
        self.palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2])
                        for i in range(256)]
        self._missing_warned = set()   # maps not in the pak we've already flagged
        self.mixer = snd.Mixer()       # CoreAudio sound mixer (muted if unavailable)

        # window
        self.root = tk.Tk()
        self.root.title(f"pq.ai — {mapname}")
        self.root.geometry("800x600")
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        # z-buffer mode blits a software framebuffer here. Created first so it
        # sits at the bottom of the stack (lines/polys/particles/HUD draw above);
        # hidden until the mode is on. self.fb_photo holds the live PhotoImage.
        self.zbuf = False
        self.textured = True            # texture-map world faces in z-buffer mode
        self.fb_photo = None
        self.fb_item = self.canvas.create_image(0, 0, anchor="nw", state="hidden")
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
        # point-sprite pool for particles (teleport fog, fireball trails)
        self.partpool = [self.canvas.create_rectangle(
            -10, -10, -8, -8, outline="", fill="#ffffff")
            for _ in range(PREGROW_PART)]
        self.partfill = [None] * PREGROW_PART
        self.part_prev = 0
        self.hud = self.canvas.create_text(
            8, 8, anchor="nw", fill="#00ff66", font=("Menlo", 11), text="")
        self.crosshair = self.canvas.create_text(
            0, 0, fill="#00ff66", font=("Menlo", 18), text="+")
        self.center_text = self.canvas.create_text(
            0, 0, fill="#ffff00", font=("Menlo", 16, "bold"), text="",
            justify="center")
        # bottom status bar: health / armor / ammo (Quake-style readout)
        self.statusbar = self.canvas.create_text(
            0, 0, anchor="sw", fill="#ffcc00", font=("Menlo", 16, "bold"), text="")

        # input state
        self.keys = set()
        self.mouselook = False
        self._last_mouse = None
        self.last_t = time.perf_counter()
        self.fps = 0.0
        # fire (button0) comes from two inputs -- the mouse and the Ctrl key --
        # OR'd together so releasing one doesn't cancel the other. attacking is
        # the combined state the QC weapon frame reads (it handles cadence).
        self.fire_mouse = False
        self.fire_key = False
        self.attacking = False
        self.pending_impulse = 0     # weapon-select keypress, sent once to the QC
        self.intermission = False    # frozen at the end-of-level camera spot

        if not self._load_map(mapname):
            sys.exit(f"no such map: maps/{mapname}.bsp")

        self._bind()
        self.canvas.focus_set()
        self.root.after(16, self.tick)

    # ---- level loading ----
    def _load_map(self, mapname, skill=1):
        """Build everything tied to a specific level: BSP, renderer, physics,
        the QuakeC server (entities spawned + logic running), precached models
        and the player spawn. Reused for the initial map and for changelevel.
        Returns False (leaving current state intact) if the map isn't in the
        pak -- e.g. the registered-episode slipgates in shareware start.bsp."""
        path = f"maps/{mapname}.bsp"
        if path not in self.pak.files:
            if mapname not in self._missing_warned:
                print(f"changelevel to {mapname}: not in this pak "
                      f"(registered content) -- staying put")
                self._missing_warned.add(mapname)
            return False

        self.root.title(f"pq.ai — {mapname}")
        self.bsp = Bsp(self.pak.read(path))
        self.rend = Renderer(self.bsp, self.palette)
        self.phys = Physics(self.bsp)

        # QuakeC server: spawn the level's entities and run their logic. Doors,
        # buttons and lifts are entities; their brush models are drawn at the
        # origins the QC sets, and invisible triggers no longer render.
        self.sv = Server(Progs(self.progs_data), bsp=self.bsp, mapname=path,
                         skill=skill, physics=self.phys)
        self.sv.load_level()
        self.sv_accum = 0.0

        # load the .mdl models the level precached, indexed to match modelindex
        self.models = [None] * len(self.sv.model_precache)
        for idx, name in enumerate(self.sv.model_precache):
            if name.endswith(".mdl") and name in self.pak.files:
                try:
                    self.models[idx] = Mdl(self.pak.read(name), self.palette)
                except Exception as e:
                    print(f"mdl load failed for {name}: {e}")
        # load the external .bsp pickup models (health/ammo boxes -- maps/b_*.bsp),
        # also indexed by modelindex. Skip index 1, the world map itself.
        self.bmodels = [None] * len(self.sv.model_precache)
        for idx, name in enumerate(self.sv.model_precache):
            if idx > 1 and name.endswith(".bsp") and name in self.pak.files:
                try:
                    self.bmodels[idx] = PickupModel(Bsp(self.pak.read(name)),
                                                    self.palette)
                except Exception as e:
                    print(f"bsp pickup load failed for {name}: {e}")
        # first-person weapon view models, loaded on demand (v_*.mdl are not all
        # precached); path -> Mdl, or None if the file is missing / failed
        self._vmodels = {}

        # sound: decode every precached sample once, drop the old level's voices,
        # then start the deferred looping ambients. sv.snd is wired last so the
        # QC's spawn-time sound() calls during load_level stay silent (like the
        # Quake signon), and only live gameplay sounds reach the mixer.
        self.mixer.stop_all()
        for name in self.sv.sound_precache:
            # QC precaches bare names ("weapons/sgun1.wav"); the pak stores them
            # under "sound/". Key the mixer by the bare name the QC will pass.
            path = "sound/" + name
            if name and path in self.pak.files:
                self.mixer.precache(name, self.pak.read(path))
        for name, pos, vol, atten in self.sv.ambients:
            self.mixer.start_sound(0, 0, name, vol, atten, pos, loop=True)
        self.sv.snd = self.mixer

        # player origin from the level's spawn point (eye sits VIEW_HEIGHT above)
        (sx, sy, sz), yaw = self.bsp.find_spawn()
        self.pos = [sx, sy, sz]
        self.vel = [0.0, 0.0, 0.0]
        self.bobtime = 0.0          # wall-clock phase for the weapon bob
        self.onground = False
        self.waterlevel = 0
        self.noclip = False
        self.yaw = yaw
        self.pitch = 0.0

        # a client edict driven by the camera: gives monsters a target and gives
        # fired shots an attacker (so QC's damage/death logic runs)
        self.sv.spawn_player(tuple(self.pos), (self.pitch, self.yaw, 0.0))

        # a changelevel doesn't re-fire a <Configure>, so match the new renderer
        # to the current canvas size ourselves (skip before first layout)
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w > 1 and h > 1:
            self.rend.resize(w, h)
        return True

    def _change_level(self, target):
        """Consume a pending changelevel: load the next map, carrying the skill
        the player chose at the start-map setskill triggers."""
        skill = int(self.sv.cvars.get("skill", self.sv.skill))
        if not self._load_map(target, skill=skill):
            self.sv.changelevel = None      # missing map: don't retry every frame

    # ---- input ----
    def _bind(self):
        r = self.root
        r.bind("<KeyPress>", self._keydown)
        r.bind("<KeyRelease>", self._keyup)
        r.bind("<Motion>", self._motion)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        # bind on the canvas (not root) and use the event's own size: at startup
        # the canvas may not be laid out when the root's first <Configure> fires,
        # so winfo_width() would read 1 and the projection would collapse.
        self.canvas.bind("<Configure>", self._resize)

    def _set_attack(self):
        self.attacking = self.fire_mouse or self.fire_key

    def _click(self, e):
        # first click captures the mouse; while captured, hold to fire (button0).
        # The QC weapon frame handles per-weapon cadence, ammo and animation.
        if not self.mouselook:
            self._set_mouselook(True)
        else:
            self.fire_mouse = True
            self._set_attack()

    def _release(self, e):
        self.fire_mouse = False
        self._set_attack()

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
        if k == "z":
            self.zbuf = not self.zbuf
            if self.zbuf:                        # park both vector pools, show fb
                self._park(self.pool, self.prev_n, 4); self.prev_n = 0
                self._park(self.polypool, self.poly_prev, 6); self.poly_prev = 0
                self.canvas.itemconfig(self.fb_item, state="normal")
            else:                                # hide fb; flat/wire redraws next
                self.canvas.itemconfig(self.fb_item, state="hidden")
            return
        if k == "t":                             # texturing on/off (z-buffer mode)
            self.textured = not self.textured
            return
        if len(k) == 1 and "1" <= k <= "8":   # select a weapon (Quake impulse 1-8)
            self.pending_impulse = int(k)
            return
        if k == "control_l" or k == "control_r":   # Ctrl fires (Quake +attack)
            self.fire_key = True
            self._set_attack()
            return
        self.keys.add(k)

    def _keyup(self, e):
        k = e.keysym.lower()
        if k == "control_l" or k == "control_r":
            self.fire_key = False
            self._set_attack()
            return
        self.keys.discard(k)

    def _set_mouselook(self, on):
        self.mouselook = on
        if not on:
            self.fire_mouse = False       # releasing the mouse stops mouse-firing
            self._set_attack()
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
            rise = ("space" in self.keys) - ("c" in self.keys)
            speed = NOCLIP_SPEED * (3.0 if fast else 1.0) * dt
            for i in range(3):
                self.pos[i] += (forward[i] * fwd + right[i] * strafe +
                                up[i] * rise) * speed
            self.vel = [0.0, 0.0, 0.0]
            return

        speed = MAXSPEED * (1.6 if fast else 1.0)

        # ground/air movement uses a horizontal wish direction from yaw only
        fwdh, righth, _ = angle_vectors(self.yaw, 0.0)
        wx = fwdh[0] * fwd + righth[0] * strafe
        wy = fwdh[1] * fwd + righth[1] * strafe
        wl = math.hypot(wx, wy)
        if wl < 1e-6:
            wishdir, wishspeed = (0.0, 0.0, 0.0), 0.0
        else:
            wishdir = (wx / wl, wy / wl, 0.0)
            wishspeed = speed

        # swimming (and wall friction) use the full 3D view; space/ctrl swim up/down
        forward, right, _ = angle_vectors(self.yaw, self.pitch)
        down = "c" in self.keys
        upmove = (("space" in self.keys) - down) * speed

        # clamp dt so a hitch can't tunnel the player through a wall
        step = min(dt, 0.05)
        self.onground, self.waterlevel = self.phys.player_move(
            self.pos, self.vel, wishdir, wishspeed,
            forward, right, fwd * speed, strafe * speed, upmove, speed,
            self.onground, "space" in self.keys, step)

    # ---- main loop ----
    def tick(self):
        now = time.perf_counter()
        dt = now - self.last_t
        self.last_t = now
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)
        dead = False                 # set below once health hits 0 (death cam)
        # Intermission: the QC has frozen the player at the end-of-level camera
        # spot. Don't move or camera-drive them -- just advance the QC and let
        # IntermissionThink load the next map on a fire press after the delay.
        if self.intermission or self.sv.intermission_active():
            self.intermission = True
            self.sv_accum += dt
            steps = 0
            while self.sv_accum >= SV_TICK and steps < 5:
                self.sv.run_frame(SV_TICK)
                self.sv.run_intermission(self.attacking)
                self.sv_accum -= SV_TICK
                steps += 1
        else:
            # Dead: PlayerDie turned the player into a MOVETYPE_TOSS corpse the
            # QC now owns. Stop driving it from input -- no movement, and don't
            # push the camera into the edict (that would fight the body's fall).
            # We just feed the fire button through and follow the corpse, while
            # PlayerDeathThink runs the respawn FSM server-side.
            dead = self.sv.player_health() <= 0
            self.bobtime += dt          # phase for the weapon bob

            if not dead:
                # refresh the brush models the player collides with (doors,
                # func_walls, gates), at the positions last set by the QC tick
                self.phys.set_brush_entities(self.sv.solid_brush_models())
                self._move(dt)

            # listener for 3D sound: ear at the eye, right-vector for the stereo
            # pan. Set before the QC tick so sounds fired this frame spatialize
            # against the current view.
            _f, right, _u = angle_vectors(self.yaw, self.pitch)
            self.mixer.set_listener(
                (self.pos[0], self.pos[1], self.pos[2] + VIEW_HEIGHT), right)

            if not dead:
                # push the camera into the client edict so monsters target the
                # player and shots originate from the current view
                self.sv.update_player((self.pos[0], self.pos[1], self.pos[2]),
                                      (self.pitch, self.yaw, 0.0))
                # SV_Impact: fire touch on the solid movers the move just bumped,
                # so walking into a button presses it / into a key door opens it
                self.sv.touch_impacts(self.phys.touched)

            # advance the QC server at a fixed tick (catch up real time, capped
            # so a hitch can't trigger a spiral of death), then read back entity
            # positions. The impulse is one-shot (a keypress switches once).
            self.sv.set_input(self.attacking, self.pending_impulse)
            self.pending_impulse = 0
            self.sv_accum += dt
            steps = 0
            while self.sv_accum >= SV_TICK and steps < 5:
                self.sv.run_frame(SV_TICK)
                # ride lifts/doors: fold the pusher's carry into the camera
                if not dead:
                    cx, cy, cz = self.sv.player_carry
                    if cx or cy or cz:
                        self.pos[0] += cx
                        self.pos[1] += cy
                        self.pos[2] += cz
                        self.onground = True       # still standing on the mover
                self.sv_accum -= SV_TICK
                steps += 1
            if dead:
                # follow the falling/sliding body so the death cam stays on it
                org = self.sv.player_origin()
                if org is not None:
                    self.pos = [org[0], org[1], org[2]]
            elif steps:
                self._sync_from_player()      # adopt teleports / trigger moves
            # the exit may have started intermission during this frame's QC tick
            self.intermission = self.sv.intermission_active()

        # a changelevel can be queued by a slipgate (normal play) or by
        # GotoNextMap (intermission). Load it and render the new map next frame.
        if self.sv.changelevel:
            self._change_level(self.sv.changelevel)
            self.intermission = False
            self.root.after(16, self.tick)
            return                        # sv/bsp just swapped; render next frame
        brush_ents = self.sv.brush_models()
        alias_ents = self._alias_ents()
        bsp_ents = self._bsp_ents()

        if self.intermission:
            # V_CalcIntermissionRefdef: camera sits at the spot origin (no view
            # height, no bob) looking along its mangle, and the gun is hidden.
            org = self.sv.player_origin()
            ang = self.sv.player_angles()
            if org:
                self.pos = [org[0], org[1], org[2]]
            if ang:
                self.pitch = max(-89.0, min(89.0, ang[0]))
                self.yaw = ang[1]
            eye = (self.pos[0], self.pos[1], self.pos[2])
            view_model = None
        elif dead:
            # death cam: the eye sinks to the corpse on the floor (PlayerDie set
            # view_ofs z = -8), with no head-bob and no weapon model.
            vofs = self.sv.player_view_ofs()
            vz = vofs[2] if vofs else -8.0
            eye = (self.pos[0], self.pos[1], self.pos[2] + vz)
            view_model = None
        else:
            # head-bob: shift both the view origin and the gun by it, as Quake
            # does, so the weapon rides nearly still instead of sloshing.
            bob = self._calc_bob()
            fwd, _r, _u = angle_vectors(self.yaw, self.pitch)
            eye, gun_org = view_origins(self.pos, VIEW_HEIGHT, fwd, bob)
            view_model = self._view_model(gun_org)
        if self.zbuf:
            styles = lightstyle_values(self.sv.lightstyles, self.sv.time)
            fbdata, leaf = self.rend.render_zbuffer(eye, self.yaw, self.pitch,
                                                    brush_ents, alias_ents,
                                                    view_model, bsp_ents,
                                                    textured=self.textured,
                                                    lightstyles=styles,
                                                    time=self.sv.time)
            self._draw_fb(fbdata)
            nprim = fbdata[1] * fbdata[2]
        elif self.flat:
            styles = lightstyle_values(self.sv.lightstyles, self.sv.time)
            polys, leaf = self.rend.render_shaded(eye, self.yaw, self.pitch,
                                                  brush_ents, alias_ents, view_model,
                                                  bsp_ents, lightstyles=styles)
            self._draw_polys(polys)
            nprim = len(polys)
        else:
            segs, leaf = self.rend.render(eye, self.yaw, self.pitch,
                                          brush_ents, alias_ents, view_model,
                                          bsp_ents)
            self._draw(segs)
            nprim = len(segs)

        self._draw_particles(eye)

        spd = math.hypot(self.vel[0], self.vel[1])
        mode = ("NOCLIP" if self.noclip else
                "water" if self.waterlevel >= 2 else
                "ground" if self.onground else "air")
        hp = self.sv.player_health()
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        self.canvas.coords(self.crosshair, w // 2, h // 2)
        # centred message (centerprint): skill choice, the registered notice, ...
        cm = self.sv.center_msg
        if cm and self.sv.time - cm[1] < CENTER_MSG_TIME:
            self.canvas.coords(self.center_text, w // 2, h // 3)
            self.canvas.itemconfig(self.center_text, text=cm[0])
        else:
            self.canvas.itemconfig(self.center_text, text="")
        self.canvas.itemconfig(
            self.hud,
            text=(f"{self.fps:5.1f} fps   "
                  f"{'pixels' if self.zbuf else 'polys' if self.flat else 'segs'} {nprim}   "
                  f"leaf {leaf}   {mode}   health {hp:.0f}\n"
                  f"pos {self.pos[0]:.0f} {self.pos[1]:.0f} {self.pos[2]:.0f}   "
                  f"spd {spd:.0f}   yaw {self.yaw:.0f} pitch {self.pitch:.0f}   "
                  f"{'MOUSELOOK — mouse/Ctrl fire, 1-8 weapons' if self.mouselook else 'click to capture mouse'} "
                  f"[N]oclip [F]lat [Z]buffer [T]exture"))
        # bottom status bar: health / armor / current-weapon ammo, plus the four
        # ammo pools. Health reddens when low so it reads at a glance.
        st = self.sv.hud_status()
        if st:
            self.canvas.coords(self.statusbar, 10, h - 8)
            self.canvas.itemconfig(
                self.statusbar, fill="#ff4040" if st["health"] <= 25 else "#ffcc00",
                text=(f"HEALTH {st['health']:3d}    ARMOR {st['armor']:3d}    "
                      f"{st['weapon']}: {st['ammo']:3d}\n"
                      f"shells {st['shells']:3d}  nails {st['nails']:3d}  "
                      f"rockets {st['rockets']:3d}  cells {st['cells']:3d}"))
        self.canvas.tag_raise(self.hud)
        self.canvas.tag_raise(self.crosshair)
        self.canvas.tag_raise(self.center_text)
        self.canvas.tag_raise(self.statusbar)
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
            ang = spin_yaw(m.flags, ang, now)
            out.append((m, m.frame_verts(frame, now), org, ang))
        return out

    def _bsp_ents(self):
        """Resolve live external-.bsp pickup entities to (PickupModel, origin,
        angles) for the renderer. Skips any whose model failed to load."""
        out = []
        bmodels = self.bmodels
        nmodels = len(bmodels)
        for mi, org, ang in self.sv.bsp_model_entities():
            pm = bmodels[mi] if mi < nmodels else None
            if pm is None:
                continue
            out.append((pm, org, ang))
        return out

    def _calc_bob(self):
        """Quake's V_CalcBob: weapon bob amplitude from horizontal speed and a
        wall-clock phase. Returns units to shift the view model by."""
        speed = math.hypot(self.vel[0], self.vel[1])
        cycle = (self.bobtime % CL_BOBCYCLE) / CL_BOBCYCLE
        if cycle < CL_BOBUP:
            cycle = math.pi * cycle / CL_BOBUP
        else:
            cycle = math.pi + math.pi * (cycle - CL_BOBUP) / (1.0 - CL_BOBUP)
        bob = speed * CL_BOB
        bob = bob * 0.3 + bob * 0.7 * math.sin(cycle)
        return max(-7.0, min(4.0, bob))

    def _view_model(self, org):
        """The first-person weapon as (mdl, verts, origin, angles), or None.
        Reads the QC's .weaponmodel/.weaponframe and fixes it to the (already
        bob-shifted) gun origin. Negating pitch aligns model_axes with the view."""
        vw = self.sv.view_weapon()
        if not vw:
            return None
        path, frame = vw
        if path not in self._vmodels:
            try:
                self._vmodels[path] = (Mdl(self.pak.read(path), self.palette)
                                       if path in self.pak.files else None)
            except Exception as e:
                print(f"viewmodel load failed for {path}: {e}")
                self._vmodels[path] = None
        mdl = self._vmodels[path]
        if mdl is None:
            return None
        ang = (-self.pitch, self.yaw, 0.0)
        return (mdl, mdl.frame_verts(frame, self.sv.time), org, ang)

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

    def _draw_particles(self, eye):
        """Project the live particles to screen and draw them as 2px sprites,
        coloured from the Quake palette (teleport fog, fireball sparks)."""
        c = self.canvas
        pool = self.partpool
        fillc = self.partfill
        coords = c.coords
        itemconfig = c.itemconfig
        project = self.rend.project_point
        pal = self.palette
        W = self.canvas.winfo_width()
        H = self.canvas.winfo_height()
        n = 0
        for p in self.sv.particles:
            sp = project(eye, self.yaw, self.pitch, (p[0], p[1], p[2]))
            if sp is None:
                continue
            x, y = sp
            if x < 0 or y < 0 or x > W or y > H:
                continue
            if n >= len(pool):
                pool.append(c.create_rectangle(-10, -10, -8, -8,
                                               outline="", fill="#ffffff"))
                fillc.append(None)
            coords(pool[n], x, y, x + 2, y + 2)
            r, g, b = pal[p[6] & 255]
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
        c.tag_raise(self.hud)

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
