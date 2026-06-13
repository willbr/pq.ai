"""The client half of the loopback: client_state_t (ClientState) plus the
CL_ParseServerMessage dispatch and per-message handlers (cl_parse.c). Builds a
client-side entity list from the server's protocol-15 datagram, deltas updates
against per-entity baselines, and (relink) interpolates positions and grows
client-side particle trails. The renderer reads this, not the server edicts.

The writer half is quake/sv_send.py; this parser is its exact mirror -- the
clientdata SU_* order, the always-sent tail, and the entity U_* field order must
stay in lockstep with that module."""
from . import protocol as P
from . import particles
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
        # the last two demo-frame header viewangles, lerped each render frame so
        # the demo camera doesn't snap at the message cadence (cl.mviewangles in
        # WinQuake; V_CalcRefdef interpolates them like entity origins)
        self.mviewangles = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        self.lerp_frac = 0.0         # fraction relink used this frame (for views)
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
        self.center_time = 0.0       # cl.time when the last centerprint arrived
        self.last_print = None       # svc_print text (demos; engine Con_Printf)
        self.last_stuff = None       # svc_stufftext text (demos; engine Cbuf)
        self.particles = []          # client-side particle system (relink); each
                                     # entry is the renderer's 10-element form
                                     # [x,y,z, vx,vy,vz, color, die, type, ramp]
                                     # -- same as sv.particles (quake/particles.py)
        self._tracer_state = [0]     # R_RocketTrail's static tracer counter
        self.dlight_events = []      # (origin, radius, die_time, decay) -- demo dlights
        self.sound_events = []       # (ent, chan, name, vol, atten, origin) for the
                                     # mixer -- drained each demo frame (no server)
        self.static_sounds = []      # (name, vol, atten, origin) looping ambients
                                     # from svc_spawnstaticsound -- started once
        self.completed_time = 0.0    # level time frozen at intermission (cl.completed_time)
        self.maxclients = 0
        self.gametype = 0

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
            self.center_time = self.mtime[0]
        elif cmd == P.svc_intermission:
            # NQ protocol-15: svc_intermission carries NO payload. (Reading a
            # string here would consume the next message's bytes and desync.)
            self.intermission = True
            self.completed_time = self.mtime[0]    # cl.completed_time = cl.time
        elif cmd == P.svc_finale:
            # svc_finale DOES carry the finale text string.
            self.intermission = True
            self.completed_time = self.mtime[0]    # cl.completed_time = cl.time
            self.center_msg = r.string()
            self.center_time = self.mtime[0]
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
        elif cmd == P.svc_killedmonster:
            self.stats[P.STAT_MONSTERS] += 1
        elif cmd == P.svc_foundsecret:
            self.stats[P.STAT_SECRETS] += 1
        elif cmd in (P.svc_sellscreen, P.svc_disconnect):
            return
        elif cmd == P.svc_spawnstaticsound:        # CL_ParseStaticSound
            org = (r.coord(), r.coord(), r.coord())
            sound_num = r.byte()
            vol = r.byte() / 255.0
            atten = r.byte() / 64.0
            name = (self.sound_precache[sound_num]
                    if 0 < sound_num < len(self.sound_precache) else "")
            if name:                               # looping ambient (fans, water)
                self.static_sounds.append((name, vol, atten, org))
        # --- svc types the live loopback never sends but real demos do
        # (cl_parse.c). Parse their payload so the stream stays in sync; the
        # render surface doesn't consume them in Phase 2, so they drop.
        elif cmd == P.svc_version:
            r.long()                               # protocol version (checked in C)
        elif cmd == P.svc_print:
            self.last_print = r.string()           # Con_Printf in the engine
        elif cmd == P.svc_stufftext:
            self.last_stuff = r.string()           # Cbuf_AddText in the engine
        elif cmd == P.svc_updatename:
            r.byte(); r.string()                   # scoreboard slot + name
        elif cmd == P.svc_updatefrags:
            r.byte(); r.short()                    # scoreboard slot + frags
        elif cmd == P.svc_updatecolors:
            r.byte(); r.byte()                     # scoreboard slot + colors
        elif cmd == P.svc_stopsound:
            r.short()                              # (channel<<3)|entity
        elif cmd == P.svc_damage:
            r.byte(); r.byte()                     # armor + blood
            for _ in range(3):
                r.coord()                          # inflictor origin (V_ParseDamage)
        elif cmd == P.svc_cutscene:
            self.intermission = True
            self.completed_time = self.mtime[0]    # cl.completed_time = cl.time
            self.center_msg = r.string()           # cutscene text
            self.center_time = self.mtime[0]
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
        particles.run_particle_effect(self.particles, org, dirv, color, count,
                                      self.time)

    def parse_sound(self, r):                      # CL_ParseStartSoundPacket
        field_mask = r.byte()
        vol = (r.byte() / 255.0) if (field_mask & P.SND_VOLUME) else 1.0
        atten = (r.byte() / 64.0) if (field_mask & P.SND_ATTENUATION) else 1.0
        channel = r.short()
        sound_num = r.byte()
        org = (r.coord(), r.coord(), r.coord())
        ent = channel >> 3                         # cl_parse.c: ent/chan packed
        chan = channel & 7
        name = (self.sound_precache[sound_num]
                if 0 < sound_num < len(self.sound_precache) else "")
        if name:
            # record for the demo frame loop to play (no server to call the
            # mixer); live play still drives sound server-side and ignores this
            self.sound_events.append((ent, chan, name, vol, atten, org))

    def parse_temp_entity(self, r):                # cl_parse.c (svc_temp_entity)
        kind = r.byte()
        if kind in (P.TE_LIGHTNING1, P.TE_LIGHTNING2, P.TE_LIGHTNING3):
            r.short()                              # owner entity
            for _ in range(6):
                r.coord()                          # start + end
        elif kind in (P.TE_EXPLOSION, P.TE_TAREXPLOSION, P.TE_LAVASPLASH,
                      P.TE_TELEPORT):
            org = (r.coord(), r.coord(), r.coord())
            # CL_ParseTEnt: spawn the r_part.c burst for each (mirrors the
            # server's _spawn_te). TE_EXPLOSION also flashes a transient dlight.
            if kind == P.TE_EXPLOSION:
                particles.particle_explosion(self.particles, org, self.time)
                self.dlight_events.append((org, 350.0, self.mtime[0] + 0.5, 300.0))
            elif kind == P.TE_TAREXPLOSION:
                particles.blob_explosion(self.particles, org, self.time)
                self.dlight_events.append((org, 350.0, self.mtime[0] + 0.5, 300.0))
            elif kind == P.TE_LAVASPLASH:
                particles.lava_splash(self.particles, org, self.time)
            elif kind == P.TE_TELEPORT:
                particles.teleport_splash(self.particles, org, self.time)
        else:                                      # spikes/gunshot: a point burst
            org = (r.coord(), r.coord(), r.coord())
            z = (0.0, 0.0, 0.0)
            # WinQuake CL_ParseTEnt counts: gunshot 20, spike 10, superspike 20,
            # wizspike 30 (colour 20), knightspike 20 (colour 226).
            if kind == P.TE_GUNSHOT:
                particles.run_particle_effect(self.particles, org, z, 0, 20, self.time)
            elif kind == P.TE_SPIKE:
                particles.run_particle_effect(self.particles, org, z, 0, 10, self.time)
            elif kind == P.TE_SUPERSPIKE:
                particles.run_particle_effect(self.particles, org, z, 0, 20, self.time)
            elif kind == P.TE_WIZSPIKE:
                particles.run_particle_effect(self.particles, org, z, 20, 30, self.time)
            elif kind == P.TE_KNIGHTSPIKE:
                particles.run_particle_effect(self.particles, org, z, 226, 20, self.time)

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

    def lerp_viewangles(self, frac):
        """Interpolate the demo camera angles between the last two demo-frame
        headers (mviewangles[1] -> [0]) by `frac`, taking the short way around
        each axis (180-degree wrap), like the entity angle lerp in relink. This
        is what keeps demo playback smooth instead of snapping per message."""
        new, old = self.mviewangles[0], self.mviewangles[1]
        out = []
        for j in range(3):
            d = new[j] - old[j]
            if d > 180:
                d -= 360
            elif d < -180:
                d += 360
            out.append(old[j] + frac * d)
        return out

    def relink(self, dt=0.0):
        """CL_RelinkEntities (cl_parse.c:442): interpolate every updated entity
        between its last two messages (snap on teleport / forcelink), lerp the
        player velocity, then advance the client particle system."""
        frac = self.lerp_point()
        self.lerp_frac = frac        # reused by lerp_viewangles for the demo camera
        for i in range(3):
            self.velocity[i] = (self.mvelocity[1][i]
                                + frac * (self.mvelocity[0][i]
                                          - self.mvelocity[1][i]))
        for e in self.entities:
            if e is None or not e.model:
                continue
            if e.msgtime != self.mtime[0]:        # not in the last packet:
                e.model = None                    # clear it so it stops
                continue                          # rendering (cl_main.c:491)
            new, old = e.msg_origins[0], e.msg_origins[1]
            na, oa = e.msg_angles[0], e.msg_angles[1]
            if e.forcelink:
                e.origin = new
                e.angles = na
            else:
                snap = any(abs(new[j] - old[j]) > 100.0 for j in range(3))
                f = 1.0 if snap else frac                 # teleport snaps all axes
                e.origin = tuple(old[j] + f * (new[j] - old[j]) for j in range(3))
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
        """R_RocketTrail (CL_RelinkEntities): lay a particle trail along the
        segment a flagged entity moved this frame, with the SAME r_part.c physics
        as live play (quake/particles.py). WinQuake keys the trail type off the
        model's .mdl EF flags, which the demo client has not loaded; we derive it
        from the model NAME instead, which covers the common visible trails
        (rockets / grenades / blood). msg_origins[1] is last frame's message
        origin and e.origin this frame's interpolated one, so we only emit when
        the entity actually moved."""
        name = e.model or ""
        if "missile" in name:
            ttype = 0                              # EF_ROCKET: fire smoke
        elif "grenade" in name:
            ttype = 1                              # EF_GRENADE: fire smoke (darker)
        elif "gib" in name or "zom_gib" in name:
            ttype = 2                              # EF_GIB / EF_ZOMGIB: blood
        else:
            return
        start = e.msg_origins[1]
        if start == e.origin:                      # didn't move -> no trail
            return
        particles.rocket_trail(self.particles, start, e.origin, ttype,
                               self.time, self._tracer_state)

    def _advance_particles(self, dt):
        """R_DrawParticles: integrate/age/expire every client particle with the
        SAME r_part.c physics live play uses (quake/particles.py). sv_gravity is
        the stock 800.0 (the demo client has no cvars)."""
        self.particles = particles.advance(self.particles, self.time, dt, 800.0)


