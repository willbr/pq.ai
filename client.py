"""UI-agnostic game client: owns the engine stack (Pak/Bsp/Renderer/Physics/Server)
and all camera/player/game state, and turns one frame of input into a RenderFrame
the frontend draws. Imports only quake.* and stdlib -- no tkinter, no ctypes -- so
both the tkinter frontend (main.py) and the gdi32 frontend (win_gdi.py) share it."""

import math
import sys
from dataclasses import dataclass, field

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import (Renderer, PickupModel, angle_vectors, ZBUF_SCALE,
                          lightstyle_values)
from quake.physics import Physics, VIEW_HEIGHT, MAXSPEED
from quake.progs import Progs
from quake.sv import Server, anglemod
from quake.mdl import Mdl, EF_ROTATE
from quake import snd

PAK_PATH = "quake-shareware/id1/pak0.pak"
# Quake runs the server once per rendered frame with the real frametime (clamped
# so a hitch can't break physics) -- NOT a fixed 10 Hz clock. Doors, lifts and
# missiles are integrated by frametime each frame, so this is what keeps them
# smooth; thinks (monster AI, etc.) stay gated by nextthink, firing at their own
# ~10 Hz cadence regardless. host_frametime caps at 0.1 in WinQuake (Host_FilterTime).
SV_MAXFRAME = 0.1          # clamp a single server frame to 100ms (hitch guard)
NOCLIP_SPEED = 500.0       # units / second when flying
LOOK_SENS = 0.15           # degrees / pixel
YAW_SPEED = 140.0          # degrees / second (keyboard turning)
# particle sprites are sized by distance: half-size px = focal * RADIUS / depth,
# clamped so near puffs read as chunky and far ones never vanish to a single dot
PARTICLE_RADIUS = 2.0      # world half-extent of a particle puff
PARTICLE_MIN_HALF = 2.0    # never smaller than a 4px square
PARTICLE_MAX_HALF = 14.0   # cap so a point-blank puff doesn't fill the screen
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


@dataclass
class InputState:
    """One frame of intent, filled by the frontend from native events. Read-only
    to Client. look_dx/dy are mouse counts since the last frame, sent only while
    mouselook is engaged. commands holds one-shot edge-triggered toggles fired this
    frame, a subset of {'noclip','flat','zbuf','texture'}."""
    move_forward: float = 0.0
    move_strafe: float = 0.0
    move_up: float = 0.0
    turn: float = 0.0
    look_dx: float = 0.0
    look_dy: float = 0.0
    run: bool = False
    fire: bool = False
    impulse: int = 0
    commands: frozenset = frozenset()


@dataclass
class RenderFrame:
    """What Client.frame() returns; the frontend draws it. mode is 'wire'|'flat'|
    'zbuf'. Exactly one of segs/polys/framebuffer is set per mode. overlays are
    (x, y, text, (r,g,b), anchor) with anchor in {'nw','center','sw'}."""
    mode: str
    segs: list = None                       # mode 'wire': line segments
    polys: list = None                      # mode 'flat': (points, color)
    framebuffer: tuple = None               # mode 'zbuf': (bytes, w, h)
    particles: list = field(default_factory=list)
    overlays: list = field(default_factory=list)
    crosshair: tuple = (0, 0)


class Client:
    def __init__(self, mapname):
        self.pak = Pak(PAK_PATH)
        self.progs_data = self.pak.read("progs.dat")
        pal = self.pak.read("gfx/palette.lmp")
        self.palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2])
                        for i in range(256)]
        self._missing_warned = set()   # maps not in the pak we've already flagged
        # The mixer is platform-agnostic; a backend (chosen by OS) opens the
        # output stream and flips mixer.ok on. Kept on self so its ctypes
        # callback trampoline isn't garbage-collected. No backend -> muted.
        self.mixer = snd.Mixer()
        self.audio = None
        if sys.platform == "darwin":
            import mac
            self.audio = mac.CoreAudioBackend(self.mixer)
        elif sys.platform == "win32":
            import win
            self.audio = win.WinmmBackend(self.mixer)
        # else: runs muted until a linux backend is added

        # z-buffer mode blits a software framebuffer.
        self.zbuf = False
        self.textured = True            # texture-map world faces in z-buffer mode
        # filled-polygon (flat shading) mode flag
        self.flat = True

        self.fps = 0.0
        # fire (button0) comes from two inputs -- the mouse and the Ctrl key --
        # OR'd together so releasing one doesn't cancel the other. attacking is
        # the combined state the QC weapon frame reads (it handles cadence).
        self.fire_mouse = False
        self.fire_key = False
        self.attacking = False
        self.pending_impulse = 0     # weapon-select keypress, sent once to the QC
        self.intermission = False    # frozen at the end-of-level camera spot

        # mode derived from the renderer flags above
        self.mode = "zbuf" if self.zbuf else "flat" if self.flat else "wire"

        if not self._load_map(mapname):
            raise ValueError(f"no such map: maps/{mapname}.bsp")

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

        self.bsp = Bsp(self.pak.read(path))
        self.rend = Renderer(self.bsp, self.palette)
        self.phys = Physics(self.bsp)

        # QuakeC server: spawn the level's entities and run their logic. Doors,
        # buttons and lifts are entities; their brush models are drawn at the
        # origins the QC sets, and invisible triggers no longer render.
        self.sv = Server(Progs(self.progs_data), bsp=self.bsp, mapname=path,
                         skill=skill, physics=self.phys, pak=self.pak)
        self.sv.load_level()

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

        self._view_wh = (0, 0)
        return True

    def resize(self, w, h):
        self._view_wh = (w, h)
        self.rend.resize(w, h)

    def _change_level(self, target):
        """Consume a pending changelevel: load the next map, carrying the skill
        the player chose at the start-map setskill triggers."""
        skill = int(self.sv.cvars.get("skill", self.sv.skill))
        if not self._load_map(target, skill=skill):
            self.sv.changelevel = None      # missing map: don't retry every frame

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
