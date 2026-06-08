"""Quake player physics: clip-hull tracing + walkmove. Pure stdlib.

Ported from WinQuake world.c (SV_RecursiveHullCheck, SV_HullPointContents) and
sv_phys.c / sv_user.c (ClipVelocity, SV_FlyMove, SV_WalkMove, friction, accel).

The clip hull (hull 1) was pre-expanded by the player's bounding box when the map
was compiled, so we trace the player *origin point* through it — no box math, and
the offset is zero against the world model.
"""

import math

# clipnode / leaf contents
CONTENTS_SOLID = -2
CONTENTS_WATER = -3     # water/slime/lava are <= this (slime -4, lava -5, sky -6)

# physics constants (Quake cvar defaults)
GRAVITY = 800.0
FRICTION = 4.0
EDGEFRICTION = 2.0      # sv_edgefriction: friction multiplier when over a dropoff
STOPSPEED = 100.0
MAXSPEED = 320.0
ACCELERATE = 10.0
AIRACCEL_CAP = 30.0     # SV_AirAccelerate caps wishspeed to 30
STEPSIZE = 18.0
JUMPSPEED = 270.0
DIST_EPSILON = 0.03125
STOP_EPSILON = 0.1
VIEW_HEIGHT = 22.0      # eye above the player origin (view_ofs[2])
PLAYER_MINS_Z = -24.0   # player bounding box bottom, origin-relative
PLAYER_MAXS_Z = 32.0    # player bounding box top, origin-relative


class Trace:
    __slots__ = ("allsolid", "startsolid", "fraction", "endpos", "plane_normal",
                 "ent")

    def __init__(self, end):
        self.allsolid = True
        self.startsolid = False
        self.fraction = 1.0
        self.endpos = list(end)
        self.plane_normal = None
        self.ent = None             # brush entity that produced the impact (SV_Impact)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


CONTENTS_EMPTY = -1