class SceneFromClient:
    """Adapter exposing a ClientState through the subset of the Server query
    interface the renderer consumes (client.py's render block). Lets the
    existing _alias_ents/_sprite_ents/_bsp_ents/brush paths read loopback state
    with no change to their call sites.

    The world entity set this presents must match what the old direct-from-sv
    path produced: every live entity with a model, MINUS the player's own body
    (CL_RelinkEntities skips cl.viewentity in first person -- WinQuake
    cl_main.c:610) and MINUS the world (modelindex 1, the maps/*.bsp), which is
    drawn by the BSP walker, not as an entity. Entities with no model
    (modelindex 0) never carry a precache name and so are skipped naturally."""

    def __init__(self, cl):
        self.cl = cl

    def _world_alias_sprite(self, ext):
        """Live entities whose model has the given extension, excluding the
        player's own body (viewentity). Yields ClEntity. Gated on the entity
        being present in the latest packet (msgtime == mtime[0]) so PVS culling
        in demos is honoured -- entities that leave the view stop rendering and
        reappear when seen again. A no-op for the live loopback (the server
        sends every entity each frame, so msgtime always equals mtime[0])."""
        cl = self.cl
        ve = cl.viewentity
        for num, e in enumerate(cl.entities):
            if e is None or not e.model:
                continue
            if num == ve:                      # don't draw our own body (1st person)
                continue
            if e.msgtime != cl.mtime[0]:       # not in the last packet (PVS)
                continue
            if e.model.endswith(ext):
                yield e

    def alias_entities(self):                  # .mdl
        return [(e.modelindex, e.origin, e.angles, e.frame)
                for e in self._world_alias_sprite(".mdl")]

    def sprite_entities(self):                 # .spr
        return [(e.modelindex, e.origin, e.frame)
                for e in self._world_alias_sprite(".spr")]

    def bsp_model_entities(self):              # external b_*.bsp pickups
        # modelindex 1 is the world map (maps/*.bsp); external pickup .bsps live
        # above it. The inline submodels are "*N" strings, handled separately by
        # brush_models, so an extension check + index>1 isolates the b_*.bsp set.
        out = []
        for e in self._world_alias_sprite(".bsp"):
            if e.modelindex > 1:
                out.append((e.modelindex, e.origin, e.angles))
        return out

    def brush_models(self):                    # inline submodels "*N"
        # Doors/plats/buttons/triggers. Only inline "*N" models; the world
        # ("*0" is the world map, but it is sent as modelindex 1 = maps/*.bsp,
        # not "*0") is excluded. Matches Server.brush_models' tuple shape
        # (submodel_index, origin, angles, frame). The player can't be a brush.
        out = []
        cl = self.cl
        for e in cl.entities:
            if e is None or not e.model or not e.model.startswith("*"):
                continue
            if e.msgtime != cl.mtime[0]:       # not in the last packet (PVS)
                continue
            out.append((int(e.model[1:]), e.origin, e.angles, e.frame))
        return out

    @property
    def particles(self):
        return self.cl.particles

    @property
    def lightstyles(self):
        return self.cl.lightstyles

    @property
    def time(self):
        return self.cl.time

    # --- full client-visible surface for demo-mode rendering ---
    # These mirror the Server query methods the render block reads (sv.py), but
    # source their values from the parsed client state (cl.stats / cl.items /
    # cl.entities) instead of the server edicts, so client.py can drive the HUD,
    # view model, dynamic lights and intermission from a demo with no server.

    def player_health(self):
        return self.cl.stats[P.STAT_HEALTH]

    def hud_status(self):
        """Same dict shape as Server.hud_status(), sourced from cl.stats/items.
        The weapon-name / keys / powerups item-bit decode is the SAME logic the
        live server uses: both call sv.decode_hud_items (single source of truth).
        STAT_ACTIVEWEAPON holds the active weapon's IT_ bit (see
        sv_send.write_clientdata_to_message); items is cl.items."""
        from .sv import decode_hud_items
        st = self.cl.stats
        items = self.cl.items
        weapon_bit = st[P.STAT_ACTIVEWEAPON]
        weapon, keys, powerups = decode_hud_items(items, weapon_bit)
        return {
            "health": st[P.STAT_HEALTH],
            "armor": st[P.STAT_ARMOR],
            "weapon": weapon,
            "ammo": st[P.STAT_AMMO],
            "shells": st[P.STAT_SHELLS],
            "nails": st[P.STAT_NAILS],
            "rockets": st[P.STAT_ROCKETS],
            "cells": st[P.STAT_CELLS],
            "keys": keys,
            "powerups": powerups,
            "items": items,
            "weapon_bit": weapon_bit,
        }

    def light_entities(self):
        """Entities carrying engine light effects, like sv.light_entities():
        (num, origin, effects, is_rocket). is_rocket keys off the model name
        (rocket/grenade .mdl glow). Mirrors CL_RelinkEntities' dlight logic.
        Gated on the entity being in the latest packet (PVS-correct in demos)."""
        out = []
        cl = self.cl
        for num, e in enumerate(cl.entities):
            if e is None or not e.model or num == cl.viewentity:
                continue
            if e.msgtime != cl.mtime[0]:        # not in the last packet (PVS)
                continue
            name = e.model
            is_rocket = name.endswith("missile.mdl") or "lavaball" in name
            if e.effects or is_rocket:
                out.append((num, e.origin, e.effects, is_rocket))
        return out

    @property
    def dlight_events(self):
        return self.cl.dlight_events

    def view_weapon(self):
        """(view-model path, frame) from clientdata. STAT_WEAPON holds the
        modelindex of the active v_*.mdl in the precache (see
        sv_send.write_clientdata_to_message, which writes sv.model_index of
        .weaponmodel there); STAT_WEAPONFRAME holds the frame. None if unset."""
        mi = self.cl.stats[P.STAT_WEAPON]
        if mi <= 0 or mi >= len(self.cl.model_precache):
            return None
        return (self.cl.model_precache[mi], self.cl.stats[P.STAT_WEAPONFRAME])

    @property
    def center_msg(self):
        """The latest centerprint as (text, time), matching Server.center_msg's
        tuple shape (the render block tests sv.time - cm[1] < CENTER_MSG_TIME).
        None when no message has arrived."""
        if not self.cl.center_msg:
            return None
        return (self.cl.center_msg, self.cl.center_time)

    def intermission_active(self):
        return self.cl.intermission

    def intermission_stats(self):
        if not self.cl.intermission:
            return None
        st = self.cl.stats
        return {"time": int(self.cl.completed_time),
                "secrets": st[P.STAT_SECRETS],
                "total_secrets": st[P.STAT_TOTALSECRETS],
                "monsters": st[P.STAT_MONSTERS],
                "total_monsters": st[P.STAT_TOTALMONSTERS]}


if __name__ == "__main__":                       # python -m quake.cl_parse
    from .msg import MsgWriter
    cl = ClientState()
    w = MsgWriter()
    w.byte(P.svc_time); w.float(3.5)
    w.byte(P.svc_lightstyle); w.byte(2); w.string("mmnmm")
    cl.parse_message(MsgReader(bytes(w.data)))
    assert cl.mtime[0] == 3.5 and cl.lightstyles[2] == "mmnmm"
    print("quake.cl_parse OK")
