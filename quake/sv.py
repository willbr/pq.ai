"""QuakeC server layer: builtins, entity spawning, and the think frame loop.

This is the SV side -- it owns a VM, installs the ~65 builtin functions the game
logic calls, parses the BSP entity string into edicts (ED_LoadFromFile), and runs
each entity's think function when its nextthink comes due (SV_RunThink).

Physics-dependent builtins (traceline, walkmove, droptofloor, movetogoal, ...) are
wired to physics.py, so monsters navigate, items drop to the floor, and shots
trace against the real clip hulls -- not just animate in place via OP_STATE chains.
"""

import math
import random

from .pr_exec import VM, PR_RunError

# spawnflags for skill / deathmatch inhibition (defs.qc)
SPAWNFLAG_NOT_EASY = 256
SPAWNFLAG_NOT_MEDIUM = 512
SPAWNFLAG_NOT_HARD = 1024
SPAWNFLAG_NOT_DEATHMATCH = 2048

# .flags bits, mirroring defs.qc. Not all are acted on by this engine: the water
# and jump flags (FL_INWATER/FL_WATERJUMP/FL_JUMPRELEASED) are managed by the QC
# (client.qc WaterMove/PlayerJump) once the engine feeds the edict waterlevel/
# watertype, and FL_WATERJUMP is never set because id left CheckWaterJump
# commented out. FL_JUMPRELEASED's debounce is mirrored engine-side in
# physics.player_move, which owns the jump impulse.
FL_FLY = 1
FL_SWIM = 2
FL_CLIENT = 8                     # set for all client edicts
FL_INWATER = 16                   # for enter / leave water splash
FL_MONSTER = 32
FL_GODMODE = 64                   # player cheat: damage immunity
FL_NOTARGET = 128                 # player cheat: monsters ignore you
FL_ITEM = 256                     # extra wide size for bonus items
FL_ONGROUND = 512                 # standing on something
FL_PARTIALGROUND = 1024           # not all corners are valid
FL_WATERJUMP = 2048               # player jumping out of water
FL_JUMPRELEASED = 4096            # for jump debouncing (don't pogo-stick)
STEPSIZE = 18.0             # monsters step up/down ledges this tall (SV_movestep)
DI_NODIR = -1.0            # SV_NewChaseDir: "no direction"
SOLID_BSP = 4
MOVETYPE_NONE = 0
MOVETYPE_STEP = 4           # walking monsters: freefall only when unsupported
MOVETYPE_FLY = 5
MOVETYPE_TOSS = 6
MOVETYPE_PUSH = 7
MOVETYPE_NOCLIP = 8
MOVETYPE_FLYMISSILE = 9
MOVETYPE_BOUNCE = 10
SV_GRAVITY = 800.0          # sv_gravity default, scaled per-entity by .gravity
CONTENTS_EMPTY = -1
CONTENTS_SOLID = -2
CONTENTS_WATER = -3

SVC_TEMPENTITY = 23         # broadcast effect message (gunshots, teleport fog, ...)

# --- particles (r_part.c) ---------------------------------------------------
# colour-ramp tables explosions and fire animate through as they cool (r_part.c:29)
_RAMP1 = (0x6f, 0x6d, 0x6b, 0x69, 0x67, 0x65, 0x63, 0x61)   # pt_explode
_RAMP2 = (0x6f, 0x6e, 0x6d, 0x6c, 0x6b, 0x6a, 0x68, 0x66)   # pt_explode2
_RAMP3 = (0x6d, 0x6b, 6, 5, 4, 3)                           # pt_fire (rocket smoke)

# particle types (r_part.c ptype_t): pick the per-frame gravity/decel/colour path
PT_STATIC   = 0     # no gravity (tracers, voor trail)
PT_GRAV     = 1     # falls (blood)
PT_SLOWGRAV = 2     # falls (gunshots, splashes) -- same integration as pt_grav
PT_FIRE     = 3     # rises, cools via ramp3, dies at ramp >= 6 (rocket/grenade smoke)
PT_EXPLODE  = 4     # accelerates, cools via ramp1, dies at ramp >= 8
PT_EXPLODE2 = 5     # decelerates (1x), cools via ramp2, dies at ramp >= 8
PT_BLOB     = 6     # accelerates (tarbaby/quad)
PT_BLOB2    = 7     # decelerates in x/y (tarbaby/quad)

MAX_PARTICLES = 2048    # id's pool size (r_part.c MAX_PARTICLES); drop the oldest past it
# id spawns ~1024 particles per explosion/splash; the pure-Python point rasteriser
# can't carry that, so the big bursts are subsampled to this many while keeping
# id's exact per-particle physics, colours, lifetimes and jitter. The small
# effects (spikes, gunshots, trails) keep their full id counts.
_BIG_BURST = 128
# beam temp entities: WriteEntity(owner) + two WriteCoord vectors, drawn as
# chained bolt models (CL_ParseBeam). Type -> model.
_TE_BEAMS = {
    5: "progs/bolt.mdl",        # TE_LIGHTNING1 (shambler)
    6: "progs/bolt2.mdl",       # TE_LIGHTNING2 (player lightning gun)
    9: "progs/bolt3.mdl",       # TE_LIGHTNING3 (boss)
    13: "progs/beam.mdl",       # TE_BEAM (grappling hook mods)
}

# movetypes the engine integrates each frame (origin += velocity*dt)
_MOVE_INTEGRATE = frozenset((MOVETYPE_PUSH, MOVETYPE_NOCLIP, MOVETYPE_FLY,
                             MOVETYPE_TOSS, MOVETYPE_FLYMISSILE, MOVETYPE_BOUNCE))
# of those, the ones that fall under gravity (tossed projectiles: fireballs, gibs)
_MOVE_GRAVITY = frozenset((MOVETYPE_TOSS, MOVETYPE_BOUNCE))
# movetypes that collide with the world and other entities (SV_Physics_Toss):
# rockets/spikes (FLYMISSILE), grenades/gibs (BOUNCE), fireballs (TOSS), FLY.
# These trace their move and fire touch on impact instead of phasing through.
_MOVE_PROJECTILE = frozenset((MOVETYPE_TOSS, MOVETYPE_BOUNCE, MOVETYPE_FLY,
                              MOVETYPE_FLYMISSILE))

# entity fields we touch from the engine side
_FIELDS = ("classname", "model", "modelindex", "origin", "angles", "mins", "maxs",
           "size", "nextthink", "think", "frame", "flags", "solid", "movetype",
           "velocity", "avelocity", "groundentity", "ideal_yaw", "yaw_speed",
           "chain", "spawnflags", "view_ofs", "gravity",
           # player / combat
           "absmin", "absmax", "health", "max_health", "takedamage", "v_angle",
           "weapon", "weaponmodel", "weaponframe", "items", "impulse",
           "attack_finished", "currentammo", "ammo_shells", "ammo_nails",
           "ammo_rockets", "ammo_cells", "armorvalue", "armortype",
           "button0", "deadflag", "enemy", "owner", "touch", "goalentity",
           "waterlevel", "watertype", "air_finished", "th_die",
           "dmg_take", "dmg_save", "dmg_inflictor", "punchangle", "effects",
           # render state the protocol serializer (sv_send.py) snapshots/deltas
           "colormap", "skin")

SOLID_NOT = 0
SOLID_TRIGGER = 1
SOLID_BBOX = 2
SOLID_SLIDEBOX = 3
MOVETYPE_WALK = 3
DAMAGE_AIM = 2

# deadflag values (defs.qc): the player's death state machine, advanced by
# PlayerDeathThink once PlayerDie has dropped the corpse.
DEAD_NO = 0
DEAD_DYING = 1
DEAD_DEAD = 2
DEAD_RESPAWNABLE = 3

# item / weapon flags (defs.qc). The player's .items is a bitfield of these;
# .weapon holds the single active weapon flag.
IT_SHOTGUN = 1
IT_SUPER_SHOTGUN = 2
IT_NAILGUN = 4
IT_SUPER_NAILGUN = 8
IT_GRENADE_LAUNCHER = 16
IT_ROCKET_LAUNCHER = 32
IT_LIGHTNING = 64
IT_SHELLS = 256
IT_NAILS = 512
IT_ROCKETS = 1024
IT_CELLS = 2048
IT_AXE = 4096
IT_KEY1 = 131072            # silver key
IT_KEY2 = 262144            # gold key
IT_INVISIBILITY = 524288
IT_INVULNERABILITY = 1048576
IT_SUIT = 2097152
IT_QUAD = 4194304
_WEAPON_NAMES = {
    IT_AXE: "Axe", IT_SHOTGUN: "Shotgun", IT_SUPER_SHOTGUN: "Super Shotgun",
    IT_NAILGUN: "Nailgun", IT_SUPER_NAILGUN: "Super Nailgun",
    IT_GRENADE_LAUNCHER: "Grenade Launcher", IT_ROCKET_LAUNCHER: "Rocket Launcher",
    IT_LIGHTNING: "Lightning Gun",
}

# system globals we read/write
_GLOBALS = ("self", "other", "time", "frametime", "force_retouch", "skill",
            "v_forward", "v_right", "v_up", "msg_entity", "mapname",
            "intermission_running", "intermission_exittime",
            "total_secrets", "total_monsters", "found_secrets", "killed_monsters",
            "trace_allsolid", "trace_startsolid", "trace_fraction", "trace_endpos",
            "trace_plane_normal", "trace_plane_dist", "trace_ent",
            "trace_inopen", "trace_inwater",
            "serverflags") + tuple(f"parm{i}" for i in range(1, 17))


def anglemod(a):
    return (360.0 / 65536) * (int(a * (65536 / 360.0)) & 65535)


def _ray_box(p, q, mn, mx):
    """Slab clip of segment p->q against AABB [mn,mx]. Returns (entry_frac,
    outward_normal) for the first face crossed, or None (miss / starts inside)."""
    tmin, tmax = 0.0, 1.0
    axis = -1
    for i in range(3):
        d = q[i] - p[i]
        if -1e-9 < d < 1e-9:
            if p[i] < mn[i] or p[i] > mx[i]:
                return None
            continue
        inv = 1.0 / d
        t1 = (mn[i] - p[i]) * inv
        t2 = (mx[i] - p[i]) * inv
        if t1 > t2:
            t1, t2 = t2, t1
        if t1 > tmin:
            tmin, axis = t1, i
        if t2 < tmax:
            tmax = t2
        if tmin > tmax:
            return None
    if axis < 0:
        return None                     # segment starts inside the box
    n = [0.0, 0.0, 0.0]
    n[axis] = -1.0 if (q[axis] - p[axis]) > 0 else 1.0
    return tmin, tuple(n)


def angle_vectors(angles):
    """Quake AngleVectors: (pitch, yaw, roll) degrees -> forward, right, up."""
    p = math.radians(angles[0]); y = math.radians(angles[1]); r = math.radians(angles[2])
    sp, cp = math.sin(p), math.cos(p)
    sy, cy = math.sin(y), math.cos(y)
    sr, cr = math.sin(r), math.cos(r)
    forward = (cp * cy, cp * sy, -sp)
    right = (-sr * sp * cy + cr * sy, -sr * sp * sy - cr * cy, -sr * cp)
    up = (cr * sp * cy + sr * sy, cr * sp * sy - sr * cy, cr * cp)
    return forward, right, up


