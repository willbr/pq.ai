"""QuakeC server layer: builtins, entity spawning, and the think frame loop.

This is the SV side -- it owns a VM, installs the ~65 builtin functions the game
logic calls, parses the BSP entity string into edicts (ED_LoadFromFile), and runs
each entity's think function when its nextthink comes due (SV_RunThink).

Physics-dependent builtins (traceline, walkmove, droptofloor, movetogoal, ...) are
stubbed with safe "clear path / stay put" results for now -- enough to spawn the
whole level and let monsters/items animate in place via OP_STATE frame chains.
Wiring them to physics.py is the next step.
"""

import math
import random

from pr_exec import VM, PR_RunError

# spawnflags for skill / deathmatch inhibition (defs.qc)
SPAWNFLAG_NOT_EASY = 256
SPAWNFLAG_NOT_MEDIUM = 512
SPAWNFLAG_NOT_HARD = 1024
SPAWNFLAG_NOT_DEATHMATCH = 2048

FL_ONGROUND = 512
SOLID_BSP = 4
MOVETYPE_NONE = 0
MOVETYPE_FLY = 5
MOVETYPE_TOSS = 6
MOVETYPE_PUSH = 7
MOVETYPE_NOCLIP = 8
MOVETYPE_FLYMISSILE = 9
MOVETYPE_BOUNCE = 10
SV_GRAVITY = 800.0          # sv_gravity default, scaled per-entity by .gravity
CONTENTS_EMPTY = -1

SVC_TEMPENTITY = 23         # broadcast effect message (gunshots, teleport fog, ...)
# temp-entity type -> (palette colour, particle count). Point effects only; beam
# types (lightning) just spark once at their start point, which is harmless.
_TE_EFFECT = {
    0: (0, 6), 1: (0, 8), 2: (0, 6),        # spike, superspike, gunshot (grey)
    3: (75, 24), 4: (75, 24),               # explosion, tarexplosion (orange)
    7: (60, 10), 8: (0, 8),                 # wizspike (green), knightspike
    10: (244, 32), 11: (244, 30),           # lavasplash, teleport (white fog)
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
           "button0", "deadflag", "enemy", "owner", "touch")

FL_CLIENT = 8
SOLID_NOT = 0
SOLID_TRIGGER = 1
SOLID_SLIDEBOX = 3
MOVETYPE_WALK = 3
DAMAGE_AIM = 2

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
# every weapon + every ammo-type bit: a full single-player arsenal
IT_ALL_WEAPONS = (IT_AXE | IT_SHOTGUN | IT_SUPER_SHOTGUN | IT_NAILGUN |
                  IT_SUPER_NAILGUN | IT_GRENADE_LAUNCHER | IT_ROCKET_LAUNCHER |
                  IT_LIGHTNING)
IT_ALL_AMMO = IT_SHELLS | IT_NAILS | IT_ROCKETS | IT_CELLS
_WEAPON_NAMES = {
    IT_AXE: "Axe", IT_SHOTGUN: "Shotgun", IT_SUPER_SHOTGUN: "Super Shotgun",
    IT_NAILGUN: "Nailgun", IT_SUPER_NAILGUN: "Super Nailgun",
    IT_GRENADE_LAUNCHER: "Grenade Launcher", IT_ROCKET_LAUNCHER: "Rocket Launcher",
    IT_LIGHTNING: "Lightning Gun",
}

