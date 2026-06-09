"""UI-agnostic game client: owns the engine stack (Pak/Bsp/Renderer/Physics/Server)
and all camera/player/game state, and turns one frame of input into a RenderFrame
the frontend draws. Imports only quake.* and stdlib -- no tkinter, no ctypes -- so
both the tkinter frontend (main.py) and the gdi32 frontend (win_gdi.py) share it."""

import math
import sys
from dataclasses import dataclass, field

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import (Renderer, PickupModel, angle_vectors,
                          lightstyle_values, ZBUF_SCALE)
from quake.console import Console
from quake.menu import Menu, ChoiceItem, ActionItem
from quake.physics import Physics, VIEW_HEIGHT, MAXSPEED
from quake.progs import Progs
from quake.sv import Server, anglemod
from quake.mdl import Mdl, EF_ROTATE
from quake.perf import PROFILER
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
# Selectable textured-mode render resolutions for the video-options menu.
# "Auto" = derive from the window via zbuf_scale (today's behaviour, keeps the
# zbuf_scale cvar meaningful); the fixed modes set the framebuffer exactly.
VIDEO_MODES = [("Auto", None), ("80x40", (80, 40)), ("160x80", (160, 80)),
               ("240x160", (240, 160)), ("320x240", (320, 240)),
               ("640x480", (640, 480))]
DEFAULT_VIDEO_RES = (320, 240)


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
    mouselook: bool = False  # frontend hint: only used to pick the HUD prompt string


@dataclass
class RenderFrame:
    """What Client.frame() returns; the frontend draws it. mode is 'wire'|'flat'|
    'zbuf'. Exactly one of segs/polys/framebuffer is set per mode; framebuffer
    is (index_bytes, w, h) -- 8-bit palette indices the frontend expands via
    Client.palette (tk) or blits as an 8bpp palettised DIB (gdi32). overlays
    are (x, y, text, (r,g,b), anchor) with anchor in {'nw','center','sw'}.
    menu is the overlay menu's view (title, rows) when open, else None."""
    mode: str
    segs: list = None                       # mode 'wire': line segments
    polys: list = None                      # mode 'flat': (points, color)
    framebuffer: tuple = None               # mode 'zbuf': (bytes, w, h)
    particles: list = field(default_factory=list)
    overlays: list = field(default_factory=list)
    crosshair: tuple = (0, 0)
    console: tuple = None    # (lines, input_line, cursor_col) when open, else None
    menu: tuple = None       # (title, [(label, value, selected), ...]) when open, else None