def _tokenize(text):
    """COM_Parse-style tokenizer for the entity lump."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":   # // comment
            while i < n and text[i] != "\n":
                i += 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            i += 1
            start = i
            while i < n and text[i] != '"':
                i += 1
            yield text[start:i]
            i += 1
        else:
            start = i
            while i < n and text[i] not in " \t\r\n":
                i += 1
            yield text[start:i]


def parse_entities(text):
    """Yield one {key: value} dict per entity block."""
    toks = _tokenize(text)
    for tok in toks:
        if tok != "{":
            continue
        ent = {}
        for key in toks:
            if key == "}":
                break
            value = next(toks)
            ent[key] = value
        yield ent


class Server:
    def __init__(self, progs, bsp=None, mapname="", skill=1, max_edicts=600,
                 physics=None, pak=None, serverflags=0.0):
        self.pr = progs
        self.vm = VM(progs, max_edicts=max_edicts)
        self.bsp = bsp
        self.pak = pak             # to resolve external brush-model bounds (b_*.bsp)
        self._ext_bounds = {}      # cache: external .bsp name -> (mins, maxs)
        self.mapname = mapname
        self.skill = skill
        self.phys = physics         # for traceline / hitscan; None -> clear path
        self.player = 0             # player edict number (0 until spawn_player)
        self.button0 = False        # attack held (host sets it each frame)
        self.pending_impulse = 0    # queued weapon-select impulse (consumed once)
        # how far pushers (lifts/doors) carried the player this frame, so the host
        # can fold it into the camera position it owns (SV_PushMove riders)
        self.player_carry = [0.0, 0.0, 0.0]
        self.changelevel = None     # set by the changelevel builtin; host reads it
        self.bonus_flash = False    # stuffcmd "bf": pickup flash; host consumes
        self.changelevel_restart = False  # the pending change is a death restart
        self.serverflags = serverflags  # episode sigils, carried across levels
        self.spawn_parms = None     # parm1..16 the player spawned with
        self.intermission_time = None  # level time frozen when intermission began
        self.center_msg = None      # (text, time) from centerprint; host displays it
        self.particles = []         # live points: [x,y,z, vx,vy,vz, color, die, type, ramp]
        self._tracercount = 0       # R_RocketTrail's static counter (tracer zigzag)
        self._te = None             # in-progress temp-entity message being parsed
        self.beams = []             # live lightning beams (CL_ParseBeam state)
        self.dlight_events = []     # one-shot dynamic lights (explosions);
                                    # (origin, radius, die, decay) for the host
        self._ent_lastorg = {}      # edict -> last origin, for trail segments
        self._model_trail = {}      # modelindex -> trail type (or None), cached
        self.snd = None             # sound mixer (set by host after precache); None -> muted
        self.ambients = []          # deferred looping ambientsounds: (name, pos, vol, atten)

        self.time = 0.0
        self.frametime = 0.1
        self.developer = False
        self.model_precache = [""]
        self.sound_precache = [""]
        self.lightstyles = {}
        # protocol serialization state (quake/sv_send.py):
        self.baselines = {}          # SV_CreateBaseline: edict -> spawn-time state
        # Per-frame unreliable protocol events (sounds, temp entities,
        # svc_particle bursts) accumulated during the QC tick and drained into
        # the datagram by sv_send.build_datagram. Cleared each frame
        # (SV_ClearDatagram). Each item is a write_fn closure taking a writer.
        self.unreliable = []
        self._prev_stats = {}         # SV_UpdateToReliableMessages stat diffing
        self._prev_lightstyles = {}   # svc_lightstyle change detection
        self._setangle = None         # pending svc_setangle (teleport fixangle)
        self.cvars = {"skill": float(skill), "deathmatch": 0.0, "coop": 0.0,
                      "teamplay": 0.0, "temp1": 0.0, "noexit": 0.0,
                      "samelevel": 0.0, "sv_gravity": SV_GRAVITY}
        # share the cvar dict with physics so it reads sv_gravity live (e1m8's
        # worldspawn cvar_set lowers it to 100); see Physics.gravity.
        if self.phys is not None:
            self.phys.host_cvars = self.cvars

        # resolve field / global offsets once
        self.f = {name: progs.field_ofs(name) for name in _FIELDS}
        self.g = {name: progs.global_ofs(name) for name in _GLOBALS}

        self.vm.builtins = self._build_builtin_table()

    # ================================================================
    # global helpers
    # ================================================================
    def gset_f(self, name, v):
        o = self.g[name]
        if o is not None:
            self.vm.gf[o] = v

    def gset_i(self, name, v):
        o = self.g[name]
        if o is not None:
            self.vm.gi[o] = v

    def gset_v(self, name, v):
        o = self.g[name]
        if o is not None:
            self.vm.gf[o], self.vm.gf[o + 1], self.vm.gf[o + 2] = v[0], v[1], v[2]

    def gget_v(self, name):
        o = self.g[name]
        return (self.vm.gf[o], self.vm.gf[o + 1], self.vm.gf[o + 2])

    def gget_i(self, name):
        return self.vm.gi[self.g[name]]

    def gget_f(self, name):
        o = self.g[name]
        return self.vm.gf[o] if o is not None else 0.0

    # ================================================================
    # level load: build edicts from the entity string and spawn them
    # ================================================================
    def load_level(self):
        vm = self.vm
        # model precache: 0 empty, 1 worldmodel, then inline brush models *1.. *N
        self.ambients = []          # rebuilt below as ambient entities spawn
        self.model_precache = ["", self.mapname]
        if self.bsp is not None:
            for i in range(1, len(self.bsp.models)):
                self.model_precache.append(f"*{i}")

        # world is edict 0
        vm.clear_edict(0)
        self.vm.free[0] = False
        if self.bsp is not None:
            m0 = self.bsp.models[0]
            self.vm.fset_v(0, self.f["mins"], m0["mins"])
            self.vm.fset_v(0, self.f["maxs"], m0["maxs"])
        self.vm.fset_i(0, self.f["model"], self.pr.new_string(self.mapname))
        self.vm.fset_i(0, self.f["modelindex"], 1)
        self.vm.fset_f(0, self.f["solid"], SOLID_BSP)
        self.vm.fset_f(0, self.f["movetype"], MOVETYPE_PUSH)

        self.gset_f("time", self.time)
        self.gset_f("skill", float(self.skill))   # QC reads this for difficulty
        self.gset_f("serverflags", float(self.serverflags))  # episode sigils
        self.gset_i("mapname", self.pr.new_string(self.mapname))

        spawned = inhibited = noclass = 0
        first = True
        for fields in parse_entities(self.bsp.entities):
            if first:
                num = 0                 # the worldspawn block fills edict 0
                first = False
            else:
                num = vm.alloc_edict()
            self._parse_edict(num, fields)

            if self._inhibited(num):
                vm.free_edict(num)
                inhibited += 1
                continue

            cn = self.pr.string(vm.fget_i(num, self.f["classname"]))
            if not cn:
                vm.free_edict(num)
                noclass += 1
                continue
            func = self.pr.find_function(cn)
            if func is None:
                vm.free_edict(num)
                noclass += 1
                continue

            self.gset_i("self", num)
            self.gset_i("other", 0)
            try:
                vm.execute(func)
                spawned += 1
            except PR_RunError as e:
                print(f"  spawn {cn} (edict {num}) aborted: {e}")

        return {"spawned": spawned, "inhibited": inhibited, "noclass": noclass,
                "num_edicts": vm.num_edicts}

    def _inhibited(self, num):
        sf = int(self.vm.fget_f(num, self.f["spawnflags"]))
        if self.skill == 0 and (sf & SPAWNFLAG_NOT_EASY):
            return True
        if self.skill == 1 and (sf & SPAWNFLAG_NOT_MEDIUM):
            return True
        if self.skill >= 2 and (sf & SPAWNFLAG_NOT_HARD):
            return True
        return False

    def _parse_edict(self, num, fields):
        pr, vm = self.pr, self.vm
        for key, value in fields.items():
            if key.startswith("_"):
                continue
            if key == "angle":                  # anglehack: scalar yaw -> vector
                key = "angles"
                value = f"0 {value} 0"
            elif key == "light":
                key = "light_lev"
            d = pr.field_by_name.get(key)
            if d is None:
                continue
            etype, ofs = d
            self._set_field(num, etype, ofs, value)

    def _set_field(self, num, etype, ofs, value):
        pr, vm = self.pr, self.vm
        # etype: 1 string, 2 float, 3 vector, 4 entity, 5 field, 6 function
        if etype == 2:                    # ev_float
            vm.fset_f(num, ofs, _atof(value))
        elif etype == 3:                  # ev_vector
            parts = (value.split() + ["0", "0", "0"])[:3]
            vm.fset_v(num, ofs, tuple(_atof(p) for p in parts))
        elif etype == 1:                  # ev_string
            vm.fset_i(num, ofs, pr.new_string(value))
        elif etype == 4:                  # ev_entity
            vm.fset_i(num, ofs, int(_atof(value)))
        elif etype == 5:                  # ev_field
            d = pr.field_by_name.get(value)
            vm.fset_i(num, ofs, d[1] if d else 0)
        elif etype == 6:                  # ev_function
            fi = pr.find_function(value)
            vm.fset_i(num, ofs, fi if fi is not None else 0)

    # ================================================================
    # frame: run every edict's think when nextthink comes due
    # ================================================================
    def run_frame(self, dt=0.1):
        vm = self.vm
        self.frametime = dt
        self.time += dt
        vm.time = self.time      # for ED_Alloc's freed-slot reuse guard
        self.player_carry = [0.0, 0.0, 0.0]   # reset rider carry for this frame
        self.unreliable = []                  # SV_ClearDatagram
        self.gset_f("frametime", dt)
        self.gset_f("time", self.time)
        # SV_Physics runs QC StartFrame first: id's re-reads the teamplay and
        # skill cvars into the globals and bumps framecount; mods hook it.
        self._exec_named("StartFrame", 0)

        ntf, thf, mtf = self.f["nextthink"], self.f["think"], self.f["movetype"]
        forg, fang = self.f["origin"], self.f["angles"]
        fvel, favel, fgrav = self.f["velocity"], self.f["avelocity"], self.f["gravity"]

        # Pusher movers (doors/plats/buttons, MOVETYPE_PUSH) run on their own clock
        # 'ltime'. QC schedules every move and wait as self.ltime + delay, so if
        # ltime never advances those deadlines sit in the past and SUB_CalcMoveDone
        # fires instantly -- the door snaps open and slams shut in two frames.
        # Keep ltime in lockstep with server time so the deadlines land ahead.
        fltime = self.pr.field_by_name.get("ltime")
        fltime = fltime[1] if fltime is not None else None
        if fltime is not None:
            for num in range(1, vm.num_edicts):
                if not vm.free[num] and int(vm.fget_f(num, mtf)) == MOVETYPE_PUSH:
                    vm.fset_f(num, fltime, self.time)

        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            # movers: integrate the linear move, then run think. Brush movers
            # (doors/plats, MOVETYPE_PUSH) move at constant velocity and carry any
            # rider standing on them (SV_PushMove). Projectiles (rockets/grenades/
            # nails/fireballs/gibs) trace their move and fire touch on impact.
            mt = int(vm.fget_f(num, mtf))
            if num == self.player and mt in (MOVETYPE_FLY, MOVETYPE_NOCLIP):
                mt = MOVETYPE_NONE      # fly/noclip cheats: the host drives us
            if mt == MOVETYPE_PUSH:
                # SV_Physics_Pusher: when the mover's move completes partway
                # through this frame (its think is due), advance it by only the
                # time left until then (nextthink - frame start), not the whole
                # frame. Otherwise it overshoots its destination and carries its
                # rider past the stop point; the think then snaps the mover back
                # to the destination, leaving the rider embedded in it (allsolid,
                # so it can't move or jump -- the stuck-on-lift bug).
                nt = vm.fget_f(num, ntf)
                movetime = dt
                if 0.0 < nt <= self.time:
                    movetime = nt - (self.time - dt)
                    if movetime < 0.0:
                        movetime = 0.0
                    elif movetime > dt:
                        movetime = dt
                self._push_move(num, movetime)
            elif mt == MOVETYPE_STEP:
                self._physics_step(num, dt)
            elif mt in _MOVE_PROJECTILE:
                self._physics_toss(num, mt, dt)
            elif mt in _MOVE_INTEGRATE:
                vx, vy, vz = vm.fget_v(num, fvel)
                if vx or vy or vz:
                    ox, oy, oz = vm.fget_v(num, forg)
                    vm.fset_v(num, forg, (ox + vx * dt, oy + vy * dt, oz + vz * dt))
                ax, ay, az = vm.fget_v(num, favel)
                if ax or ay or az:
                    cx, cy, cz = vm.fget_v(num, fang)
                    vm.fset_v(num, fang, (cx + ax * dt, cy + ay * dt, cz + az * dt))

            nt = vm.fget_f(num, ntf)
            if nt <= 0 or nt > self.time:
                continue
            vm.fset_f(num, ntf, 0.0)
            # SV_Physics_Pusher only runs a pusher think scheduled within the last
            # frame (oldltime < nextthink <= ltime); one scheduled in the past is
            # dropped, never run. This is how a wait -1 door locks open: door_hit_top
            # sets door_go_down at ltime + (-1), in the past, so it must not fire.
            # A legitimately-due pusher think always lands in (time-dt, time].
            if mt == MOVETYPE_PUSH and nt < self.time - dt:
                continue
            self.gset_f("time", nt)
            self.gset_i("self", num)
            self.gset_i("other", 0)
            think = vm.fget_i(num, thf)
            if think:
                # combat AI exercises many builtins; an error in one edict's
                # think must not take down the whole frame.
                try:
                    vm.execute(think)
                except PR_RunError as ex:
                    cn = self.pr.string(vm.fget_i(num, self.f["classname"]))
                    print(f"think {cn} (edict {num}) aborted: {ex}")
        self.run_drop_punch_angle(dt)           # SV_ClientThink: bleed weapon kick
        self.run_water_move()                   # PlayerPreThink: drown/splash/damage
        self.run_weapon_frame()                 # PlayerPostThink: drive the weapons
        self.run_player_death_think()           # PlayerPreThink's dead->respawn FSM
        self.touch_triggers(self.player)        # fire teleports/triggers we touch
        self._emit_trails()                     # R_RocketTrail for moving missiles
        self._advance_particles(dt)
        # freeze the completed-level time the frame execute_changelevel fires, so
        # the intermission overlay shows when the exit was reached, not a clock
        # that keeps ticking while the camera sits on the spot.
        if self.intermission_time is None and self.intermission_active():
            self.intermission_time = self.time
        self.gset_f("time", self.time)

    def _sv_impact(self, e1, e2):
        """SV_Impact: two entities collided -- run each one's touch function with
        self/other set appropriately. e2 may be 0 (the world), which has no touch.
        This is what makes a rocket explode and a nail wound a monster on contact."""
        vm, f = self.vm, self.f
        toff, fsol = f["touch"], f["solid"]
        old_self, old_other = self.gget_i("self"), self.gget_i("other")
        self.gset_f("time", self.time)
        for a, b in ((e1, e2), (e2, e1)):
            if not a or vm.free[a]:
                continue
            t = vm.fget_i(a, toff)
            if not t or vm.fget_f(a, fsol) == SOLID_NOT:
                continue
            self.gset_i("self", a)
            self.gset_i("other", b)
            try:
                vm.execute(t)
            except PR_RunError as ex:
                cn = self.pr.string(vm.fget_i(a, f["classname"]))
                print(f"touch {cn} (edict {a}) aborted: {ex}")
        self.gset_i("self", old_self)
        self.gset_i("other", old_other)

    def _physics_toss(self, num, mt, dt):
        """SV_Physics_Toss: move a free-flying entity (projectile, gib, fireball)
        by tracing its velocity through the world + solid entities, firing touch on
        impact. Gravity for TOSS/BOUNCE; BOUNCE rebounds (restitution 1.5) while
        the rest stop dead and rest on the floor."""
        vm, f = self.vm, self.f
        fvel, favel, forg, fang, fgrav = (f["velocity"], f["avelocity"],
                                          f["origin"], f["angles"], f["gravity"])
        if int(vm.fget_f(num, f["flags"])) & FL_ONGROUND:
            return                              # already at rest
        self._check_velocity(num)

        vx, vy, vz = vm.fget_v(num, fvel)
        if mt in _MOVE_GRAVITY:
            gs = (fgrav is not None and vm.fget_f(num, fgrav)) or 1.0
            vz -= self.cvars["sv_gravity"] * gs * dt
            vm.fset_v(num, fvel, (vx, vy, vz))

        # angular velocity spins the model regardless of translation
        ax, ay, az = vm.fget_v(num, favel)
        if ax or ay or az:
            cx, cy, cz = vm.fget_v(num, fang)
            vm.fset_v(num, fang, (cx + ax * dt, cy + ay * dt, cz + az * dt))

        if not (vx or vy or vz):
            return
        ox, oy, oz = vm.fget_v(num, forg)
        end = (ox + vx * dt, oy + vy * dt, oz + vz * dt)
        # An entity with a real bounding box (the player corpse, mins/maxs the
        # player size) must sweep its BOX, like SV_PushEntity's SV_Move -- so it
        # rests its box bottom on the floor. Point missiles (rockets, grenades,
        # gibs: zero size) keep the cheap point trace. Tracing the corpse as a
        # point sank its origin to the floor, dropping the death-cam eye
        # (origin - 8) below it -- the "death cam noclips through the floor".
        maxs = vm.fget_v(num, f["maxs"]); mins = vm.fget_v(num, f["mins"])
        if maxs[2] - mins[2] > 16.0:
            tr = self._box_move(num, (ox, oy, oz), end)
            frac, endpos = tr.fraction, tr.endpos
            pnorm = tr.plane_normal or (0.0, 0.0, 1.0)
            hit = tr.ent if tr.ent is not None else 0
        else:
            frac, endpos, pnorm, _allsolid, _startsolid, hit = \
                self._move_trace((ox, oy, oz), end, 0, num)
        vm.fset_v(num, forg, endpos)
        self._link_abs(num)
        if frac >= 1.0:
            self.check_water_transition(num)
            return                              # flew the whole way, no contact

        self._sv_impact(num, hit)               # explode / wound on impact
        if vm.free[num]:
            return                              # removed by its own touch

        # bounce off (grenades/gibs) or stop dead (rockets already gone; fireballs)
        backoff = 1.5 if mt == MOVETYPE_BOUNCE else 1.0
        nv = self.phys.clip_velocity((vx, vy, vz), pnorm, backoff)
        vm.fset_v(num, fvel, nv)
        if pnorm[2] > 0.7 and (nv[2] < 60.0 or mt != MOVETYPE_BOUNCE):
            flags = int(vm.fget_f(num, f["flags"])) | FL_ONGROUND
            vm.fset_f(num, f["flags"], float(flags))
            vm.fset_i(num, f["groundentity"], hit)
            vm.fset_v(num, fvel, (0.0, 0.0, 0.0))
            vm.fset_v(num, favel, (0.0, 0.0, 0.0))
        self.check_water_transition(num)

    def _check_velocity(self, num):
        """SV_CheckVelocity: bound each velocity component to +/-2000
        (sv_maxvelocity) and scrub NaN to zero before physics runs it."""
        vm, fvel = self.vm, self.f["velocity"]
        vx, vy, vz = vm.fget_v(num, fvel)
        out, changed = [], False
        for v in (vx, vy, vz):
            if v != v:                          # NaN
                v, changed = 0.0, True
            elif v > 2000.0:
                v, changed = 2000.0, True
            elif v < -2000.0:
                v, changed = -2000.0, True
            out.append(v)
        if changed:
            vm.fset_v(num, fvel, tuple(out))

    def _physics_step(self, num, dt):
        """SV_Physics_Step: a walking monster freefalls only while nothing
        supports it -- QC's T_Damage strips FL_ONGROUND and adds velocity for
        knockback, and this integrates it until the monster lands (with the
        thud of a hard fall). Locomotion itself comes from walkmove/movetogoal,
        and the think runs from run_frame's shared dispatch."""
        if self.phys is None:
            return                  # no collision world (headless/test boot)
        vm, f = self.vm, self.f
        flags = int(vm.fget_f(num, f["flags"]))
        if not (flags & (FL_ONGROUND | FL_FLY | FL_SWIM)):
            self._check_velocity(num)
            vx, vy, vz = vm.fget_v(num, f["velocity"])
            grav = self.cvars["sv_gravity"]
            hitsound = vz < -0.1 * grav
            gs = (f["gravity"] is not None and vm.fget_f(num, f["gravity"])) or 1.0
            vel = [vx, vy, vz - grav * gs * dt]
            # SV_FlyMove, reduced to the falling case: slide along whatever the
            # box hits, ground out on a floor plane (n.z > 0.7)
            org = list(vm.fget_v(num, f["origin"]))
            time_left = dt
            for _ in range(4):
                if time_left <= 0.0 or not (vel[0] or vel[1] or vel[2]):
                    break
                end = [org[i] + vel[i] * time_left for i in range(3)]
                tr = self._box_move(num, org, end)
                if tr.allsolid:
                    vel = [0.0, 0.0, 0.0]
                    break
                org = list(tr.endpos)
                if tr.fraction >= 1.0:
                    break
                time_left *= 1.0 - tr.fraction
                n = tr.plane_normal or (0.0, 0.0, 1.0)
                if n[2] > 0.7:                  # landed
                    flags |= FL_ONGROUND
                    vm.fset_f(num, f["flags"], float(flags))
                    vm.fset_i(num, f["groundentity"],
                              tr.ent if tr.ent is not None else 0)
                # SV_FlyMove runs the touch on impact (sv_phys.c:300) -- this is
                # what fires Demon_JumpTouch so a leaping demon recovers from its
                # jump, and wounds whatever it lands on. Origin is already at the
                # contact point, so touch sees the entity where it hit.
                vm.fset_v(num, f["origin"], tuple(org))
                self._link_abs(num)
                self._sv_impact(num, tr.ent if tr.ent is not None else 0)
                if vm.free[num]:
                    return                      # removed by its own touch
                vel = self.phys.clip_velocity(vel, n, 1.0)
            vm.fset_v(num, f["origin"], tuple(org))
            vm.fset_v(num, f["velocity"], tuple(vel))
            self._link_abs(num)
            if (flags & FL_ONGROUND) and hitsound:
                self._start_sound(num, 0, "demon/dland2.wav", 1.0, 1.0)
        self.check_water_transition(num)

    def check_water_transition(self, num):
        """SV_CheckWaterTransition: stamp .watertype/.waterlevel from the point
        contents at the origin, splashing when the entity crosses into or out
        of a liquid (projectiles and falling monsters hitting water)."""
        if self.phys is None:
            return
        vm, f = self.vm, self.f
        cont = self.phys.point_contents_0(vm.fget_v(num, f["origin"]))
        wt = int(vm.fget_f(num, f["watertype"]))
        if wt == 0:                             # just spawned: no transition
            vm.fset_f(num, f["watertype"], float(cont))
            vm.fset_f(num, f["waterlevel"], 1.0)
        elif cont <= CONTENTS_WATER:
            if wt == CONTENTS_EMPTY:            # crossed into water
                self._start_sound(num, 0, "misc/h2ohit1.wav", 1.0, 1.0)
            vm.fset_f(num, f["watertype"], float(cont))
            vm.fset_f(num, f["waterlevel"], 1.0)
        else:
            if wt != CONTENTS_EMPTY:            # crossed out of water
                self._start_sound(num, 0, "misc/h2ohit1.wav", 1.0, 1.0)
            vm.fset_f(num, f["watertype"], float(CONTENTS_EMPTY))
            vm.fset_f(num, f["waterlevel"], float(cont))

    def _push_move(self, num, dt):
        """SV_PushMove (WinQuake sv_phys.c): advance a brush mover by velocity*dt,
        then displace every solid entity that rides it or that it moves into. Each
        contacted entity is moved by the same delta and block-tested -- if its new
        spot is solid it's left where it can fit, and if it can't fit anywhere the
        mover's .blocked fires and the already-moved entities are restored. This
        is what carries the player on a lift/train without ramming them through a
        wall, and what lets a door crush/reverse on something in the way."""
        vm, f = self.vm, self.f
        fvel, favel, forg, fang = (f["velocity"], f["avelocity"],
                                   f["origin"], f["angles"])
        vx, vy, vz = vm.fget_v(num, fvel)
        ax, ay, az = vm.fget_v(num, favel)
        if ax or ay or az:
            cx, cy, cz = vm.fget_v(num, fang)
            vm.fset_v(num, fang, (cx + ax * dt, cy + ay * dt, cz + az * dt))
        if not (vx or vy or vz):
            return
        move = (vx * dt, vy * dt, vz * dt)
        pushorg = vm.fget_v(num, forg)
        vm.fset_v(num, forg, (pushorg[0] + move[0], pushorg[1] + move[1],
                              pushorg[2] + move[2]))
        self._link_abs(num)                   # bounds must track the moved origin
        # Mirror SV_LinkEdict inside SV_PushMove: the mover's cached collision
        # position must update the instant it moves, so a monster re-grounding
        # later this frame (SV_movestep) lands on its NEW top instead of the stale
        # one and rides the lift up rather than being left "stuck underneath".
        if self.phys is not None:
            self.phys.relink_brush(num, vm.fget_v(num, forg))
        pmn = vm.fget_v(num, f["absmin"]); pmx = vm.fget_v(num, f["absmax"])

        fmt, fsol = f["movetype"], f["solid"]
        amn, amx = f["absmin"], f["absmax"]
        moved = []                            # (edict, old_origin) restored if blocked
        for e in range(1, vm.num_edicts):
            if e == num or vm.free[e]:
                continue
            mt = int(vm.fget_f(e, fmt))
            if mt in (MOVETYPE_PUSH, MOVETYPE_NONE, MOVETYPE_NOCLIP):
                continue
            # NOTE: SV_PushMove does NOT skip SOLID_NOT here -- a corpse standing
            # on the pusher rides it up like anything else (the e1m1 dead-grunt-
            # on-the-lift case). SOLID_NOT is only special-cased in the block path
            # below, where Quake squishes a corpse rather than blocking the mover.
            # carried if standing on the mover; otherwise only if the mover's new
            # position actually penetrates it (bbox overlap THEN a real hull test
            # against the mover's brush), like SV_PushMove's two cases. The hull
            # test is what stops a door sliding *past* a bystander from dragging
            # them -- only something the door moves *into* gets shoved.
            if not self._rides_pusher(e, num, pmn, pmx):
                emn = vm.fget_v(e, amn); emx = vm.fget_v(e, amx)
                if (emn[0] >= pmx[0] or emn[1] >= pmx[1] or emn[2] >= pmx[2] or
                        emx[0] <= pmn[0] or emx[1] <= pmn[1] or emx[2] <= pmn[2]):
                    continue
                if not self._penetrates_pusher(e, num):
                    continue
            old = vm.fget_v(e, forg)
            vm.fset_v(e, forg, (old[0] + move[0], old[1] + move[1], old[2] + move[2]))
            self._link_abs(e)
            # "moved fine" means clear of every solid: the world, the pusher, and
            # any OTHER mover (a brush-model staircase, a barrel, a monster).
            # SV_TestEntityPosition tests all of these; checking only world solid
            # let a roof shove a player DOWN through a brush-model floor (the
            # unraised stairs) whenever the world floor below wasn't solid,
            # instead of crushing them. A rider carried on top sits above the
            # brush, so it stays "fine"; only something the pusher moves *into*
            # and can't clear is blocked.
            if not self._stuck_in_solids(e, num) and not self._penetrates_pusher(e, num):
                moved.append((e, old))        # pushed/carried fine
                continue
            # a corpse/trigger never blocks a pusher: WinQuake leaves it at the
            # pushed spot (shrinking its box to fit) rather than firing .blocked,
            # so a door squishes a dead body instead of reversing off it.
            if int(vm.fget_f(e, fsol)) in (SOLID_NOT, SOLID_TRIGGER):
                moved.append((e, old))
                continue
            # blocked there -- if it can stay where it was, leave it (no carry).
            # "Can stay" means clear of all solids and the pusher: a roof
            # descending onto a player pinned on the (brush-model) stairs is still
            # stuck at the old spot -> fire .blocked (crush). Without testing the
            # stairs + pusher the player just slides down through them, uncrushed.
            vm.fset_v(e, forg, old); self._link_abs(e)
            if not self._stuck_in_solids(e, num) and not self._penetrates_pusher(e, num):
                continue
            # truly stuck: restore everyone moved so far and fire .blocked. The
            # pusher stays put (WinQuake leaves it; the QC reverses next think).
            # Nothing was added to player_carry yet (that happens only below, once
            # the whole sweep succeeds), so there's nothing to undo there.
            for me, mo in moved:
                vm.fset_v(me, forg, mo); self._link_abs(me)
            blk = self.pr.field_by_name.get("blocked")
            bfn = vm.fget_i(num, blk[1]) if blk else 0
            if bfn:
                self.gset_f("time", self.time)
                self.gset_i("self", num)
                self.gset_i("other", e)
                try:
                    vm.execute(bfn)
                except PR_RunError as ex:
                    print(f"pusher {num} blocked() aborted: {ex}")
            return

        # record how far the player was actually carried, for the camera
        p = self.player
        for e, old in moved:
            if e == p:
                no = vm.fget_v(p, forg)
                self.player_carry[0] += no[0] - old[0]
                self.player_carry[1] += no[1] - old[1]
                self.player_carry[2] += no[2] - old[2]

    def _rides_pusher(self, e, pusher, pmn, pmx):
        """True if entity `e` is carried by the pusher: either standing on it
        (its .groundentity, as monsters track) or resting on its top surface
        within its footprint (the player, whose ground link the engine doesn't
        keep). pmn/pmx are the pusher's post-move abs bounds."""
        vm, f = self.vm, self.f
        if (int(vm.fget_f(e, f["flags"])) & FL_ONGROUND
                and vm.fget_i(e, f["groundentity"]) == pusher):
            return True
        emn = vm.fget_v(e, f["absmin"]); emx = vm.fget_v(e, f["absmax"])
        E = 1.0
        if (emn[0] - E > pmx[0] or emx[0] + E < pmn[0] or
                emn[1] - E > pmx[1] or emx[1] + E < pmn[1]):
            return False                      # outside the footprint
        return pmx[2] - 4.0 <= emn[2] <= pmx[2] + STEPSIZE   # feet on top

    def _test_position(self, e):
        """SV_TestEntityPosition for edict `e`: is its box stuck in world solid?"""
        if self.phys is None:
            return False
        vm, f = self.vm, self.f
        org = vm.fget_v(e, f["origin"])
        return self.phys.test_position(org, vm.fget_v(e, f["mins"]))

    def _stuck_in_solids(self, e, pusher):
        """SV_TestEntityPosition for the push: is e embedded in the world OR any
        other solid -- a brush model (a func_door/func_train staircase, a wall) or
        a box entity (barrel, monster) -- but NOT the pusher itself? A descending
        crusher must see a player it shoves down onto a *brush-model* floor (e.g.
        the unraised stairs) as stuck, or it pushes them straight through; the
        plain world-only test missed that and never crushed. The pusher is
        excluded (its overlap is the separate _penetrates_pusher gate, and
        excluding it keeps a rider from reading as stuck in its own mover)."""
        if self.phys is None:
            return False
        vm, f = self.vm, self.f
        org = vm.fget_v(e, f["origin"])
        return self.phys.move(list(org), list(org), record=False,
                              mins=vm.fget_v(e, f["mins"]),
                              maxs=vm.fget_v(e, f["maxs"]),
                              passedict=e, exclude_brush=pusher).startsolid

    def _penetrates_pusher(self, e, pusher):
        """True if entity `e`'s box overlaps the pusher's brush at its current
        position -- the gate SV_PushMove uses before shoving a non-rider, so only
        what the mover moves *into* is pushed. Traced against the pusher's hull 1
        (exact for the player; close enough for monsters)."""
        if self.phys is None or self.bsp is None:
            return False
        vm, f = self.vm, self.f
        mp = self.model_precache
        mi = vm.fget_i(pusher, f["modelindex"])
        if not (0 < mi < len(mp)) or mp[mi][:1] != "*":
            return False
        sub = int(mp[mi][1:])
        if sub >= len(self.bsp.models):
            return False
        headnode = self.bsp.models[sub]["headnodes"][1]
        po = vm.fget_v(pusher, f["origin"])
        eo = vm.fget_v(e, f["origin"])
        ls = [eo[0] - po[0], eo[1] - po[1], eo[2] - po[2]]
        return self.phys.trace_hull(headnode, list(ls), list(ls)).startsolid

    def touch_triggers(self, ent):
        """Fire the touch function of every SOLID_TRIGGER whose volume overlaps
        ent's bounding box (SV_TouchLinks, restricted to one mover -- the player).
        This is what makes trigger_teleport / trigger_changelevel / trigger_multiple
        and the rider-trigger plats spawn fire, plus the trigger field a normal door
        spawns around itself. Walking into a button or a key/shootable door (which
        has no trigger field) is handled separately by touch_impacts."""
        if not ent:
            return
        vm, f = self.vm, self.f
        if vm.free[ent] or vm.fget_f(ent, f["solid"]) == SOLID_NOT:
            return
        toff = self.pr.field_by_name.get("touch")
        if toff is None:
            return
        toff = toff[1]
        amn, amx, fsol = f["absmin"], f["absmax"], f["solid"]
        e0x, e0y, e0z = vm.fget_v(ent, amn)
        e1x, e1y, e1z = vm.fget_v(ent, amx)
        for e in range(1, vm.num_edicts):
            if e == ent or vm.free[e]:
                continue
            if vm.fget_f(e, fsol) != SOLID_TRIGGER:
                continue
            tf = vm.fget_i(e, toff)
            if not tf:
                continue
            tmn = vm.fget_v(e, amn)
            tmx = vm.fget_v(e, amx)
            if (e0x > tmx[0] or e1x < tmn[0] or e0y > tmx[1] or e1y < tmn[1] or
                    e0z > tmx[2] or e1z < tmn[2]):
                continue
            self.gset_i("self", e)
            self.gset_i("other", ent)
            self.gset_f("time", self.time)
            try:
                vm.execute(tf)
            except PR_RunError as ex:
                cn = self.pr.string(vm.fget_i(e, f["classname"]))
                print(f"touch {cn} (edict {e}) aborted: {ex}")
            if vm.free[ent]:        # player removed (e.g. changelevel)
                break

    def brush_models(self):
        """Live brush-model entities as (submodel_index, origin, angles, frame),
        for the renderer. `frame` is 0/1: a set frame swaps animated surfaces to
        their alternate textures (R_TextureAnimation -- a pressed button lights
        up). Skips entities whose modelindex isn't an inline '*N' model --
        notably triggers, which QC makes invisible by clearing modelindex."""
        vm = self.vm
        mp = self.model_precache
        fmi, forg = self.f["modelindex"], self.f["origin"]
        fang, ffr = self.f["angles"], self.f["frame"]
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            mi = vm.fget_i(num, fmi)
            if 0 < mi < len(mp) and mp[mi][:1] == "*":
                out.append((int(mp[mi][1:]), vm.fget_v(num, forg),
                            vm.fget_v(num, fang), int(vm.fget_f(num, ffr))))
        return out

    def solid_brush_models(self):
        """Solid brush-model entities (func_wall, doors, gates) as
        (hull-1 headnode, origin, edict), for clipping the player's movement. The
        edict lets the host fire the entity's touch when the player bumps it
        (SV_Impact -- see touch_impacts). Skips the world, non-solid brushes (open
        episode gates) and non-inline models. This is what makes func_walls and
        closed doors block you."""
        vm = self.vm
        if self.bsp is None:
            return []
        mp = self.model_precache
        fmi, forg, fsol = self.f["modelindex"], self.f["origin"], self.f["solid"]
        models = self.bsp.models
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num] or int(vm.fget_f(num, fsol)) != SOLID_BSP:
                continue
            mi = vm.fget_i(num, fmi)
            if not (0 < mi < len(mp)) or mp[mi][:1] != "*":
                continue
            sub = int(mp[mi][1:])
            if sub < len(models):
                out.append((models[sub]["headnodes"][1], vm.fget_v(num, forg), num))
        return out

    def solid_box_entities(self):
        """Solid bounding-box entities (SOLID_BBOX barrels and nails,
        SOLID_SLIDEBOX monsters and the player) as (absmin, absmax, edict, owner),
        for clipping moves. Quake's SV_ClipToLinks clips a move against every
        solid edict, not just the SOLID_BSP brush movers; this is what stops you
        walking through a barrel or a grunt -- and stops a grunt walking through
        you. The list includes the player and every projectile: move() skips the
        mover and its own missiles via passedict, so nothing has to be filtered
        out here (the player must stay in the list, or monsters could not clip
        against it)."""
        vm, f = self.vm, self.f
        if self.bsp is None:
            return []
        famn, famx, fsol, fown = f["absmin"], f["absmax"], f["solid"], f["owner"]
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            sol = int(vm.fget_f(num, fsol))
            if sol != SOLID_BBOX and sol != SOLID_SLIDEBOX:
                continue
            out.append((vm.fget_v(num, famn), vm.fget_v(num, famx),
                        num, vm.fget_i(num, fown)))
        return out

    def touch_impacts(self, edicts):
        """Fire the touch function of each solid brush mover the player bumped
        this frame (physics.touched). The engine has no SV_Impact, so this is what
        presses a button (button_touch) or opens a key/shootable door you walk into
        (door_touch) -- buttons spawn no trigger field, so contact is the only way.
        Movers are MOVETYPE_PUSH, so seed ltime like run_frame's prepass does, or
        their SUB_CalcMove deadlines would land in the past."""
        if not edicts:
            return
        vm, f = self.vm, self.f
        if not self.player or vm.free[self.player]:
            return
        toff = self.pr.field_by_name.get("touch")
        fltime = self.pr.field_by_name.get("ltime")
        if toff is None:
            return
        toff = toff[1]
        fltime = fltime[1] if fltime is not None else None
        for e in sorted(edicts):
            if vm.free[e]:
                continue
            tf = vm.fget_i(e, toff)
            if not tf:
                continue
            if fltime is not None:
                vm.fset_f(e, fltime, self.time)
            self.gset_i("self", e)
            self.gset_i("other", self.player)
            self.gset_f("time", self.time)
            try:
                vm.execute(tf)
            except PR_RunError as ex:
                cn = self.pr.string(vm.fget_i(e, f["classname"]))
                print(f"impact {cn} (edict {e}) aborted: {ex}")

    def alias_entities(self):
        """Live .mdl entities as (modelindex, origin, angles, frame), for the
        renderer (monsters, items). Skips brush '*N' and non-.mdl models."""
        vm = self.vm
        mp = self.model_precache
        fmi, forg, fang, ffr, fmod = (self.f["modelindex"], self.f["origin"],
                                      self.f["angles"], self.f["frame"],
                                      self.f["model"])
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            # picked-up items clear .model (string_null) but keep modelindex;
            # the engine hides them (sv_main.c: !pr_strings[ent->v.model]).
            if vm.fget_i(num, fmod) == 0:
                continue
            mi = vm.fget_i(num, fmi)
            if 0 < mi < len(mp) and mp[mi][-4:] == ".mdl":
                out.append((mi, vm.fget_v(num, forg), vm.fget_v(num, fang),
                            int(vm.fget_f(num, ffr))))
        return out

    def sprite_entities(self):
        """Live .spr entities as (modelindex, origin, frame) -- explosions
        (BecomeExplosion's s_explod.spr), drowning bubbles, torch flames."""
        vm = self.vm
        mp = self.model_precache
        fmi, forg, ffr, fmod = (self.f["modelindex"], self.f["origin"],
                                self.f["frame"], self.f["model"])
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num] or vm.fget_i(num, fmod) == 0:
                continue
            mi = vm.fget_i(num, fmi)
            if 0 < mi < len(mp) and mp[mi][-4:] == ".spr":
                out.append((mi, vm.fget_v(num, forg),
                            int(vm.fget_f(num, ffr))))
        return out

    def bsp_model_entities(self):
        """Live external-.bsp model entities (health/ammo pickups) as
        (modelindex, origin, angles). These use standalone maps/b_*.bsp brush
        models -- not inline '*N' submodels and not .mdl alias models -- so they
        fall through both alias_entities and brush_models. Index 1 is the world
        map itself, which is never an entity's pickup, so skip it."""
        vm = self.vm
        mp = self.model_precache
        fmi, forg, fang = self.f["modelindex"], self.f["origin"], self.f["angles"]
        fmod = self.f["model"]
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            # hidden once picked up: .model is cleared though modelindex remains
            # (sv_main.c: !pr_strings[ent->v.model]).
            if vm.fget_i(num, fmod) == 0:
                continue
            mi = vm.fget_i(num, fmi)
            if 1 < mi < len(mp) and mp[mi][:1] != "*" and mp[mi][-4:] == ".bsp":
                out.append((mi, vm.fget_v(num, forg), vm.fget_v(num, fang)))
        return out

    # ================================================================
    # builtins
    # ================================================================
    def _build_builtin_table(self):
        fixme = self._pf_fixme
        # index == builtin number; order matches pr_builtin[] in pr_cmds.c
        return [
            fixme,                # 0
            self._pf_makevectors, self._pf_setorigin, self._pf_setmodel,
            self._pf_setsize, fixme, self._pf_break, self._pf_random,
            self._pf_sound, self._pf_normalize, self._pf_error, self._pf_objerror,
            self._pf_vlen, self._pf_vectoyaw, self._pf_spawn, self._pf_remove,
            self._pf_traceline, self._pf_checkclient, self._pf_find,
            self._pf_precache_sound, self._pf_precache_model, self._pf_stuffcmd,
            self._pf_findradius, self._pf_bprint, self._pf_noop2, self._pf_dprint,  # sprint
            self._pf_ftos, self._pf_vtos, self._pf_noop, self._pf_traceon,
            self._pf_traceoff, self._pf_noop, self._pf_walkmove, fixme,        # 32,33
            self._pf_droptofloor, self._pf_lightstyle, self._pf_rint,
            self._pf_floor, self._pf_ceil, fixme, self._pf_checkbottom,        # 39,40
            self._pf_pointcontents, fixme, self._pf_fabs, self._pf_aim,        # 42..
            self._pf_cvar, self._pf_localcmd, self._pf_nextent, self._pf_particle,  # localcmd, particle
            self._pf_changeyaw, fixme, self._pf_vectoangles,                   # 49,50,51
            self._pf_writebyte, self._pf_noop2, self._pf_noop2, self._pf_noop2,  # WriteByte/Char/Short/Long
            self._pf_writecoord, self._pf_noop2, self._pf_noop2, self._pf_writeentity,  # WriteCoord/Angle/String/Entity
            fixme, fixme, fixme, fixme, fixme, fixme, fixme,                   # 60..66
            self._pf_movetogoal, self._pf_precache_file, self._pf_makestatic,  # 67,68,69
            self._pf_changelevel, fixme, self._pf_cvar_set, self._pf_centerprint,  # 70..73
            self._pf_ambientsound, self._pf_precache_model, self._pf_precache_sound,
            self._pf_precache_file, self._pf_setspawnparms,                    # 78
        ]

    # --- trivial / no-ops ---
    def _pf_fixme(self):
        raise PR_RunError("unimplemented builtin")

    def _pf_noop(self):
        pass

    def _pf_noop2(self):
        pass

    def _pf_break(self):
        print("QC break statement")

    # --- math ---
    def _pf_random(self):
        self.vm.ret_f(random.random())

    def _pf_normalize(self):
        x, y, z = self.vm.parm_v(0)
        n = math.sqrt(x * x + y * y + z * z)
        if n == 0:
            self.vm.ret_v(0.0, 0.0, 0.0)
        else:
            self.vm.ret_v(x / n, y / n, z / n)

    def _pf_vlen(self):
        x, y, z = self.vm.parm_v(0)
        self.vm.ret_f(math.sqrt(x * x + y * y + z * z))

    def _pf_vectoyaw(self):
        x, y, _ = self.vm.parm_v(0)
        if x == 0 and y == 0:
            yaw = 0.0
        else:
            yaw = int(math.atan2(y, x) * 180 / math.pi)
            if yaw < 0:
                yaw += 360
        self.vm.ret_f(float(yaw))

    def _pf_vectoangles(self):
        x, y, z = self.vm.parm_v(0)
        if x == 0 and y == 0:
            yaw = 0.0
            pitch = 90.0 if z > 0 else 270.0
        else:
            yaw = int(math.atan2(y, x) * 180 / math.pi)
            if yaw < 0:
                yaw += 360
            fwd = math.sqrt(x * x + y * y)
            pitch = int(math.atan2(z, fwd) * 180 / math.pi)
            if pitch < 0:
                pitch += 360
        self.vm.ret_v(float(pitch), float(yaw), 0.0)

    def _pf_rint(self):
        f = self.vm.parm_f(0)
        self.vm.ret_f(float(int(f + 0.5)) if f > 0 else float(int(f - 0.5)))

    def _pf_floor(self):
        self.vm.ret_f(math.floor(self.vm.parm_f(0)))

    def _pf_ceil(self):
        self.vm.ret_f(math.ceil(self.vm.parm_f(0)))

    def _pf_fabs(self):
        self.vm.ret_f(abs(self.vm.parm_f(0)))

    def _pf_makevectors(self):
        fwd, right, up = angle_vectors(self.vm.parm_v(0))
        self.gset_v("v_forward", fwd)
        self.gset_v("v_right", right)
        self.gset_v("v_up", up)

    # --- entity placement ---
    def _pf_setorigin(self):
        e = self.vm.parm_i(0)
        self.vm.fset_v(e, self.f["origin"], self.vm.parm_v(1))
        self._link_abs(e)

    def _link_abs(self, e):
        """Maintain absmin/absmax = origin + mins/maxs (SV_LinkEdict's job), then
        widen FL_ITEM bonus items by 15 on x/y, exactly as SV_LinkEdict (world.c)
        does "to make items easier to pick up and allow them to be grabbed off of
        shelves". Without it a shelf item whose trigger face is flush with a wall
        (e1m1's nail ammo at 272,2352,64) is never reachable: the player's box
        always stops a DIST_EPSILON fraction short of the trigger, so the AABB
        overlap test in touch_triggers misses by a hair.

        SV_LinkEdict also widens *non*-items by 1 on every axis (movement is
        clipped an epsilon from real edges). We deliberately skip that: items are
        SOLID_TRIGGER and never clip the player, but barrels/monsters are
        SOLID_BBOX/SOLID_SLIDEBOX and solid_box_entities feeds their absmin/absmax
        straight in as the *collision* box -- in Quake that widening only grows the
        broadphase query (the narrow phase re-clips against true mins/maxs), so
        applying it here would make the player bump them a unit early."""
        ox, oy, oz = self.vm.fget_v(e, self.f["origin"])
        mnx, mny, mnz = self.vm.fget_v(e, self.f["mins"])
        mxx, mxy, mxz = self.vm.fget_v(e, self.f["maxs"])
        amnx, amny, amnz = ox + mnx, oy + mny, oz + mnz
        amxx, amxy, amxz = ox + mxx, oy + mxy, oz + mxz
        if int(self.vm.fget_f(e, self.f["flags"])) & FL_ITEM:
            amnx -= 15; amny -= 15; amxx += 15; amxy += 15
        self.vm.fset_v(e, self.f["absmin"], (amnx, amny, amnz))
        self.vm.fset_v(e, self.f["absmax"], (amxx, amxy, amxz))

    def _set_minmax(self, e, mins, maxs):
        self.vm.fset_v(e, self.f["mins"], mins)
        self.vm.fset_v(e, self.f["maxs"], maxs)
        self.vm.fset_v(e, self.f["size"],
                       (maxs[0] - mins[0], maxs[1] - mins[1], maxs[2] - mins[2]))
        self._link_abs(e)

    def _pf_setsize(self):
        e = self.vm.parm_i(0)
        self._set_minmax(e, self.vm.parm_v(1), self.vm.parm_v(2))

    def _pf_setmodel(self):
        e = self.vm.parm_i(0)
        mofs = self.vm.parm_strofs(0 + 1)
        m = self.pr.string(mofs)
        if m not in self.model_precache:        # be lenient (vanilla errors)
            self.model_precache.append(m)
        idx = self.model_precache.index(m)
        self.vm.fset_i(e, self.f["model"], mofs)
        self.vm.fset_i(e, self.f["modelindex"], idx)
        if m.startswith("*") and self.bsp is not None:
            bm = self.bsp.models[int(m[1:])]
            self._set_minmax(e, bm["mins"], bm["maxs"])
        elif m.endswith(".bsp") and m != self.mapname:
            # external brush model (maps/b_explob.bsp etc.). Quake's setmodel
            # sets the entity's box from the model's bounds; misc_explobox relies
            # on this entirely (it never calls setsize), so without it the barrel
            # gets a zero-size box and bullets pass straight through it.
            bounds = self._external_model_bounds(m)
            if bounds is not None:
                self._set_minmax(e, bounds[0], bounds[1])
            else:
                self._set_minmax(e, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        else:
            self._set_minmax(e, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))

    def _external_model_bounds(self, name):
        """(mins, maxs) of an external brush model's model 0, read from the pak
        and cached. None if there's no pak or the file is missing/unreadable."""
        if name in self._ext_bounds:
            return self._ext_bounds[name]
        bounds = None
        if self.pak is not None and name in self.pak.files:
            try:
                from .bsp import Bsp
                bm = Bsp(self.pak.read(name)).models[0]
                bounds = (tuple(bm["mins"]), tuple(bm["maxs"]))
            except Exception as ex:
                print(f"external model bounds for {name} failed: {ex}")
        self._ext_bounds[name] = bounds
        return bounds

    # --- edict alloc ---
    def _pf_spawn(self):
        self.vm.ret_i(self.vm.alloc_edict())

    def _pf_remove(self):
        self.vm.free_edict(self.vm.parm_i(0))

    # --- entity search ---
    def _pf_find(self):
        vm = self.vm
        start = vm.parm_i(0)
        fld = vm.parm_i(1)
        s = vm.parm_str(2)
        for e in range(start + 1, vm.num_edicts):
            if vm.free[e]:
                continue
            if self.pr.string(vm.fget_i(e, fld)) == s:
                vm.ret_i(e)
                return
        vm.ret_i(0)

    def _pf_findradius(self):
        vm = self.vm
        ox, oy, oz = vm.parm_v(0)
        rad = vm.parm_f(1)
        chain = 0
        for e in range(1, vm.num_edicts):
            if vm.free[e]:
                continue
            if vm.fget_f(e, self.f["solid"]) == 0:   # SOLID_NOT
                continue
            ex, ey, ez = vm.fget_v(e, self.f["origin"])
            mnx, mny, mnz = vm.fget_v(e, self.f["mins"])
            mxx, mxy, mxz = vm.fget_v(e, self.f["maxs"])
            dx = ox - (ex + (mnx + mxx) * 0.5)
            dy = oy - (ey + (mny + mxy) * 0.5)
            dz = oz - (ez + (mnz + mxz) * 0.5)
            if math.sqrt(dx * dx + dy * dy + dz * dz) > rad:
                continue
            vm.fset_i(e, self.f["chain"], chain)
            chain = e
        vm.ret_i(chain)

    def _pf_nextent(self):
        vm = self.vm
        i = vm.parm_i(0)
        while True:
            i += 1
            if i >= vm.num_edicts:
                vm.ret_i(0)
                return
            if not vm.free[i]:
                vm.ret_i(i)
                return

    def _pf_checkclient(self):
        """SV_CheckClient/PF_checkclient: return a client a monster might be able
        to see, for FindTarget. Quake cycles through clients and gates on the
        check's PVS; single-player has one client, so return the player if alive
        (FindTarget still does its own range / line-of-sight / infront tests).
        Returns the world (0) when there is no live player."""
        e = self.player
        if e and self.vm.fget_f(e, self.f["health"]) > 0:
            self.vm.ret_i(e)
        else:
            self.vm.ret_i(0)

    # --- strings / printing ---
    def _varstring(self, first):
        parts = []
        for i in range(first, self.vm.argc):
            parts.append(self.vm.parm_str(i))
        return "".join(parts)

    def _pf_dprint(self):
        # Con_DPrintf: developer-only, silent in normal play
        if self.developer:
            print(self._varstring(0), end="")

    def _pf_bprint(self):
        print(self._varstring(0), end="")

    def _pf_ftos(self):
        v = self.vm.parm_f(0)
        s = f"{int(v)}" if v == int(v) else f"{v:5.1f}"
        self.vm.ret_i(self.pr.new_string(s))

    def _pf_vtos(self):
        x, y, z = self.vm.parm_v(0)
        self.vm.ret_i(self.pr.new_string(f"'{x:5.1f} {y:5.1f} {z:5.1f}'"))

    def _pf_error(self):
        raise PR_RunError("QC error: " + self._varstring(0))

    def _pf_objerror(self):
        msg = self._varstring(0)
        self.vm.free_edict(self.vm.gi[self.g["self"]])
        raise PR_RunError("QC objerror: " + msg)

    # --- precache (record name, return the same string) ---
    def _pf_precache_model(self):
        s = self.vm.parm_str(0)
        if s and s not in self.model_precache:
            self.model_precache.append(s)
        self.vm.ret_i(self.vm.parm_strofs(0))

    def _pf_precache_sound(self):
        s = self.vm.parm_str(0)
        if s and s not in self.sound_precache:
            self.sound_precache.append(s)
        self.vm.ret_i(self.vm.parm_strofs(0))

    def _pf_precache_file(self):
        self.vm.ret_i(self.vm.parm_strofs(0))

    # --- cvars ---
    def _pf_cvar(self):
        self.vm.ret_f(self.cvars.get(self.vm.parm_str(0), 0.0))

    def _pf_cvar_set(self):
        name = self.vm.parm_str(0)
        val = _atof(self.vm.parm_str(1))
        self.cvars[name] = val
        if name == "skill":            # trigger_setskill: carry into next level
            self.skill = max(0, min(3, int(val)))
            self.gset_f("skill", float(self.skill))

    # --- sound ---
    def _start_sound(self, ent, chan, sample, vol, atten):
        """SV_StartSound from engine code (landing thuds, splashes): emit from
        the entity's bbox center, like the sound() builtin."""
        if self.snd is None:
            return
        ox, oy, oz = self.vm.fget_v(ent, self.f["origin"])
        nx, ny, nz = self.vm.fget_v(ent, self.f["mins"])
        xx, xy, xz = self.vm.fget_v(ent, self.f["maxs"])
        origin = (ox + 0.5 * (nx + xx), oy + 0.5 * (ny + xy), oz + 0.5 * (nz + xz))
        self.snd.start_sound(ent, chan, sample, vol, atten, origin)

    def _pf_sound(self):
        # sound(entity e, float chan, string sample, float vol, float atten)
        self._start_sound(self.vm.parm_i(0), int(self.vm.parm_f(1)),
                          self.vm.parm_str(2), self.vm.parm_f(3),
                          self.vm.parm_f(4))

    def _pf_ambientsound(self):
        # ambientsound(vector pos, string sample, float vol, float atten):
        # defer -- the sound isn't precached until the host loads the level
        pos = self.vm.parm_v(0)
        sample = self.vm.parm_str(1)
        vol = self.vm.parm_f(2)
        atten = self.vm.parm_f(3)
        self.ambients.append((sample, pos, vol, atten))

    def _pf_lightstyle(self):
        self.lightstyles[int(self.vm.parm_f(0))] = self.vm.parm_str(1)

    def _pf_makestatic(self):
        pass            # keep the edict alive so it still renders + animates

    def _pf_changelevel(self):
        self.changelevel = self.vm.parm_str(0)
        self.changelevel_restart = False

    def _pf_localcmd(self):
        """localcmd(string): the QC pushes a console command. The only one we
        care about is single-player respawn(): it issues "restart" to reload the
        current level fresh. Route it through the host's changelevel path, which
        takes a bare map name (the same form the changelevel builtin uses)."""
        cmd = self.vm.parm_str(0).strip()
        if cmd == "restart":
            name = self.mapname
            if name.startswith("maps/"):
                name = name[len("maps/"):]
            if name.endswith(".bsp"):
                name = name[:-len(".bsp")]
            self.changelevel = name
            # Host_Restart_f does not re-save spawn parms: a death restart
            # respawns with the loadout the player entered the level with.
            self.changelevel_restart = True

    def _pf_stuffcmd(self):
        # stuffcmd(client, string): pushes console text at a client. The one
        # gameplay use we honour is items.qc's "bf\n" -- the gold bonus flash
        # on every pickup (V_BonusFlash).
        if self.vm.parm_i(0) == self.player and "bf" in self.vm.parm_str(1):
            self.bonus_flash = True

    def _pf_centerprint(self):
        # centerprint(client, string): keep only the player's message; the host
        # draws it centred on screen for a few seconds.
        if self.vm.parm_i(0) == self.player:
            msg = self.vm.parm_str(1)
            if msg:
                self.center_msg = (msg, self.time)

    def _cap_particles(self):
        """Bound the live list at MAX_PARTICLES, dropping the oldest. id stops
        spawning when its fixed pool is exhausted; trimming the front is the
        visually gentler equivalent for our growable list."""
        n = len(self.particles)
        if n > MAX_PARTICLES:
            del self.particles[:n - MAX_PARTICLES]

    def _run_particle_effect(self, org, dirv, color, count):
        """R_RunParticleEffect: gunshots/spikes. Each particle is pt_slowgrav,
        seeded at dir*15 (barely moving -- the random kick is commented out in
        id), coloured (color & ~7) + rand&7, jittered +/-8 about org. count==1024
        is id's rocket-explosion shorthand and routes to R_ParticleExplosion."""
        if count == 1024:
            self._particle_explosion(org)
            return
        t = self.time
        for _ in range(count):
            self.particles.append([
                org[0] + (random.randint(0, 15) - 8),
                org[1] + (random.randint(0, 15) - 8),
                org[2] + (random.randint(0, 15) - 8),
                dirv[0] * 15.0, dirv[1] * 15.0, dirv[2] * 15.0,
                ((color & ~7) + random.randint(0, 7)) & 255,
                t + 0.1 * random.randint(0, 4), PT_SLOWGRAV, 0.0])
        self._cap_particles()

    def _particle_explosion(self, org):
        """R_ParticleExplosion: rocket/grenade blast. Alternating pt_explode /
        pt_explode2 (one accelerates and cools through ramp1, the other
        decelerates through ramp2), seeded +/-256 u/s, +/-16 about org, ramp
        rand&3. id spawns 1024; we subsample to _BIG_BURST."""
        t = self.time
        for i in range(_BIG_BURST):
            ptype = PT_EXPLODE if (i & 1) else PT_EXPLODE2
            self.particles.append([
                org[0] + (random.randint(0, 31) - 16),
                org[1] + (random.randint(0, 31) - 16),
                org[2] + (random.randint(0, 31) - 16),
                float(random.randint(0, 511) - 256),
                float(random.randint(0, 511) - 256),
                float(random.randint(0, 511) - 256),
                _RAMP1[0], t + 5.0, ptype, float(random.randint(0, 3))])
        self._cap_particles()

    def _blob_explosion(self, org):
        """R_BlobExplosion: tarbaby/Quad blast. Alternating pt_blob (colour
        66+rand%6) / pt_blob2 (150+rand%6), +/-256 u/s, +/-16 about org. id
        spawns 1024; we subsample to _BIG_BURST."""
        t = self.time
        for i in range(_BIG_BURST):
            die = t + 1.0 + random.randint(0, 1) * 8 * 0.05   # (rand&8)*0.05: 0 or .4
            if i & 1:
                ptype, col = PT_BLOB, 66 + random.randint(0, 5)
            else:
                ptype, col = PT_BLOB2, 150 + random.randint(0, 5)
            self.particles.append([
                org[0] + (random.randint(0, 31) - 16),
                org[1] + (random.randint(0, 31) - 16),
                org[2] + (random.randint(0, 31) - 16),
                float(random.randint(0, 511) - 256),
                float(random.randint(0, 511) - 256),
                float(random.randint(0, 511) - 256),
                col & 255, die, ptype, 0.0])
        self._cap_particles()

    def _lava_splash(self, org):
        """R_LavaSplash: a ring of pt_slowgrav jets shooting up (dir.z = 256),
        colour 224+rand&7. id walks a 32x32 grid (1024); we step it coarser."""
        t = self.time
        for i in range(-16, 16, 3):
            for j in range(-16, 16, 3):
                dx = j * 8 + random.randint(0, 7)
                dy = i * 8 + random.randint(0, 7)
                dz = 256.0
                n = math.sqrt(dx * dx + dy * dy + dz * dz)
                vel = 50 + random.randint(0, 63)
                s = vel / n
                self.particles.append([
                    org[0] + dx, org[1] + dy, org[2] + random.randint(0, 63),
                    dx * s, dy * s, dz * s,
                    (224 + random.randint(0, 7)) & 255,
                    t + 2.0 + random.randint(0, 31) * 0.02, PT_SLOWGRAV, 0.0])
        self._cap_particles()

    def _teleport_splash(self, org):
        """R_TeleportSplash: a box of pt_slowgrav sparks, colour 7+rand&7. id
        walks a 8x8x14 grid (~896); we step it coarser."""
        t = self.time
        for i in range(-16, 16, 8):
            for j in range(-16, 16, 8):
                for k in range(-24, 32, 8):
                    dx, dy, dz = j * 8.0, i * 8.0, k * 8.0
                    n = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
                    vel = 50 + random.randint(0, 63)
                    s = vel / n
                    self.particles.append([
                        org[0] + i + random.randint(0, 3),
                        org[1] + j + random.randint(0, 3),
                        org[2] + k + random.randint(0, 3),
                        dx * s, dy * s, dz * s,
                        (7 + random.randint(0, 7)) & 255,
                        t + 0.2 + random.randint(0, 7) * 0.02, PT_SLOWGRAV, 0.0])
        self._cap_particles()

    def _pf_particle(self):
        # particle(origin, dir, color, count): R_RunParticleEffect via SVC_PARTICLE
        vm = self.vm
        self._run_particle_effect(vm.parm_v(0), vm.parm_v(1), int(vm.parm_f(2)),
                                  int(vm.parm_f(3)))

    def _pf_writebyte(self):
        # decode broadcast temp-entity messages (gunshots, teleport fog, ...) into
        # particle bursts: WriteByte(SVC_TEMPENTITY); WriteByte(type); 3x WriteCoord
        v = int(self.vm.parm_f(1))
        if self._te is None:
            if v == SVC_TEMPENTITY:
                self._te = [None, [], 0]         # [type, coords, owner entity]
        elif self._te[0] is None:
            self._te[0] = v                      # the temp-entity type
        # any further bytes (counts/colours) are ignored; coords come via WriteCoord

    def _pf_writeentity(self):
        # beam temp entities carry their owner before the two endpoints
        if self._te is not None and self._te[0] is not None:
            self._te[2] = self.vm.parm_i(1)

    def _pf_writecoord(self):
        if self._te is None or self._te[0] is None:
            return
        te_type, coords, ent = self._te
        coords.append(self.vm.parm_f(1))
        if te_type in _TE_BEAMS:                 # entity + start + end
            if len(coords) == 6:
                self._add_beam(te_type, ent, tuple(coords[:3]),
                               tuple(coords[3:]))
                self._te = None
        elif len(coords) == 3:                   # have a full position -> spark it
            self._spawn_te(te_type, tuple(coords))
            self._te = None

    def _spawn_te(self, te_type, pos):
        """CL_ParseTEnt: dispatch a point temp-entity to its r_part.c effect.
        Beam types are handled separately; unknown types spark nothing."""
        z = (0.0, 0.0, 0.0)
        if te_type in (0, 1, 2):                 # spike / superspike / gunshot
            self._run_particle_effect(pos, z, 0, 10 if te_type == 0 else 20)
        elif te_type == 7:                       # wizspike (green)
            self._run_particle_effect(pos, z, 20, 30)
        elif te_type == 8:                       # knightspike
            self._run_particle_effect(pos, z, 226, 20)
        elif te_type == 3:                       # rocket explosion (+ dlight)
            self._particle_explosion(pos)
            self.dlight_events.append((pos, 350.0, self.time + 0.5, 300.0))
        elif te_type == 4:                       # tarbaby explosion (no dlight)
            self._blob_explosion(pos)
        elif te_type == 10:                      # lava splash
            self._lava_splash(pos)
        elif te_type == 11:                      # teleport fog
            self._teleport_splash(pos)

    def _add_beam(self, te_type, ent, start, end):
        """CL_ParseBeam: one live beam per owner entity (continuous lightning
        fire re-feeds it rather than stacking), 0.2 s lifetime."""
        model = _TE_BEAMS[te_type]
        die = self.time + 0.2
        if ent:
            for b in self.beams:
                if b["ent"] == ent:
                    b.update(model=model, start=start, end=end, die=die)
                    return
        self.beams.append({"ent": ent, "model": model,
                           "start": start, "end": end, "die": die})

    def light_entities(self):
        """Entities carrying engine light effects this frame: (edict, origin,
        effects bits, is_rocket) per CL_RelinkEntities. Clears one-shot
        EF_MUZZLEFLASH after reporting it (SV_CleanupEnts). Rocket glow comes
        from the model's smoke-trail flag, like model->flags & EF_ROCKET."""
        out = []
        vm, f = self.vm, self.f
        fe, fmi = f["effects"], f["modelindex"]
        for e in range(1, vm.num_edicts):
            if vm.free[e]:
                continue
            eff = int(vm.fget_f(e, fe))
            rocket = self._trail_type(int(vm.fget_f(e, fmi))) == 0
            if not eff and not rocket:
                continue
            out.append((e, vm.fget_v(e, f["origin"]), eff, rocket))
            if eff & 2:                          # EF_MUZZLEFLASH is one-shot
                vm.fset_f(e, fe, float(eff & ~2))
        return out

    def live_beams(self):
        """Live beam temp entities for the client, pruning expired ones."""
        if self.beams:
            self.beams = [b for b in self.beams if b["die"] >= self.time]
        return self.beams

    def _trail_type(self, modelindex):
        """Trail type for a model (by precache index) from its .mdl effect flags,
        cached. None if the model has no trail flag / can't be read."""
        if modelindex in self._model_trail:
            return self._model_trail[modelindex]
        tt = None
        mp = self.model_precache
        if self.pak is not None and 0 < modelindex < len(mp):
            name = mp[modelindex]
            if name.endswith(".mdl") and name in self.pak.files:
                try:
                    from .mdl import (model_flags, EF_ROCKET, EF_GRENADE, EF_GIB,
                                     EF_TRACER, EF_ZOMGIB, EF_TRACER2, EF_TRACER3)
                    fl = model_flags(self.pak.read(name))
                    # priority matches CL_RelinkEntities' if/else ladder
                    if fl & EF_ROCKET:    tt = 0
                    elif fl & EF_GRENADE: tt = 1
                    elif fl & EF_GIB:     tt = 2
                    elif fl & EF_ZOMGIB:  tt = 4
                    elif fl & EF_TRACER:  tt = 3
                    elif fl & EF_TRACER2: tt = 5
                    elif fl & EF_TRACER3: tt = 6
                except Exception as ex:
                    print(f"trail flags for {name} failed: {ex}")
        self._model_trail[modelindex] = tt
        return tt

    def _emit_trails(self):
        """R_RocketTrail: for every live entity whose model carries a trail flag,
        lay particles along the segment it moved this frame. Runs client-side in
        Quake; we do it here since this is where particles live and entity origins
        are known. Rebuilding the last-origin map each frame prunes dead edicts."""
        vm, f = self.vm, self.f
        fmi, forg = f["modelindex"], f["origin"]
        last = self._ent_lastorg
        newlast = {}
        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            tt = self._trail_type(vm.fget_i(num, fmi))
            if tt is None:
                continue
            org = vm.fget_v(num, forg)
            newlast[num] = org
            old = last.get(num)
            if old is not None and old != org:
                self._rocket_trail(old, org, tt)
        self._ent_lastorg = newlast

    def _rocket_trail(self, start, end, ttype):
        """R_RocketTrail: lay particles from start to end, one per unit step
        (id advances `start` by the unit direction each iteration while burning
        `dec` units of remaining length -- so the trail is dense and covers ~1/3
        of the move, by design). Per-type: pt_fire smoke (rocket/grenade), pt_grav
        blood (gibs), pt_static tracers with a perpendicular kick, voor sparkle."""
        sx, sy, sz = start
        dx, dy, dz = end[0] - sx, end[1] - sy, end[2] - sz
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        if length < 1e-6:
            return
        vx, vy, vz = dx / length, dy / length, dz / length   # unit direction
        dec = 3.0
        t = self.time
        guard = 0
        while length > 0.0 and guard < 1024:         # guard a pathological teleport
            guard += 1
            length -= dec
            if ttype in (0, 1):                      # rocket / grenade smoke (fire)
                ramp = float(random.randint(0, 3) + (2 if ttype == 1 else 0))
                self.particles.append([
                    sx + (random.randint(0, 5) - 3),
                    sy + (random.randint(0, 5) - 3),
                    sz + (random.randint(0, 5) - 3),
                    0.0, 0.0, 0.0, _RAMP3[int(ramp)], t + 2.0, PT_FIRE, ramp])
            elif ttype in (2, 4):                    # blood / slight blood (grav)
                self.particles.append([
                    sx + (random.randint(0, 5) - 3),
                    sy + (random.randint(0, 5) - 3),
                    sz + (random.randint(0, 5) - 3),
                    0.0, 0.0, 0.0,
                    67 + random.randint(0, 3), t + 2.0, PT_GRAV, 0.0])
                if ttype == 4:
                    length -= 3.0                    # slight blood: sparser
            elif ttype in (3, 5):                    # tracer / tracer2 (static)
                self._tracercount += 1
                col = (52 if ttype == 3 else 230) + ((self._tracercount & 4) << 1)
                if self._tracercount & 1:
                    pvx, pvy = 30.0 * vy, 30.0 * -vx
                else:
                    pvx, pvy = 30.0 * -vy, 30.0 * vx
                self.particles.append([
                    sx, sy, sz, pvx, pvy, 0.0,
                    col & 255, t + 0.5, PT_STATIC, 0.0])
            else:                                    # ttype 6: voor trail (static)
                self.particles.append([
                    sx + (random.randint(0, 15) - 8),
                    sy + (random.randint(0, 15) - 8),
                    sz + (random.randint(0, 15) - 8),
                    0.0, 0.0, 0.0,
                    9 * 16 + 8 + random.randint(0, 3), t + 0.3, PT_STATIC, 0.0])
            sx += vx; sy += vy; sz += vz
        self._cap_particles()

    def _advance_particles(self, dt):
        """R_DrawParticles: integrate and age each live particle, branching on
        its type for gravity, velocity ramp and colour fade (r_part.c:697)."""
        if not self.particles:
            return
        t = self.time
        grav = self.cvars["sv_gravity"] * 0.05 * dt        # frametime*sv_gravity*0.05
        dvel = 4.0 * dt
        time1, time2, time3 = 5.0 * dt, 10.0 * dt, 15.0 * dt
        live = []
        for p in self.particles:
            if p[7] <= t:
                continue
            # move by current velocity, then apply this type's per-frame physics
            p[0] += p[3] * dt
            p[1] += p[4] * dt
            p[2] += p[5] * dt
            ptype = p[8] if len(p) > 8 else PT_STATIC
            if ptype == PT_STATIC:
                pass
            elif ptype == PT_FIRE:
                p[9] += time1
                if p[9] >= 6.0:
                    p[7] = -1.0
                else:
                    p[6] = _RAMP3[int(p[9])]
                p[5] += grav                           # fire rises
            elif ptype == PT_EXPLODE:
                p[9] += time2
                if p[9] >= 8.0:
                    p[7] = -1.0
                else:
                    p[6] = _RAMP1[int(p[9])]
                p[3] += p[3] * dvel; p[4] += p[4] * dvel; p[5] += p[5] * dvel
                p[5] -= grav
            elif ptype == PT_EXPLODE2:
                p[9] += time3
                if p[9] >= 8.0:
                    p[7] = -1.0
                else:
                    p[6] = _RAMP2[int(p[9])]
                p[3] -= p[3] * dt; p[4] -= p[4] * dt; p[5] -= p[5] * dt   # 1x decel
                p[5] -= grav
            elif ptype == PT_BLOB:
                p[3] += p[3] * dvel; p[4] += p[4] * dvel; p[5] += p[5] * dvel
                p[5] -= grav
            elif ptype == PT_BLOB2:
                p[3] -= p[3] * dvel; p[4] -= p[4] * dvel                  # x/y only
                p[5] -= grav
            else:                                       # PT_GRAV / PT_SLOWGRAV
                p[5] -= grav
            live.append(p)
        self.particles = live

    def _pf_setspawnparms(self):
        # PF_setspawnparms(client): restore the parm1..16 globals to the values
        # the client was spawned with (coop respawn keeps the entry loadout).
        if self.spawn_parms:
            for i, v in enumerate(self.spawn_parms, 1):
                self.gset_f(f"parm{i}", float(v))

    def _pf_traceon(self):
        self.vm.trace = True

    def _pf_traceoff(self):
        self.vm.trace = False

    # --- debug / dump (cheap) ---
    def _pf_aim(self):
        """PF_aim(entity, missilespeed): vertical autoaim. If the straight
        v_forward trace lands on something damageable, shoot straight; else
        scan every takedamage == DAMAGE_AIM entity inside the sv_aim cone
        (0.93 dot) the shooter can trace to, and return v_forward's horizontal
        heading pitched vertically onto the best one."""
        vm, f = self.vm, self.f
        ent = vm.parm_i(0)
        fx, fy, fz = self.gget_v("v_forward")
        ox, oy, oz = vm.fget_v(ent, f["origin"])
        start = (ox, oy, oz + 20.0)

        # try sending a trace straight
        end = (start[0] + 2048.0 * fx, start[1] + 2048.0 * fy,
               start[2] + 2048.0 * fz)
        _frac, _ep, _pn, _as, _ss, hit = self._move_trace(start, end, 0, ent)
        if hit and vm.fget_f(hit, f["takedamage"]) == DAMAGE_AIM:
            vm.ret_v(fx, fy, fz)
            return

        # try all possible entities for the smallest turn inside the cone
        bestdist = 0.93                     # sv_aim default
        bestent = 0
        for check in range(1, vm.num_edicts):
            if vm.free[check] or check == ent:
                continue
            if vm.fget_f(check, f["takedamage"]) != DAMAGE_AIM:
                continue
            cx, cy, cz = vm.fget_v(check, f["origin"])
            nx, ny, nz = vm.fget_v(check, f["mins"])
            xx, xy, xz = vm.fget_v(check, f["maxs"])
            tgt = (cx + 0.5 * (nx + xx), cy + 0.5 * (ny + xy),
                   cz + 0.5 * (nz + xz))
            dx, dy, dz = (tgt[0] - start[0], tgt[1] - start[1],
                          tgt[2] - start[2])
            ln = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
            dist = (dx * fx + dy * fy + dz * fz) / ln
            if dist < bestdist:
                continue                    # too far to turn
            _frac, _ep, _pn, _as, _ss, hit = self._move_trace(start, tgt, 0, ent)
            if hit == check:                # can shoot at this one
                bestdist = dist
                bestent = check

        if bestent:
            bx, by, bz = vm.fget_v(bestent, f["origin"])
            dx, dy, dz = bx - ox, by - oy, bz - oz
            dist = dx * fx + dy * fy + dz * fz
            ex, ey, ez = fx * dist, fy * dist, dz   # horizontal kept, z led
            ln = math.sqrt(ex * ex + ey * ey + ez * ez) or 1.0
            vm.ret_v(ex / ln, ey / ln, ez / ln)
        else:
            vm.ret_v(fx, fy, fz)

    # --- traceline: world (hull 0) + entity bbox clip ---
    def _pf_traceline(self):
        vm = self.vm
        v1 = vm.parm_v(0)
        v2 = vm.parm_v(1)
        nomonsters = vm.parm_f(2)
        ignore = vm.parm_i(3)
        frac, endpos, pnorm, allsolid, startsolid, ent = \
            self._move_trace(v1, v2, nomonsters, ignore)
        self.gset_f("trace_allsolid", 1.0 if allsolid else 0.0)
        self.gset_f("trace_startsolid", 1.0 if startsolid else 0.0)
        self.gset_f("trace_fraction", frac)
        self.gset_f("trace_inopen", 1.0)
        self.gset_f("trace_inwater", 0.0)
        self.gset_v("trace_endpos", endpos)
        self.gset_v("trace_plane_normal", pnorm)
        self.gset_f("trace_plane_dist", 0.0)
        self.gset_i("trace_ent", ent)

    def _move_trace(self, start, end, nomonsters, ignore):
        """Line trace through the world point hull, then clipped against solid
        brush-model entities (doors, secret doors, func_walls) and -- unless
        nomonsters -- monster/player bboxes. Returns
        (fraction, endpos, plane_normal, allsolid, startsolid, hit_ent).

        Clipping bullets against SOLID_BSP submodels is what lets you shoot a
        shootable/secret door open: the trace returns the door as trace_ent so the
        QC applies T_Damage and fires its th_pain (fd_secret_use)."""
        if self.phys is not None:
            wtr = self.phys.trace_point(start, end)
            frac = wtr.fraction
            pnorm = wtr.plane_normal or (0.0, 0.0, 0.0)
            allsolid, startsolid = wtr.allsolid, wtr.startsolid
        else:
            frac, pnorm, allsolid, startsolid = 1.0, (0.0, 0.0, 0.0), False, False
        hit_ent = 0
        vm = self.vm
        fsol, famn, famx = self.f["solid"], self.f["absmin"], self.f["absmax"]
        fown, fmi, forg = self.f["owner"], self.f["modelindex"], self.f["origin"]
        mp = self.model_precache
        models = self.bsp.models if self.bsp is not None else None
        # a moving missile never clips against its owner or its owner's other
        # missiles (so a rocket doesn't blow up on the player who fired it)
        ig_owner = vm.fget_i(ignore, fown) if ignore else 0
        for e in range(1, vm.num_edicts):
            if e == ignore or e == ig_owner or vm.free[e]:
                continue
            if ignore and vm.fget_i(e, fown) == ignore:
                continue
            sol = vm.fget_f(e, fsol)
            if sol == SOLID_BSP and self.phys is not None and models is not None:
                # clip the bullet against this solid brush model's point hull,
                # in the entity's local space (its hull was compiled when closed)
                mi = vm.fget_i(e, fmi)
                if not (0 < mi < len(mp)) or mp[mi][:1] != "*":
                    continue
                sub = int(mp[mi][1:])
                if sub >= len(models):
                    continue
                org = vm.fget_v(e, forg)
                ls = [start[i] - org[i] for i in range(3)]
                le = [end[i] - org[i] for i in range(3)]
                t2 = self.phys.trace_hull0(models[sub]["headnode"], ls, le)
                if t2.fraction < frac:
                    frac = t2.fraction
                    pnorm = t2.plane_normal or (0.0, 0.0, 0.0)
                    hit_ent = e
                continue
            if nomonsters:
                continue
            if sol != SOLID_SLIDEBOX and sol != 2:   # SLIDEBOX or BBOX only
                continue
            hit = _ray_box(start, end, vm.fget_v(e, famn), vm.fget_v(e, famx))
            if hit is not None and hit[0] < frac:
                frac, pnorm, hit_ent = hit[0], hit[1], e
        endpos = [start[i] + (end[i] - start[i]) * frac for i in range(3)]
        return frac, endpos, pnorm, allsolid, startsolid, hit_ent

    # ---- player edict + firing (engine-driven; QC does the damage) ----
    def _exec_named(self, name, ent):
        """Run the progs function `name` with self = ent. Returns False if
        this progs has no such function; a PR_RunError is reported but does
        not propagate (matching the per-think containment in run_frame)."""
        func = self.pr.find_function(name)
        if func is None:
            return False
        self.gset_f("time", self.time)
        self.gset_i("self", ent)
        self.gset_i("other", 0)
        try:
            self.vm.execute(func)
        except PR_RunError as ex:
            print(f"{name} aborted: {ex}")
        return True

    def save_spawn_parms(self):
        """SV_SaveSpawnparms: grab the player's state for the transition to
        another level. Runs QC SetChangeParms (strips keys/powerups, caps
        health, writes parm1..parm9) and returns the parm1..16 globals for the
        host to feed the next level's spawn_player. Also latches the
        serverflags global (episode sigils) into self.serverflags. Returns
        None if there is no live player."""
        self.serverflags = self.gget_f("serverflags")
        if not self.player or self.vm.free[self.player]:
            return None
        self._exec_named("SetChangeParms", self.player)
        return [self.gget_f(f"parm{i}") for i in range(1, 17)]

    # ---- savegames (Host_Savegame_f / Host_Loadgame_f, host_cmd.c) ----
    def _format_value(self, etype, ival, fvals):
        """PR_UglyValueString: one value as savegame text. etype is the def
        type: 1 string, 2 float, 3 vector, 4 entity, 5 field, 6 function."""
        if etype == 1:
            return self.pr.string(ival)
        if etype == 2:
            return f"{fvals[0]:.6f}"
        if etype == 3:
            return f"{fvals[0]:.6f} {fvals[1]:.6f} {fvals[2]:.6f}"
        if etype == 4:
            return str(ival)
        if etype == 5:
            for name, d in self.pr.field_by_name.items():
                if d[1] == ival:
                    return name
            return ""
        if etype == 6 and 0 <= ival < len(self.pr.functions):
            return self.pr.functions[ival].name
        return ""

    def save_text(self):
        """The body of a .sav file, laid out like Host_Savegame_f writes it:
        version, comment, 16 spawn parms, skill, map name, time, 64 lightstyle
        lines, then ED_WriteGlobals' block and one block per edict (free slots
        are empty {} blocks)."""
        out = ["5"]                                     # SAVEGAME_VERSION
        kills = (f"kills:{int(self.gget_f('killed_monsters')):3d}/"
                 f"{int(self.gget_f('total_monsters')):3d}")
        out.append(f"{self.mapname} {kills}"[:39])
        parms = self.spawn_parms or [0.0] * 16
        out.extend(f"{parms[i]:.6f}" for i in range(16))
        out.append(f"{float(self.cvars.get('skill', self.skill)):.6f}")
        name = self.mapname
        if name.startswith("maps/"):
            name = name[len("maps/"):]
        if name.endswith(".bsp"):
            name = name[:-len(".bsp")]
        out.append(name)
        out.append(f"{self.time:.6f}")
        out.extend(self.lightstyles.get(i) or "m" for i in range(64))

        out.append("{")                                 # ED_WriteGlobals
        for etype, ofs, gname, save in self.pr.globaldefs:
            if not save or etype not in (1, 2, 4):
                continue
            val = self._format_value(etype, self.pr.gi[ofs], (self.pr.gf[ofs],))
            out.append(f'"{gname}" "{val}"')
        out.append("}")

        for e in range(self.vm.num_edicts):             # ED_Write each edict
            out.append("{")
            if not self.vm.free[e]:
                for etype, ofs, fname, _save in self.pr.fielddefs:
                    if not fname or (len(fname) > 2 and fname[-2] == "_"):
                        continue                        # skip _x/_y/_z members
                    size = 3 if etype == 3 else 1
                    if all(self.vm.fget_i(e, ofs + k) == 0 for k in range(size)):
                        continue                        # zero: don't write it
                    if etype == 3:
                        val = self._format_value(3, 0, self.vm.fget_v(e, ofs))
                    elif etype == 2:
                        val = self._format_value(2, 0, (self.vm.fget_f(e, ofs),))
                    else:
                        val = self._format_value(etype, self.vm.fget_i(e, ofs), ())
                    out.append(f'"{fname}" "{val}"')
            out.append("}")
        return "\n".join(out) + "\n"

    def restore_save(self, time, lightstyles, body):
        """Host_Loadgame_f's second half: the level has just been spawned
        normally (rebuilding precaches); overwrite the globals and every
        edict's fields with the saved blocks, then rebind the player and
        relink everything."""
        vm = self.vm
        self.lightstyles = {i: s for i, s in enumerate(lightstyles)
                            if s and s != "m"}
        blocks = list(parse_entities(body))
        for key, value in blocks[0].items():            # ED_ParseGlobals
            d = self.pr.global_by_name.get(key)
            if d is None:
                continue
            etype, ofs = d
            if etype == 2:
                self.pr.gf[ofs] = _atof(value)
            elif etype == 1:
                self.pr.gi[ofs] = self.pr.new_string(value)
            elif etype == 4:
                self.pr.gi[ofs] = int(_atof(value))

        edicts = blocks[1:]
        for e, fields in enumerate(edicts):
            vm.clear_edict(e)
            if not fields and e:                        # saved as free
                vm.free_edict(e)
                continue
            vm.free[e] = False
            self._parse_edict(e, fields)
        for e in range(len(edicts), vm.num_edicts):     # not in the save
            vm.free_edict(e)
        if len(edicts) > vm.num_edicts:
            vm.num_edicts = len(edicts)

        self.time = time
        self.gset_f("time", self.time)
        self.changelevel = None
        self.intermission_time = None
        self.player = 0
        for e in range(1, vm.num_edicts):
            if vm.free[e]:
                continue
            if self.pr.string(vm.fget_i(e, self.f["classname"])) == "player":
                self.player = e
            self._link_abs(e)                           # SV_LinkEdict

    def spawn_player(self, origin, angles, parms=None):
        """Create a client edict so monsters have a target and shots have an
        attacker. The camera drives it each frame via update_player().
        `parms` is the parm1..16 list save_spawn_parms captured on the
        previous level, or None for a fresh game's default loadout."""
        vm, f = self.vm, self.f
        e = vm.alloc_edict()
        self.player = e
        vm.fset_i(e, f["classname"], self.pr.new_string("player"))
        vm.fset_f(e, f["health"], 100.0)
        vm.fset_f(e, f["max_health"], 100.0)
        vm.fset_f(e, f["takedamage"], float(DAMAGE_AIM))
        vm.fset_f(e, f["solid"], float(SOLID_SLIDEBOX))
        vm.fset_f(e, f["movetype"], float(MOVETYPE_WALK))
        vm.fset_f(e, f["flags"], float(int(vm.fget_f(e, f["flags"])) | FL_CLIENT))
        # PutClientInServer normally wires th_die = PlayerDie; we hand-build the
        # client edict instead, so set it ourselves -- without it combat.qc's
        # Killed() calls a null .th_die and the player never actually dies.
        die = self.pr.find_function("PlayerDie")
        if die is not None:
            vm.fset_i(e, f["th_die"], die)
        # Loadout via the real QC, as PutClientInServer does: a fresh game runs
        # SetNewParms (axe + shotgun, 25 shells); a changelevel hands us the
        # parms SetChangeParms saved on the previous level. Either way the
        # parm1..16 globals are filled and DecodeLevelParms restores
        # items/health/armor/ammo/weapon from them.
        if parms is None:
            self._exec_named("SetNewParms", e)
            parms = [self.gget_f(f"parm{i}") for i in range(1, 17)]
        else:
            for i, v in enumerate(parms, 1):
                self.gset_f(f"parm{i}", float(v))
        self.spawn_parms = list(parms)   # PF_setspawnparms restores from these
        self._exec_named("DecodeLevelParms", e)
        vm.fset_v(e, f["view_ofs"], (0.0, 0.0, 22.0))
        # PutClientInServer gives the player a full 12s of air; without it the
        # first WaterMove sees air_finished == 0 < time and plays the drowning
        # gasp the instant the level loads.
        vm.fset_f(e, f["air_finished"], self.time + 12.0)
        self._set_minmax(e, (-16.0, -16.0, -24.0), (16.0, 16.0, 32.0))
        # let the real QC pick the view model: W_SetCurrentAmmo sets .weaponmodel
        # ("progs/v_shot.mdl") and .weaponframe from .weapon, exactly as the game
        # does in PutClientInServer -- so the first-person weapon renders
        self._exec_named("W_SetCurrentAmmo", e)
        self.update_player(origin, angles)
        return e

    def player_health(self):
        if not self.player:
            return 0.0
        return self.vm.fget_f(self.player, self.f["health"])

    def toggle_god(self):
        """Flip FL_GODMODE on the player edict; returns the new state. No-op
        (False) if there is no live player."""
        if not self.player:
            return False
        vm, f, e = self.vm, self.f, self.player
        flags = int(vm.fget_f(e, f["flags"])) ^ FL_GODMODE
        vm.fset_f(e, f["flags"], float(flags))
        return bool(flags & FL_GODMODE)

    def toggle_notarget(self):
        """Flip FL_NOTARGET on the player edict (QC FindTarget skips you);
        returns the new state. No-op (False) without a live player."""
        if not self.player:
            return False
        vm, f, e = self.vm, self.f, self.player
        flags = int(vm.fget_f(e, f["flags"])) ^ FL_NOTARGET
        vm.fset_f(e, f["flags"], float(flags))
        return bool(flags & FL_NOTARGET)

    def give(self, what, amount=None):
        """Cheat: set the player's health or one of the four ammo pools.
        `what` is h/health or s/n/r/c; `amount` defaults to 100. Returns a
        status string for the console."""
        if not self.player:
            return "no player"
        vm, f, e = self.vm, self.f, self.player
        amt = 100 if amount is None else amount
        key = what.lower()
        if key in ("h", "health"):
            vm.fset_f(e, f["health"], float(amt))
            return f"health {int(vm.fget_f(e, f['health']))}"
        pools = {"s": "ammo_shells", "n": "ammo_nails",
                 "r": "ammo_rockets", "c": "ammo_cells"}
        if key in pools:
            fld = pools[key]
            vm.fset_f(e, f[fld], float(amt))
            return f"{fld} {int(vm.fget_f(e, f[fld]))}"
        return f"give: unknown item {what}"

    def player_origin(self):
        return self.vm.fget_v(self.player, self.f["origin"]) if self.player else None

    def player_angles(self):
        return self.vm.fget_v(self.player, self.f["angles"]) if self.player else None

    def player_velocity(self):
        return self.vm.fget_v(self.player, self.f["velocity"]) if self.player else None

    def player_view_ofs(self):
        """Eye offset above the edict origin. 22 alive; PlayerDie drops it to -8
        so the death cam sinks to the corpse on the floor."""
        return self.vm.fget_v(self.player, self.f["view_ofs"]) if self.player else None

    def weapon_status(self):
        """(weapon name, current ammo) for the HUD, or None. Reads the active
        .weapon flag and .currentammo the QC keeps in sync."""
        if not self.player:
            return None
        vm, f, e = self.vm, self.f, self.player
        name = _WEAPON_NAMES.get(int(vm.fget_f(e, f["weapon"])), "?")
        return name, int(vm.fget_f(e, f["currentammo"]))

    def hud_status(self):
        """Player status-bar values, or None if there's no player. Returns a dict
        with health, armor, the current weapon + its ammo, all four ammo counts,
        keys, powerups -- everything the QC keeps on the client edict -- plus the
        raw ``items`` int (with episode sigils folded into bits 28-31 as
        SV_WriteClientdataToMessage does) and ``weapon_bit`` (the raw IT_ flag for
        the active weapon, as QC stores in .weapon)."""
        if not self.player:
            return None
        vm, f, e = self.vm, self.f, self.player
        g = lambda n: int(vm.fget_f(e, f[n]))
        items = g("items")
        keys = " ".join(name for bit, name in
                        ((IT_KEY1, "silver key"), (IT_KEY2, "gold key"))
                        if items & bit)
        powerups = " ".join(name for bit, name in
                            ((IT_INVISIBILITY, "ring"), (IT_INVULNERABILITY,
                             "pent"), (IT_SUIT, "suit"), (IT_QUAD, "quad"))
                            if items & bit)
        return {
            "health": g("health"),
            "armor": g("armorvalue"),
            "weapon": _WEAPON_NAMES.get(g("weapon"), "?"),
            "ammo": g("currentammo"),
            "shells": g("ammo_shells"),
            "nails": g("ammo_nails"),
            "rockets": g("ammo_rockets"),
            "cells": g("ammo_cells"),
            "keys": keys,
            "powerups": powerups,
            # SV_WriteClientdataToMessage: bits |= (int)pr_global_struct->serverflags << 28
            "items": items | ((int(self.gget_f("serverflags")) & 15) << 28),
            "weapon_bit": g("weapon"),       # QC .weapon is the raw IT_ bit
        }

    def view_weapon(self):
        """The first-person weapon model the QC has selected, as
        (path, frame) -- e.g. ("progs/v_shot.mdl", 0). None if there is no
        player or no weapon model set (e.g. axe-less / dead). Mirrors what
        R_DrawViewModel reads from the client: .weaponmodel and .weaponframe."""
        if not self.player:
            return None
        vm, f, e = self.vm, self.f, self.player
        path = self.pr.string(vm.fget_i(e, f["weaponmodel"]))
        if not path:
            return None
        return path, int(vm.fget_f(e, f["weaponframe"]))

    # ================================================================
    # protocol serialization (delegated to quake/sv_send.py to keep the
    # message-building out of this already-large module)
    # ================================================================
    def create_baseline(self):
        """SV_CreateBaseline (sv_main.c:925) -- snapshot spawn-time entity state
        into self.baselines. Call after load_level + spawn_player."""
        from .sv_send import create_baseline
        create_baseline(self)

    def model_index(self, name):
        """modelindex for a precached model name, or 0 (SV_ModelIndex,
        sv_main.c). The client resolves it back via cl.model_precache."""
        if not name:
            return 0
        try:
            return self.model_precache.index(name)
        except ValueError:
            return 0

    def level_name(self):
        """Printable level title for svc_serverinfo: the worldspawn "message"
        key (the map's display name), falling back to the map filename."""
        if self.bsp is not None:
            try:
                for fields in parse_entities(self.bsp.entities):
                    return fields.get("message", self.mapname)
            except Exception:
                pass
        return self.mapname

    def update_player(self, origin, angles):
        if not self.player:
            return
        vm, f, e = self.vm, self.f, self.player
        vm.fset_v(e, f["origin"], origin)
        vm.fset_v(e, f["angles"], angles)
        vm.fset_v(e, f["v_angle"], angles)
        self._link_abs(e)

    def update_player_water(self, waterlevel, watertype):
        """Stamp the player edict's waterlevel/watertype before the QC tick, as
        SV_ClientThink's SV_CheckWater does. The ported client.qc WaterMove then
        owns the rest: the enter/leave/gasp sounds, drowning and lava/slime
        damage, and the FL_INWATER flag. (CheckWaterJump/FL_WATERJUMP stays a
        no-op -- id left it commented out in PlayerPreThink.)"""
        if not self.player:
            return
        vm, f, e = self.vm, self.f, self.player
        vm.fset_f(e, f["waterlevel"], float(waterlevel))
        vm.fset_f(e, f["watertype"], float(watertype))

    def intermission_active(self):
        """True once the QC has entered intermission (execute_changelevel set
        intermission_running). The host freezes the camera at the player's
        intermission spot and hides the view model while this holds."""
        return self.gget_f("intermission_running") > 0.0

    def intermission_stats(self):
        """End-of-level tallies for the intermission overlay (Sbar_Intermission-
        Overlay): completed_time in seconds plus secrets-found/total and
        monsters-killed/total. None outside intermission.

        completed_time is the level time frozen by run_frame the frame the exit
        was reached (mirrors cl.completed_time, stamped when svc_intermission
        arrives)."""
        if not self.intermission_active():
            return None
        return {
            "time": int(self.intermission_time or 0.0),
            "secrets": int(self.gget_f("found_secrets")),
            "total_secrets": int(self.gget_f("total_secrets")),
            "monsters": int(self.gget_f("killed_monsters")),
            "total_monsters": int(self.gget_f("total_monsters")),
        }

    def run_intermission(self, button0):
        """Drive the player's IntermissionThink, which PlayerPreThink would run
        each frame during intermission: once `time` passes intermission_exittime
        and the player presses a button, it calls GotoNextMap -> changelevel
        (which sets self.changelevel for the host to load). The player edict was
        already frozen at the camera spot by execute_changelevel."""
        if not self.player:
            return
        func = self.pr.find_function("IntermissionThink")
        if func is None:
            return
        vm, f, e = self.vm, self.f, self.player
        vm.fset_f(e, f["button0"], 1.0 if button0 else 0.0)
        self.gset_f("time", self.time)
        self.gset_i("self", e)
        self.gset_i("other", 0)
        try:
            vm.execute(func)
        except PR_RunError as ex:
            print(f"IntermissionThink aborted: {ex}")

    def set_input(self, button0, impulse=0):
        """Host -> server input for the player: attack-held state and a weapon
        select impulse. The impulse is queued and consumed by the next weapon
        frame so a single keypress switches once."""
        self.button0 = bool(button0)
        if impulse:
            self.pending_impulse = int(impulse)

    def run_drop_punch_angle(self, dt):
        """DropPunchAngle (sv_user.c): bleed the weapon-fire view kick the QC
        set on .punchangle back to zero at 10 deg/s."""
        if not self.player or self.vm.free[self.player]:
            return
        fp = self.f["punchangle"]
        if fp is None:
            return
        px, py, pz = self.vm.fget_v(self.player, fp)
        ln = math.sqrt(px * px + py * py + pz * pz)
        if not ln:
            return
        s = max(0.0, ln - 10.0 * dt) / ln
        self.vm.fset_v(self.player, fp, (px * s, py * s, pz * s))

    def run_water_move(self):
        """Run the QC's WaterMove for the player (the water half of PlayerPreThink):
        air/drown timing with gasp sounds, lava/slime damage, the enter/leave-water
        sounds and the FL_INWATER flag. The engine stamps the edict's waterlevel/
        watertype first (update_player_water) exactly as SV_ClientThink's water
        check does; WaterMove reads them. Mirrors how run_weapon_frame drives
        PlayerPostThink's weapon logic straight from the game's own QC."""
        if not self.player or self.phys is None:
            return
        if self.intermission_active():
            return
        vm, f, e = self.vm, self.f, self.player
        if vm.fget_f(e, f["health"]) <= 0:
            return
        func = self.pr.find_function("WaterMove")
        if func is None:
            return
        self.gset_f("time", self.time)
        self.gset_f("frametime", self.frametime)
        self.gset_i("self", e)
        self.gset_i("other", 0)
        try:
            vm.execute(func)
        except PR_RunError as ex:
            print(f"water move aborted: {ex}")

    def run_weapon_frame(self):
        """One tick of the real Quake weapon system (what PlayerPostThink runs):
        W_WeaponFrame honours .attack_finished cadence, runs ImpulseCommands for
        weapon switching, and calls W_Attack -> W_Fire* when .button0 is held.
        This drives every weapon -- ammo, view-model animation and all -- from the
        game's own QC, instead of the engine hardcoding a single weapon."""
        if not self.player or self.phys is None:
            return
        if self.intermission_active():     # PlayerPreThink returns before weapons
            return
        vm, f, e = self.vm, self.f, self.player
        if vm.fget_f(e, f["health"]) <= 0:
            return
        func = self.pr.find_function("W_WeaponFrame")
        if func is None:
            return
        # latch this frame's inputs onto the player edict, then let the QC read them
        vm.fset_f(e, f["button0"], 1.0 if self.button0 else 0.0)
        if self.pending_impulse:
            vm.fset_f(e, f["impulse"], float(self.pending_impulse))
            self.pending_impulse = 0
        self.gset_f("time", self.time)
        self.gset_f("frametime", self.frametime)
        self.gset_i("self", e)
        self.gset_i("other", 0)
        # W_Attack calls makevectors(self.v_angle) itself; seed the vectors anyway
        # so aim() (which returns v_forward) points where the player is looking.
        fwd, right, up = angle_vectors(vm.fget_v(e, f["v_angle"]))
        self.gset_v("v_forward", fwd)
        self.gset_v("v_right", right)
        self.gset_v("v_up", up)
        try:
            vm.execute(func)
        except PR_RunError as ex:
            print(f"weapon frame aborted: {ex}")

    def run_player_death_think(self):
        """Once PlayerDie has run the corpse to DEAD_DEAD, this is the part of
        PlayerPreThink that matters: PlayerDeathThink decelerates the body, waits
        for all buttons up, then for fire to be pressed again, and calls respawn()
        -- which in single player restarts the level. We feed it the fire button
        (set_input) and run it server-side, since the engine drives the live
        player from the camera and never runs the rest of PlayerPreThink."""
        if not self.player:
            return
        vm, f, e = self.vm, self.f, self.player
        # only after the death animation has settled to DEAD_DEAD; running it
        # mid-DEAD_DYING would hit the 'fire pressed -> respawn' path early.
        if vm.fget_f(e, f["deadflag"]) < DEAD_DEAD:
            return
        func = self.pr.find_function("PlayerDeathThink")
        if func is None:
            return
        vm.fset_f(e, f["button0"], 1.0 if self.button0 else 0.0)
        self.gset_f("time", self.time)
        self.gset_i("self", e)
        self.gset_i("other", 0)
        try:
            vm.execute(func)
        except PR_RunError as ex:
            print(f"PlayerDeathThink aborted: {ex}")

    def _pf_pointcontents(self):
        # PF_pointcontents -> SV_PointContents (hull 0): what the point is
        # inside of. The QC rules that hang off this include the lightning gun
        # discharging underwater and fish/scrag water checks.
        if self.phys is None:
            self.vm.ret_f(CONTENTS_EMPTY)
            return
        self.vm.ret_f(float(self.phys.point_contents_0(self.vm.parm_v(0))))

    def _pf_walkmove(self):
        """PF_walkmove(yaw, dist): try to step the monster `dist` units along
        `yaw`. Returns whether the step succeeded. Used by the QC for melee
        lunges, dodges and the spawn placement walk."""
        vm, f = self.vm, self.f
        e = vm.gi[self.g["self"]]
        if self.phys is None:
            vm.ret_f(0.0)            # no collision world -> can't step (test boot)
            return
        if not (int(vm.fget_f(e, f["flags"])) & (FL_ONGROUND | FL_FLY | FL_SWIM)):
            vm.ret_f(0.0)
            return
        yaw = math.radians(vm.parm_f(0))
        dist = vm.parm_f(1)
        move = (math.cos(yaw) * dist, math.sin(yaw) * dist, 0.0)
        # SV_movestep may run other progs (touch); save/restore self.
        oldself = vm.gi[self.g["self"]]
        ok = self._sv_movestep(e, move, relink=True)
        self.gset_i("self", oldself)
        vm.ret_f(1.0 if ok else 0.0)

    def _pf_droptofloor(self):
        """PF_droptofloor: drop the entity straight down (up to 256 units) onto
        the floor and mark it standing. Monsters call this at spawn to settle
        onto the ground; returns 0 (and the QC removes the monster) if it is
        stuck in solid or floating with no floor below."""
        vm, f = self.vm, self.f
        e = vm.gi[self.g["self"]]
        if self.phys is None:
            # no collision world (headless/test boot): leave the entity at its
            # placed origin and mark it grounded, as the old stub did
            fl = int(vm.fget_f(e, f["flags"])) | FL_ONGROUND
            vm.fset_f(e, f["flags"], float(fl))
            vm.fset_i(e, f["groundentity"], 0)
            vm.ret_f(1.0)
            return
        org = vm.fget_v(e, f["origin"])
        end = (org[0], org[1], org[2] - 256.0)
        tr = self._box_move(e, org, end)
        if tr.fraction == 1.0 or tr.allsolid:
            vm.ret_f(0.0)
            return
        vm.fset_v(e, f["origin"], tuple(tr.endpos))
        self._link_abs(e)
        fl = int(vm.fget_f(e, f["flags"])) | FL_ONGROUND
        vm.fset_f(e, f["flags"], float(fl))
        vm.fset_i(e, f["groundentity"], tr.ent if tr.ent is not None else 0)
        vm.ret_f(1.0)

    def _pf_checkbottom(self):
        if self.phys is None:
            self.vm.ret_f(1.0)
            return
        e = self.vm.gi[self.g["self"]]
        self.vm.ret_f(1.0 if self._sv_check_bottom(e) else 0.0)

    def _pf_movetogoal(self):
        """SV_MoveToGoal(dist): the heart of monster locomotion. Step `dist`
        toward .goalentity, re-deriving a path (SV_NewChaseDir) when the current
        heading is blocked or at random, and stopping once close to the goal."""
        vm, f = self.vm, self.f
        if self.phys is None:
            return
        e = vm.gi[self.g["self"]]
        goal = vm.fget_i(e, f["goalentity"])
        dist = vm.parm_f(0)
        if not (int(vm.fget_f(e, f["flags"])) & (FL_ONGROUND | FL_FLY | FL_SWIM)):
            vm.ret_f(0.0)
            return
        # if the next step reaches the enemy, stop here
        if vm.fget_i(e, f["enemy"]) != 0 and self._sv_close_enough(e, goal, dist):
            return
        oldself = vm.gi[self.g["self"]]
        if (random.randint(0, 3) == 1
                or not self._sv_step_direction(e, vm.fget_f(e, f["ideal_yaw"]), dist)):
            self._sv_new_chase_dir(e, goal, dist)
        self.gset_i("self", oldself)

    # ---- SV_Move helpers for walkmonsters (sv_move.c) ----
    def _box_move(self, ent, start, end):
        """SV_Move for an entity's bounding box (hull 1), clipped against the
        world, solid brush models and solid box entities (so a monster collides
        with the player and barrels, not just walls). passedict=ent skips the
        mover and its own missiles; record=False keeps these probes from firing
        player touches. Passes the entity's origin-relative mins so the hull
        offset is applied -- items rest with mins.z = 0, so without it their floor
        trace comes back allsolid."""
        mins = self.vm.fget_v(ent, self.f["mins"])
        maxs = self.vm.fget_v(ent, self.f["maxs"])
        return self.phys.move(list(start), list(end), record=False, mins=mins,
                              maxs=maxs, passedict=ent)

    def _sv_movestep(self, ent, move, relink):
        """SV_movestep: try to move `ent` by `move`, stepping up to STEPSIZE over
        ledges and refusing moves that walk off an edge (CheckBottom). Returns
        True on success, with the entity's origin advanced."""
        vm, f = self.vm, self.f
        org = vm.fget_v(ent, f["origin"])
        flags = int(vm.fget_f(ent, f["flags"]))

        if flags & (FL_SWIM | FL_FLY):
            # flying/swimming: try with a little vertical chase, then flat
            enemy = vm.fget_i(ent, f["enemy"])
            for i in range(2):
                neworg = [org[0] + move[0], org[1] + move[1], org[2] + move[2]]
                if i == 0 and enemy != 0:
                    eo = vm.fget_v(enemy, f["origin"])
                    dz = org[2] - eo[2]
                    if dz > 40:
                        neworg[2] -= 8
                    if dz < 30:
                        neworg[2] += 8
                tr = self._box_move(ent, org, neworg)
                if tr.fraction == 1.0:
                    if (flags & FL_SWIM) and \
                            self.phys.point_contents_0(tr.endpos) == CONTENTS_EMPTY:
                        return False        # swim monster left the water
                    vm.fset_v(ent, f["origin"], tuple(tr.endpos))
                    if relink:
                        self._link_abs(ent)
                    return True
                if enemy == 0:
                    break
            return False

        # walking: drop down from a step above the wished spot
        neworg = [org[0] + move[0], org[1] + move[1], org[2] + move[2] + STEPSIZE]
        end = [neworg[0], neworg[1], neworg[2] - STEPSIZE * 2.0]
        tr = self._box_move(ent, neworg, end)
        if tr.allsolid:
            return False
        if tr.startsolid:
            neworg[2] -= STEPSIZE
            tr = self._box_move(ent, neworg, end)
            if tr.allsolid or tr.startsolid:
                return False
        if tr.fraction == 1.0:
            # walked off a ledge into the air -- only ok if the floor was pulled
            # out from under a standing monster (FL_PARTIALGROUND)
            if flags & FL_PARTIALGROUND:
                vm.fset_v(ent, f["origin"],
                          (org[0] + move[0], org[1] + move[1], org[2] + move[2]))
                if relink:
                    self._link_abs(ent)
                vm.fset_f(ent, f["flags"], float(flags & ~FL_ONGROUND))
                return True
            return False

        vm.fset_v(ent, f["origin"], tuple(tr.endpos))
        if not self._sv_check_bottom(ent):
            if flags & FL_PARTIALGROUND:
                if relink:
                    self._link_abs(ent)
                return True
            vm.fset_v(ent, f["origin"], org)        # restore: no footing
            return False

        if flags & FL_PARTIALGROUND:
            vm.fset_f(ent, f["flags"], float(flags & ~FL_PARTIALGROUND))
        vm.fset_i(ent, f["groundentity"], tr.ent if tr.ent is not None else 0)
        if relink:
            self._link_abs(ent)
        return True

    def _sv_step_direction(self, ent, yaw, dist):
        """SV_StepDirection: face `yaw`, then movestep that way. Backs the move
        out if the turn didn't finish, so monsters round corners cleanly."""
        vm, f = self.vm, self.f
        vm.fset_f(ent, f["ideal_yaw"], yaw)
        self.gset_i("self", ent)
        self._pf_changeyaw_ent(ent)
        ry = math.radians(yaw)
        move = (math.cos(ry) * dist, math.sin(ry) * dist, 0.0)
        oldorg = vm.fget_v(ent, f["origin"])
        if self._sv_movestep(ent, move, relink=False):
            cur = vm.fget_v(ent, f["angles"])[1]
            delta = cur - vm.fget_f(ent, f["ideal_yaw"])
            if delta > 45 and delta < 315:        # turned too little; don't step
                vm.fset_v(ent, f["origin"], oldorg)
            self._link_abs(ent)
            return True
        self._link_abs(ent)
        return False

    def _sv_new_chase_dir(self, actor, enemy, dist):
        """SV_NewChaseDir: pick a fresh heading toward the goal when the straight
        path is blocked -- try the two axis directions, then sweep all 8."""
        vm, f = self.vm, self.f
        olddir = anglemod(int(vm.fget_f(actor, f["ideal_yaw"]) / 45) * 45)
        turnaround = anglemod(olddir - 180)
        ao = vm.fget_v(actor, f["origin"])
        eo = vm.fget_v(enemy, f["origin"]) if enemy != 0 else ao
        deltax, deltay = eo[0] - ao[0], eo[1] - ao[1]
        d1 = 0.0 if deltax > 10 else 180.0 if deltax < -10 else DI_NODIR
        d2 = 270.0 if deltay < -10 else 90.0 if deltay > 10 else DI_NODIR

        # straight diagonal toward the goal first
        if d1 != DI_NODIR and d2 != DI_NODIR:
            tdir = (45.0 if d2 == 90 else 315.0) if d1 == 0 else \
                   (135.0 if d2 == 90 else 215.0)
            if tdir != turnaround and self._sv_step_direction(actor, tdir, dist):
                return

        if random.randint(0, 1) or abs(deltay) > abs(deltax):
            d1, d2 = d2, d1
        if d1 != DI_NODIR and d1 != turnaround and \
                self._sv_step_direction(actor, d1, dist):
            return
        if d2 != DI_NODIR and d2 != turnaround and \
                self._sv_step_direction(actor, d2, dist):
            return
        if olddir != DI_NODIR and self._sv_step_direction(actor, olddir, dist):
            return

        dirs = range(0, 316, 45) if random.randint(0, 1) else range(315, -1, -45)
        for tdir in dirs:
            if tdir != turnaround and self._sv_step_direction(actor, float(tdir), dist):
                return
        if turnaround != DI_NODIR and \
                self._sv_step_direction(actor, turnaround, dist):
            return
        vm.fset_f(actor, f["ideal_yaw"], olddir)     # can't move

    def _sv_close_enough(self, ent, goal, dist):
        """SV_CloseEnough: are ent and goal within `dist` on every axis?"""
        if goal == 0:
            return False
        vm, f = self.vm, self.f
        amn, amx = vm.fget_v(ent, f["absmin"]), vm.fget_v(ent, f["absmax"])
        gmn, gmx = vm.fget_v(goal, f["absmin"]), vm.fget_v(goal, f["absmax"])
        for i in range(3):
            if gmn[i] > amx[i] + dist:
                return False
            if gmx[i] < amn[i] - dist:
                return False
        return True

    def _sv_check_bottom(self, ent):
        """SV_CheckBottom: is the entity standing on solid ground? Quick test --
        all four bottom corners over solid world -- then a fuller probe that the
        corners are within STEPSIZE of the centre, so monsters don't teeter off
        ledges."""
        vm, f = self.vm, self.f
        org = vm.fget_v(ent, f["origin"])
        mn = vm.fget_v(ent, f["mins"])
        mx = vm.fget_v(ent, f["maxs"])
        mins = [org[0] + mn[0], org[1] + mn[1], org[2] + mn[2]]
        maxs = [org[0] + mx[0], org[1] + mx[1], org[2] + mx[2]]

        pc = self.phys.point_contents_0
        start = [0.0, 0.0, mins[2] - 1.0]
        easy = True
        for x in (mins[0], maxs[0]):
            for y in (mins[1], maxs[1]):
                start[0], start[1] = x, y
                if pc(start) != CONTENTS_SOLID:
                    easy = False
                    break
            if not easy:
                break
        if easy:
            return True

        # fuller check uses point traces straight down (SV_Move with a zero box)
        start[2] = mins[2]
        midx = (mins[0] + maxs[0]) * 0.5
        midy = (mins[1] + maxs[1]) * 0.5
        stopz = start[2] - 2.0 * STEPSIZE
        tr = self.phys.trace_point([midx, midy, start[2]], [midx, midy, stopz])
        if tr.fraction == 1.0:
            return False
        mid = bottom = tr.endpos[2]
        for x in (mins[0], maxs[0]):
            for y in (mins[1], maxs[1]):
                tr = self.phys.trace_point([x, y, start[2]], [x, y, stopz])
                if tr.fraction != 1.0 and tr.endpos[2] > bottom:
                    bottom = tr.endpos[2]
                if tr.fraction == 1.0 or mid - tr.endpos[2] > STEPSIZE:
                    return False
        return True

    def _pf_changeyaw_ent(self, ent):
        """changeyaw for a specific edict (SV_StepDirection turns the actor)."""
        self.gset_i("self", ent)
        self._pf_changeyaw()

    def _pf_changeyaw(self):
        vm = self.vm
        e = vm.gi[self.g["self"]]
        ax, ay, az = vm.fget_v(e, self.f["angles"])
        current = anglemod(ay)
        ideal = vm.fget_f(e, self.f["ideal_yaw"])
        speed = vm.fget_f(e, self.f["yaw_speed"])
        if current == ideal:
            return
        move = ideal - current
        if ideal > current:
            if move >= 180:
                move -= 360
        else:
            if move <= -180:
                move += 360
        if move > 0:
            move = min(move, speed)
        else:
            move = max(move, -speed)
        vm.fset_v(e, self.f["angles"], (ax, anglemod(current + move), az))


