"""The client half of the loopback: client_state_t (ClientState) plus the
CL_ParseServerMessage dispatch and per-message handlers (cl_parse.c). Builds a
client-side entity list from the server's protocol-15 datagram, deltas updates
against per-entity baselines, and (relink) interpolates positions and grows
client-side particle trails. The renderer reads this, not the server edicts.

The writer half is quake/sv_send.py; this parser is its exact mirror -- the
clientdata SU_* order, the always-sent tail, and the entity U_* field order must
stay in lockstep with that module."""
from . import protocol as P
from .msg import MsgReader  # noqa: F401  (callers pass readers; kept for typing)


class ClEntity:
    """One client-side entity (entity_t): a baseline, the last two message
    snapshots for interpolation, and the resolved render fields."""
    __slots__ = ("baseline", "model", "modelindex", "frame", "colormap",
                 "skin", "effects", "msgtime", "msg_origins", "msg_angles",
                 "origin", "angles", "forcelink")

    def __init__(self):
        from .sv_send import Baseline
        self.baseline = Baseline()
        self.model = None
        self.modelindex = 0
        self.frame = 0
        self.colormap = 0
        self.skin = 0
        self.effects = 0
        self.msgtime = -1.0
        self.msg_origins = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
        self.msg_angles = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
        self.origin = (0.0, 0.0, 0.0)
        self.angles = (0.0, 0.0, 0.0)
        self.forcelink = False