class Physics:
    def __init__(self, bsp):
        self.planes = bsp.planes
        self.clipnodes = bsp.clipnodes
        self.headnode = bsp.models[0]["headnodes"][1]   # hull 1 (player size)
        # hull 0 is the visual BSP itself (a point hull) -- used for hitscan,
        # where the "player box" expansion of hull 1 would stop bullets short.
        self.nodes = bsp.nodes
        self.leafs = bsp.leafs
        self.headnode0 = bsp.models[0]["headnode"]
        # solid brush-model entities (doors, func_walls, gates) to clip against,
        # as (hull-1 headnode, origin, edict). Refreshed each frame by the host;
        # their submodel hulls were compiled at the closed position, so we trace in
        # the entity's local space (start/end minus its current origin).
        self.brush_entities = []
        # edicts the player's move bumped this step (SV_Impact). Drained by the
        # host into the QC touch functions so walking into a button presses it.
        self.touched = set()

    # ---- hull queries ----
    def hull_point_contents(self, num, p):
        clipnodes = self.clipnodes
        planes = self.planes
        while num >= 0:
            planenum, children = clipnodes[num]
            n, dist, ptype = planes[planenum]
            d = (p[ptype] - dist) if ptype < 3 else (_dot(n, p) - dist)
            num = children[1] if d < 0 else children[0]
        return num

    def point_contents(self, p):
        return self.hull_point_contents(self.headnode, p)

    def _recurse(self, num, p1f, p2f, p1, p2, tr, top):
        if num < 0:
            if num != CONTENTS_SOLID:
                tr.allsolid = False
            else:
                tr.startsolid = True
            return True

        planenum, children = self.clipnodes[num]
        n, dist, ptype = self.planes[planenum]
        if ptype < 3:
            t1 = p1[ptype] - dist
            t2 = p2[ptype] - dist
        else:
            t1 = _dot(n, p1) - dist
            t2 = _dot(n, p2) - dist

        if t1 >= 0 and t2 >= 0:
            return self._recurse(children[0], p1f, p2f, p1, p2, tr, top)
        if t1 < 0 and t2 < 0:
            return self._recurse(children[1], p1f, p2f, p1, p2, tr, top)

        # the line crosses the plane; split at the crosspoint (nudged to near side)
        if t1 < 0:
            frac = (t1 + DIST_EPSILON) / (t1 - t2)
        else:
            frac = (t1 - DIST_EPSILON) / (t1 - t2)
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)

        midf = p1f + (p2f - p1f) * frac
        mid = [p1[i] + frac * (p2[i] - p1[i]) for i in range(3)]
        side = 1 if t1 < 0 else 0

        if not self._recurse(children[side], p1f, midf, p1, mid, tr, top):
            return False

        if self.hull_point_contents(children[side ^ 1], mid) != CONTENTS_SOLID:
            return self._recurse(children[side ^ 1], midf, p2f, mid, p2, tr, top)

        if tr.allsolid:
            return False        # never got out of the solid area

        # impact: the far side is solid
        if side == 0:
            tr.plane_normal = n
        else:
            tr.plane_normal = (-n[0], -n[1], -n[2])

        # back the midpoint out of any residual solid (float imprecision)
        while self.hull_point_contents(top, mid) == CONTENTS_SOLID:
            frac -= 0.1
            if frac < 0:
                tr.fraction = midf
                tr.endpos = mid
                return False
            midf = p1f + (p2f - p1f) * frac
            mid = [p1[i] + frac * (p2[i] - p1[i]) for i in range(3)]

        tr.fraction = midf
        tr.endpos = mid
        return False

    def trace(self, start, end):
        tr = Trace(end)
        self._recurse(self.headnode, 0.0, 1.0, start, end, tr, self.headnode)
        return tr

    def trace_hull(self, headnode, start, end):
        """Trace start->end through an arbitrary clip hull (a brush submodel)."""
        tr = Trace(end)
        self._recurse(headnode, 0.0, 1.0, start, end, tr, headnode)
        return tr

    def set_brush_entities(self, ents):
        """ents: list of (hull-1 headnode, origin) for solid brush models."""
        self.brush_entities = ents

    def move(self, start, end):
        """SV_Move: trace start->end against the world and every solid brush
        entity, returning the earliest impact. This is what makes func_walls,
        doors and gates block the player."""
        tr = self.trace(list(start), list(end))
        if not self.brush_entities:
            return tr
        for headnode, org, ent in self.brush_entities:
            ls = [start[i] - org[i] for i in range(3)]
            le = [end[i] - org[i] for i in range(3)]
            t2 = self.trace_hull(headnode, ls, le)
            if t2.startsolid:
                tr.startsolid = True
                self.touched.add(ent)       # already overlapping it
            if t2.allsolid:
                tr.allsolid = True
            if t2.fraction < tr.fraction:
                tr.fraction = t2.fraction
                tr.endpos = [t2.endpos[i] + org[i] for i in range(3)]
                tr.plane_normal = t2.plane_normal
                tr.ent = ent                # this entity blocked the move
        if tr.ent is not None:
            self.touched.add(tr.ent)        # SV_Impact: bumped while moving
        return tr

    # ---- hull 0 (point) trace for hitscan ----
    def _node_contents0(self, num, p):
        nodes = self.nodes
        planes = self.planes
        while num >= 0:
            planenum, children, _, _ = nodes[num]
            n, dist, ptype = planes[planenum]
            d = (p[ptype] - dist) if ptype < 3 else (_dot(n, p) - dist)
            num = children[0] if d >= 0 else children[1]
        return self.leafs[-num - 1][0]

    def _recurse0(self, num, p1f, p2f, p1, p2, tr):
        if num < 0:
            if self.leafs[-num - 1][0] != CONTENTS_SOLID:
                tr.allsolid = False
            else:
                tr.startsolid = True
            return True

        planenum, children, _, _ = self.nodes[num]
        n, dist, ptype = self.planes[planenum]
        if ptype < 3:
            t1 = p1[ptype] - dist
            t2 = p2[ptype] - dist
        else:
            t1 = _dot(n, p1) - dist
            t2 = _dot(n, p2) - dist

        if t1 >= 0 and t2 >= 0:
            return self._recurse0(children[0], p1f, p2f, p1, p2, tr)
        if t1 < 0 and t2 < 0:
            return self._recurse0(children[1], p1f, p2f, p1, p2, tr)

        if t1 < 0:
            frac = (t1 + DIST_EPSILON) / (t1 - t2)
        else:
            frac = (t1 - DIST_EPSILON) / (t1 - t2)
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
        midf = p1f + (p2f - p1f) * frac
        mid = [p1[i] + frac * (p2[i] - p1[i]) for i in range(3)]
        side = 1 if t1 < 0 else 0

        if not self._recurse0(children[side], p1f, midf, p1, mid, tr):
            return False
        if self._node_contents0(children[side ^ 1], mid) != CONTENTS_SOLID:
            return self._recurse0(children[side ^ 1], midf, p2f, mid, p2, tr)
        if tr.allsolid:
            return False
        tr.plane_normal = n if side == 0 else (-n[0], -n[1], -n[2])
        tr.fraction = midf
        tr.endpos = mid
        return False

    def trace_point(self, start, end):
        """Trace a point (bullet) through hull 0. Returns a Trace."""
        tr = Trace(end)
        self._recurse0(self.headnode0, 0.0, 1.0, list(start), list(end), tr)
        return tr

    def trace_hull0(self, headnode, start, end):
        """Trace a point (bullet) through an arbitrary hull-0 submodel -- a solid
        brush model's visual BSP nodes. Used for hitscan against doors and
        func_walls; callers pass start/end in the entity's local space (minus its
        current origin), since the submodel hull was compiled at the closed pose."""
        tr = Trace(end)
        self._recurse0(headnode, 0.0, 1.0, list(start), list(end), tr)
        return tr

    def push(self, origin, push):
        """Move origin by push vector with collision; return the trace."""
        end = [origin[i] + push[i] for i in range(3)]
        tr = self.move(origin, end)
        origin[:] = tr.endpos
        return tr

    # ---- velocity helpers ----
    @staticmethod
    def clip_velocity(v, normal, overbounce):
        backoff = _dot(v, normal) * overbounce
        out = [0.0, 0.0, 0.0]
        for i in range(3):
            o = v[i] - normal[i] * backoff
            if -STOP_EPSILON < o < STOP_EPSILON:
                o = 0.0
            out[i] = o
        return out

    def fly_move(self, origin, vel, dt):
        """Slide-move origin along vel for dt, clipping against up to 4 planes.
        Returns (blocked_mask, onground, step_wall_normal)."""
        blocked = 0
        onground = False
        stepnormal = None
        original = list(vel)
        primal = list(vel)
        planes = []
        time_left = dt

        for _ in range(4):
            if not (vel[0] or vel[1] or vel[2]):
                break
            end = [origin[i] + time_left * vel[i] for i in range(3)]
            tr = self.move(origin, end)

            if tr.allsolid:
                vel[:] = (0.0, 0.0, 0.0)
                return blocked, onground, stepnormal

            if tr.fraction > 0:
                origin[:] = tr.endpos
                original = list(vel)
                planes = []

            if tr.fraction == 1.0:
                break

            n = tr.plane_normal
            if n[2] > 0.7:
                blocked |= 1
                onground = True
            if n[2] == 0:
                blocked |= 2
                stepnormal = n          # vertical wall: saved for wall friction

            time_left -= time_left * tr.fraction
            planes.append(n)

            # find a velocity that parallels all clip planes
            new_vel = None
            for i in range(len(planes)):
                cand = self.clip_velocity(original, planes[i], 1.0)
                if all(j == i or _dot(cand, planes[j]) >= 0
                       for j in range(len(planes))):
                    new_vel = cand
                    break

            if new_vel is not None:
                vel[:] = new_vel
            else:
                # slide along the crease of two planes
                if len(planes) != 2:
                    vel[:] = (0.0, 0.0, 0.0)
                    break
                a, b = planes
                dir_ = (a[1] * b[2] - a[2] * b[1],
                        a[2] * b[0] - a[0] * b[2],
                        a[0] * b[1] - a[1] * b[0])
                d = _dot(dir_, vel)
                vel[:] = (dir_[0] * d, dir_[1] * d, dir_[2] * d)

            if _dot(vel, primal) <= 0:
                vel[:] = (0.0, 0.0, 0.0)
                break

        return blocked, onground, stepnormal

    def wall_friction(self, vel, forward, normal):
        """SV_WallFriction: bleed off velocity tangential to a wall, scaled by how
        head-on the view faces it (a full cut when looking straight at it). Leaves
        the vertical component alone."""
        d = _dot(normal, forward) + 0.5
        if d >= 0:
            return
        i = _dot(normal, vel)
        side = [vel[k] - normal[k] * i for k in range(3)]
        vel[0] = side[0] * (1.0 + d)
        vel[1] = side[1] * (1.0 + d)

    def try_unstick(self, origin, vel, oldvel):
        """SV_TryUnstick: the step-up wedged us on a BSP hull seam (float
        precision). Shove a couple units in each axial direction and retry the
        move; keep the first nudge that frees us. Returns (clip, step_normal)."""
        start = list(origin)
        for dx, dy in ((2.0, 0.0), (0.0, 2.0), (-2.0, 0.0), (0.0, -2.0),
                       (2.0, 2.0), (-2.0, 2.0), (2.0, -2.0), (-2.0, -2.0)):
            self.push(origin, [dx, dy, 0.0])
            vel[0], vel[1], vel[2] = oldvel[0], oldvel[1], 0.0
            clip, _, stepnormal = self.fly_move(origin, vel, 0.1)
            if abs(start[0] - origin[0]) > 4.0 or abs(start[1] - origin[1]) > 4.0:
                return clip, stepnormal
            origin[:] = start           # didn't help; undo and try the next nudge
        vel[:] = (0.0, 0.0, 0.0)
        return 7, None

    def walk_move(self, origin, vel, forward, dt, oldonground, waterlevel):
        """SV_WalkMove: slide-move, and if blocked by a step, try to climb it."""
        oldorg = list(origin)
        oldvel = list(vel)

        clip, onground, _ = self.fly_move(origin, vel, dt)

        if not (clip & 2):
            return onground            # didn't block on a step wall
        if not oldonground and waterlevel == 0:
            return onground            # don't stair-step while airborne (ok in water)

        nosteporg = list(origin)
        nostepvel = list(vel)

        # retry from the start, stepped up
        origin[:] = oldorg
        self.push(origin, [0.0, 0.0, STEPSIZE])
        vel[0], vel[1], vel[2] = oldvel[0], oldvel[1], 0.0
        clip, _, stepnormal = self.fly_move(origin, vel, dt)

        # if stepping up made no horizontal progress we're wedged on a hull seam;
        # nudge in 8 axial directions to escape
        if clip and abs(oldorg[0] - origin[0]) < DIST_EPSILON \
                and abs(oldorg[1] - origin[1]) < DIST_EPSILON:
            clip, stepnormal = self.try_unstick(origin, vel, oldvel)

        # extra friction when still shoving into a wall, scaled by view angle
        if (clip & 2) and stepnormal is not None:
            self.wall_friction(vel, forward, stepnormal)

        # step back down
        down = self.push(origin, [0.0, 0.0, -STEPSIZE + oldvel[2] * dt])
        if down.plane_normal and down.plane_normal[2] > 0.7:
            onground = True
        else:
            # didn't land on good ground — keep the no-step result
            origin[:] = nosteporg
            vel[:] = nostepvel

        return onground

    # ---- player accel / friction ----
    def friction(self, origin, vel, dt):
        speed = math.sqrt(vel[0] * vel[0] + vel[1] * vel[1])
        if speed < 0.01:
            return
        # if the leading edge is over a dropoff, increase friction: trace a point
        # 16 units ahead at foot height, 34 down. Nothing hit -> over a ledge ->
        # apply sv_edgefriction (2x). A point move traces hull 0 (the world).
        start = [origin[0] + vel[0] / speed * 16.0,
                 origin[1] + vel[1] / speed * 16.0,
                 origin[2] + PLAYER_MINS_Z]
        stop = [start[0], start[1], start[2] - 34.0]
        friction = FRICTION
        if self.trace_point(start, stop).fraction == 1.0:
            friction *= EDGEFRICTION
        control = STOPSPEED if speed < STOPSPEED else speed
        newspeed = speed - dt * control * friction
        if newspeed < 0:
            newspeed = 0.0
        newspeed /= speed
        vel[0] *= newspeed
        vel[1] *= newspeed
        vel[2] *= newspeed

    def accelerate(self, vel, wishdir, wishspeed, dt):
        currentspeed = _dot(vel, wishdir)
        addspeed = wishspeed - currentspeed
        if addspeed <= 0:
            return
        accelspeed = min(ACCELERATE * dt * wishspeed, addspeed)
        for i in range(3):
            vel[i] += accelspeed * wishdir[i]

    def air_accelerate(self, vel, wishdir, wishspeed, dt):
        wishspd = min(wishspeed, AIRACCEL_CAP)
        currentspeed = _dot(vel, wishdir)
        addspeed = wishspd - currentspeed
        if addspeed <= 0:
            return
        accelspeed = min(ACCELERATE * wishspeed * dt, addspeed)
        for i in range(3):
            vel[i] += accelspeed * wishdir[i]

    # ---- water ----
    def point_contents_0(self, p):
        """Hull-0 (visual BSP) leaf contents at p. Unlike the clip hulls this
        encodes water/slime/lava. SV_PointContents."""
        return self._node_contents0(self.headnode0, p)

    def check_water(self, origin):
        """SV_CheckWater: sample contents at feet / waist / eyes. Returns
        (waterlevel 0..3, watertype)."""
        p = [origin[0], origin[1], origin[2] + PLAYER_MINS_Z + 1.0]
        watertype = CONTENTS_EMPTY
        waterlevel = 0
        if self.point_contents_0(p) <= CONTENTS_WATER:
            watertype = self.point_contents_0(p)
            waterlevel = 1
            p[2] = origin[2] + (PLAYER_MINS_Z + PLAYER_MAXS_Z) * 0.5
            if self.point_contents_0(p) <= CONTENTS_WATER:
                waterlevel = 2
                p[2] = origin[2] + VIEW_HEIGHT
                if self.point_contents_0(p) <= CONTENTS_WATER:
                    waterlevel = 3
        return waterlevel, watertype

    def water_move(self, vel, wishvel, maxspeed, dt):
        """SV_WaterMove: 3D swim acceleration with water friction. Sets velocity
        only -- the actual move happens in walk_move via fly_move."""
        wishspeed = math.sqrt(_dot(wishvel, wishvel))
        if wishspeed > maxspeed:
            f = maxspeed / wishspeed
            wishvel = [wishvel[i] * f for i in range(3)]
            wishspeed = maxspeed
        wishspeed *= 0.7

        # water friction: 3D speed, no stopspeed floor and no edge friction
        speed = math.sqrt(_dot(vel, vel))
        if speed:
            newspeed = speed - dt * speed * FRICTION
            if newspeed < 0:
                newspeed = 0.0
            f = newspeed / speed
            vel[0] *= f
            vel[1] *= f
            vel[2] *= f
        else:
            newspeed = 0.0

        # water acceleration toward the (already 0.7-scaled) wish velocity
        if not wishspeed:
            return
        addspeed = wishspeed - newspeed
        if addspeed <= 0:
            return
        wl = math.sqrt(_dot(wishvel, wishvel))
        if wl == 0:
            return
        accelspeed = min(ACCELERATE * wishspeed * dt, addspeed)
        for i in range(3):
            vel[i] += accelspeed * wishvel[i] / wl

    def player_move(self, origin, vel, wishdir, wishspeed,
                    forward, right, fmove, smove, upmove, maxspeed,
                    onground, want_jump, dt):
        """One step of player physics. Mutates origin and vel; returns
        (onground, waterlevel). wishdir/wishspeed are the horizontal ground/air
        intent; forward/right + fmove/smove/upmove are the full 3D swim intent and
        forward also drives wall friction."""
        self.touched.clear()
        waterlevel, _ = self.check_water(origin)

        if waterlevel >= 2:
            # swimming: build a full 3D wish velocity from the view direction
            wishvel = [forward[i] * fmove + right[i] * smove for i in range(3)]
            if fmove == 0.0 and smove == 0.0 and upmove == 0.0:
                wishvel[2] -= 60.0          # idle: drift slowly toward the bottom
            else:
                wishvel[2] += upmove
            self.water_move(vel, wishvel, maxspeed, dt)
        elif onground:
            self.friction(origin, vel, dt)
            self.accelerate(vel, wishdir, wishspeed, dt)
        else:
            self.air_accelerate(vel, wishdir, wishspeed, dt)

        if want_jump and onground:
            vel[2] = JUMPSPEED
            onground = False

        if waterlevel <= 1:                 # no gravity while swimming
            vel[2] -= GRAVITY * dt

        onground = self.walk_move(origin, vel, forward, dt, onground, waterlevel)
        return onground, waterlevel