def _atof(s):
    """Parse a leading float like C atof (tolerant of trailing junk)."""
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        out = ""
        for ch in s:
            if ch in "0123456789+-.eE":
                out += ch
            else:
                break
        try:
            return float(out)
        except ValueError:
            return 0.0


if __name__ == "__main__":
    import sys
    from .pak import Pak
    from .progs import Progs
    from .bsp import Bsp

    pak = Pak("quake-shareware/id1/pak0.pak")
    mapname = "maps/" + (sys.argv[1] if len(sys.argv) > 1 else "e1m1") + ".bsp"
    pr = Progs(pak.read("progs.dat"))
    bsp = Bsp(pak.read(mapname))

    sv = Server(pr, bsp=bsp, mapname=mapname, skill=1)
    stats = sv.load_level()
    print(f"{mapname}: {stats}")

    # tally what spawned, by classname
    from collections import Counter
    cls = Counter()
    fcn = sv.f["classname"]
    for num in range(1, sv.vm.num_edicts):
        if sv.vm.free[num]:
            continue
        cls[pr.string(sv.vm.fget_i(num, fcn))] += 1
    print(f"\nlive edicts by classname ({sum(cls.values())} total):")
    for name, n in cls.most_common():
        print(f"  {n:3} {name}")

    # run a couple seconds of think frames; make sure nothing explodes
    print("\nrunning 20 think frames (2.0s)...")
    for _ in range(20):
        sv.run_frame(0.1)
    print(f"  ok, time={sv.time:.1f}s, num_edicts={sv.vm.num_edicts}")