class ClientState:
    """client_state_t (client.h): everything the renderer reads. Populated by
    parse_message; positioned by relink."""

    MAX_EDICTS = 600

    def __init__(self):
        self.entities = [None] * self.MAX_EDICTS
        self.static_entities = []
        self.stats = [0] * 32
        self.items = 0
        self.lightstyles = {}
        self.model_precache = [""]
        self.sound_precache = [""]
        self.viewangles = [0.0, 0.0, 0.0]
        self.viewentity = 0
        self.view_height = P.DEFAULT_VIEWHEIGHT
        self.punchangle = [0.0, 0.0, 0.0]
        self.velocity = [0.0, 0.0, 0.0]
        self.mvelocity = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        self.mtime = [0.0, 0.0]
        self.time = 0.0
        self.onground = False
        self.inwater = False
        self.intermission = False
        self.levelname = ""
        self.center_msg = None
        self.particles = []          # client-side particle system (relink)

    def entity(self, num):
        e = self.entities[num]
        if e is None:
            e = ClEntity()
            self.entities[num] = e
        return e

    # ---- top-level dispatch (CL_ParseServerMessage, cl_parse.c:720) ----
    def parse_message(self, r):
        while True:
            if r.at_end:
                return
            cmd = r.byte()
            if cmd & 128:                         # fast entity update
                self.parse_update(cmd & 127, r)
                continue
            self._dispatch(cmd, r)

    def _dispatch(self, cmd, r):
        if cmd == P.svc_nop:
            return
        if cmd == P.svc_time:
            self.mtime[1] = self.mtime[0]
            self.mtime[0] = r.float()
        elif cmd == P.svc_clientdata:
            self.parse_clientdata(r)
        elif cmd == P.svc_serverinfo:
            self.parse_serverinfo(r)
        elif cmd == P.svc_setangle:
            self.viewangles = [r.angle(), r.angle(), r.angle()]
        elif cmd == P.svc_setview:
            self.viewentity = r.short()
        elif cmd == P.svc_spawnbaseline:
            num = r.short()
            self.parse_baseline(self.entity(num), r)
        elif cmd == P.svc_spawnstatic:
            e = ClEntity()
            self.parse_baseline(e, r)
            self.static_entities.append(e)
        elif cmd == P.svc_lightstyle:
            i = r.byte()
            self.lightstyles[i] = r.string()
        elif cmd == P.svc_signonnum:
            r.byte()                               # phase number -- noted, no-op
        elif cmd == P.svc_centerprint:
            self.center_msg = r.string()
        elif cmd == P.svc_intermission:
            # NQ protocol-15: svc_intermission carries NO payload. (Reading a
            # string here would consume the next message's bytes and desync.)
            self.intermission = True
        elif cmd == P.svc_finale:
            # svc_finale DOES carry the finale text string.
            self.intermission = True
            self.center_msg = r.string()
        elif cmd == P.svc_setpause:
            r.byte()
        elif cmd == P.svc_updatestat:
            i = r.byte()
            self.stats[i] = r.long()
        elif cmd == P.svc_particle:
            self.parse_particle(r)
        elif cmd == P.svc_sound:
            self.parse_sound(r)
        elif cmd == P.svc_temp_entity:
            self.parse_temp_entity(r)
        elif cmd == P.svc_cdtrack:
            r.byte(); r.byte()
        elif cmd in (P.svc_killedmonster, P.svc_foundsecret,
                     P.svc_sellscreen, P.svc_disconnect):
            return
        else:
            raise ValueError(f"unknown svc {cmd} at byte {r.pos}")

    # ---- handlers ----
    def parse_serverinfo(self, r):                 # cl_parse.c:204
        ver = r.long()
        if ver != P.PROTOCOL_VERSION:
            raise ValueError(f"demo/server protocol {ver}, expected 15")
        self.maxclients = r.byte()
        self.gametype = r.byte()
        self.levelname = r.string()
        self.model_precache = [""]
        while True:
            s = r.string()
            if not s:
                break
            self.model_precache.append(s)
        self.sound_precache = [""]
        while True:
            s = r.string()
            if not s:
                break
            self.sound_precache.append(s)

    def parse_baseline(self, e, r):                # cl_parse.c:491
        b = e.baseline
        b.modelindex = r.byte()
        b.frame = r.byte()
        b.colormap = r.byte()
        b.skin = r.byte()
        ox = []; ax = []
        for _ in range(3):
            ox.append(r.coord())
            ax.append(r.angle())
        b.origin = tuple(ox)
        b.angles = tuple(ax)
        e.modelindex = b.modelindex
        e.frame = b.frame
        e.model = (self.model_precache[e.modelindex]
                   if e.modelindex < len(self.model_precache) else None)
        e.msg_origins = [b.origin, b.origin]
        e.msg_angles = [b.angles, b.angles]

    def parse_update(self, bits, r):               # cl_parse.c:330
        if bits & P.U_MOREBITS:
            bits |= r.byte() << 8
        num = r.short() if (bits & P.U_LONGENTITY) else r.byte()
        e = self.entity(num)
        b = e.baseline
        e.forcelink = (e.msgtime != self.mtime[1])  # gap -> snap, no lerp
        e.msgtime = self.mtime[0]

        if bits & P.U_MODEL:
            e.modelindex = r.byte()
        else:
            e.modelindex = b.modelindex
        e.model = (self.model_precache[e.modelindex]
                   if e.modelindex < len(self.model_precache) else None)
        e.frame = r.byte() if (bits & P.U_FRAME) else b.frame
        e.colormap = r.byte() if (bits & P.U_COLORMAP) else b.colormap
        e.skin = r.byte() if (bits & P.U_SKIN) else b.skin
        e.effects = r.byte() if (bits & P.U_EFFECTS) else b.effects

        e.msg_origins[1] = e.msg_origins[0]
        e.msg_angles[1] = e.msg_angles[0]
        o0 = r.coord() if (bits & P.U_ORIGIN1) else b.origin[0]
        a0 = r.angle() if (bits & P.U_ANGLE1) else b.angles[0]
        o1 = r.coord() if (bits & P.U_ORIGIN2) else b.origin[1]
        a1 = r.angle() if (bits & P.U_ANGLE2) else b.angles[1]
        o2 = r.coord() if (bits & P.U_ORIGIN3) else b.origin[2]
        a2 = r.angle() if (bits & P.U_ANGLE3) else b.angles[2]
        e.msg_origins[0] = (o0, o1, o2)
        e.msg_angles[0] = (a0, a1, a2)
        if bits & P.U_NOLERP:
            e.forcelink = True

    def parse_clientdata(self, r):                 # cl_parse.c:514
        bits = r.short()
        self.view_height = (r.char() if (bits & P.SU_VIEWHEIGHT)
                            else P.DEFAULT_VIEWHEIGHT)
        if bits & P.SU_IDEALPITCH:
            r.char()
        self.mvelocity[1] = self.mvelocity[0][:]
        punch = [0.0, 0.0, 0.0]
        mvel = [0.0, 0.0, 0.0]
        for i in range(3):
            punch[i] = r.char() if (bits & (P.SU_PUNCH1 << i)) else 0.0
            mvel[i] = (r.char() * 16) if (bits & (P.SU_VELOCITY1 << i)) else 0.0
        self.punchangle = punch
        self.mvelocity[0] = mvel
        self.items = r.long()
        self.onground = bool(bits & P.SU_ONGROUND)
        self.inwater = bool(bits & P.SU_INWATER)
        if bits & P.SU_WEAPONFRAME:
            self.stats[P.STAT_WEAPONFRAME] = r.byte()
        else:
            self.stats[P.STAT_WEAPONFRAME] = 0
        if bits & P.SU_ARMOR:
            self.stats[P.STAT_ARMOR] = r.byte()
        if bits & P.SU_WEAPON:
            self.stats[P.STAT_WEAPON] = r.byte()
        self.stats[P.STAT_HEALTH] = r.short()
        self.stats[P.STAT_AMMO] = r.byte()
        self.stats[P.STAT_SHELLS] = r.byte()
        self.stats[P.STAT_NAILS] = r.byte()
        self.stats[P.STAT_ROCKETS] = r.byte()
        self.stats[P.STAT_CELLS] = r.byte()
        self.stats[P.STAT_ACTIVEWEAPON] = r.byte()

    def parse_particle(self, r):                   # cl_parse.c (svc_particle)
        org = (r.coord(), r.coord(), r.coord())
        dirv = (r.char(), r.char(), r.char())
        count = r.byte()
        color = r.byte()
        self.particles.append((org, dirv, count, color))

    def parse_sound(self, r):                      # cl_parse.c:101 (minimal)
        field_mask = r.byte()
        if field_mask & P.SND_VOLUME:
            r.byte()
        if field_mask & P.SND_ATTENUATION:
            r.byte()
        channel = r.short()
        sound_num = r.byte()
        org = (r.coord(), r.coord(), r.coord())
        # Phase 1: parsed and dropped (audio still driven server-side); recorded
        # for the renderer in a later phase. Touch vars so the bytes are consumed.
        _ = (channel, sound_num, org)

    def parse_temp_entity(self, r):                # cl_parse.c (svc_temp_entity)
        kind = r.byte()
        if kind in (P.TE_LIGHTNING1, P.TE_LIGHTNING2, P.TE_LIGHTNING3):
            r.short()                              # owner entity
            for _ in range(6):
                r.coord()                          # start + end
        elif kind in (P.TE_EXPLOSION, P.TE_TAREXPLOSION, P.TE_LAVASPLASH,
                      P.TE_TELEPORT):
            for _ in range(3):
                r.coord()
        else:                                      # spikes/gunshot: a point
            for _ in range(3):
                r.coord()

    # ---- relink / interpolation (CL_RelinkEntities, cl_parse.c:442) ----
    def lerp_point(self):
        """CL_LerpPoint: fraction of the way from mtime[1] to mtime[0] that
        cl.time has reached, clamped 0..1. With messages one frame apart this is
        Quake's gentle one-message smoothing."""
        span = self.mtime[0] - self.mtime[1]
        if span <= 0:
            self.time = self.mtime[0]
            return 1.0
        frac = (self.time - self.mtime[1]) / span
        if frac < 0:
            self.time = self.mtime[1]
            return 0.0
        if frac > 1:
            self.time = self.mtime[0]
            return 1.0
        return frac

    def relink(self, dt=0.0):
        """CL_RelinkEntities (cl_parse.c:442): interpolate every updated entity
        between its last two messages (snap on teleport / forcelink), lerp the
        player velocity, then advance the client particle system."""
        frac = self.lerp_point()
        for i in range(3):
            self.velocity[i] = (self.mvelocity[1][i]
                                + frac * (self.mvelocity[0][i]
                                          - self.mvelocity[1][i]))
        for e in self.entities:
            if e is None or not e.model:
                continue
            if e.msgtime != self.mtime[0]:        # not updated this message
                continue
            new, old = e.msg_origins[0], e.msg_origins[1]
            na, oa = e.msg_angles[0], e.msg_angles[1]
            if e.forcelink:
                e.origin = new
                e.angles = na
            else:
                o = []
                for j in range(3):
                    d = new[j] - old[j]
                    f = 1.0 if abs(d) > 100.0 else frac   # teleport guard
                    o.append(old[j] + f * d)
                e.origin = tuple(o)
                ang = []
                for j in range(3):
                    d = na[j] - oa[j]
                    if d > 180:
                        d -= 360
                    elif d < -180:
                        d += 360
                    ang.append(oa[j] + frac * d)
                e.angles = tuple(ang)
            self._emit_trail(e)
        self._advance_particles(dt)

    def _emit_trail(self, e):
        """Client-side trail seeding from entity effects/model (R_RocketTrail /
        CL_RelinkEntities). Phase 1: rockets/grenades leave a sparse smoke trail.
        Refine ramps/types later."""
        name = e.model or ""
        if "missile" in name or "grenade" in name or (e.effects & 8):
            ox, oy, oz = e.origin
            self.particles.append(((ox, oy, oz), (0.0, 0.0, 0.0), 1, 6))

    def _advance_particles(self, dt):
        """Age client particles; drop expired. Phase 1 holds simple (origin,
        dir, count, color) puffs; here we just cap the list so it cannot grow
        unbounded. Replace with the WinQuake p_free ramp system in a later pass."""
        if len(self.particles) > 2048:
            del self.particles[:-2048]


if __name__ == "__main__":                       # python -m quake.cl_parse
    from .msg import MsgWriter
    cl = ClientState()
    w = MsgWriter()
    w.byte(P.svc_time); w.float(3.5)
    w.byte(P.svc_lightstyle); w.byte(2); w.string("mmnmm")
    cl.parse_message(MsgReader(bytes(w.data)))
    assert cl.mtime[0] == 3.5 and cl.lightstyles[2] == "mmnmm"
    print("quake.cl_parse OK")
