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


if __name__ == "__main__":
    test_parse_time_and_lightstyle()
    test_baseline_then_update_delta()
    test_intermission_no_payload()
    test_finale_reads_string()
    test_relink_lerps_between_messages()
    test_relink_teleport_snaps()
    test_relink_teleport_snaps_all_axes()
    test_spawnstaticsound_consumed()
    print("OK")
