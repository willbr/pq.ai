"""Quake player physics: clip-hull tracing + walkmove. Pure stdlib.

Ported from WinQuake world.c (SV_RecursiveHullCheck, SV_HullPointContents) and
sv_phys.c / sv_user.c (ClipVelocity, SV_FlyMove, SV_WalkMove, friction, accel).

The clip hull (hull 1) was pre-expanded by the player's bounding box when the map
was compiled, so we trace the player *origin point* through it — no box math, and
the offset is zero against the world model.
"""

import math

# clipnode contents
CONTENTS_SOLID = -2

# physics constants (Quake cvar defaults)
GRAVITY = 800.0
FRICTION = 4.0
STOPSPEED = 100.0
MAXSPEED = 320.0
ACCELERATE = 10.0
AIRACCEL_CAP = 30.0     # SV_AirAccelerate caps wishspeed to 30
STEPSIZE = 18.0
JUMPSPEED = 270.0
DIST_EPSILON = 0.03125
STOP_EPSILON = 0.1
VIEW_HEIGHT = 22.0      # eye above the player origin


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
        Returns (blocked_mask, onground)."""
        blocked = 0
        onground = False
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
                return blocked, onground

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

        return blocked, onground

    def walk_move(self, origin, vel, dt, oldonground):
        """SV_WalkMove: slide-move, and if blocked by a step, try to climb it."""
        oldorg = list(origin)
        oldvel = list(vel)

        clip, onground = self.fly_move(origin, vel, dt)

        if not (clip & 2):
            return onground            # didn't block on a step wall
        if not oldonground:
            return onground            # don't stair-step while airborne

        nosteporg = list(origin)
        nostepvel = list(vel)

        # retry from the start, stepped up
        origin[:] = oldorg
        self.push(origin, [0.0, 0.0, STEPSIZE])
        vel[0], vel[1], vel[2] = oldvel[0], oldvel[1], 0.0
        self.fly_move(origin, vel, dt)

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
    def friction(self, vel, dt):
        speed = math.sqrt(vel[0] * vel[0] + vel[1] * vel[1])
        if speed < 0.01:
            return
        control = STOPSPEED if speed < STOPSPEED else speed
        newspeed = speed - dt * control * FRICTION
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

    def player_move(self, origin, vel, wishdir, wishspeed, onground, want_jump, dt):
        """One step of walking physics. Mutates origin and vel; returns onground."""
        self.touched.clear()
        if onground:
            self.friction(vel, dt)
            self.accelerate(vel, wishdir, wishspeed, dt)
        else:
            self.air_accelerate(vel, wishdir, wishspeed, dt)

        if want_jump and onground:
            vel[2] = JUMPSPEED
            onground = False

        vel[2] -= GRAVITY * dt
        return self.walk_move(origin, vel, dt, onground)
