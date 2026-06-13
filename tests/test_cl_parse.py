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


if __name__ == "__main__":
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
    print("OK")