# system globals we read/write
_GLOBALS = ("self", "other", "time", "frametime", "force_retouch", "skill",
            "v_forward", "v_right", "v_up", "msg_entity", "mapname",
            "trace_allsolid", "trace_startsolid", "trace_fraction", "trace_endpos",
            "trace_plane_normal", "trace_plane_dist", "trace_ent",
            "trace_inopen", "trace_inwater")


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
                 physics=None):
        self.pr = progs
        self.vm = VM(progs, max_edicts=max_edicts)
        self.bsp = bsp
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
        self.center_msg = None      # (text, time) from centerprint; host displays it
        self.particles = []         # live point sprites: [x,y,z, vx,vy,vz, color, die]
        self._te = None             # in-progress temp-entity message being parsed

        self.time = 0.0
        self.frametime = 0.1
        self.developer = False
        self.model_precache = [""]
        self.sound_precache = [""]
        self.lightstyles = {}
        self.cvars = {"skill": float(skill), "deathmatch": 0.0, "coop": 0.0,
                      "teamplay": 0.0, "temp1": 0.0, "noexit": 0.0, "samelevel": 0.0}

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

    # ================================================================
    # level load: build edicts from the entity string and spawn them
    # ================================================================
    def load_level(self):
        vm = self.vm
        # model precache: 0 empty, 1 worldmodel, then inline brush models *1.. *N
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
        self.player_carry = [0.0, 0.0, 0.0]   # reset rider carry for this frame
        self.gset_f("frametime", dt)
        self.gset_f("time", self.time)

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
            if mt == MOVETYPE_PUSH:
                self._push_move(num, dt)
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
        self.run_weapon_frame()                 # PlayerPostThink: drive the weapons
        self.touch_triggers(self.player)        # fire teleports/triggers we touch
        self._advance_particles(dt)
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

        vx, vy, vz = vm.fget_v(num, fvel)
        if mt in _MOVE_GRAVITY:
            gs = (fgrav is not None and vm.fget_f(num, fgrav)) or 1.0
            vz -= SV_GRAVITY * gs * dt
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
        frac, endpos, pnorm, _allsolid, _startsolid, hit = \
            self._move_trace((ox, oy, oz), end, 0, num)
        vm.fset_v(num, forg, endpos)
        self._link_abs(num)
        if frac >= 1.0:
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

    def _push_move(self, num, dt):
        """SV_PushMove: advance a brush mover (door/plat/button) by velocity*dt,
        keep its abs bounds current, and carry the player if they ride it. Unlike
        a free projectile a pusher does not stop on contact -- blocking/crushing
        is left to the QC, which the player riding it never triggers."""
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
        ox, oy, oz = vm.fget_v(num, forg)
        vm.fset_v(num, forg, (ox + move[0], oy + move[1], oz + move[2]))
        self._link_abs(num)                   # bounds must track the moved origin
        self._carry_player(num, move)

    def _player_rides(self, pusher):
        """True if the player's box overlaps the pusher's (post-move) box, so a
        rising lift carries them up instead of clipping through their feet. The
        1-unit slop makes simply resting on top count as contact."""
        vm, f = self.vm, self.f
        p = self.player
        amn, amx = f["absmin"], f["absmax"]
        pmn = vm.fget_v(p, amn); pmx = vm.fget_v(p, amx)
        umn = vm.fget_v(pusher, amn); umx = vm.fget_v(pusher, amx)
        E = 1.0
        return not (pmn[0] - E > umx[0] or pmx[0] + E < umn[0] or
                    pmn[1] - E > umx[1] or pmx[1] + E < umn[1] or
                    pmn[2] - E > umx[2] or pmx[2] + E < umn[2])

    def _carry_player(self, pusher, move):
        """Move the player edict by a pusher's delta when they ride it, and record
        the carry so the host can apply the same shift to the camera it owns."""
        p = self.player
        if not p or self.vm.free[p] or not self._player_rides(pusher):
            return
        vm, f = self.vm, self.f
        ox, oy, oz = vm.fget_v(p, f["origin"])
        vm.fset_v(p, f["origin"], (ox + move[0], oy + move[1], oz + move[2]))
        self._link_abs(p)
        self.player_carry[0] += move[0]
        self.player_carry[1] += move[1]
        self.player_carry[2] += move[2]

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
        """Live brush-model entities as (submodel_index, origin, angles), for the
        renderer. Skips entities whose modelindex isn't an inline '*N' model --
        notably triggers, which QC makes invisible by clearing modelindex."""
        vm = self.vm
        mp = self.model_precache
        fmi, forg, fang = self.f["modelindex"], self.f["origin"], self.f["angles"]
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            mi = vm.fget_i(num, fmi)
            if 0 < mi < len(mp) and mp[mi][:1] == "*":
                out.append((int(mp[mi][1:]), vm.fget_v(num, forg), vm.fget_v(num, fang)))
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
        fmi, forg, fang, ffr = (self.f["modelindex"], self.f["origin"],
                                self.f["angles"], self.f["frame"])
        out = []
        for num in range(1, vm.num_edicts):
            if vm.free[num]:
                continue
            mi = vm.fget_i(num, fmi)
            if 0 < mi < len(mp) and mp[mi][-4:] == ".mdl":
                out.append((mi, vm.fget_v(num, forg), vm.fget_v(num, fang),
                            int(vm.fget_f(num, ffr))))
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
            self._pf_precache_sound, self._pf_precache_model, self._pf_noop,  # stuffcmd
            self._pf_findradius, self._pf_bprint, self._pf_noop2, self._pf_dprint,  # sprint
            self._pf_ftos, self._pf_vtos, self._pf_noop, self._pf_traceon,
            self._pf_traceoff, self._pf_noop, self._pf_walkmove, fixme,        # 32,33
            self._pf_droptofloor, self._pf_lightstyle, self._pf_rint,
            self._pf_floor, self._pf_ceil, fixme, self._pf_checkbottom,        # 39,40
            self._pf_pointcontents, fixme, self._pf_fabs, self._pf_aim,        # 42..
            self._pf_cvar, self._pf_noop, self._pf_nextent, self._pf_particle,  # localcmd, particle
            self._pf_changeyaw, fixme, self._pf_vectoangles,                   # 49,50,51
            self._pf_writebyte, self._pf_noop2, self._pf_noop2, self._pf_noop2,  # WriteByte/Char/Short/Long
            self._pf_writecoord, self._pf_noop2, self._pf_noop2, self._pf_noop2,  # WriteCoord/Angle/String/Entity
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
        """Maintain absmin/absmax = origin + mins/maxs (SV_LinkEdict's job)."""
        ox, oy, oz = self.vm.fget_v(e, self.f["origin"])
        mnx, mny, mnz = self.vm.fget_v(e, self.f["mins"])
        mxx, mxy, mxz = self.vm.fget_v(e, self.f["maxs"])
        self.vm.fset_v(e, self.f["absmin"], (ox + mnx, oy + mny, oz + mnz))
        self.vm.fset_v(e, self.f["absmax"], (ox + mxx, oy + mxy, oz + mxz))

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
        else:
            self._set_minmax(e, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))

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

    # --- misc no-ops with side data ---
    def _pf_sound(self):
        pass

    def _pf_ambientsound(self):
        pass

    def _pf_lightstyle(self):
        self.lightstyles[int(self.vm.parm_f(0))] = self.vm.parm_str(1)

    def _pf_makestatic(self):
        pass            # keep the edict alive so it still renders + animates

    def _pf_changelevel(self):
        self.changelevel = self.vm.parm_str(0)

    def _pf_centerprint(self):
        # centerprint(client, string): keep only the player's message; the host
        # draws it centred on screen for a few seconds.
        if self.vm.parm_i(0) == self.player:
            msg = self.vm.parm_str(1)
            if msg:
                self.center_msg = (msg, self.time)

    def _burst(self, org, vel, color, count):
        """Spawn `count` point sprites at org with a velocity + random spread."""
        count = max(1, min(count, 24))
        die = self.time + 0.6
        for i in range(count):
            self.particles.append([
                org[0], org[1], org[2],
                vel[0] + (random.random() - 0.5) * 40,
                vel[1] + (random.random() - 0.5) * 40,
                vel[2] + (random.random() - 0.5) * 40,
                (color + i) & 255, die])
        if len(self.particles) > 400:                # bound the list
            del self.particles[:len(self.particles) - 400]

    def _pf_particle(self):
        # particle(origin, dir, color, count): spawn point sprites the host draws
        vm = self.vm
        self._burst(vm.parm_v(0), vm.parm_v(1), int(vm.parm_f(2)),
                    int(vm.parm_f(3)))

    def _pf_writebyte(self):
        # decode broadcast temp-entity messages (gunshots, teleport fog, ...) into
        # particle bursts: WriteByte(SVC_TEMPENTITY); WriteByte(type); 3x WriteCoord
        v = int(self.vm.parm_f(1))
        if self._te is None:
            if v == SVC_TEMPENTITY:
                self._te = [None, []]            # [type, collected coords]
        elif self._te[0] is None:
            self._te[0] = v                      # the temp-entity type
        # any further bytes (counts/colours) are ignored; coords come via WriteCoord

    def _pf_writecoord(self):
        if self._te is None or self._te[0] is None:
            return
        coords = self._te[1]
        coords.append(self.vm.parm_f(1))
        if len(coords) == 3:                     # have a full position -> spark it
            color, count = _TE_EFFECT.get(self._te[0], (0, 6))
            self._burst(coords, (0.0, 0.0, 0.0), color, count)
            self._te = None

    def _advance_particles(self, dt):
        if not self.particles:
            return
        t = self.time
        live = []
        for p in self.particles:
            if p[7] <= t:
                continue
            p[0] += p[3] * dt
            p[1] += p[4] * dt
            p[2] += p[5] * dt
            p[5] -= SV_GRAVITY * 0.05 * dt           # gentle droop
            live.append(p)
        self.particles = live

    def _pf_setspawnparms(self):
        pass

    def _pf_traceon(self):
        self.vm.trace = True

    def _pf_traceoff(self):
        self.vm.trace = False

    # --- debug / dump (cheap) ---
    def _pf_aim(self):
        self.vm.ret_v(*self.gget_v("v_forward"))

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
    def spawn_player(self, origin, angles):
        """Create a client edict so monsters have a target and shots have an
        attacker. The camera drives it each frame via update_player()."""
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
        # full arsenal: every weapon owned with ammo, so all of them are usable
        # and selectable (ImpulseCommands/W_ChangeWeapon gate on .items + ammo).
        vm.fset_f(e, f["items"], float(IT_ALL_WEAPONS | IT_ALL_AMMO))
        vm.fset_f(e, f["weapon"], float(IT_SHOTGUN))
        vm.fset_f(e, f["currentammo"], 100.0)
        vm.fset_f(e, f["ammo_shells"], 100.0)
        vm.fset_f(e, f["ammo_nails"], 200.0)
        vm.fset_f(e, f["ammo_rockets"], 100.0)
        vm.fset_f(e, f["ammo_cells"], 100.0)
        vm.fset_v(e, f["view_ofs"], (0.0, 0.0, 22.0))
        self._set_minmax(e, (-16.0, -16.0, -24.0), (16.0, 16.0, 32.0))
        # let the real QC pick the view model: W_SetCurrentAmmo sets .weaponmodel
        # ("progs/v_shot.mdl") and .weaponframe from .weapon, exactly as the game
        # does in PutClientInServer -- so the first-person weapon renders
        func = self.pr.find_function("W_SetCurrentAmmo")
        if func is not None:
            self.gset_f("time", self.time)
            self.gset_i("self", e)
            self.gset_i("other", 0)
            try:
                vm.execute(func)
            except PR_RunError as ex:
                print(f"W_SetCurrentAmmo aborted: {ex}")
        self.update_player(origin, angles)
        return e

    def player_health(self):
        if not self.player:
            return 0.0
        return self.vm.fget_f(self.player, self.f["health"])

    def player_origin(self):
        return self.vm.fget_v(self.player, self.f["origin"]) if self.player else None

    def player_angles(self):
        return self.vm.fget_v(self.player, self.f["angles"]) if self.player else None

    def player_velocity(self):
        return self.vm.fget_v(self.player, self.f["velocity"]) if self.player else None

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
        with health, armor, the current weapon + its ammo, and all four ammo
        counts -- everything the QC keeps on the client edict."""
        if not self.player:
            return None
        vm, f, e = self.vm, self.f, self.player
        g = lambda n: int(vm.fget_f(e, f[n]))
        return {
            "health": g("health"),
            "armor": g("armorvalue"),
            "weapon": _WEAPON_NAMES.get(g("weapon"), "?"),
            "ammo": g("currentammo"),
            "shells": g("ammo_shells"),
            "nails": g("ammo_nails"),
            "rockets": g("ammo_rockets"),
            "cells": g("ammo_cells"),
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

    def update_player(self, origin, angles):
        if not self.player:
            return
        vm, f, e = self.vm, self.f, self.player
        vm.fset_v(e, f["origin"], origin)
        vm.fset_v(e, f["angles"], angles)
        vm.fset_v(e, f["v_angle"], angles)
        self._link_abs(e)

    def set_input(self, button0, impulse=0):
        """Host -> server input for the player: attack-held state and a weapon
        select impulse. The impulse is queued and consumed by the next weapon
        frame so a single keypress switches once."""
        self.button0 = bool(button0)
        if impulse:
            self.pending_impulse = int(impulse)

    def run_weapon_frame(self):
        """One tick of the real Quake weapon system (what PlayerPostThink runs):
        W_WeaponFrame honours .attack_finished cadence, runs ImpulseCommands for
        weapon switching, and calls W_Attack -> W_Fire* when .button0 is held.
        This drives every weapon -- ammo, view-model animation and all -- from the
        game's own QC, instead of the engine hardcoding a single weapon."""
        if not self.player or self.phys is None:
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

    def _pf_pointcontents(self):
        self.vm.ret_f(CONTENTS_EMPTY)

    def _pf_walkmove(self):
        self.vm.ret_f(0.0)        # blocked: no real movement yet

    def _pf_droptofloor(self):
        # leave the entity at its placed origin, mark it grounded
        e = self.vm.gi[self.g["self"]]
        fl = int(self.vm.fget_f(e, self.f["flags"])) | FL_ONGROUND
        self.vm.fset_f(e, self.f["flags"], float(fl))
        self.vm.fset_i(e, self.f["groundentity"], 0)
        self.vm.ret_f(1.0)

    def _pf_checkbottom(self):
        self.vm.ret_f(1.0)

    def _pf_movetogoal(self):
        pass

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
    from pak import Pak
    from progs import Progs
    from bsp import Bsp

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
