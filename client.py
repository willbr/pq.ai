"""UI-agnostic game client: owns the engine stack (Pak/Bsp/Renderer/Physics/Server)
and all camera/player/game state, and turns one frame of input into a RenderFrame
the frontend draws. Imports only quake.* and stdlib -- no tkinter, no ctypes -- so
both the tkinter frontend (main.py) and the gdi32 frontend (win_gdi.py) share it."""

import math
import os
import random
import sys
from datetime import datetime
from dataclasses import dataclass, field

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import (Renderer, PickupModel, angle_vectors,
                          lightstyle_values, ZBUF_SCALE)
from quake.console import Console
from quake.menu import Menu, ChoiceItem, ActionItem
from quake.physics import Physics, VIEW_HEIGHT, MAXSPEED, CONTENTS_EMPTY
from quake.progs import Progs
from quake.sv import (Server, anglemod, MOVETYPE_WALK, MOVETYPE_FLY,
                      MOVETYPE_NOCLIP)
from quake.mdl import Mdl, EF_ROTATE
from quake.spr import Spr
from quake.wad import Wad
from quake.sbar import Sbar, SBAR_LINES
from quake.conchars import ConFont, load_qpic, blit_conback, fade_region
from quake.perf import PROFILER
from quake import snd
from quake.cl_parse import ClientState, SceneFromClient
from quake.sv_send import build_signon, build_datagram
from quake.msg import MsgWriter, MsgReader

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
# V_AddIdle: the gentle view sway Quake forces on during intermission
# (v_idlescale=1). (cycle, level) per axis -- view.c defaults, degrees.
V_IYAW_CYCLE, V_IYAW_LEVEL = 2.0, 0.3
V_IPITCH_CYCLE, V_IPITCH_LEVEL = 1.0, 0.3
V_IROLL_CYCLE, V_IROLL_LEVEL = 0.5, 0.1
# weapon view-model bob (Quake's V_CalcBob: cl_bob / cl_bobcycle / cl_bobup)
CL_BOB = 0.02
CL_BOBCYCLE = 0.6
CL_BOBUP = 0.5
# Selectable textured-mode render resolutions for the video-options menu.
# "Auto" = derive from the window via zbuf_scale (today's behaviour, keeps the
# zbuf_scale cvar meaningful); the fixed modes set the framebuffer exactly.
VIDEO_MODES = [("Auto", None), ("80x40", (80, 40)), ("160x80", (160, 80)),
               ("240x160", (240, 160)), ("320x200", (320, 200)),
               ("320x240", (320, 240)), ("640x480", (640, 480))]
DEFAULT_VIDEO_RES = (320, 200)        # classic: the 320-wide sbar fits exactly
# Pixel aspect for the textured mode: vertical/horizontal pixel size. CRT is
# R_ViewChanged's "proper 320*200 pixelAspect = 0.8333333" -- VGA mode 13h
# pixels were 5/6 as wide as tall on a 4:3 monitor.
ASPECT_MODES = [("Square", 1.0), ("CRT", 5.0 / 6.0)]
HUD_GREEN = (0, 255, 102)  # the HUD/overlay text colour


def prof_total_color(total_ms):
    """Traffic-light colour for the profiler HUD's total row: green while the
    frame fits a 60fps budget, yellow above 30fps, orange above 20fps, red
    below that."""
    if total_ms <= 1000.0 / 60.0:
        return HUD_GREEN
    if total_ms <= 1000.0 / 30.0:
        return (255, 204, 0)
    if total_ms <= 1000.0 / 20.0:
        return (255, 140, 0)
    return (255, 64, 64)


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


def stair_smooth(eye, gun_org, dz):
    """Lag the eye and the gun by the same stair-step offset (V_CalcRefdef oldz,
    view.c:975-976: the offset is added to BOTH r_refdef.vieworg[2] and
    view->origin[2]). Applying it to the eye alone -- the old bug -- left the
    weapon at the unsmoothed height, so it drifted up into the middle of the
    screen while climbing steps or riding a lift up. gun_org may be None (dead /
    intermission: no weapon)."""
    eye = (eye[0], eye[1], eye[2] + dz)
    if gun_org is not None:
        gun_org = (gun_org[0], gun_org[1], gun_org[2] + dz)
    return eye, gun_org


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
    'zbuf'|'wire_hidden'. Exactly one of segs/polys/framebuffer is set per mode
    ('wire_hidden' uses polys, painted as background-filled green-outlined
    polygons for hidden-line wireframe); framebuffer
    is (index_bytes, w, h) -- 8-bit palette indices the frontend expands via
    Client.palette (tk) or blits as an 8bpp palettised DIB (gdi32). overlays
    are (x, y, text, color, anchor) with anchor in {'nw','center','sw'};
    color is one (r,g,b) for the whole block, or a list of per-line (r,g,b)
    (short lists extend with their last entry) -- the profiler HUD uses a
    list to tint just its total row by frame budget.
    menu is the overlay menu's view (title, rows) when open, else None."""
    mode: str
    segs: list = None                       # mode 'wire': line segments
    polys: list = None                      # mode 'flat': (points, color)
    framebuffer: tuple = None               # mode 'zbuf': (bytes, w, h)
    palette: list = None     # zbuf: tinted view palette (V_UpdatePalette)
    palette_version: int = 0  # bumps when palette changes; frontends key LUTs on it
    particles: list = field(default_factory=list)
    overlays: list = field(default_factory=list)
    crosshair: tuple = (0, 0)
    console: tuple = None    # (lines, input_line, cursor_col) when open, else None
    menu: tuple = None       # (title, [(label, value, selected), ...]) when open, else None
    pixel_aspect: float = 1.0  # zbuf: display rows taller by 1/this (CRT look)
    # pixel_aspect asks the frontend to display the framebuffer stretched to
    # h/pixel_aspect rows (values <1 make pixels taller, giving the VGA CRT look)


class Demo:
    """Drives .dem playback: holds the DemoReader and the timing/timedemo state
    (cl_demo.c CL_GetMessage). Reading is gated on cl.time so messages play at
    their recorded cadence; timedemo reads one per frame and reports fps. Task 4
    fleshes out the playback loop that consumes this state."""

    def __init__(self, reader, timedemo=False):
        self.reader = reader
        self.timedemo = timedemo
        self.finished = False
        self.frames = 0          # timedemo frame counter
        self.start_time = None   # wall clock at timedemo frame 1


