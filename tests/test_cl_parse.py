"""Client parser tests (quake/cl_parse.py): hand-built messages -> cl state,
baseline/update delta, and relink interpolation.
Run muted: PQ_AUDIO=0 python tests/test_cl_parse.py -> prints OK."""
import _bootstrap  # noqa: F401
from quake.msg import MsgWriter, MsgReader
from quake import protocol as P
from quake.cl_parse import ClientState


def test_parse_time_and_lightstyle():
    cl = ClientState()
    w = MsgWriter()
    w.byte(P.svc_time); w.float(3.5)
    w.byte(P.svc_lightstyle); w.byte(2); w.string("mmnmm")
    cl.parse_message(MsgReader(bytes(w.data)))
    assert cl.mtime[0] == 3.5
    assert cl.lightstyles[2] == "mmnmm"


def test_baseline_then_update_delta():
    cl = ClientState()
    # spawn baseline for entity 5 at origin (10,20,30), model 3
    w = MsgWriter()
    w.byte(P.svc_spawnbaseline); w.short(5)
    w.byte(3); w.byte(0); w.byte(0); w.byte(0)
    for v in (10.0, 20.0, 30.0):
        w.coord(v); w.angle(0.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    e = cl.entities[5]
    assert e.baseline.modelindex == 3
    # need a svc_time first so msgtime links; send one
    t = MsgWriter(); t.byte(P.svc_time); t.float(1.0)
    cl.parse_message(MsgReader(bytes(t.data)))
    # update: only ORIGIN1 changes to 12.0, everything else from baseline
    w = MsgWriter()
    bits = P.U_ORIGIN1
    w.byte((bits & 0xff) | P.U_SIGNAL); w.byte(5); w.coord(12.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert abs(cl.entities[5].msg_origins[0][0] - 12.0) < 1e-6
    assert abs(cl.entities[5].msg_origins[0][1] - 20.0) < 1e-6  # from baseline


def test_intermission_no_payload():
    # svc_intermission carries NO payload; a following svc_time must parse clean.
    cl = ClientState()
    w = MsgWriter()
    w.byte(P.svc_intermission)
    w.byte(P.svc_time); w.float(9.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert cl.intermission is True
    assert cl.mtime[0] == 9.0


def test_finale_reads_string():
    cl = ClientState()
    w = MsgWriter()
    w.byte(P.svc_finale); w.string("the end")
    cl.parse_message(MsgReader(bytes(w.data)))
    assert cl.intermission is True
    assert cl.center_msg == "the end"


def test_relink_lerps_between_messages():
    cl = ClientState()
    e = cl.entity(5)
    e.model = "progs/soldier.mdl"
    e.msg_origins = [(20.0, 0.0, 0.0), (10.0, 0.0, 0.0)]  # [new, old]
    e.msg_angles = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    e.msgtime = 2.0
    cl.mtime = [2.0, 1.0]
    cl.time = 1.5                       # halfway -> x = 15
    cl.relink()
    assert abs(cl.entities[5].origin[0] - 15.0) < 1e-6


def test_relink_teleport_snaps():
    cl = ClientState()
    e = cl.entity(6)
    e.model = "x"
    e.msg_origins = [(500.0, 0.0, 0.0), (10.0, 0.0, 0.0)]  # delta 490 > 100
    e.msg_angles = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    e.msgtime = 2.0
    cl.mtime = [2.0, 1.0]
    cl.time = 1.5
    cl.relink()
    assert abs(cl.entities[6].origin[0] - 500.0) < 1e-6     # snapped to newest


def test_relink_teleport_snaps_all_axes():
    """X jumps >100 so the whole entity must snap: Y must also snap, not lerp."""
    cl = ClientState()
    e = cl.entity(7)
    e.model = "x"
    # X delta = 490 (triggers snap); Y delta = 20 (small, would lerp to 10 without fix)
    e.msg_origins = [(500.0, 20.0, 0.0), (10.0, 0.0, 0.0)]  # [new, old]
    e.msg_angles = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    e.msgtime = 2.0
    cl.mtime = [2.0, 1.0]
    cl.time = 1.5                        # frac = 0.5 without snap
    cl.relink()
    # Both axes must snap to the newest message values (f=1.0 for all axes)
    assert abs(cl.entities[7].origin[0] - 500.0) < 1e-6
    assert abs(cl.entities[7].origin[1] - 20.0) < 1e-6


def test_spawnstaticsound_consumed():
    """svc_spawnstaticsound (29) must be consumed cleanly; following svc_time parses."""
    cl = ClientState()
    w = MsgWriter()
    w.byte(P.svc_spawnstaticsound)
    w.coord(100.0); w.coord(200.0); w.coord(300.0)  # origin (3 coords)
    w.byte(5)    # sound number
    w.byte(255)  # volume
    w.byte(1)    # attenuation
    w.byte(P.svc_time); w.float(7.25)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert cl.mtime[0] == 7.25           # proves no desync after spawnstaticsound


def test_scene_hud_status_from_stats():
    from quake.cl_parse import SceneFromClient
    cl = ClientState()
    cl.stats[P.STAT_HEALTH] = 87
    cl.stats[P.STAT_ARMOR] = 50
    cl.stats[P.STAT_SHELLS] = 25
    cl.stats[P.STAT_ACTIVEWEAPON] = 0
    cl.items = 1                                 # IT_SHOTGUN
    s = SceneFromClient(cl).hud_status()
    assert s["health"] == 87 and s["armor"] == 50 and s["shells"] == 25
    assert "weapon" in s and "items" in s


def test_scene_hud_status_matches_sv_decode():
    """The weapon/keys/powerups decode must be identical to sv.hud_status."""
    from quake.cl_parse import SceneFromClient
    from quake.sv import (decode_hud_items, IT_SHOTGUN, IT_KEY1, IT_QUAD)
    cl = ClientState()
    cl.items = IT_SHOTGUN | IT_KEY1 | IT_QUAD
    cl.stats[P.STAT_ACTIVEWEAPON] = IT_SHOTGUN
    s = SceneFromClient(cl).hud_status()
    weapon, keys, powerups = decode_hud_items(cl.items, IT_SHOTGUN)
    assert s["weapon"] == weapon == "Shotgun"
    assert s["keys"] == keys == "silver key"
    assert s["powerups"] == powerups == "quad"


def test_scene_light_entities_from_effects():
    from quake.cl_parse import SceneFromClient
    cl = ClientState()
    e = cl.entity(3)
    e.model = "progs/missile.mdl"
    e.origin = (10.0, 20.0, 30.0)
    e.effects = 0
    e.msgtime = cl.mtime[0]
    out = SceneFromClient(cl).light_entities()
    # a rocket model emits a glow even with no EF_ bits (is_rocket True)
    assert any(num == 3 for num, org, eff, rocket in out)


def test_scene_view_weapon_uses_stat_weapon_modelindex():
    """STAT_WEAPON holds the v_*.mdl model index, STAT_WEAPONFRAME the frame."""
    from quake.cl_parse import SceneFromClient
    cl = ClientState()
    cl.model_precache = ["", "maps/x.bsp", "progs/v_shot.mdl"]
    cl.stats[P.STAT_WEAPON] = 2
    cl.stats[P.STAT_WEAPONFRAME] = 3
    vw = SceneFromClient(cl).view_weapon()
    assert vw == ("progs/v_shot.mdl", 3)


def test_static_entities_render_as_alias():
    """svc_spawnstatic (wall torches / flames) must be drawn: the entity is
    freed server-side (faithful PF_makestatic) and delivered only as a static,
    so SceneFromClient.alias_entities() must include it -- always, with no
    msgtime gate -- at its baseline origin/angles/modelindex. The fix that
    restored torches in live play after makestatic became faithful."""
    from quake.cl_parse import SceneFromClient
    cl = ClientState()
    cl.model_precache = ["", "maps/x.bsp", "progs/flame.mdl"]
    w = MsgWriter()
    w.byte(P.svc_spawnstatic)
    w.byte(2)            # modelindex -> progs/flame.mdl
    w.byte(0)            # frame
    w.byte(0); w.byte(0) # colormap, skin
    for v in (64.0, 128.0, 256.0):
        w.coord(v); w.angle(0.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert len(cl.static_entities) == 1
    se = cl.static_entities[0]
    # parse_baseline must seed concrete render fields (not leave origin 0,0,0)
    assert se.origin == (64.0, 128.0, 256.0)
    assert se.modelindex == 2 and se.model == "progs/flame.mdl"
    # alias_entities yields the static at its origin/modelindex (no msgtime gate)
    ae = SceneFromClient(cl).alias_entities()
    assert any(mi == 2 and abs(org[0] - 64.0) < 1e-6 and abs(org[2] - 256.0) < 1e-6
               for mi, org, ang, frame in ae), ae


def test_temp_entity_explosion_pushes_dlight():
    cl = ClientState()
    cl.mtime[0] = 5.0
    w = MsgWriter()
    w.byte(P.svc_temp_entity); w.byte(P.TE_EXPLOSION)
    w.coord(100.0); w.coord(200.0); w.coord(300.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert len(cl.dlight_events) == 1
    org, radius, die, decay = cl.dlight_events[0]
    assert abs(org[0] - 100.0) < 1e-6 and abs(org[2] - 300.0) < 1e-6
    assert radius == 350.0 and abs(die - 5.5) < 1e-6


def test_svc_particle_dir_scaled_by_16():
    """R_ParseParticleEffect reads the wire direction as char*(1/16); then
    R_RunParticleEffect sets velocity = dir*15. A wire char of 16 means dir 1.0
    -> velocity 15, NOT 16*15=240 (the missing /16 made demo bursts fly off)."""
    cl = ClientState()
    w = MsgWriter()
    w.byte(P.svc_particle)
    w.coord(0.0); w.coord(0.0); w.coord(0.0)   # origin
    w.char(16); w.char(0); w.char(0)           # dir char 16 -> dir 1.0
    w.byte(1)                                   # count
    w.byte(0)                                   # color
    cl.parse_message(MsgReader(bytes(w.data)))
    assert len(cl.particles) == 1
    vx = cl.particles[0][3]                     # velocity x (deterministic)
    assert abs(vx - 15.0) < 1e-6, vx           # 1.0*15, not 16*15


def test_tarexplosion_has_no_dlight():
    """CL_ParseTEnt: TE_EXPLOSION flashes a dlight, TE_TAREXPLOSION (tarbaby)
    does not -- it is a pure particle blast."""
    cl = ClientState(); cl.mtime[0] = 2.0
    w = MsgWriter()
    w.byte(P.svc_temp_entity); w.byte(P.TE_TAREXPLOSION)
    w.coord(0.0); w.coord(0.0); w.coord(0.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert len(cl.dlight_events) == 0          # no flash for the tar blast
    assert len(cl.particles) > 0               # but it does spawn the blob burst


def test_svc_sound_records_event_for_mixer():
    """In demo playback there is no server to call the mixer, so svc_sound must
    be recorded as a client sound event (ent, chan, name, vol, atten, origin)
    that the demo frame loop drains to the mixer -- otherwise demos are silent."""
    cl = ClientState()
    cl.sound_precache = ["", "a.wav", "b.wav", "c.wav"]
    w = MsgWriter()
    w.byte(P.svc_sound)
    w.byte(P.SND_VOLUME)                       # field mask: explicit volume
    w.byte(128)                                # volume byte (128/255)
    chan = (5 << 3) | 2                        # ent 5, channel 2
    w.short(chan)
    w.byte(3)                                  # sound_num -> "c.wav"
    w.coord(10.0); w.coord(20.0); w.coord(30.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert len(cl.sound_events) == 1
    ent, ch, name, vol, atten, org = cl.sound_events[0]
    assert ent == 5 and ch == 2 and name == "c.wav"
    assert abs(vol - 128 / 255) < 1e-3 and abs(atten - 1.0) < 1e-6
    assert abs(org[0] - 10.0) < 1e-6 and abs(org[2] - 30.0) < 1e-6


def test_demo_view_angles_interpolate():
    """Demo playback must lerp the view angles between the last two demo-frame
    headers (cl.mviewangles), not snap to the latest -- otherwise the camera
    jitters at the demo's message cadence. With 180-degree wrap like the entity
    angle lerp."""
    cl = ClientState()
    cl.mviewangles[1] = [0.0, 10.0, 0.0]       # previous header
    cl.mviewangles[0] = [0.0, 50.0, 0.0]       # latest header
    va = cl.lerp_viewangles(0.5)
    assert abs(va[1] - 30.0) < 1e-6            # halfway between 10 and 50
    va0 = cl.lerp_viewangles(0.0)
    assert abs(va0[1] - 10.0) < 1e-6          # frac 0 -> previous
    # wrap the short way: 350 -> 10 goes forward through 0, not backward 340 deg
    cl.mviewangles[1] = [0.0, 350.0, 0.0]
    cl.mviewangles[0] = [0.0, 10.0, 0.0]
    vw = cl.lerp_viewangles(0.5)
    assert abs((vw[1] % 360.0) - 0.0) < 1e-6   # halfway = 360 == 0, not 180


def test_demo_particles_move():
    """The bug: in demo playback (no server) the client particle system was a
    no-op stub -- svc_particle bursts appeared but never moved or expired. Now
    cl runs the SAME r_part.c physics live play uses (quake/particles.py), so a
    parsed burst MUST integrate (positions change) and eventually expire."""
    cl = ClientState()
    cl.time = 1.0
    # svc_particle with a nonzero dir so the spawned pt_slowgrav has velocity.
    w = MsgWriter()
    w.byte(P.svc_particle)
    w.coord(100.0); w.coord(0.0); w.coord(50.0)   # origin
    w.char(10); w.char(0); w.char(0)              # dir (becomes vel*15)
    w.byte(30)                                     # count
    w.byte(0)                                       # color
    cl.parse_message(MsgReader(bytes(w.data)))
    assert len(cl.particles) == 30, "burst should spawn 30 particles"
    # particles are the renderer-native 10-element form (x,y,z, vx,vy,vz, ...).
    p = cl.particles[0]
    assert len(p) == 10, "client particle must be the 10-element renderer shape"
    x0, y0, z0 = p[0], p[1], p[2]
    assert p[3] != 0.0, "nonzero dir -> nonzero x velocity"
    n0 = len(cl.particles)
    # advance a few frames -- the position MUST change (it was frozen before)
    for _ in range(5):
        cl.time += 0.05
        cl._advance_particles(0.05)
    assert cl.particles, "particles vanished too soon"
    moved = any(abs(q[0] - x0) > 1e-3 or abs(q[2] - z0) > 1e-3
                for q in cl.particles)
    assert moved, "demo particles did not move (the bug)"
    # and they must eventually all expire (pt_slowgrav dies <= 0.5s after spawn)
    for _ in range(20):
        cl.time += 0.05
        cl._advance_particles(0.05)
    assert len(cl.particles) == 0, "particles never expired"
    assert n0 == 30
    _ = (y0,)   # silence unused (kept for symmetry/readability)


def test_demo_explosion_spawns_moving_burst():
    """TE_EXPLOSION in a demo must spawn the r_part.c particle burst (not just a
    dlight). The accelerating pt_explode particles move fast."""
    cl = ClientState()
    cl.time = 2.0
    cl.mtime[0] = 2.0
    w = MsgWriter()
    w.byte(P.svc_temp_entity); w.byte(P.TE_EXPLOSION)
    w.coord(0.0); w.coord(0.0); w.coord(0.0)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert len(cl.particles) > 0, "explosion spawned no particles"
    assert len(cl.dlight_events) == 1, "explosion still flashes a dlight"
    before = [(p[0], p[1], p[2]) for p in cl.particles]
    cl.time += 0.05
    cl._advance_particles(0.05)
    after = [(p[0], p[1], p[2]) for p in cl.particles]
    assert before[:len(after)] != after[:len(before)], "explosion burst frozen"


if __name__ == "__main__":
    test_demo_particles_move()
    test_demo_explosion_spawns_moving_burst()
    test_svc_particle_dir_scaled_by_16()
    test_tarexplosion_has_no_dlight()
    test_svc_sound_records_event_for_mixer()
    test_demo_view_angles_interpolate()
    test_parse_time_and_lightstyle()
    test_baseline_then_update_delta()
    test_intermission_no_payload()
    test_finale_reads_string()
    test_relink_lerps_between_messages()
    test_relink_teleport_snaps()
    test_relink_teleport_snaps_all_axes()
    test_spawnstaticsound_consumed()
    test_scene_hud_status_from_stats()
    test_scene_hud_status_matches_sv_decode()
    test_scene_light_entities_from_effects()
    test_scene_view_weapon_uses_stat_weapon_modelindex()
    test_temp_entity_explosion_pushes_dlight()
    test_static_entities_render_as_alias()
    print("OK")