class Client:
    def __init__(self, mapname):
        self.pak = Pak(PAK_PATH)
        self.progs_data = self.pak.read("progs.dat")
        pal = self.pak.read("gfx/palette.lmp")
        self.palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2])
                        for i in range(256)]
        # 64 light rows x 256 palette indices; the z-buffer renderer lights
        # every texel through it and returns an 8-bit indexed framebuffer.
        self.colormap = self.pak.read("gfx/colormap.lmp")[:64 * 256]
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
        self.show_prof = False       # append the profiler section breakdown to the HUD

        # mode derived from the renderer flags above
        self.mode = "zbuf" if self.zbuf else "flat" if self.flat else "wire"

        self.quit_requested = False
        self._zbuf_scale = ZBUF_SCALE     # desired textured divisor, persists across maps
        # fixed textured render resolution (video-options menu), persists across
        # maps like _zbuf_scale; applied to each freshly built Renderer.
        self.video_res = DEFAULT_VIDEO_RES
        self.con = Console()

        if not self._load_map(mapname):
            raise ValueError(f"no such map: maps/{mapname}.bsp")
        self._register_console()
        self.menu = self._build_menu()

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
        self.rend = Renderer(self.bsp, self.palette, self.colormap)
        self.rend.zbuf_scale = self._zbuf_scale   # keep the console's chosen scale
        self.rend.video_res = self.video_res      # keep the menu's chosen resolution
        self.rend.resize(self.rend.width, self.rend.height)  # rebuild buffer at the chosen res
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
            snd_path = "sound/" + name
            if name and snd_path in self.pak.files:
                self.mixer.precache(name, self.pak.read(snd_path))
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
    def _wishmove(self, inp):
        """Forward/strafe intent from input, as -1..1 each."""
        fwd = inp.move_forward
        strafe = inp.move_strafe
        return fwd, strafe

    def _move(self, dt, inp):
        fwd, strafe = self._wishmove(inp)
        fast = bool(inp.run)

        if self.noclip:
            # free fly along the full view direction (pitch included), no gravity
            forward, right, up = angle_vectors(self.yaw, self.pitch)
            rise = inp.move_up
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
        jump = inp.move_up > 0.0
        upmove = inp.move_up * speed

        # clamp dt so a hitch can't tunnel the player through a wall
        step = min(dt, 0.05)
        self.onground, self.waterlevel = self.phys.player_move(
            self.pos, self.vel, wishdir, wishspeed,
            forward, right, fwd * speed, strafe * speed, upmove, speed,
            self.onground, jump, step)

    # ---- render-state toggles (shared by the hotkeys and the console) ----
    def _toggle_noclip(self):
        self.noclip = not self.noclip
        self.vel = [0.0, 0.0, 0.0]

    def _toggle_flat(self):
        self.flat = not self.flat

    def _toggle_zbuf(self):
        self.zbuf = not self.zbuf

    def _toggle_texture(self):
        self.textured = not self.textured

    def _toggle_prof(self):
        self.show_prof = not self.show_prof

    def _apply_mode(self):
        self.mode = "zbuf" if self.zbuf else "flat" if self.flat else "wire"

    def set_video_res(self, wh):
        """Set the textured render resolution (None = Auto/window-derived) and
        rebuild the framebuffer now, so a menu change takes effect immediately
        even when the window size hasn't changed."""
        self.video_res = wh
        self.rend.video_res = wh
        self.rend.resize(self.rend.width, self.rend.height)

    def _menu_back(self):
        self.menu.active = False

    def _build_menu(self):
        """Build the Escape overlay menu: Resolution (cycles VIDEO_MODES), Back,
        Quit. Closures bind to this Client's methods, like console commands."""
        idx = next((i for i, (_, v) in enumerate(VIDEO_MODES)
                    if v == self.video_res), 0)
        res = ChoiceItem("Resolution", VIDEO_MODES, idx, self.set_video_res)
        back = ActionItem("Back", self._menu_back)
        quit_item = ActionItem("Quit", self._cmd_quit_menu)
        return Menu("VIDEO OPTIONS", [res, back, quit_item])

    def _cmd_quit_menu(self):
        self.quit_requested = True

    # ---- console registration / commands ----
    def _register_console(self):
        """Register the built-in commands and cvars that bind to this Client's
        state. Called after the first _load_map so self.rend exists."""
        con = self.con

        def mode_cmd(toggle):
            def run(args):
                toggle()
                self._apply_mode()
            return run

        con.register_command("noclip", mode_cmd(self._toggle_noclip), "toggle noclip flight")
        con.register_command("flat", mode_cmd(self._toggle_flat), "toggle flat-shaded mode")
        con.register_command("zbuf", mode_cmd(self._toggle_zbuf), "toggle textured z-buffer mode")
        con.register_command("texture", mode_cmd(self._toggle_texture), "toggle texturing")
        con.register_command("prof", mode_cmd(self._toggle_prof), "toggle the profiler HUD")
        con.register_command("map", self._cmd_map, "map <name>: load a level")
        con.register_command("god", self._cmd_god, "toggle god mode")
        con.register_command("give", self._cmd_give, "give <h|s|n|r|c> [amount]")
        con.register_command("set", self._cmd_set, "set <cvar> [value]: a QuakeC cvar")
        con.register_command("echo", lambda a: con.print(" ".join(a)), "echo text")
        con.register_command("clear", lambda a: con.lines.clear(), "clear the console")
        con.register_command("alias", self._cmd_alias, "alias <name> <text...>")
        con.register_command("exec", self._cmd_exec, "exec <file>: run console lines")
        con.register_command("cmdlist", self._cmd_cmdlist, "list commands")
        con.register_command("cvarlist", self._cmd_cvarlist, "list cvars")
        con.register_command("help", self._cmd_help, "help [name]")
        con.register_command("quit", self._cmd_quit, "quit the game")
        con.register_command("exit", self._cmd_quit, "quit the game")
        con.register_cvar("zbuf_scale", self._zbuf_scale,
                          on_change=self._on_zbuf_scale,
                          help="textured rasteriser resolution divisor (1-16)")

    def _on_zbuf_scale(self, cv):
        v = max(1, min(16, cv.as_int()))
        cv.value = str(v)                         # write the clamped value back
        self._zbuf_scale = v
        self.rend.zbuf_scale = v
        if self._view_wh != (0, 0):
            self.rend.resize(*self._view_wh)
        self.con.print(f"zbuf_scale {v}")

    def _cmd_map(self, args):
        if not args:
            self.con.print("usage: map <name>")
            return
        if self._load_map(args[0]):               # rebuilds rend/sv; prints its own miss
            self.con.print(f"loading {args[0]}")

    def _cmd_god(self, args):
        self.con.print("godmode " + ("ON" if self.sv.toggle_god() else "OFF"))

    def _cmd_give(self, args):
        if not args:
            self.con.print("usage: give <h|s|n|r|c> [amount]")
            return
        amount = (int(args[1]) if len(args) > 1 and args[1].lstrip("-").isdigit()
                  else None)
        self.con.print(self.sv.give(args[0], amount))

    def _cmd_set(self, args):
        if not args:
            self.con.print("usage: set <cvar> [value]")
            return
        name = args[0]
        if len(args) >= 2:
            try:
                val = float(args[1])
            except ValueError:
                val = 0.0
            self.sv.cvars[name] = val
            self.con.print(f"{name} = {val:g}")
        else:
            self.con.print(f"{name} = {self.sv.cvars.get(name, 0.0):g}")

    def _cmd_alias(self, args):
        if not args:
            for name, text in sorted(self.con.aliases.items()):
                self.con.print(f"{name}: {text}")
            return
        if len(args) == 1:
            self.con.print(f"usage: alias {args[0]} <text...>")
            return
        self.con.register_alias(args[0], " ".join(args[1:]))

    def _cmd_exec(self, args):
        if not args:
            self.con.print("usage: exec <file>")
            return
        try:
            with open(args[0], "r", encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeError) as e:
            self.con.print(f"exec: {e}")
            return
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                self.con.execute(line)

    def _cmd_cmdlist(self, args):
        for name in sorted(self.con.commands):
            self.con.print(f"{name:<12}{self.con.commands[name].help}")

    def _cmd_cvarlist(self, args):
        for name in sorted(self.con.cvars):
            cv = self.con.cvars[name]
            self.con.print(f"{name:<14}{cv.value:<6}{cv.help}")

    def _cmd_help(self, args):
        if args:
            name = args[0]
            if name in self.con.commands:
                self.con.print(f"{name}: {self.con.commands[name].help}")
            elif name in self.con.cvars:
                self.con.print(f"{name}: {self.con.cvars[name].help}")
            else:
                self.con.print(f"no such command or cvar: {name}")
            return
        self.con.print("commands: " + "  ".join(sorted(self.con.commands)))
        self.con.print("cvars: " + "  ".join(sorted(self.con.cvars)))

    def _cmd_quit(self, args):
        self.quit_requested = True

    # ---- main loop ----
    def frame(self, dt, inp):
        """Advance one frame from `dt` seconds and `inp` intent, returning a
        RenderFrame the frontend draws. Ports main.py's App.tick minus drawing,
        timing/after scheduling and diagnostics."""
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

        # apply input -> view angles
        self.yaw -= inp.look_dx * LOOK_SENS
        self.pitch = max(-89.0, min(89.0, self.pitch + inp.look_dy * LOOK_SENS))
        self.yaw -= inp.turn * YAW_SPEED * dt

        # fire (button0) is mouse OR Ctrl key; combine into the attacking state
        # the QC weapon frame reads.
        self.fire_mouse = bool(inp.fire)
        self.attacking = self.fire_mouse or self.fire_key
        if inp.impulse:
            self.pending_impulse = inp.impulse

        # one-shot edge-triggered toggles fired this frame (keyboard keys)
        if inp.commands:
            dispatch = {"noclip": self._toggle_noclip, "flat": self._toggle_flat,
                        "zbuf": self._toggle_zbuf, "texture": self._toggle_texture,
                        "prof": self._toggle_prof}
            for cmd in inp.commands:
                fn = dispatch.get(cmd)
                if fn:
                    fn()
            self._apply_mode()

        PROFILER.begin("server")     # QuakeC tick + physics for this frame
        dead = False                 # set below once health hits 0 (death cam)
        # Intermission: the QC has frozen the player at the end-of-level camera
        # spot. Don't move or camera-drive them -- just advance the QC and let
        # IntermissionThink load the next map on a fire press after the delay.
        if self.intermission or self.sv.intermission_active():
            self.intermission = True
            self.sv.run_frame(dt if dt < SV_MAXFRAME else SV_MAXFRAME)
            self.sv.run_intermission(self.attacking)
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
                self._move(dt, inp)

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

            # advance the QC server, then read back entity positions. The impulse
            # is one-shot (a keypress switches weapon once).
            self.sv.set_input(self.attacking, self.pending_impulse)
            self.pending_impulse = 0
            # one server frame per rendered frame (Quake's model): movers and
            # missiles step every frame so they read smooth, while nextthink keeps
            # AI on its own cadence. Clamp the frametime so a hitch can't tunnel.
            self.sv.run_frame(dt if dt < SV_MAXFRAME else SV_MAXFRAME)
            if dead:
                # follow the falling/sliding body so the death cam stays on it
                org = self.sv.player_origin()
                if org is not None:
                    self.pos = [org[0], org[1], org[2]]
            else:
                # ride lifts/doors: fold the pusher's carry into the camera
                cx, cy, cz = self.sv.player_carry
                if cx or cy or cz:
                    self.pos[0] += cx
                    self.pos[1] += cy
                    self.pos[2] += cz
                    self.onground = True           # still standing on the mover
                self._sync_from_player()           # adopt teleports / trigger moves
            # the exit may have started intermission during this frame's QC tick
            self.intermission = self.sv.intermission_active()

        # a changelevel can be queued by a slipgate (normal play) or by
        # GotoNextMap (intermission). Load it and render the new map this frame.
        if self.sv.changelevel:
            self._change_level(self.sv.changelevel)
            self.intermission = False
            # sv/bsp just swapped; render the new map's current state below
        PROFILER.end("server")
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

        PROFILER.begin("render")
        segs = polys = framebuffer = None
        if self.mode == "zbuf":
            styles = lightstyle_values(self.sv.lightstyles, self.sv.time)
            fbdata, leaf = self.rend.render_zbuffer(eye, self.yaw, self.pitch,
                                                    brush_ents, alias_ents,
                                                    view_model, bsp_ents,
                                                    textured=self.textured,
                                                    lightstyles=styles,
                                                    time=self.sv.time)
            framebuffer = fbdata
            nprim = fbdata[1] * fbdata[2]
        elif self.mode == "flat":
            styles = lightstyle_values(self.sv.lightstyles, self.sv.time)
            polys, leaf = self.rend.render_shaded(eye, self.yaw, self.pitch,
                                                  brush_ents, alias_ents, view_model,
                                                  bsp_ents, lightstyles=styles)
            nprim = len(polys)
        else:
            segs, leaf = self.rend.render(eye, self.yaw, self.pitch,
                                          brush_ents, alias_ents, view_model,
                                          bsp_ents)
            nprim = len(segs)
        PROFILER.end("render")

        particles = self._particle_sprites(eye)

        spd = math.hypot(self.vel[0], self.vel[1])
        movemode = ("NOCLIP" if self.noclip else
                    "water" if self.waterlevel >= 2 else
                    "ground" if self.onground else "air")
        hp = self.sv.player_health()
        w, h = self._view_wh

        overlays = []
        hud_str = (f"{self.fps:5.1f} fps   "
                   f"{'pixels' if self.zbuf else 'polys' if self.flat else 'segs'} {nprim}   "
                   f"leaf {leaf}   {movemode}   health {hp:.0f}\n"
                   f"pos {self.pos[0]:.0f} {self.pos[1]:.0f} {self.pos[2]:.0f}   "
                   f"spd {spd:.0f}   yaw {self.yaw:.0f} pitch {self.pitch:.0f}   "
                   f"{'MOUSELOOK — mouse/Ctrl fire, 1-8 weapons' if inp.mouselook else 'click to capture mouse'} "
                   f"[N]oclip [F]lat [Z]buffer [T]exture [P]rofile")
        if self.show_prof:
            # previous completed frame's smoothed section ms (server/render/
            # raster/present) as a bar chart. present is timed in the frontend
            # and frame_end() rolls the buckets, so the figures lag one frame
            # uniformly.
            hud_str += "\n" + PROFILER.bars()
        overlays.append((8, 8, hud_str, (0, 255, 102), "nw"))

        # bottom status bar: health / armor / current-weapon ammo, plus the four
        # ammo pools. Health reddens when low so it reads at a glance.
        st = self.sv.hud_status()
        if st:
            status_rgb = (255, 64, 64) if st["health"] <= 25 else (255, 204, 0)
            status_str = (f"HEALTH {st['health']:3d}    ARMOR {st['armor']:3d}    "
                          f"{st['weapon']}: {st['ammo']:3d}\n"
                          f"shells {st['shells']:3d}  nails {st['nails']:3d}  "
                          f"rockets {st['rockets']:3d}  cells {st['cells']:3d}")
            overlays.append((10, h - 8, status_str, status_rgb, "sw"))

        cm = self.sv.center_msg
        if cm and self.sv.time - cm[1] < CENTER_MSG_TIME:
            overlays.append((w // 2, h // 3, cm[0], (255, 255, 0), "center"))

        con = self.con
        console = None
        if con.active:
            con.width = max(20, w // 9)           # ~9px per monospace cell at the HUD size
            rows = max(1, (h * 2 // 5) // 16 - 1)  # panel is ~40% tall, ~16px lines
            console = (con.view_lines(rows), "]" + con.input, con.cursor + 1)

        menu = self.menu.view() if self.menu.active else None

        return RenderFrame(mode=self.mode, segs=segs, polys=polys,
                           framebuffer=framebuffer, particles=particles,
                           overlays=overlays, crosshair=(w // 2, h // 2),
                           console=console, menu=menu)

    def _particle_sprites(self, eye):
        """Project the live particles to screen and return a list of (x, y, half,
        (r,g,b)) sprite tuples (teleport fog, rocket/blood trails). Each sprite is
        sized by distance -- focal * radius / depth -- with a floor so far ones
        stay visible, and occluded against the world (no depth test in the
        overlay). Ports main.py's App._draw_particles minus the drawing."""
        out = []
        project = self.rend.project_point
        trace_point = self.phys.trace_point
        focal_r = self.rend.focal * PARTICLE_RADIUS
        pal = self.palette
        W, H = self._view_wh
        for p in self.sv.particles:
            sp = project(eye, self.yaw, self.pitch, (p[0], p[1], p[2]))
            if sp is None:
                continue
            x, y, cz = sp
            if x < 0 or y < 0 or x > W or y > H:
                continue
            # occlude against the world: the sprites are a flat overlay with no
            # depth test, so without this they'd show through walls. A clear
            # line of sight from the eye means trace_point reaches the particle
            # (fraction 1.0); anything less means a wall is in front of it.
            if trace_point(eye, (p[0], p[1], p[2])).fraction < 1.0:
                continue
            half = focal_r / cz                      # sprite half-size in pixels
            if half < PARTICLE_MIN_HALF:
                half = PARTICLE_MIN_HALF
            elif half > PARTICLE_MAX_HALF:
                half = PARTICLE_MAX_HALF
            out.append((x, y, half, pal[p[6] & 255]))
        return out

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