class Client:
    def __init__(self, mapname):
        self._init_assets_only()        # inits demo_loop/demo_index/in_demo_loop
        # title demo loop: no map (or the "start" episode-select shim) boots into
        # the demo1->demo2->demo3 loop (CL_NextDemo/startdemos) instead of a live
        # level. Live construction (Client("e1m1")) takes the else branch and is
        # left byte-identical.
        if mapname in (None, "start"):
            # _next_demo -> _load_demo builds the render stack (self.rend) first;
            # _finish_construction registers the console + menu afterwards.
            # The closures registered there access self.rend lazily at invocation,
            # so the only ordering constraint is that self.rend exists when a
            # command actually runs -- not at registration time.
            self._next_demo()
            self._finish_construction()
        else:
            if not self._load_map(mapname):
                raise ValueError(f"no such map: maps/{mapname}.bsp")
            self._finish_construction()

    def _finish_construction(self):
        """Register the console + build the menu exactly once, after a map or
        demo has built self.rend. Shared by the live and title-demo-loop
        construction paths so neither double-registers."""
        if not getattr(self, "menu", None):
            self._register_console()
            self.menu = self._build_menu()

    def _init_assets_only(self):
        """The server-independent half of construction: pak, palette, colormap,
        sbar/console fonts, the mixer/audio backend, render-mode flags, and the
        persisted render settings -- everything that does NOT depend on a loaded
        map or a running server. Live __init__ calls this then _load_map; demo
        construction (Task 6) calls it then _load_demo. Does NOT register the
        console or build the menu (the caller does that after a map/demo loads,
        because _register_console touches self.rend)."""
        self.pak = Pak(PAK_PATH)
        self.progs_data = self.pak.read("progs.dat")
        pal = self.pak.read("gfx/palette.lmp")
        self.palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2])
                        for i in range(256)]
        # 64 light rows x 256 palette indices; the z-buffer renderer lights
        # every texel through it and returns an 8-bit indexed framebuffer.
        self.colormap = self.pak.read("gfx/colormap.lmp")[:64 * 256]
        # classic sprite status bar (sbar.c), drawn in zbuf mode when the
        # framebuffer is >=320 wide; the per-item/face timers are initialised
        # by _load_map (CL_ClearState) so they reset correctly on every level.
        self.sbar = Sbar(Wad(self.pak.read("gfx.wad")))
        # conchars UI text composited into the zbuf framebuffer (centerprint,
        # console, menu) -- the real Quake bitmap font, like the sbar. Reuses
        # the lump the Sbar already loaded; conback is the console backdrop.
        self.confont = ConFont(self.sbar.conchars)
        self.conback = load_qpic(self.pak.read("gfx/conback.lmp"))
        # intermission ("level complete") screen pics -- Sbar_IntermissionOverlay
        # draws these (the big digit pics live on the Sbar), not the conchars font.
        self.sb_complete = load_qpic(self.pak.read("gfx/complete.lmp"))
        self.sb_inter = load_qpic(self.pak.read("gfx/inter.lmp"))
        # screen colour shifts (view.c): contents/damage/bonus/powerup blend
        # the base palette into view_palette each frame (V_UpdatePalette)
        self.view_palette = self.palette
        self.palette_version = 0
        self._tint_key = ()
        self._cshift_damage = [255, 0, 0, 0.0]   # r, g, b, percent
        self._cshift_bonus = 0.0                 # gold flash percent
        # view feel (view.c): damage kick [time, roll, pitch], stair-step eye
        # smoothing, and the blended (pitch, yaw, roll) the renderers use
        self._v_dmg = [0.0, 0.0, 0.0]
        self._eye_oldz = 0.0
        self.view_angles = (0.0, 0.0, 0.0)
        self.eye_z_offset = 0.0
        self._missing_warned = set()   # maps not in the pak we've already flagged
        self._view_wh = (0, 0)         # window size; (0, 0) until resize()
        self._beam_models = {}         # bolt .mdl cache for lightning beams
        self.dlights = {}              # CL_AllocDlight pool: key -> [x,y,z,
        self._dlight_seq = 0           #   radius, die, decay, minlight]
        # inventory carried across changelevel (SV_SaveSpawnparms): parm1..16
        # from the previous level's SetChangeParms, plus the episode sigils
        self.spawn_parms = None
        self.serverflags = 0.0
        # The mixer is platform-agnostic; a backend (chosen by OS) opens the
        # output stream and flips mixer.ok on. Kept on self so its ctypes
        # callback trampoline isn't garbage-collected. No backend -> muted.
        # PQ_AUDIO=0 skips the OS backend entirely (headless test runs: the
        # CoreAudio callback thread can crash a sandboxed interpreter)
        self.mixer = snd.Mixer()
        self.audio = None
        if os.environ.get("PQ_AUDIO", "1") == "0":
            pass                            # muted by request
        elif sys.platform == "darwin":
            import mac
            self.audio = mac.CoreAudioBackend(self.mixer)
        elif sys.platform == "win32":
            import win
            self.audio = win.WinmmBackend(self.mixer)
        # else: runs muted until a linux backend is added

        # z-buffer mode blits a software framebuffer. Default on: the game boots
        # straight into the textured software rasteriser.
        self.zbuf = True
        self.textured = True            # texture-map world faces in z-buffer mode
        # filled-polygon (flat shading) mode flag
        self.flat = True
        # wireframe hidden-line removal: when set, wire mode renders through the
        # back-to-front (painter's) polygon path so walls occlude, instead of the
        # edge-only X-ray wireframe. Toggled by the `wire_hidden` console cvar.
        self.wire_hidden = False

        self.fps = 0.0
        # wall-clock uptime: advances every rendered frame even when the server
        # is paused (console/menu open), so blinking UI cursors keep flashing --
        # Quake's `realtime`, distinct from the frozen sv.time
        self._uptime = 0.0
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
        self._pixel_aspect = dict(ASPECT_MODES)["CRT"]   # default to the VGA
                                    # CRT 4:3 look; zbuf vertical pixel aspect,
                                    # persists across maps like _zbuf_scale
        # fixed textured render resolution (video-options menu), persists across
        # maps like _zbuf_scale; applied to each freshly built Renderer.
        self.video_res = DEFAULT_VIDEO_RES
        self.con = Console()
        # no demo playing in live mode; set here so live frames see no demo and
        # demo construction (Task 6) can flip it on after _load_demo.
        self.demo = None
        # title demo loop state (Task 6); defaulted here so every construction
        # path -- live, title loop, and the test's __new__ + _init_assets_only --
        # has them before _demo_frame / _play_named_demo read in_demo_loop.
        self.demo_loop = ["demo1", "demo2", "demo3"]
        self.demo_index = 0
        self.in_demo_loop = False
        # active DemoWriter while `record` is running; None when not recording.
        # The live loopback tees its per-frame datagram here (CL_WriteDemoMessage).
        self.recording = None

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

        self.mapname = mapname                    # bare name, for the "map" query
        self.demo = None                          # leave any demo: live server now
        # CL_ClearState (cl_main.c): cosmetic HUD timers reset on every new
        # server connection -- sv.time restarts at 0 each level, so stale
        # values from the old level would leave the pain face stuck and
        # pickup-flash frames blinking for the difference.
        self.item_gettime = [0.0] * 32
        self._prev_items = 0
        self.faceanimtime = 0.0
        self.bsp = Bsp(self.pak.read(path))
        self.rend = Renderer(self.bsp, self.palette, self.colormap)
        self.rend.zbuf_scale = self._zbuf_scale   # keep the console's chosen scale
        self.rend.pixel_aspect = self._pixel_aspect  # keep the chosen aspect
        self.rend.video_res = self.video_res      # keep the menu's chosen resolution
        self.rend.resize(self.rend.width, self.rend.height)  # rebuild buffer at the chosen res
        self.phys = Physics(self.bsp)

        # QuakeC server: spawn the level's entities and run their logic. Doors,
        # buttons and lifts are entities; their brush models are drawn at the
        # origins the QC sets, and invisible triggers no longer render.
        self.sv = Server(Progs(self.progs_data), bsp=self.bsp, mapname=path,
                         skill=skill, physics=self.phys, pak=self.pak,
                         serverflags=self.serverflags)
        self.sv.load_level()

        self._load_render_models(self.sv.model_precache)

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
        # the engine's own ambient loops (S_Init precaches these): water/sky
        # levels come from the listener leaf each frame (update_ambients)
        for name in snd.AMBIENT_SOUNDS:
            snd_path = "sound/" + name
            if snd_path in self.pak.files:
                self.mixer.precache(name, self.pak.read(snd_path))
        self.sv.snd = self.mixer

        # player origin from the level's spawn point (eye sits VIEW_HEIGHT above)
        (sx, sy, sz), yaw = self.bsp.find_spawn()
        self.pos = [sx, sy, sz]
        self.vel = [0.0, 0.0, 0.0]
        self.bobtime = 0.0          # wall-clock phase for the weapon bob
        self.onground = False
        self.waterlevel = 0
        self.watertype = CONTENTS_EMPTY   # fed to the player edict for QC WaterMove
        self.noclip = False
        self.flymode = False
        self.dlights = {}           # old map's lights die with it
        self.yaw = yaw
        self.pitch = 0.0

        # a client edict driven by the camera: gives monsters a target and gives
        # fired shots an attacker (so QC's damage/death logic runs)
        self.sv.spawn_player(tuple(self.pos), (self.pitch, self.yaw, 0.0),
                             parms=self.spawn_parms)

        # keep the window size across map swaps: overlays/crosshair lay out
        # from _view_wh every frame, and the gdi/cocoa frontends only call
        # resize() when the WINDOW size changes -- zeroing it here pushed the
        # status bar to y=-8 and the crosshair to (0,0) after a changelevel
        # (the tk frontend masked it by resizing every tick). The rebuilt
        # renderer gets sized from it too, replacing its construction default.
        if self._view_wh != (0, 0):
            self.rend.resize(*self._view_wh)

        # Faithful loopback: the renderer reads a client-side entity list (cl)
        # fed by the server's protocol-15 datagram, not the server edicts
        # directly. Build the signon once per level (here, and again on every
        # changelevel since _load_map reruns) so cl's model/sound precache and
        # baselines exist before the first per-frame datagram. (Rebuilt after
        # the player spawn so the player edict gets a baseline too.)
        self.cl = ClientState()
        self.sv.create_baseline()
        sw = MsgWriter()
        for block in build_signon(self.sv):
            sw.data += block
        self.cl.parse_message(MsgReader(sw.data))
        self.scene = SceneFromClient(self.cl)
        return True

    def _load_render_models(self, model_precache):
        """Load the .mdl/.spr/external-.bsp render models indexed to match
        modelindex, from a precache name list (the live sv's or a demo cl's).
        Shared by _load_map and _load_demo so both build self.models/smodels/
        bmodels identically."""
        # load the .mdl models the level precached, indexed to match modelindex
        self.models = [None] * len(model_precache)
        for idx, name in enumerate(model_precache):
            if name.endswith(".mdl") and name in self.pak.files:
                try:
                    self.models[idx] = Mdl(self.pak.read(name), self.palette)
                except Exception as e:
                    print(f"mdl load failed for {name}: {e}")
        # sprite models (.spr): explosions, bubbles -- billboarded by the
        # zbuf renderer, indexed by modelindex like the .mdl list
        self.smodels = [None] * len(model_precache)
        for idx, name in enumerate(model_precache):
            if name.endswith(".spr") and name in self.pak.files:
                try:
                    self.smodels[idx] = Spr(self.pak.read(name))
                except Exception as e:
                    print(f"spr load failed for {name}: {e}")
        # load the external .bsp pickup models (health/ammo boxes -- maps/b_*.bsp),
        # also indexed by modelindex. Skip index 1, the world map itself.
        self.bmodels = [None] * len(model_precache)
        for idx, name in enumerate(model_precache):
            if idx > 1 and name.endswith(".bsp") and name in self.pak.files:
                try:
                    self.bmodels[idx] = PickupModel(Bsp(self.pak.read(name)),
                                                    self.palette)
                except Exception as e:
                    print(f"bsp pickup load failed for {name}: {e}")
        # first-person weapon view models, loaded on demand (v_*.mdl are not all
        # precached); path -> Mdl, or None if the file is missing / failed
        self._vmodels = {}

    def _load_demo(self, blob):
        """Set up the render stack to play a .dem: parse the signon (first demo
        frame) into a fresh cl, take the map + precache from cl, and build
        bsp/renderer/physics/models WITHOUT a server. Sets self.demo to the
        playback controller (Task 4 fleshes out the frame loop). Returns False
        on an empty/invalid demo."""
        from quake.demo import DemoReader
        reader = DemoReader(blob)
        self.cl = ClientState()
        # the signon now spans multiple demo frames (the WinQuake 3-phase
        # handshake; the genuine shareware demos also use 3). Parse frames until
        # the signon is complete: serverinfo (model_precache filled) AND the
        # spawn-block svc_time seen (cl.mtime[0] set).
        first_angles = None
        # the signon is at most a handful of frames; cap the scan so a malformed
        # demo (precache but never a spawn svc_time) can't spin to end-of-file
        for _ in range(64):
            fr = reader.next_frame()
            if fr is None:
                self.con.print("demo: no playable frames")
                return False
            if first_angles is None:
                first_angles = fr[0]
            self.cl.parse_message(MsgReader(fr[1]))
            if (self.cl.model_precache and len(self.cl.model_precache) > 1
                    and self.cl.mtime[0] > 0.0):
                break        # serverinfo (precache) + spawn svc_time seen -> ready
        else:
            self.con.print("demo: signon never completed")
            return False
        self.cl.viewangles = list(first_angles)
        self.cl.mviewangles[0] = list(first_angles)
        self.cl.mviewangles[1] = list(first_angles)
        mappath = self.cl.model_precache[1]            # "maps/xxx.bsp"
        self.mapname = mappath[len("maps/"):-len(".bsp")]
        self.bsp = Bsp(self.pak.read(mappath))
        self.rend = Renderer(self.bsp, self.palette, self.colormap)
        self.rend.zbuf_scale = self._zbuf_scale
        self.rend.pixel_aspect = self._pixel_aspect
        self.rend.video_res = self.video_res
        self.rend.resize(self.rend.width, self.rend.height)
        self.phys = Physics(self.bsp)
        self._load_render_models(self.cl.model_precache)
        # sound precache for the demo (so svc_sound could play later)
        self.mixer.stop_all()
        for name in self.cl.sound_precache[1:]:
            snd_path = "sound/" + name
            if name and snd_path in self.pak.files:
                self.mixer.precache(name, self.pak.read(snd_path))
        # the engine's own ambient loops (water/sky), keyed off the listener leaf
        # each frame by update_ambients -- same as _load_map
        for name in snd.AMBIENT_SOUNDS:
            snd_path = "sound/" + name
            if snd_path in self.pak.files:
                self.mixer.precache(name, self.pak.read(snd_path))
        # start the looping static ambients the signon spawned (fans, drips):
        # svc_spawnstaticsound was parsed into cl.static_sounds above
        for sname, svol, satten, sorg in self.cl.static_sounds:
            self.mixer.start_sound(0, 0, sname, svol, satten, sorg, loop=True)
        self.scene = SceneFromClient(self.cl)
        self.sv = None                                  # no server in demo mode
        # CL_ClearState cosmetic HUD timers (mirror _load_map)
        self.item_gettime = [0.0] * 32
        self._prev_items = 0
        self.faceanimtime = 0.0
        self.dlights = {}
        self.intermission = False
        if self._view_wh != (0, 0):
            self.rend.resize(*self._view_wh)
        # camera/player state the live path sets in _load_map (find_spawn); the
        # demo drives pos/yaw/pitch from cl every frame, but these must exist so
        # the shared render/view code never reads an unset attribute on frame 1.
        self.pos = [0.0, 0.0, 0.0]
        self.vel = [0.0, 0.0, 0.0]    # view-bob / HUD speed read this
        self.yaw = 0.0
        self.pitch = 0.0
        self.bobtime = 0.0
        self.onground = False
        self.waterlevel = 0
        self.watertype = CONTENTS_EMPTY
        self.noclip = False
        self.flymode = False
        self.demo = Demo(reader)                         # controller (Task 4)
        # the console binds to self.rend (built above); register it and build the
        # menu if the caller booted straight into a demo (Client.__new__ +
        # _init_assets_only + _load_demo) rather than through live __init__.
        if not getattr(self, "menu", None):
            self._register_console()
            self.menu = self._build_menu()
        return True

    # ---- demo vs live render source (Task 4) ----
    # Each returns the cl/scene value in demo mode and EXACTLY the prior
    # self.sv.X value live, so factoring the render block through these leaves
    # live output byte-identical.
    def _cur_time(self):
        return self.cl.time if self.demo is not None else self.sv.time

    def _cur_hud(self):
        return self.scene.hud_status() if self.demo is not None else self.sv.hud_status()

    def _cur_lightstyles(self):
        return self.cl.lightstyles if self.demo is not None else self.sv.lightstyles

    def _cur_particles(self):
        # cl.particles and sv.particles are now the SAME renderer-native shape
        # ([x,y,z, vx,vy,vz, color, die, type, ramp], quake/particles.py), so no
        # remap is needed -- pick the right source for the mode.
        return self.cl.particles if self.demo is not None else self.sv.particles

    def _cur_health(self):
        return (self.scene.player_health() if self.demo is not None
                else self.sv.player_health())

    def _cur_intermission(self):
        return (self.scene.intermission_active() if self.demo is not None
                else self.sv.intermission_active())

    def _cur_intermission_stats(self):
        return (self.scene.intermission_stats() if self.demo is not None
                else self.sv.intermission_stats())

    def _cur_view_weapon(self):
        return self.scene.view_weapon() if self.demo is not None else self.sv.view_weapon()

    def _cur_center_msg(self):
        return self.scene.center_msg if self.demo is not None else self.sv.center_msg

    def _light_source(self):
        """The source of light_entities()/dlight_events for _update_dlights:
        the SceneFromClient adapter in demo mode, the live server otherwise."""
        return self.scene if self.demo is not None else self.sv

    def resize(self, w, h):
        self._view_wh = (w, h)
        # the renderer may not exist yet (a demo Client resized before its first
        # _load_demo); _load_demo/_load_map apply the stored _view_wh when built.
        if getattr(self, "rend", None) is not None:
            self.rend.resize(w, h)

    def shutdown(self):
        """Tear down the audio backend deterministically on quit, before the
        frontend destroys its window and the interpreter exits. Doing it here
        (rather than leaving it to the backend's atexit backstop) stops the
        CoreAudio callback thread while the process is still healthy. Idempotent
        and safe to call with no backend."""
        if self.audio is not None:
            self.audio.shutdown()

    # ---- screen colour shifts (view.c V_UpdatePalette) ----
    def _update_palette(self, dt):
        """Blend the four colour shifts over the base palette: contents
        (water/slime/lava), damage (red, fed by the player's dmg_take/dmg_save
        and decaying 150/s), bonus (gold pickup flash, 100/s) and powerup
        (quad/suit/ring/pent from .items). Rebuilds view_palette and bumps
        palette_version only when the blend actually changes."""
        if self.demo is not None:
            return self._update_palette_demo(dt)
        sv, f, vm = self.sv, self.sv.f, self.sv.vm
        e = sv.player

        # damage: T_Damage stamps dmg_take/dmg_save; consume like svc_damage
        dmg = self._cshift_damage
        if e and not vm.free[e]:
            blood = vm.fget_f(e, f["dmg_take"])
            armor = vm.fget_f(e, f["dmg_save"])
            if blood or armor:
                vm.fset_f(e, f["dmg_take"], 0.0)
                vm.fset_f(e, f["dmg_save"], 0.0)
                count = max(10.0, 0.5 * blood + 0.5 * armor)
                dmg[3] = min(150.0, dmg[3] + 3.0 * count)
                dmg[:3] = ((200, 100, 100) if armor > blood else
                           (220, 50, 50) if armor else (255, 0, 0))
                # V_ParseDamage's other half: kick the view toward whatever
                # hurt us (decayed over v_kicktime by _update_view_feel)
                src = vm.fget_i(e, f["dmg_inflictor"])
                ix, iy, iz = vm.fget_v(src, f["origin"])
                nx, ny, nz = vm.fget_v(src, f["mins"])
                xx, xy, xz = vm.fget_v(src, f["maxs"])
                px, py, pz = vm.fget_v(e, f["origin"])
                fr = (ix + 0.5 * (nx + xx) - px, iy + 0.5 * (ny + xy) - py,
                      iz + 0.5 * (nz + xz) - pz)
                ln = math.sqrt(fr[0] ** 2 + fr[1] ** 2 + fr[2] ** 2) or 1.0
                fwd, right, _up = angle_vectors(self.yaw, self.pitch)
                side = (fr[0] * right[0] + fr[1] * right[1]
                        + fr[2] * right[2]) / ln
                self._v_dmg[1] = count * side * 0.6         # v_kickroll
                side = (fr[0] * fwd[0] + fr[1] * fwd[1] + fr[2] * fwd[2]) / ln
                self._v_dmg[2] = count * side * 0.6         # v_kickpitch
                self._v_dmg[0] = 0.5                        # v_kicktime
                self.faceanimtime = self.sv.time + 0.2      # V_ParseDamage
        dmg[3] = max(0.0, dmg[3] - 150.0 * dt)

        if sv.bonus_flash:                      # stuffcmd "bf" (V_BonusFlash)
            sv.bonus_flash = False
            self._cshift_bonus = 50.0
        self._cshift_bonus = max(0.0, self._cshift_bonus - 100.0 * dt)

        shifts = []
        # V_SetContentsColor keys off the *view leaf*: tint only when the eye
        # is submerged, not when standing ankle-deep (self.watertype tracks
        # the feet for QC WaterMove)
        wt = CONTENTS_EMPTY
        if self.phys is not None:
            vofs = sv.player_view_ofs()
            eye_z = self.pos[2] + (vofs[2] if vofs else VIEW_HEIGHT)
            wt = self.phys.point_contents_0((self.pos[0], self.pos[1], eye_z))
        if wt == -3:
            shifts.append((130, 80, 50, 128))   # water
        elif wt == -4:
            shifts.append((0, 25, 5, 150))      # slime
        elif wt <= -5:
            shifts.append((255, 80, 0, 150))    # lava (and sky, like the C)
        if dmg[3] > 0:
            shifts.append((dmg[0], dmg[1], dmg[2], int(dmg[3])))
        if self._cshift_bonus > 0:
            shifts.append((215, 186, 69, int(self._cshift_bonus)))
        items = int(vm.fget_f(e, f["items"])) if e and not vm.free[e] else 0
        if items & 4194304:                     # IT_QUAD
            shifts.append((0, 0, 255, 30))
        elif items & 2097152:                   # IT_SUIT
            shifts.append((0, 255, 0, 20))
        elif items & 524288:                    # IT_INVISIBILITY
            shifts.append((100, 100, 100, 100))
        elif items & 1048576:                   # IT_INVULNERABILITY
            shifts.append((255, 255, 0, 30))

        key = tuple(s for s in shifts if s[3] > 0)
        if key == self._tint_key:
            return
        self._tint_key = key
        self.palette_version += 1
        if not key:
            self.view_palette = self.palette
            return
        pal = []
        for r, g, b in self.palette:
            for sr, sg, sb, pct in key:
                r += (pct * (sr - r)) >> 8
                g += (pct * (sg - g)) >> 8
                b += (pct * (sb - b)) >> 8
            pal.append((r, g, b))
        self.view_palette = pal

    def _update_palette_demo(self, dt):
        """Demo-mode V_UpdatePalette: there is no server edict to read dmg_take /
        dmg_inflictor / bonus_flash from, so blend only the two shifts that are
        recoverable from cl -- the contents tint (eye leaf via the BSP) and the
        powerup tint (cl.items). Same view_palette/palette_version contract as
        the live path so the renderer sees an identical interface."""
        shifts = []
        wt = CONTENTS_EMPTY
        if self.phys is not None:
            eye_z = self.pos[2] + self.cl.view_height
            wt = self.phys.point_contents_0((self.pos[0], self.pos[1], eye_z))
        if wt == -3:
            shifts.append((130, 80, 50, 128))   # water
        elif wt == -4:
            shifts.append((0, 25, 5, 150))      # slime
        elif wt <= -5:
            shifts.append((255, 80, 0, 150))    # lava (and sky, like the C)
        items = self.cl.items
        if items & 4194304:                     # IT_QUAD
            shifts.append((0, 0, 255, 30))
        elif items & 2097152:                   # IT_SUIT
            shifts.append((0, 255, 0, 20))
        elif items & 524288:                    # IT_INVISIBILITY
            shifts.append((100, 100, 100, 100))
        elif items & 1048576:                   # IT_INVULNERABILITY
            shifts.append((255, 255, 0, 30))

        key = tuple(s for s in shifts if s[3] > 0)
        if key == self._tint_key:
            return
        self._tint_key = key
        self.palette_version += 1
        if not key:
            self.view_palette = self.palette
            return
        pal = []
        for r, g, b in self.palette:
            for sr, sg, sb, pct in key:
                r += (pct * (sr - r)) >> 8
                g += (pct * (sg - g)) >> 8
                b += (pct * (sb - b)) >> 8
            pal.append((r, g, b))
        self.view_palette = pal

    def _update_dlights(self, dt):
        """CL_AllocDlight / CL_DecayLights: refresh the dynamic-light pool
        from entity effects (muzzle flash one-shot, bright/dim light, rocket
        glow via the model flag) and the server's one-shot explosion events;
        decay radii and expire dead lights."""
        src = self._light_source()
        now = self._cur_time()
        dl = self.dlights
        for e, org, eff, rocket in src.light_entities():
            x, y, z = org
            if eff & 2:                          # EF_MUZZLEFLASH
                dl[e] = [x, y, z + 16.0, 200.0 + random.random() * 32.0,
                         now + 0.1, 0.0, 32.0]
            elif eff & 4:                        # EF_BRIGHTLIGHT
                dl[e] = [x, y, z + 16.0, 400.0 + random.random() * 32.0,
                         now + 0.001, 0.0, 0.0]
            elif eff & 8:                        # EF_DIMLIGHT (powerups)
                dl[e] = [x, y, z, 200.0 + random.random() * 32.0,
                         now + 0.001, 0.0, 0.0]
            elif rocket:
                dl[e] = [x, y, z, 200.0, now + 0.01, 0.0, 0.0]
        for org, radius, die, decay in src.dlight_events:
            self._dlight_seq += 1
            dl[("ev", self._dlight_seq)] = [org[0], org[1], org[2], radius,
                                            die, decay, 0.0]
        src.dlight_events.clear()
        for k in list(dl):                       # CL_DecayLights
            L = dl[k]
            L[3] -= L[5] * dt
            if L[4] < now or L[3] <= 0.0:
                del dl[k]

    def _update_view_feel(self, dt, dead):
        """V_CalcViewRoll plus the punchangle and damage-kick parts of
        V_CalcRefdef: blend strafe lean (cl_rollangle 2 deg at cl_rollspeed
        200), the QC's .punchangle weapon kick, and the decaying damage kick
        into view_angles = (pitch, yaw, roll); lag the eye 80 u/s behind
        stair-step pops (oldz smoothing, max 12 units)."""
        pitch, yaw = self.pitch, self.yaw
        if self.intermission:
            # V_CalcIntermissionRefdef forces v_idlescale=1, so V_AddIdle drifts
            # the view angles gently while the camera origin stays put -- the one
            # place Quake's idle sway is always on. Off the wall clock so it
            # animates even though the game is frozen at the spot.
            t = self._uptime
            self.view_angles = (
                pitch + math.sin(t * V_IPITCH_CYCLE) * V_IPITCH_LEVEL,
                yaw + math.sin(t * V_IYAW_CYCLE) * V_IYAW_LEVEL,
                math.sin(t * V_IROLL_CYCLE) * V_IROLL_LEVEL)
            return
        _fwd, right, _up = angle_vectors(yaw, pitch)
        side = (self.vel[0] * right[0] + self.vel[1] * right[1]
                + self.vel[2] * right[2])
        sign = -1.0 if side < 0 else 1.0
        side = abs(side)
        roll = (side * 2.0 / 200.0 if side < 200.0 else 2.0) * sign
        vd = self._v_dmg
        if vd[0] > 0:
            roll += vd[0] / 0.5 * vd[1]
            pitch += vd[0] / 0.5 * vd[2]
            vd[0] -= dt
        if dead:
            roll = 80.0          # V_CalcViewRoll: dead men see the floor sideways
        if self.demo is not None:
            px, py, pz = self.cl.punchangle    # parsed from clientdata, no edict
        else:
            sv, f, vm = self.sv, self.sv.f, self.sv.vm
            e = sv.player
            px = py = pz = 0.0
            if e and not vm.free[e]:
                px, py, pz = vm.fget_v(e, f["punchangle"])
        pitch += px
        yaw += py
        roll += pz
        self.view_angles = (pitch, yaw, roll)

        z = self.pos[2]          # smooth out stair step ups (V_CalcRefdef oldz)
        if self.onground and z - self._eye_oldz > 0:
            self._eye_oldz = min(z, self._eye_oldz + 80.0 * dt)
            if z - self._eye_oldz > 12.0:
                self._eye_oldz = z - 12.0
            self.eye_z_offset = self._eye_oldz - z
        else:
            self._eye_oldz = z
            self.eye_z_offset = 0.0

    # ---- save / load (Host_Savegame_f / Host_Loadgame_f) ----
    def save_game(self, path):
        """Write the current game to `path`. Refused (False) while dead or at
        an intermission, like the original."""
        if self.sv.player_health() <= 0 or self.sv.intermission_active():
            return False
        with open(path, "w") as fp:
            fp.write(self.sv.save_text())
        return True

    def load_game(self, path):
        """Restore a save: respawn its map normally (rebuilding precaches the
        way SV_SpawnServer does before the parse), overwrite the world with
        the saved blocks, and point the camera at the restored player.
        Returns False on a missing/corrupt file or missing map."""
        try:
            with open(path) as fp:
                lines = fp.read().split("\n")
            if int(lines[0]) != 5:                  # SAVEGAME_VERSION
                return False
            parms = [float(x) for x in lines[2:18]]
            skill = int(float(lines[18]))
            mapname = lines[19].strip()
            time = float(lines[20])
            styles = lines[21:85]
            body = "\n".join(lines[85:])
        except (OSError, ValueError, IndexError):
            return False
        self.spawn_parms = parms
        if not self._load_map(mapname, skill=skill):
            return False
        self.sv.restore_save(time, styles, body)
        self.sv.spawn_parms = parms
        org = self.sv.player_origin()
        if org:
            self.pos = list(org)
            ang = self.sv.player_angles() or (0.0, 0.0, 0.0)
            self.yaw = ang[1]
            va = self.sv.vm.fget_v(self.sv.player, self.sv.f["v_angle"])
            self.pitch = va[0]
        self.vel = [0.0, 0.0, 0.0]
        return True

    def _save_path(self, name):
        return os.path.join(os.path.dirname(PAK_PATH), name + ".sav")

    def _cmd_save(self, args):
        if not args:
            self.con.print("usage: save <name>")
            return
        if self.save_game(self._save_path(args[0])):
            self.con.print(f"saved {args[0]}")
        else:
            self.con.print("can't save: dead or intermission")

    def _cmd_load(self, args):
        if not args:
            self.con.print("usage: load <name>")
            return
        if self.load_game(self._save_path(args[0])):
            self.con.print(f"loaded {args[0]}")
        else:
            self.con.print(f"load failed: {args[0]}")

    def _cmd_logperf(self, args):
        """Toggle per-frame CSV perf logging. `logperf <file>` starts; a bare
        `logperf` starts logging to a timestamped `perf-<ISO>.csv`; a bare
        `logperf` while already logging stops and reports the path + frame count."""
        result = PROFILER.stop_log()
        if result is not None:
            path, n = result
            self.con.print(f"logged {n} frames to {path}")
            return
        # colon-free ISO 8601 so the name is valid on every filesystem
        path = args[0] if args else datetime.now().strftime("perf-%Y-%m-%dT%H-%M-%S.csv")
        PROFILER.start_log(path)
        self.con.print(f"logging perf to {path} (run logperf again to stop)")

    def _change_level(self, target):
        """Consume a pending changelevel: load the next map, carrying the skill
        the player chose at the start-map setskill triggers and the inventory
        SetChangeParms saves. A death restart skips the save (Host_Restart_f):
        the player respawns with the loadout they entered the level with."""
        if not self.sv.changelevel_restart:
            parms = self.sv.save_spawn_parms()
            if parms is not None:
                self.spawn_parms = parms
            self.serverflags = self.sv.serverflags
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

        if self.flymode:
            # fly cheat (Host_Fly_f, MOVETYPE_FLY): free flight along the full
            # view direction, but clipped against the world -- no phasing
            forward, right, up = angle_vectors(self.yaw, self.pitch)
            speed = MAXSPEED * (1.6 if fast else 1.0)
            self.vel = [(forward[i] * fwd + right[i] * strafe
                         + up[i] * inp.move_up) * speed for i in range(3)]
            self.phys.fly_move(self.pos, self.vel, dt)
            self.onground = False
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
        self.onground, self.waterlevel, self.watertype = self.phys.player_move(
            self.pos, self.vel, wishdir, wishspeed,
            forward, right, fwd * speed, strafe * speed, upmove, speed,
            self.onground, jump, step)
        if self.phys.jumped and self.sv.player:
            # the engine owns the jump impulse, so it owns the QC's sound too:
            # client.qc PlayerJump sound(self, CHAN_BODY, "player/plyrjmp8.wav",
            # 1, ATTN_NORM); CHAN_BODY is 4
            self.sv._start_sound(self.sv.player, 4, "player/plyrjmp8.wav",
                                 1.0, 1.0)

    # ---- render-state toggles (shared by the hotkeys and the console) ----
    def _set_player_movetype(self, mt):
        """Stamp the player edict's movetype so the QC (and run_frame's
        dispatch, which skips host-driven FLY/NOCLIP players) can tell."""
        sv = self.sv
        if sv.player and not sv.vm.free[sv.player]:
            sv.vm.fset_f(sv.player, sv.f["movetype"], float(mt))

    def _toggle_noclip(self):
        self.noclip = not self.noclip
        self.flymode = False
        self.vel = [0.0, 0.0, 0.0]
        self._set_player_movetype(MOVETYPE_NOCLIP if self.noclip
                                  else MOVETYPE_WALK)

    def _toggle_fly(self):
        self.flymode = not self.flymode
        self.noclip = False
        self.vel = [0.0, 0.0, 0.0]
        self._set_player_movetype(MOVETYPE_FLY if self.flymode
                                  else MOVETYPE_WALK)

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

    def set_pixel_aspect(self, v):
        """Vertical pixel aspect for the zbuf view (1.0 square, 5/6 VGA CRT);
        clamped to a sane range. Takes effect next frame -- the projection
        reads it live, no framebuffer rebuild needed."""
        v = max(0.5, min(1.0, float(v)))
        self._pixel_aspect = v
        self.rend.pixel_aspect = v
        self._sync_aspect_menu()

    def _sync_aspect_menu(self):
        """Point the video menu's Aspect row at the option nearest the live
        value, so a console `pixel_aspect` set is reflected when the menu
        reopens (and 0.8333333 resolves to CRT despite float imprecision)."""
        menu = getattr(self, "menu", None)
        if menu is None:
            return
        for item in menu.items:
            if getattr(item, "title", None) == "Aspect":
                item.index = min(range(len(item.options)),
                                 key=lambda j: abs(item.options[j][1]
                                                   - self._pixel_aspect))
                break

    def _menu_back(self):
        self.menu.active = False

    def _build_menu(self):
        """Build the Escape overlay menu: Resolution (cycles VIDEO_MODES), Aspect
        (cycles ASPECT_MODES), Back, Quit. Closures bind to this Client's methods,
        like console commands."""
        idx = next((i for i, (_, v) in enumerate(VIDEO_MODES)
                    if v == self.video_res), 0)
        res = ChoiceItem("Resolution", VIDEO_MODES, idx, self.set_video_res)
        aidx = next((i for i, (_, v) in enumerate(ASPECT_MODES)
                     if v == self._pixel_aspect), 0)
        aspect = ChoiceItem("Aspect", ASPECT_MODES, aidx, self.set_pixel_aspect)
        back = ActionItem("Back", self._menu_back)
        quit_item = ActionItem("Quit", self._cmd_quit_menu)
        return Menu("VIDEO OPTIONS", [res, aspect, back, quit_item])

    def _cmd_quit_menu(self):
        self.quit_requested = True

    # ---- console registration / commands ----
    def _register_console(self):
        """Register the built-in commands and cvars for this Client. Called by
        _finish_construction after a map or demo has built self.rend. The
        closures capture `self` and access self.rend lazily at invocation, so
        registration just needs to happen before any command is actually run."""
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
        con.register_command("logperf", self._cmd_logperf,
                             "logperf [file]: start/stop per-frame CSV perf logging "
                             "(file defaults to a timestamped perf-<ISO>.csv)")
        con.register_command("map", self._cmd_map, "map <name>: load a level")
        con.register_command("record", self._cmd_record,
                             "record <name> [map]: record live play to a .dem file")
        con.register_command("playdemo", self._cmd_playdemo,
                             "playdemo <name>: play a .dem file")
        con.register_command("timedemo", self._cmd_timedemo,
                             "timedemo <name>: play a demo flat-out, report fps")
        con.register_command("stop", self._cmd_stopdemo,
                             "stop: end demo playback, return to a live map")
        con.register_command("save", self._cmd_save, "save <name>: save the game")
        con.register_command("load", self._cmd_load, "load <name>: load a save")
        con.register_command("god", self._cmd_god, "toggle god mode")
        con.register_command("notarget", self._cmd_notarget,
                             "toggle monster blindness")
        con.register_command("fly", mode_cmd(self._toggle_fly),
                             "toggle fly mode (collides, unlike noclip)")
        con.register_command("kill", self._cmd_kill, "suicide (QC ClientKill)")
        con.register_command("impulse", self._cmd_impulse,
                             "impulse <n>: send a QC impulse (9 = cheat)")
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
        con.register_cvar("pixel_aspect", self._pixel_aspect,
                          on_change=self._on_pixel_aspect,
                          help="zbuf pixel aspect: 1.0 square, 0.8333 VGA CRT")
        con.register_cvar("wire_hidden", 0, on_change=self._on_wire_hidden,
                          help="wireframe hidden-line removal (occlude walls): 0/1")

    def _on_wire_hidden(self, cv):
        self.wire_hidden = cv.as_bool()
        self.con.print(f"wire_hidden {1 if self.wire_hidden else 0}")

    def _on_zbuf_scale(self, cv):
        v = max(1, min(16, cv.as_int()))
        cv.value = str(v)                         # write the clamped value back
        self._zbuf_scale = v
        self.rend.zbuf_scale = v
        if self._view_wh != (0, 0):
            self.rend.resize(*self._view_wh)
        self.con.print(f"zbuf_scale {v}")

    def _on_pixel_aspect(self, cv):
        self.set_pixel_aspect(cv.as_float())
        cv.value = str(self._pixel_aspect)        # write the clamped value back

    def _cmd_notarget(self, args):
        on = self.sv.toggle_notarget()
        self.con.print(f"notarget {'ON' if on else 'OFF'}")

    def _cmd_kill(self, args):
        self.sv._exec_named("ClientKill", self.sv.player)

    def _cmd_impulse(self, args):
        if not args:
            self.con.print("usage: impulse <n>")
            return
        try:
            self.sv.pending_impulse = int(args[0])
        except ValueError:
            self.con.print(f"impulse: not a number: {args[0]}")

    def _cmd_map(self, args):
        if not args:
            self.con.print(f"map: {self.mapname}")
            return
        # Host_Map_f starts a new game: default loadout, no episode sigils
        self.spawn_parms = None
        self.serverflags = 0.0
        if self._load_map(args[0]):               # rebuilds rend/sv; prints its own miss
            self.con.print(f"loading {args[0]}")

    def _cmd_record(self, args):
        """record <name> [map]: start a fresh game on the map and capture the
        live protocol-15 stream to <name>.dem (CL_Record_f). The signon is the
        demo's first frame; each subsequent live frame's datagram is teed in the
        loopback drive. `stop` finishes it."""
        if len(args) < 1:
            self.con.print("usage: record <name> [map]")
            return
        name = args[0]
        mapname = args[1] if len(args) > 1 else self.mapname
        # Host_Record_f loads the level: start a new game (default loadout, no
        # sigils) like the `map` command -- this rebuilds sv + cl + the signon.
        self.spawn_parms = None
        self.serverflags = 0.0
        if not self._load_map(mapname):
            self.con.print(f"record: no such map: {mapname}")
            return
        path = name if name.endswith(".dem") else name + ".dem"
        path = os.path.join(os.path.dirname(PAK_PATH), path)
        try:
            fp = open(path, "wb")
        except OSError as e:
            self.con.print(f"record: {e}")
            return
        from quake.demo import DemoWriter
        self.recording = DemoWriter(fp, cdtrack="0")
        # write the signon as the demo's first three frames -- the 3-phase
        # WinQuake handshake (svc_signonnum 1/2/3). create_baseline ran in
        # _load_map.
        for block in build_signon(self.sv):
            self.recording.write_frame((self.pitch, self.yaw, 0.0), block)
        self.con.print(f"recording to {path}")

    def _cmd_playdemo(self, args):
        """playdemo <name>: play a .dem file (CL_PlayDemo_f). Looks in the pak
        first (the shareware demos live there), then the filesystem."""
        if not args:
            self.con.print("usage: playdemo <name>")
            return
        self._play_named_demo(args[0])

    def _cmd_timedemo(self, args):
        """timedemo <name>: play a demo flat-out (one message per frame) and
        report average fps (CL_TimeDemo_f). _demo_frame calls _finish_timedemo
        when the run ends."""
        if not args:
            self.con.print("usage: timedemo <name>")
            return
        if self._play_named_demo(args[0]):
            self.demo.timedemo = True

    def _next_demo(self):
        """CL_NextDemo: play the next demo in the title loop, wrapping. Sets
        in_demo_loop so the demo auto-advances to the following one on finish."""
        if not self.demo_loop:
            return
        name = self.demo_loop[self.demo_index % len(self.demo_loop)]
        self.demo_index += 1
        self._play_named_demo(name)        # clears in_demo_loop...
        self.in_demo_loop = True           # ...we re-set it for the loop

    def _play_named_demo(self, name):
        """Load and start a named demo, pak-first then filesystem (.dem suffix
        optional). Returns False if not found / invalid. An explicit command
        (not the title loop) -- clears in_demo_loop so the demo does not
        auto-advance into the loop when it finishes."""
        fn = name if name.endswith(".dem") else name + ".dem"
        blob = None
        if fn in self.pak.files:
            blob = self.pak.read(fn)
        elif os.path.exists(fn):
            with open(fn, "rb") as fh:
                blob = fh.read()
        if blob is None:
            self.con.print(f"playdemo: not found: {fn}")
            return False
        self.con.active = False
        self.in_demo_loop = False        # explicit command: no auto-advance
        if not self._load_demo(blob):
            self.con.print(f"playdemo: failed: {fn}")
            return False
        return True

    def _cmd_stopdemo(self, args):
        """stop: end demo playback and return to a live map (Host_Stopdemo_f),
        so the renderer has a server again."""
        if self.recording is not None:   # finish a recording (Host_Stop_f) first
            self.recording.close()
            self.recording = None
            self.con.print("stopped recording")
            return
        self.in_demo_loop = False        # explicit stop: leave the title loop
        if self.demo is not None:
            self.demo = None
            self.con.print("demo stopped")
        self._cmd_map(["e1m1"])

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

    def _intermission_block(self, ist):
        """Sbar_IntermissionOverlay's text: completed time (m:ss), secrets and
        kills tallies. Shared by the zbuf framebuffer composite and the wire/
        flat overlay path so the two never diverge."""
        mins, secs = divmod(ist["time"], 60)
        return ("LEVEL COMPLETE\n\n"
                f"Time      {mins}:{secs:02d}\n"
                f"Secrets   {ist['secrets']} / {ist['total_secrets']}\n"
                f"Kills     {ist['monsters']} / {ist['total_monsters']}")

    def _composite_zbuf_ui(self, fb, vw, vh, fh):
        """zbuf mode: draw centerprint/intermission, console, and menu into the
        framebuffer with the conchars font -- the real Quake bitmap UI, drawn
        like the sbar -- instead of handing them to the frontend as OS-native
        overlays. vw/vh are the 3D view region (vh excludes the appended sbar
        rows); fh is the full framebuffer height including them -- the
        intermission overlay uses fh because it replaces the whole bar.
        Ports SCR_DrawCenterString, Con_DrawConsole/Con_DrawInput, and the
        menu's M_Print/cursor spinner."""
        cf = self.confont

        # intermission: the authentic Sbar_IntermissionOverlay pics (not text);
        # centerprint: the conchars centered text. The big-digit layout needs the
        # 320x200 design space, so tiny framebuffers fall back to the text panel.
        ist = self._cur_intermission_stats() if self.intermission else None
        if ist and vw >= 320 and fh >= 200:
            self.sbar.intermission_overlay(fb, vw, fh, ist,
                                           self.sb_complete, self.sb_inter)
        else:
            if ist:
                block = self._intermission_block(ist)          # tiny-fb fallback
            else:
                cm = self._cur_center_msg()
                block = (cm[0] if cm and self._cur_time() - cm[1] < CENTER_MSG_TIME
                         else None)
            if block:
                lines = block.split("\n")
                y0 = int(0.35 * vh) - len(lines) * 4
                for i, ln in enumerate(lines):
                    cf.text_centered(fb, vw, vw // 2, y0 + i * 8, ln)

        # console: conback backdrop over the top ~40%, text + flashing cursor.
        con = self.con
        if con.active:
            panel = vh * 2 // 5
            blit_conback(fb, vw, vh, self.conback, panel)
            con.width = max(20, vw // 8)
            rows = max(1, panel // 8 - 2)
            y = 0
            for ln in con.view_lines(rows):
                cf.text(fb, vw, 0, y, ln)
                y += 8
            cf.text(fb, vw, 0, y, "]" + con.input)
            if int(self._uptime * 4) & 1:               # Con_DrawInput cursor
                cf.char(fb, vw, (con.cursor + 1) * 8, y, 11)

        # menu: dim the view (Draw_FadeScreen), then title + rows; the selected
        # row gets the spinning cursor (conchars 12/13).
        if self.menu.active:
            title, rows = self.menu.view()
            fade_region(fb, vw, 0, 0, vw, vh)
            cx = vw // 2
            y = vh // 4
            cf.text_centered(fb, vw, cx, y, title)
            y += 16
            col = cx - 80
            for label, value, sel in rows:
                if sel:
                    cf.char(fb, vw, col - 16, y, 12 + (int(self._uptime * 4) & 1))
                cf.text(fb, vw, col, y, label)
                if value:
                    cf.text(fb, vw, cx + 16, y, value)
                y += 8

    # ---- main loop ----
    def _finish_timedemo(self):
        """CL_FinishTimeDemo: report average fps over the run and drop to the
        console with the result. Called from _demo_frame when a timedemo ends."""
        import time as _time
        d = self.demo
        if d.start_time and d.frames > 1:
            elapsed = _time.monotonic() - d.start_time
            fps = (d.frames - 1) / elapsed if elapsed > 0 else 0.0
            self.con.print(f"{d.frames - 1} frames {elapsed:.1f} seconds "
                           f"{fps:.1f} fps")
        self.con.active = True

    def _demo_frame(self, dt, inp):
        """One frame of .dem playback (cl_demo.c CL_ReadDemoMessage path): advance
        cl.time, read+parse demo messages on the timing gate (cl.time >= mtime[0];
        timedemo reads exactly one per frame), relink, drive the camera/HUD/view
        from cl, and render through the shared _render_scene. No server, no input,
        no physics. The Escape menu / console pause the demo clock (host.c)."""
        import time as _time
        from quake.msg import MsgReader
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)
        self._uptime += dt
        PROFILER.begin("server")
        d = self.demo
        paused = self.menu.active or self.con.active
        if not paused and not d.finished:
            self.cl.time += dt
            while True:
                if not d.timedemo and self.cl.time < self.cl.mtime[0]:
                    break
                fr = d.reader.next_frame()
                if fr is None:
                    d.finished = True
                    break
                # shift the demo-header viewangles into the two-snapshot buffer
                # (paired with mtime) so the camera lerps between them, not snaps
                self.cl.mviewangles[1] = self.cl.mviewangles[0]
                self.cl.mviewangles[0] = list(fr[0])
                self.cl.viewangles = list(fr[0])
                self.cl.parse_message(MsgReader(fr[1]))
                if d.timedemo:
                    if d.start_time is None:
                        d.start_time = _time.monotonic()   # clock starts at frame 1
                    d.frames += 1
                    break
            self.cl.relink(dt)
            if d.timedemo and d.finished:
                self._finish_timedemo()
            elif d.finished and self.in_demo_loop:
                # title loop: advance to the next demo (CL_NextDemo). This
                # rebuilds self.demo/self.cl/self.scene, so bail out of this
                # frame's stale demo and render the new one next frame.
                self._next_demo()
        PROFILER.end("server")

        # drive the camera from cl (CL_RelinkEntities: view entity origin + the
        # demo frame's recorded viewangles; the eye sits view_height above)
        ve = self.cl.entities[self.cl.viewentity]
        org = ve.origin if ve is not None else (0.0, 0.0, 0.0)
        self.pos = [org[0], org[1], org[2]]
        self.vel = list(self.cl.velocity)
        # interpolate the camera angles between the last two demo headers by the
        # same fraction relink used for positions -- smooth, not stepped per msg
        va = self.cl.lerp_viewangles(self.cl.lerp_frac)
        self.pitch = va[0]
        self.yaw = va[1]
        self.intermission = self.cl.intermission
        if self.intermission:
            eye = (self.pos[0], self.pos[1], self.pos[2])
            gun_org = None
        else:
            self.bobtime += dt
            bob = self._calc_bob()
            fwd, _r, _u = angle_vectors(self.yaw, self.pitch)
            eye, gun_org = view_origins(self.pos, self.cl.view_height, fwd, bob)
        # 3D sound: ear at the eye (right vector for the stereo pan), then play
        # the demo's queued svc_sound events -- there is no server to call the
        # mixer in demo mode, so we drain cl.sound_events here.
        _f, right, _u = angle_vectors(self.yaw, self.pitch)
        self.mixer.set_listener((eye[0], eye[1], eye[2]), right)
        for ent, chan, sname, svol, satten, sorg in self.cl.sound_events:
            self.mixer.start_sound(ent, chan, sname, svol, satten, sorg)
        self.cl.sound_events.clear()
        return self._render_scene(dt, eye, gun_org, dead=False, inp=inp)

    def frame(self, dt, inp):
        """Advance one frame from `dt` seconds and `inp` intent, returning a
        RenderFrame the frontend draws. Ports main.py's App.tick minus drawing,
        timing/after scheduling and diagnostics."""
        if self.demo is not None:           # .dem playback: no server, no input
            return self._demo_frame(dt, inp)
        if dt > 0:
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)
        self._uptime += dt          # real time, ticks even while paused

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
        # "always pause in single player if in console or menus"
        # (host.c Host_ServerFrame): skip the QC tick and player movement while
        # the Escape menu or console is open; the world renders frozen.
        if self.menu.active or self.con.active:
            dead = self.sv.player_health() <= 0
        # Intermission: the QC has frozen the player at the end-of-level camera
        # spot. Don't move or camera-drive them -- just advance the QC and let
        # IntermissionThink load the next map on a fire press after the delay.
        elif self.intermission or self.sv.intermission_active():
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
                # and the solid box entities (barrels, monsters, the player) --
                # SV_Move clips against these too, so you can't walk through them.
                # passent is the player edict: the move skips it and its own nails
                # (SV_ClipToLinks' passedict), so firing the nailgun while walking
                # no longer trips over the spikes spawned at the muzzle.
                self.phys.passent = self.sv.player
                self.phys.set_box_entities(self.sv.solid_box_entities())
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
                # feed the move's water sampling to the edict so the QC WaterMove
                # runs (drown/splash sounds, lava/slime damage, FL_INWATER), as
                # SV_ClientThink's water check does before PlayerPreThink
                self.sv.update_player_water(self.waterlevel, self.watertype)
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

        # ---- client/server loopback: serialize this frame into a protocol-15
        # datagram, parse it into the per-Client ClientState (cl), and relink
        # (interpolate) so the renderer reads cl, not the server VM edicts.
        # _change_level above reruns _load_map, which rebuilds self.cl/self.scene
        # for the new map, so this drives whichever cl is current. Camera and HUD
        # still read self.sv this phase (explicit Phase 1 scope).
        dg = MsgWriter()
        # PVS-cull the per-frame entity updates against the player eye's PVS
        # (SV_WriteEntitiesToClient): only entities the player could see are sent,
        # shrinking recordings and matching WinQuake. The player edict is never
        # culled. Live render reads cl, which already gates dynamic ents on
        # msgtime, so a culled (off-PVS) ent vanishes that frame and reappears
        # with forcelink when back in PVS.
        peye = self.sv.player_origin()
        vofs = self.sv.player_view_ofs()
        if peye is not None:
            eye_pvs = (peye[0], peye[1], peye[2] + (vofs[2] if vofs else VIEW_HEIGHT))
        else:
            eye_pvs = None
        build_datagram(self.sv, dg, pvs_test=self._pvs_tester(eye_pvs))
        if self.recording is not None:        # tee this frame to the .dem (live path only)
            self.recording.write_frame((self.pitch, self.yaw, 0.0), bytes(dg.data))
        self.cl.time = self.sv.time           # single-player: client time tracks server
        self.cl.parse_message(MsgReader(dg.data))
        self.cl.relink(dt)

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
            gun_org = None
        elif dead:
            # death cam: the eye sinks to the corpse on the floor (PlayerDie set
            # view_ofs z = -8), with no weapon model. (V_CalcRefdef also adds the
            # head-bob here, view.c:893, but the port leaves it out: self.vel is
            # not refreshed while dead, so it would bob on a stale velocity.)
            vofs = self.sv.player_view_ofs()
            vz = vofs[2] if vofs else -8.0
            eye = (self.pos[0], self.pos[1], self.pos[2] + vz)
            gun_org = None
        else:
            # head-bob: shift both the view origin and the gun by it, as Quake
            # does, so the weapon rides nearly still instead of sloshing.
            bob = self._calc_bob()
            fwd, _r, _u = angle_vectors(self.yaw, self.pitch)
            eye, gun_org = view_origins(self.pos, VIEW_HEIGHT, fwd, bob)
            if self.rend.sbar_lines:
                # V_CalcRefdef: "fudge position around to keep amount of
                # weapon visible roughly equal with different FOV" -- +2 at
                # viewsize 100 (status bar shown). Without it the bar-shrunken
                # view clips the gun to a sliver. (Stale sbar_lines for one
                # frame after a mode switch; self-heals with the render sync.)
                gun_org = (gun_org[0], gun_org[1], gun_org[2] + 2.0)

        return self._render_scene(dt, eye, gun_org, dead, inp)

    def _pvs_tester(self, eye):
        """Build a PVS box-test from the eye leaf's visibility row, for culling
        per-frame entity updates (a single-leaf approximation of SV_FatPVS).
        Returns pvs_test(mins, maxs)->bool, or None (cull nothing -- send all)
        when the eye is in the solid leaf or the leaf carries no vis data, so
        solid/no-vis leaves don't blank the datagram."""
        if eye is None:
            return None
        leaf = self.rend.point_leaf(eye)
        if leaf <= 0:                          # leaf 0 / solid -> no PVS, send all
            return None
        visofs = self.bsp.leafs[leaf][1]
        if visofs < 0:
            return None
        vis = self.rend.decompress_vis(visofs)
        return lambda mins, maxs: self.rend.box_in_pvs(mins, maxs, vis)

    def _render_scene(self, dt, eye, gun_org, dead, inp):
        """The render half of a frame, shared by live play and demo playback.
        Given the computed eye/gun origins (each caller derives these from its
        own source -- the live server, or the demo's cl), it runs the per-frame
        view feel, dynamic lights and palette, builds the world entity lists
        from self.scene, rasterises, and assembles the RenderFrame. Every render
        read of game state goes through the _cur_* accessors, which return the
        live self.sv.X values when self.demo is None (so live output is
        byte-identical) and the cl/scene equivalents during demo playback."""
        brush_ents = self.scene.brush_models()
        alias_ents = self._alias_ents()
        alias_ents.extend(self._beam_ents())   # lightning bolts (CL_UpdateTEnts)
        bsp_ents = self._bsp_ents()

        self._update_palette(dt)     # V_UpdatePalette: tint shifts for this frame
        self._update_view_feel(dt, dead)   # strafe lean / punch / damage kick
        self._update_dlights(dt)     # muzzle flashes / explosions / glows
        vpitch, vyaw, vroll = self.view_angles
        if not (self.intermission or dead):
            # stair smoothing: lag the eye and the gun together (view.c:975-976),
            # so the weapon stays locked to the view while stepping up a stair or
            # riding a lift instead of drifting up into the middle of the screen.
            eye, gun_org = stair_smooth(eye, gun_org, self.eye_z_offset)
        # build the view model only now, from the smoothed gun origin
        view_model = self._view_model(gun_org) if gun_org is not None else None

        # sprite status bar (sbar.c): zbuf mode with a >=320-wide screen.
        # Sync the renderer's reserved rows here so every path that changes
        # mode/resolution/zbuf_scale self-heals on the next frame.
        st = self._cur_hud()
        # mirrors _setup_zbuf's zw formula rather than reading rend.zw, which
        # is stale until the resize this very block may trigger
        screen_w = (self.video_res[0] if self.video_res
                    else max(1, self._view_wh[0] // self.rend.zbuf_scale))
        # the intermission overlay covers the screen and hides the status bar
        # (id draws one or the other), so render the 3D view full-height then --
        # no shrunk band -- like Quake zeroes sb_lines at intermission
        sbar_lines = SBAR_LINES if (self.mode == "zbuf"
                                    and screen_w >= 320
                                    and not self.intermission) else 0
        if self.rend.sbar_lines != sbar_lines:
            self.rend.sbar_lines = sbar_lines
            self.rend.resize(self.rend.width, self.rend.height)
        if st:
            items = st["items"]
            if items != self._prev_items:        # CL_ParseClientdata
                for j in range(32):
                    if items & (1 << j) and not self._prev_items & (1 << j):
                        self.item_gettime[j] = self._cur_time()
                self._prev_items = items

        PROFILER.begin("render")
        segs = polys = framebuffer = None
        render_mode = self.mode
        if self.mode == "zbuf":
            styles = lightstyle_values(self._cur_lightstyles(), self._cur_time())
            self.rend.apply_dlights(
                [(L[0], L[1], L[2], L[3], L[6]) for L in self.dlights.values()],
                styles)
            fbdata, leaf = self.rend.render_zbuffer(eye, vyaw, vpitch,
                                                    brush_ents, alias_ents,
                                                    view_model, bsp_ents,
                                                    textured=self.textured,
                                                    lightstyles=styles,
                                                    time=self._cur_time(),
                                                    roll=vroll,
                                                    sprites=self._sprite_ents(),
                                                    particles=self._cur_particles())
            framebuffer = fbdata
            nprim = fbdata[1] * fbdata[2]
            fb, vw, vh = fbdata                            # view region (pre-sbar)
            full_h = vh
            if self.rend.sbar_lines:
                fb.extend(bytes(vw * self.rend.sbar_lines))   # the bar rows
                full_h = vh + self.rend.sbar_lines
                # the intermission overlay replaces the status bar (id draws one
                # or the other), so skip the sbar while intermission is up
                if st and not self.intermission:
                    self.sbar.draw(fb, vw, full_h, st, self._cur_time(),
                                   self.item_gettime, self.faceanimtime)
                framebuffer = fbdata = (fb, vw, full_h)
            self._composite_zbuf_ui(fb, vw, vh, full_h)   # conchars UI + intermission
        elif self.mode == "flat" or self.wire_hidden:
            # flat shading, or hidden-line wireframe: both want the back-to-front
            # (painter's) polygon path so near faces occlude far ones. They differ
            # only in how the frontend paints the polys (filled vs outlined), so
            # tag the frame "wire_hidden" when it's the wireframe variant.
            styles = lightstyle_values(self._cur_lightstyles(), self._cur_time())
            self.rend.apply_dlights(
                [(L[0], L[1], L[2], L[3], L[6]) for L in self.dlights.values()],
                styles)
            polys, leaf = self.rend.render_shaded(eye, vyaw, vpitch,
                                                  brush_ents, alias_ents, view_model,
                                                  bsp_ents, lightstyles=styles,
                                                  roll=vroll)
            nprim = len(polys)
            if self.mode == "wire":
                render_mode = "wire_hidden"
        else:
            segs, leaf = self.rend.render(eye, vyaw, vpitch,
                                          brush_ents, alias_ents, view_model,
                                          bsp_ents, roll=vroll)
            nprim = len(segs)
        PROFILER.end("render")
        # scene context for the perf log: the real frame dt, where the player
        # stood, and what was on screen. A logged spike can then be placed in
        # the map and reproduced, instead of being an anonymous slow row.
        PROFILER.gauge("dt", dt * 1000.0)
        PROFILER.gauge("x", self.pos[0])
        PROFILER.gauge("y", self.pos[1])
        PROFILER.gauge("z", self.pos[2])
        PROFILER.gauge("dlights", len(self.dlights))
        PROFILER.gauge("particles", len(self._cur_particles()))
        PROFILER.gauge("ents", len(alias_ents) + len(brush_ents) + len(bsp_ents))
        PROFILER.gauge("map", self.mapname)

        # ambient loops follow the listener leaf (S_UpdateAmbientSounds)
        if 0 <= leaf < len(self.bsp.leaf_ambients):
            self.mixer.update_ambients(self.bsp.leaf_ambients[leaf], dt)

        # zbuf mode rasterised the particles into the framebuffer with the depth
        # buffer (proper per-pixel occlusion); the depthless wire/flat modes get
        # the projected overlay sprites instead, occluded by a per-particle trace.
        particles = [] if self.mode == "zbuf" else self._particle_sprites(eye)

        spd = math.hypot(self.vel[0], self.vel[1])
        movemode = ("NOCLIP" if self.noclip else
                    "water" if self.waterlevel >= 2 else
                    "ground" if self.onground else "air")
        hp = self._cur_health()
        w, h = self._view_wh

        overlays = []
        prim_word = ("pixels" if render_mode == "zbuf" else
                     "segs" if render_mode == "wire" else "polys")
        hud_str = (f"{self.fps:5.1f} fps   "
                   f"{prim_word} {nprim}   "
                   f"leaf {leaf}   {movemode}   health {hp:.0f}\n"
                   f"pos {self.pos[0]:.0f} {self.pos[1]:.0f} {self.pos[2]:.0f}   "
                   f"spd {spd:.0f}   yaw {self.yaw:.0f} pitch {self.pitch:.0f}   "
                   f"{'MOUSELOOK — mouse/Ctrl fire, 1-8 weapons' if inp.mouselook else 'click to capture mouse'} "
                   f"[N]oclip [F]lat [Z]buffer [T]exture [P]rofile")
        hud_rgb = HUD_GREEN
        if self.show_prof:
            # previous completed frame's smoothed section ms (server/render/
            # raster/present) as a bar chart, then a sparkline of recent raw
            # frame totals. present is timed in the frontend and frame_end()
            # rolls the buckets, so the figures lag one frame uniformly. The
            # total row (top of the chart) is tinted by frame budget via a
            # per-line colour list; every other line stays green.
            prof = PROFILER.bars()
            graph = PROFILER.graph()
            if graph:
                prof += "\n" + graph
            base = hud_str.count("\n") + 1
            colors = [HUD_GREEN] * (base + prof.count("\n") + 1)
            for i, ln in enumerate(prof.split("\n")):
                if ln.startswith("total"):
                    colors[base + i] = prof_total_color(PROFILER.total_ms)
            hud_str += "\n" + prof
            hud_rgb = colors
        overlays.append((8, 8, hud_str, hud_rgb, "nw"))

        # bottom status bar: health / armor / current-weapon ammo, plus the four
        # ammo pools. Health reddens when low so it reads at a glance. Hidden at
        # intermission (sbar_lines is 0 there, but the screen belongs to the
        # level-complete overlay, not the HUD).
        if st and not self.rend.sbar_lines and not self.intermission:
            status_rgb = (255, 64, 64) if st["health"] <= 25 else (255, 204, 0)
            carried = "  ".join(s for s in (st["keys"], st["powerups"]) if s)
            status_str = (f"HEALTH {st['health']:3d}    ARMOR {st['armor']:3d}    "
                          f"{st['weapon']}: {st['ammo']:3d}"
                          + (f"    [{carried}]" if carried else "") + "\n"
                          f"shells {st['shells']:3d}  nails {st['nails']:3d}  "
                          f"rockets {st['rockets']:3d}  cells {st['cells']:3d}")
            overlays.append((10, h - 8, status_str, status_rgb, "sw"))

        # intermission: Sbar_IntermissionOverlay's three stat rows -- completed
        # time (m:ss), secrets found/total, monsters killed/total -- centered over
        # the frozen end-of-level camera.
        # In zbuf mode these were composited into the framebuffer by
        # _composite_zbuf_ui; only the wire/flat overlay path emits them here.
        console = None
        menu = None
        if self.mode != "zbuf":
            ist = self._cur_intermission_stats() if self.intermission else None
            if ist:
                overlays.append((w // 2, h // 3, self._intermission_block(ist),
                                 (255, 255, 0), "center"))

            cm = self._cur_center_msg()
            if not ist and cm and self._cur_time() - cm[1] < CENTER_MSG_TIME:
                overlays.append((w // 2, h // 3, cm[0], (255, 255, 0), "center"))

            con = self.con
            if con.active:
                con.width = max(20, w // 9)           # ~9px per monospace cell at the HUD size
                rows = max(1, (h * 2 // 5) // 16 - 1)  # panel is ~40% tall, ~16px lines
                console = (con.view_lines(rows), "]" + con.input, con.cursor + 1)

            menu = self.menu.view() if self.menu.active else None

        return RenderFrame(mode=render_mode, segs=segs, polys=polys,
                           framebuffer=framebuffer, particles=particles,
                           overlays=overlays, crosshair=(w // 2, h // 2),
                           console=console, menu=menu,
                           palette=self.view_palette,
                           palette_version=self.palette_version,
                           pixel_aspect=(self._pixel_aspect
                                         if self.mode == "zbuf" else 1.0))

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
        for p in self._cur_particles():
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
        now = self._cur_time()
        for mi, org, ang, frame in self.scene.alias_entities():
            m = models[mi] if mi < nmodels else None
            if m is None:
                continue
            ang = spin_yaw(m.flags, ang, now)
            out.append((m, m.frame_verts(frame, now), org, ang))
        return out

    def _sprite_ents(self):
        """Resolve live .spr entities to (frame tuple, origin) billboards."""
        out = []
        smodels = self.smodels
        n = len(smodels)
        for mi, org, frame in self.scene.sprite_entities():
            s = smodels[mi] if mi < n else None
            if s is None:
                continue
            out.append((s.frame(frame), org))
        return out

    def _beam_model(self, name):
        """Lazily load a bolt/beam .mdl from the pak (they're QC-precached on
        maps with the weapons, but the beam may cross maps via cheats)."""
        if name not in self._beam_models:
            m = None
            if name in self.pak.files:
                try:
                    m = Mdl(self.pak.read(name), self.palette)
                except Exception as e:
                    print(f"beam mdl load failed for {name}: {e}")
            self._beam_models[name] = m
        return self._beam_models[name]

    def _beam_ents(self):
        """CL_UpdateTEnts: chop each live beam into 30-unit bolt-model
        segments aimed along it with a random roll, riding the normal
        alias-model render path."""
        if self.demo is not None:
            return []                # beams come off the server; demos have none
        beams = self.sv.live_beams()
        if not beams:
            return []
        out = []
        now = self.sv.time
        for b in beams:
            m = self._beam_model(b["model"])
            if m is None:
                continue
            sx, sy, sz = b["start"]
            dx = b["end"][0] - sx
            dy = b["end"][1] - sy
            dz = b["end"][2] - sz
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist < 1e-6:
                continue
            if not dx and not dy:
                yaw = 0.0
                pitch = 90.0 if dz > 0 else 270.0
            else:
                yaw = math.degrees(math.atan2(dy, dx)) % 360.0
                pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy))) % 360.0
            ux, uy, uz = dx / dist, dy / dist, dz / dist
            verts = m.frame_verts(0, now)
            x, y, z = sx, sy, sz
            d = dist
            while d > 0:
                out.append((m, verts, (x, y, z),
                            (pitch, yaw, random.random() * 360.0)))
                x += ux * 30.0
                y += uy * 30.0
                z += uz * 30.0
                d -= 30.0
        return out

    def _bsp_ents(self):
        """Resolve live external-.bsp pickup entities to (PickupModel, origin,
        angles) for the renderer. Skips any whose model failed to load."""
        out = []
        bmodels = self.bmodels
        nmodels = len(bmodels)
        for mi, org, ang in self.scene.bsp_model_entities():
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
        vw = self._cur_view_weapon()
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
        return (mdl, mdl.frame_verts(frame, self._cur_time()), org, ang)
